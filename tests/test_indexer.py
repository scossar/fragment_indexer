from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from contextlib import closing
import tempfile
import unittest

from lxml import html

from fragment_indexer.embedding import MiniLM, chunk_fragment
from fragment_indexer.extraction import NextHeadingPolicy, Page, discover_pages, extract
from fragment_indexer.storage import allocate, make_map, write_sections
from fragment_indexer.hugo import verify_links


def page(body, post_id="post-1", url="/posts/example/"):
    return Page(post_id, url, "Example", "posts/example.md", 1.0, html.fragment_fromstring(body, create_parent="div"))


def fragments(body, **kwargs):
    return extract(page(body, **kwargs), NextHeadingPolicy())


class ExtractionTests(unittest.TestCase):
    def test_intro_and_heading_boundaries(self):
        result = fragments('<p>Before</p><h2 id="a">A</h2><p>Alpha</p><h3 id="b">B</h3><p>Beta</p>')
        self.assertEqual([f.anchor for f in result], [None, "a", "b"])
        self.assertEqual([f.text_blocks for f in result], [("Before",), ("Alpha",), ("Beta",)])
        self.assertEqual(result[2].headings, ("A", "B"))

    def test_headingless_post(self):
        result = fragments("<p>No heading at all</p>")
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].anchor)
        self.assertEqual(result[0].url, "/posts/example/")

    def test_unanchored_alias_first_heading(self):
        result = fragments('<h2 id="first">First</h2><p>Body</p>')
        mapping = make_map(result, {result[0].identity: 17})
        self.assertEqual(mapping["/posts/example/"], mapping["/posts/example/#first"])

    def test_intro_is_distinct_from_heading_named_intro(self):
        result = fragments('<p>Intro</p><h2 id="intro">Intro heading</h2><p>Body</p>')
        self.assertNotEqual(result[0].identity, result[1].identity)

    def test_skipped_levels_and_inline_heading_markup(self):
        result = fragments('<h1 id="a"><em>A</em></h1><h3 id="b">Use <code>B</code> now</h3><h3 id="c">C<a class="anchor" href="#c">#</a></h3>')
        self.assertEqual(result[1].headings, ("A", "Use B now"))
        self.assertEqual(result[2].headings, ("A", "C"))

    def test_supported_body_elements(self):
        result = fragments('<ul><li>List item</li></ul><blockquote><p>Quoted words</p></blockquote><table><tr><td>Cell</td></tr></table><dl><dt>Term</dt><dd>Definition</dd></dl><pre><code>plain_code()</code></pre><figure><img src="image.png" alt="Diagram"/><figcaption>Caption</figcaption></figure>')[0]
        text = " ".join(result.text_blocks)
        for expected in ("List item", "Quoted words", "Cell", "Term", "Definition", "plain_code()", "Diagram", "Caption"):
            self.assertIn(expected, text)
        self.assertIn('src="/posts/example/image.png"', result.html_fragment)

    def test_math_has_one_search_representation(self):
        result = fragments('<p>Equation <span class="katex"><span class="katex-mathml"><math><semantics><mi>x</mi><annotation encoding="application/x-tex">x^2</annotation></semantics></math></span><span class="katex-html">duplicate visual text</span></span> ends.</p>')[0]
        self.assertEqual(result.text_blocks, ("Equation x^2 ends.",))
        self.assertIn("duplicate visual text", result.html_fragment)

    def test_image_only_fragment_is_retained(self):
        result = fragments('<h2 id="image">Image</h2><img src="/image.png"/>')
        self.assertEqual(len(result), 1)
        self.assertIn("<img", result[0].html_fragment)

    def test_relative_links_and_named_anchors(self):
        fragment = fragments('<p><a name="named"></a><a href="#other">same page</a><a href="../next/">Next</a><a href="https://example.com/">External</a></p>')[0]
        self.assertIn('href="/posts/example/#other"', fragment.html_fragment)
        self.assertIn('href="/posts/next/"', fragment.html_fragment)
        self.assertIn('href="https://example.com/"', fragment.html_fragment)

    def test_footnotes_are_preserved(self):
        result = fragments('<p>Note<a href="#fn:1">1</a></p><div class="footnotes"><ol><li id="fn:1">Footnote</li></ol></div>')[0]
        self.assertIn("Footnote", " ".join(result.text_blocks))
        self.assertIn('id="fn:1"', result.html_fragment)

    def test_invalid_heading_ids_rejected(self):
        for body in ('<h2>Missing</h2>', '<h2 id="same">A</h2><h3 id="same">B</h3>'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                fragments(body)

    def test_nested_heading_requires_explicit_policy(self):
        with self.assertRaisesRegex(ValueError, "boundary policy"):
            fragments('<div><h2 id="nested">Nested</h2></div>')

    def test_raw_text_and_tails_are_preserved(self):
        result = fragments('Raw intro<h2 id="a">A</h2>Text after heading<p>Paragraph</p>Tail')
        self.assertEqual(result[0].text_blocks, ("Raw intro",))
        self.assertEqual(result[1].text_blocks, ("Text after heading", "Paragraph", "Tail"))


class DiscoveryTests(unittest.TestCase):
    def markup(self, **overrides):
        attrs = {"data-fragment-root": "v1", "data-build-environment": "minimal", "data-post-id": "id",
                 "data-page-url": "/custom/slug/", "data-page-title": "Title", "data-source": "posts/bundle/index.md", "data-updated-at": "1"}
        attrs.update(overrides)
        root = html.Element("div", attrs)
        root.append(html.fragment_fromstring("<p>Text</p>"))
        return html.tostring(root, encoding="unicode")

    def test_hugo_metadata_controls_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "arbitrary-name.html").write_text(self.markup())
            (root / "unmarked.html").write_text("<p>Ignore navigation</p>")
            result = discover_pages(root)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].url, "/custom/slug/")
            self.assertEqual(result[0].source, "posts/bundle/index.md")

    def test_empty_or_unmarked_input_rejected(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            discover_pages(Path(directory))

    def test_duplicate_post_ids_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.html").write_text(self.markup())
            (root / "b.html").write_text(self.markup(**{"data-page-url": "/another/"}))
            with self.assertRaises(ValueError):
                discover_pages(root)

    def test_htmx_and_production_input_rejected(self):
        for markup in (self.markup().replace("<p>", '<p hx-get="/bad">'), self.markup(**{"data-build-environment": "production"})):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "a.html").write_text(markup)
                with self.assertRaises(ValueError):
                    discover_pages(root)


class IdentityTests(unittest.TestCase):
    def test_rebuild_delete_restore_and_post_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "ids.db"
            original = fragments('<p>Intro</p><h2 id="first">First</h2><p>Text</p>')
            before = allocate(registry, original)
            edited = fragments('<p>Edited intro</p><h2 id="first">First</h2><p>More text</p>', url="/moved/")
            self.assertEqual(allocate(registry, edited), before)
            new = fragments('<h2 id="other">Other</h2>')
            next_ids = allocate(registry, new)
            self.assertTrue(set(next_ids.values()).isdisjoint(before.values()))
            self.assertEqual(allocate(registry, original), before)
            write_sections(root / "sections.db", new, next_ids)
            with closing(sqlite3.connect(root / "sections.db")) as db:
                self.assertEqual({r[0] for r in db.execute("SELECT id FROM sections")}, set(next_ids.values()))

    def test_ambiguous_delimiters_do_not_collide(self):
        a = fragments('<h2 id="b-c">A</h2>', post_id="a")[0]
        b = fragments('<h2 id="c">B</h2>', post_id="a-b")[0]
        self.assertNotEqual(a.identity, b.identity)

    def test_duplicate_identity_rejected_before_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            f = fragments("<p>Text</p>")[0]
            with self.assertRaises(ValueError):
                allocate(Path(directory) / "registry.db", [f, f])


class ChunkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoder = MiniLM()

    def test_oversized_prose_keeps_every_word(self):
        words = [f"item{i}" for i in range(1500)] + ["FINAL_SENTINEL"]
        fragment = fragments("<p>" + " ".join(words) + "</p>")[0]
        chunks = chunk_fragment(fragment, self.encoder)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(c.body for c in chunks).split(), words)
        self.assertTrue(all(self.encoder.count(c.text) <= 256 for c in chunks))
        self.assertIn("FINAL_SENTINEL", chunks[-1].text)

    def test_unbroken_code_unicode_and_long_title(self):
        fragment = fragments("<pre><code>" + "变量=α+β;" * 500 + "</code></pre>")[0]
        fragment = replace(fragment, page=replace(fragment.page, title="A very long title " * 500))
        chunks = chunk_fragment(fragment, self.encoder, 64)
        self.assertEqual("".join(c.body for c in chunks), "".join(fragment.text_blocks))
        self.assertTrue(all(self.encoder.count(c.text) <= 64 for c in chunks))

    def test_title_only_embedding(self):
        fragment = fragments('<h2 id="empty">An empty section</h2>')[0]
        self.assertEqual(len(chunk_fragment(fragment, self.encoder)), 1)

    def test_invalid_budget(self):
        fragment = fragments("<p>Text</p>")[0]
        for limit in (1, 257):
            with self.assertRaises(ValueError):
                chunk_fragment(fragment, self.encoder, limit)


class LinkVerificationTests(unittest.TestCase):
    def test_wrong_hugo_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text('<a href="/post/" hx-get="/api/fragment/2">Post</a>')
            with self.assertRaises(ValueError):
                verify_links(root, {"/post/": {"db_id": 1}})


if __name__ == "__main__":
    unittest.main()
