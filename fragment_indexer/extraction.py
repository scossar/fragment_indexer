"""Extract fragments independently of database allocation and embedding budgets."""
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from lxml import html, etree

HEADINGS = {f"h{n}" for n in range(1, 7)}
BLOCKS = {"p", "div", "section", "article", "blockquote", "ul", "ol", "li", "dl",
          "dt", "dd", "table", "tr", "td", "th", "pre", "figure", "figcaption", "br", "hr"} | HEADINGS


@dataclass(frozen=True)
class Page:
    post_id: str
    url: str
    title: str
    source: str
    updated_at: float
    root: html.HtmlElement


@dataclass(frozen=True)
class Section:
    anchor: str | None  # None is the introduction; a heading must have a nonempty ID.
    headings: tuple[str, ...]
    elements: tuple[html.HtmlElement, ...]


@dataclass(frozen=True)
class Fragment:
    page: Page
    anchor: str | None
    headings: tuple[str, ...]
    html_heading: str
    html_fragment: str
    text_blocks: tuple[str, ...]

    @property
    def identity(self) -> str:
        # Structured serialization avoids delimiter collisions and reserves intro identity.
        return json.dumps([self.page.post_id, "intro" if self.anchor is None else "heading", self.anchor],
                          ensure_ascii=False, separators=(",", ":"))

    @property
    def url(self) -> str:
        return self.page.url + ("#" + self.anchor if self.anchor is not None else "")


class BoundaryPolicy(Protocol):
    name: str

    def sections(self, page: Page) -> list[Section]: ...


def heading_text(element: html.HtmlElement) -> str:
    element = deepcopy(element)
    for link in element.xpath('.//a[contains(concat(" ", normalize-space(@class), " "), " anchor ")]'):
        link.drop_tree()
    return " ".join(element.text_content().split())


class NextHeadingPolicy:
    """Top-level headings delimit sections; ancestry is context, not a boundary rule.

    A different policy may inspect the entire Page (including its DOM and source
    metadata). Chunking, identities, and storage need not know how spans are chosen.
    """
    name = "next-heading-v1"

    def sections(self, page: Page) -> list[Section]:
        result = []
        anchor = None
        stack: list[tuple[int, str]] = []
        elements = []
        seen = set()
        # Hugo normally wraps text in paragraphs; preserve raw root text as well.
        if page.root.text and page.root.text.strip():
            paragraph = html.Element("p")
            paragraph.text = page.root.text
            elements.append(paragraph)
        for original in page.root:
            element = deepcopy(original)
            if not isinstance(element.tag, str):
                if element.tail and element.tail.strip():
                    paragraph = html.Element("p")
                    paragraph.text = element.tail
                    elements.append(paragraph)
                continue
            if element.tag in HEADINGS:
                if anchor is not None or elements:
                    result.append(Section(anchor, tuple(t for _, t in stack), tuple(elements)))
                anchor = element.get("id")
                if not anchor or anchor in seen:
                    raise ValueError(f"{page.source}: missing or duplicate heading ID {anchor!r}")
                seen.add(anchor)
                level = int(element.tag[1])
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, heading_text(element)))
                elements = []
                if element.tail and element.tail.strip():
                    paragraph = html.Element("p")
                    paragraph.text = element.tail
                    elements.append(paragraph)
            else:
                if any(e.tag in HEADINGS for e in element.iterdescendants()):
                    raise ValueError(f"{page.source}: nested headings require a different boundary policy")
                elements.append(element)
        if anchor is not None or elements:
            result.append(Section(anchor, tuple(t for _, t in stack), tuple(elements)))
        return result


def discover_pages(directory: Path) -> list[Page]:
    pages = []
    seen_ids, seen_urls = set(), set()
    for path in sorted(directory.rglob("*.html")):
        tree = html.parse(str(path))
        roots = tree.xpath('//*[@data-fragment-root="v1"]')
        if not roots:
            continue
        if len(roots) != 1:
            raise ValueError(f"{path}: expected exactly one fragment root")
        root = roots[0]
        attrs = root.attrib
        post_id = attrs.get("data-post-id", "").strip()
        url = attrs.get("data-page-url", "")
        parsed = urlsplit(url)
        if not post_id or not url.startswith("/") or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError(f"{path}: invalid post ID or canonical page URL")
        if post_id in seen_ids or url in seen_urls:
            raise ValueError(f"{path}: duplicate post ID or canonical URL")
        seen_ids.add(post_id)
        seen_urls.add(url)
        if attrs.get("data-build-environment") != "minimal":
            raise ValueError(f"{path}: extraction requires a minimal Hugo build")
        for element in root.iter():
            if any(k.startswith(("hx-", "data-hx-")) for k in element.attrib):
                raise ValueError(f"{path}: HTMX attributes found in extraction HTML")
        pages.append(Page(post_id, url, attrs["data-page-title"], attrs["data-source"],
                          float(attrs["data-updated-at"]), root))
    if not pages:
        raise ValueError("No marked published posts found; refusing to build an empty index")
    return sorted(pages, key=lambda p: p.post_id)


def text_blocks(root: html.HtmlElement) -> tuple[str, ...]:
    """Keep inline text contiguous, separate structural blocks, retain image alt/code."""
    parts = []

    def walk(node):
        if not isinstance(node.tag, str) or node.tag in {"script", "style"}:
            return
        if "katex" in node.get("class", "").split():
            # Hugo emits both accessible MathML and visual spans. Use the original
            # equation once for embeddings, while retaining all rendered HTML.
            annotations = node.xpath('.//*[local-name()="annotation" and @encoding="application/x-tex"]')
            if annotations:
                parts.append(" " + "".join(annotations[0].itertext()) + " ")
                return
        if node.tag in BLOCKS:
            parts.append("\n")
        if node.tag == "img" and node.get("alt"):
            parts.append(" " + node.get("alt") + " ")
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if node.tag in BLOCKS:
            parts.append("\n")

    walk(root)
    return tuple(line for line in (" ".join(s.split()) for s in "".join(parts).splitlines()) if line)


def extract(page: Page, policy: BoundaryPolicy) -> list[Fragment]:
    fragments = []
    for section in policy.sections(page):
        wrapper = html.Element("div", {"class": "article-fragment"})
        for element in section.elements:
            wrapper.append(deepcopy(element))
        for element in wrapper.iter():
            for attr in ("href", "src", "poster"):
                value = element.get(attr)
                if value:
                    element.set(attr, urljoin(page.url, value))
        title = " > ".join((page.title,) + section.headings)
        heading = html.Element("h2")
        url = page.url + ("#" + section.anchor if section.anchor is not None else "")
        link = etree.SubElement(heading, "a", href=url)
        link.text = title
        fragments.append(Fragment(page, section.anchor, section.headings,
                                  html.tostring(heading, encoding="unicode"),
                                  html.tostring(wrapper, encoding="unicode"), text_blocks(wrapper)))
    return fragments
