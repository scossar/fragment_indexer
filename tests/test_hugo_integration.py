"""Real Hugo, ONNX embeddings, persistent Chroma, and repeated staged builds."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(shutil.which("hugo"), "Hugo is required for integration tests")
class HugoIntegrationTests(unittest.TestCase):
    def test_selected_content_any_section_and_empty_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site, output = root / "site", root / "output"
            content = site / "content-development"
            content.mkdir(parents=True)
            (site / "layouts").mkdir()
            (site / "hugo.toml").write_text('baseURL = "https://example.com/"\n')
            (site / "layouts/page.html").write_text('<html><body>{{ .Content }}</body></html>')
            (site / "layouts/home.html").write_text('<html><body>Home</body></html>')
            sources = ("about.md", "posts/mountain-biking.md", "ride-logs/2026/sept-12.md")
            for ordinal, name in enumerate(sources):
                path = content / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'+++\ntitle = "Page {ordinal}"\nid = "page-{ordinal}"\n+++\nBody {ordinal}.')
            for name in ("_index.md", "ride-logs/_index.md", "bundle/index.md"):
                path = content / name
                path.parent.mkdir(parents=True, exist_ok=True)
                # Excluded index files do not need fragment IDs.
                path.write_text('+++\ntitle = "Ignored"\n+++\nIgnored content.')
            command = [sys.executable, "-m", "fragment_indexer", "build", "--site", str(site),
                       "--output", str(output), "--page-template", "layouts/page.html"]
            env = dict(os.environ, HUGO_CONTENTDIR="content-development")

            def build():
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return (output / "current").resolve()

            first = build()
            with closing(sqlite3.connect(first / "sqlite/sections.db")) as db:
                self.assertEqual({r[0] for r in db.execute("SELECT source FROM sections")}, set(sources))
            # There is deliberately no content/ or content/posts/ directory.
            self.assertFalse((site / "content").exists())
            # Also exercise contentDir from environment-specific Hugo config.
            env.pop("HUGO_CONTENTDIR")
            config = site / "config/minimal/hugo.toml"
            config.parent.mkdir(parents=True)
            config.write_text('contentDir = "content-development"\n')
            production_config = site / "config/production/hugo.toml"
            production_config.parent.mkdir(parents=True)
            production_config.write_text(config.read_text())
            for path in content.rglob("*.md"):
                path.unlink()
            empty = build()
            self.assertNotEqual(first, empty)
            with closing(sqlite3.connect(empty / "sqlite/sections.db")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM sections").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM sections_fts").fetchone()[0], 0)
            with closing(sqlite3.connect(output / "state/identities.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM identities").fetchone()[0], 3)
            self.assertEqual(json.loads((empty / "chunks.json").read_text()), [])
            self.assertEqual(json.loads((empty / "data/fragments/sections.json").read_text()), {})
            summary = json.loads((empty / "build.json").read_text())
            for key in ("pages", "fragments", "chunks", "url_mappings", "max_observed_tokens"):
                self.assertEqual(summary[key], 0)
            result = subprocess.run([sys.executable, "-m", "fragment_indexer", "query", "anything",
                                     "--output", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [])
            shutil.rmtree(content)
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Missing content directory", result.stderr)
            self.assertEqual((output / "current").resolve(), empty)

    def test_build_rebuild_delete_restore_and_failed_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site, output = root / "site", root / "output"
            (site / "content/posts").mkdir(parents=True)
            (site / "layouts/_markup").mkdir(parents=True)
            (site / "hugo.toml").write_text('baseURL = "https://example.com/"\n')
            (site / "layouts/page.html").write_text('<html><body><article><h1>{{ .Title }}</h1>{{ .Content }}</article></body></html>')
            (site / "layouts/home.html").write_text('<html><body>Home</body></html>')
            (site / "layouts/_markup/render-link.html").write_text('''{{- $u := urls.Parse .Destination -}}
{{- $url := $u.String -}}
{{- with .Page.GetPage $u.Path -}}{{- $url = .RelPermalink -}}{{- with $u.Fragment -}}{{- $url = printf "%s#%s" $url . -}}{{- end -}}{{- end -}}
{{- $data := index site.Data.fragments.sections $url -}}
<a href="{{ $url }}"{{ if and (ne hugo.Environment "minimal") $data }} hx-get="/api/fragment/{{ $data.db_id }}"{{ end }}>{{ .Text }}</a>''')

            def post(name, identifier, body, extra=""):
                path = site / "content/posts" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'+++\ntitle = "{name}"\nid = "{identifier}"\n{extra}\n+++\n\n{body}')
                return path

            long_text = " ".join(f"number{i}" for i in range(900)) + " FINAL_SENTINEL"
            intro = post("intro.md", "intro", long_text + '\n\n## Later\n\nRemaining text\n\n[First](/posts/first.md#first)\n\n[Whole first](/posts/first.md)')
            post("first.md", "first", '## First\n\nAlpha text.\n\n### Child\n\nBeta text.\n\n[Headingless](/posts/bundle/)')
            post("headingless.md", "headingless", 'No headings here.', 'slug = "custom-name"')
            post("bundle/index.md", "", 'Ignored bundle page.')
            post("_index.md", "", 'Ignored section page.')
            post("draft.md", "draft", 'Not published', 'draft = true')
            command = [sys.executable, "-m", "fragment_indexer", "build", "--site", str(site),
                       "--output", str(output), "--page-template", "layouts/page.html"]

            def build():
                result = subprocess.run(command, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                current = (output / "current").resolve()
                mapping = json.loads((current / "data/fragments/sections.json").read_text())
                chunks = json.loads((current / "chunks.json").read_text())
                return current, mapping, chunks

            first, mapping, chunks = build()
            self.assertEqual(mapping['/posts/first/'], mapping['/posts/first/#first'])
            self.assertNotEqual(mapping['/posts/intro/'], mapping['/posts/intro/#later'])
            self.assertIn('/posts/custom-name/', mapping)
            self.assertNotIn('/posts/draft/', mapping)
            intro_id = mapping['/posts/intro/']['db_id']
            intro_chunks = [c for c in chunks if c['db_id'] == intro_id]
            self.assertGreater(len(intro_chunks), 1)
            self.assertIn('FINAL_SENTINEL', intro_chunks[-1]['text'])
            with closing(sqlite3.connect(first / 'sqlite/sections.db')) as db:
                self.assertTrue(all('hx-' not in row[0] for row in db.execute('SELECT html_fragment FROM sections')))
                self.assertEqual(db.execute("SELECT rowid FROM sections_fts WHERE sections_fts MATCH ?", ('FINAL_SENTINEL',)).fetchall(), [(intro_id,)])
            summary = json.loads((first / 'build.json').read_text())
            self.assertGreaterEqual(summary['htmx_links_verified'], 2)
            self.assertEqual(summary['keyword_index'], {'version': 1, 'tokenizer': 'unicode61', 'fragments': summary['fragments']})

            # Replace body and remove a heading: retained identities keep IDs, old
            # chunks and deleted headings are absent from the new snapshot.
            post('intro.md', 'intro', 'A short replacement.')
            second, short_mapping, short_chunks = build()
            self.assertEqual(short_mapping['/posts/intro/']['db_id'], intro_id)
            self.assertNotIn('/posts/intro/#later', short_mapping)
            self.assertEqual(len([c for c in short_chunks if c['db_id'] == intro_id]), 1)
            self.assertTrue(first.exists())
            with closing(sqlite3.connect(second / 'sqlite/sections.db')) as db:
                self.assertEqual(db.execute("SELECT rowid FROM sections_fts WHERE sections_fts MATCH ?", ('FINAL_SENTINEL',)).fetchall(), [])
                self.assertNotIn(mapping['/posts/intro/#later']['db_id'], {r[0] for r in db.execute('SELECT rowid FROM sections_fts')})

            # Reintroduce the heading: its original permanent ID is restored.
            post('intro.md', 'intro', 'Intro\n\n## Later\n\nBack again.')
            third, restored, _ = build()
            self.assertEqual(restored, mapping)
            self.assertNotEqual(second, third)

            # A duplicate post ID fails without changing the current release.
            post('duplicate.md', 'intro', 'Duplicate ID')
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((output / 'current').resolve(), third)
            self.assertIn('duplicate post ID', result.stderr)

            result = subprocess.run([sys.executable, '-m', 'fragment_indexer', 'query',
                                     'Alpha text', '--output', str(output), '--limit', '3'],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = json.loads(result.stdout)
            self.assertTrue(rows)
            self.assertTrue(all(row['html'] and isinstance(row['db_id'], int) for row in rows))


if __name__ == '__main__':
    unittest.main()
