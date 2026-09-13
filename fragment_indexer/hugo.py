"""Local Hugo integration; never calls deployment scripts or edits the source site."""

import json
import shutil
import subprocess
from pathlib import Path

from lxml import html

ROOT_TEMPLATE = """{{ $indexable := false }}
{{ with .File }}
  {{ $indexable = and (in (slice "md" "markdown" "mdown") .Ext) (not (in (slice "index" "_index") .TranslationBaseName)) }}
{{ end }}
{{ if and (eq hugo.Environment "minimal") $indexable }}
  {{ if not .Params.id }}{{ errorf "Post %s is missing frontmatter id" .File.Path }}{{ end }}
  <div data-fragment-root="v1" data-build-environment="minimal"
       data-post-id="{{ .Params.id }}" data-page-url="{{ .RelPermalink }}"
       data-page-title="{{ .Title }}" data-source="{{ .File.Path }}"
       data-updated-at="{{ .Lastmod.Unix }}">
    {{ .Content }}
  </div>
{{ else }}
  {{ .Content }}
{{ end }}"""


def copy_site(source: Path, target: Path, page_template: str):
    config = json.loads(
        subprocess.check_output(
            [
                "hugo",
                "config",
                "--source",
                str(source),
                "--environment",
                "minimal",
                "--format",
                "json",
            ],
            text=True,
        )
    )
    content = source / config["contentdir"]
    if not content.is_dir():
        raise ValueError(f"Missing content directory: {content}")
    template = source / page_template
    if not template.is_file():
        raise ValueError(f"Missing page template: {template}")
    template_text = template.read_text()
    if template_text.count("{{ .Content }}") != 1:
        raise ValueError(
            "Page template must contain exactly one '{{ .Content }}' marker"
        )
    # The copy contains local inputs, not existing generated output or executables
    # with deploy semantics. Hugo itself is the only external build command used.
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "node_modules",
            "public",
            "resources",
            ".hugo_build.lock",
            "deploy",
            "full_deploy",
            "local_deploy",
        ),
    )
    override = target / "layouts/page.html"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(template_text.replace("{{ .Content }}", ROOT_TEMPLATE))
    data = target / "data/fragments/sections.json"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text("{}\n")  # do not let legacy fragment mappings affect extraction


def render(site: Path, destination: Path, environment: str):
    subprocess.run(
        [
            "hugo",
            "build",
            "--source",
            str(site),
            "--destination",
            str(destination),
            "--environment",
            environment,
            "--cacheDir",
            str(site / ".build-cache"),
        ],
        check=True,
    )


def verify_links(directory: Path, mapping: dict) -> dict:
    eligible = enhanced = 0
    for path in directory.rglob("*.html"):
        tree = html.parse(str(path))
        for link in tree.xpath("//a[@href]"):
            href = link.get("href")
            expected = mapping.get(href)
            if expected and not link.get("rel"):
                eligible += 1
                # Only hook-generated links must be enhanced. Template title/heading
                # anchors do not pass through the Markdown hook.
            get = link.get("hx-get")
            if get and "/api/fragment/" in get:
                if not expected or not get.endswith(
                    f"/api/fragment/{expected['db_id']}"
                ):
                    raise ValueError(
                        f"{path}: HTMX fragment ID disagrees with URL map: {href}"
                    )
                enhanced += 1
    return {"mapped_links": eligible, "htmx_links_verified": enhanced}
