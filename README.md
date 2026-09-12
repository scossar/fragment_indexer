# Fragment indexer

Build HTML fragments, a SQLite FTS5 keyword index, and Chroma embeddings from a minimal Hugo build, then verify
HTMX links in a second, production Hugo build. Both builds run locally in an
isolated copy of the source site. This tool never invokes deployment scripts,
contacts the deployment server, or changes the source site's generated data.

## Run

Python 3.13, `uv`, and Hugo are required. The integration currently targets the
Zalgorithm theme's page template. Dependencies are locked in `uv.lock`.

```bash
cd ~/projects/python/fragment_indexer
uv sync --locked
uv run fragment-indexer build --site ~/zalgorithm
uv run fragment-indexer query 'How does gradient descent work?'
uv run python -m unittest discover -s tests -v
```

The default output directory is `./output`. Use `--output /path/to/output` for a
separate index. Keep using that directory for subsequent builds: it owns the
permanent ID registry. `--max-tokens` can lower the default 256-token budget;
`--collection` defaults to `zalgorithm`. The encoder is explicitly
`all-MiniLM-L6-v2`, matching the existing consumer's Chroma default encoder.
The first encoder call may download the model if it is not already cached.

For a different page template, pass `--page-template` relative to the Hugo site.
The current adapter requires one exact `{{ .Content }}` marker in that template.
It creates a `layouts/page.html` override **in the isolated copy**. The actual
Hugo site and its theme are not modified by the build command.

## Agreed contract

- An introductory fragment holds content before the first content heading.
- A heading fragment ends immediately before the next heading, irrespective of
  level. Heading ancestry supplies context; it does not extend the fragment.
- A headingless post still has an introductory fragment and embeddings.
- Each embedding chunk carries the numeric `db_id` of its HTML fragment. A long
  fragment can have many chunks. Embedding size never changes HTML boundaries.
- A bare page URL maps to the introduction when present, otherwise to the first
  heading fragment. The bare URL and that first heading URL share one `db_id`.
- `data/fragments/sections.json` keeps the existing format:
  `{"/posts/example/#heading": {"db_id": 17}}`.
- Fragments come only from minimal HTML. HTMX attributes in extraction input fail
  the build. Production links are generated using the new map in the second pass.

The generated content-root attributes provide the frontmatter post ID, Hugo's
canonical URL, title, source path, and last-modified timestamp. Only Hugo-published
pages in the `posts` section are marked. Drafts, future posts, slug overrides, and
leaf bundles therefore follow Hugo's own publication and routing rules, without
recreating those rules in Python. The template title/date/tags are outside the root.

## Files and identities

```text
output/
  state/identities.sqlite3       permanent allocation history; keep and back up
  releases/<build-id>/
    sqlite/sections.db          active HTML fragments and their FTS5 keyword index
    chroma/                    complete local Chroma database
    data/fragments/sections.json
    chunks.json                exact embedding documents and source body text
    build.json                 counts, model/tokenizer/policy versions, verification
    minimal/                   first Hugo pass
    public/                    production HTML with HTMX links
  current -> releases/<build-id>
```

Logical identity is the structured tuple `(post_id, "intro", null)` or
`(post_id, "heading", actual_HTML_heading_id)`. The registry allocates each identity
one permanent numeric ID. Removed identities remain reserved, and reintroducing
the same identity restores its ID. Body edits and URL moves preserve IDs. Changing
a heading's anchor creates a new identity; use explicit Markdown heading IDs when
anchors must survive heading renames. Post IDs must never be repurposed.

The active `sections` table contains only the current fragments. An old ID whose
section was removed finds no row; it cannot retrieve another section. The
existing API's missing-fragment response is unchanged by this project.

Each build creates a fresh snapshot, eliminating stale embedding chunks and
removed posts. The Chroma worker process exits before publication. After SQLite (including FTS),
Chroma, JSON, and production-link validation pass, an atomic symlink replacement
switches `current`. A failed build leaves the previous snapshot in place. A lock
prevents concurrent builds from allocating or publishing inconsistently. Failed
builds can reserve unused IDs; gaps are intentional and harmless.

Do not delete the registry to rebuild embeddings. Keep it when removing obsolete
release directories. The tool rejects a missing registry when `current` already
exists. There is no remote release/deployment implementation in this project.

## Extraction and chunking

`extraction.py` handles discovery and HTML/text extraction. `NextHeadingPolicy`
implements the separate `BoundaryPolicy` interface. A future policy can inspect
all page blocks and choose boundaries based on heading level, content size, or
other criteria without involving the embedding splitter or storage layer.
Changing the policy still requires deciding whether existing logical identities
represent the new spans correctly.

The current policy supports headings directly inside the marked body. Nested
headings inside a wrapper, list, or blockquote fail with an explicit message; a
future policy must define how to split those structures without breaking their
HTML. Empty heading sections and media-only sections remain addressable; where
there is no body text, title/heading context supplies an embedding. Completely
empty posts yield no fragment. An entirely empty discovered site is rejected.

Text extraction includes paragraphs, lists, blockquotes, tables, definition lists,
plain/highlighted code, captions, image alt text, and footnotes. KaTeX equations use
their original LaTeX annotation once rather than duplicating MathML and visual
spans. Stored HTML keeps the original rendering and footnotes. Relative `href`,
`src`, and `poster` values resolve against the canonical source-page URL. Relative
URLs in CSS or `srcset` are not currently rewritten.

`embedding.py` uses the actual model tokenizer with truncation and padding disabled
for counting. Counts include special tokens and heading context. Blocks are packed
in order; oversized blocks are divided into fitting substrings, preferably at word
boundaries. Every input substring remains represented, including the last part of
long paragraphs and unbroken code. There is no overlap in this first version.

## Keyword index

Every new snapshot includes a `sections_fts` FTS5 table in `sqlite/sections.db`:

| Column | Content |
| ------ | ------- |
| `rowid` | The permanent fragment ID, matching `sections.id` |
| `page_title` | Plain-text post title |
| `headings` | Full heading ancestry |
| `body` | All extracted text blocks, joined with newlines |

The table uses SQLite's `unicode61` tokenizer and one row per whole fragment,
independent of embedding chunks. Heading-only fragments remain searchable through
their context. HTML is not indexed directly; code, math, image alt text, and other
supported content use the same clean extraction as embeddings. The canonical post
path remains available in `sections.page_url`.

`build.json` adds `keyword_index` with `version: 1`, `tokenizer: "unicode61"`, and
`fragments` (the number of FTS rows). The overall snapshot schema stays at version 1
because the existing HTML/Chroma contract is unchanged. Validation checks FTS
integrity and exact equality of FTS row IDs with active HTML fragment IDs before
publication. Fresh builds remove obsolete words and deleted fragments automatically.

The API owns query interpretation, keyword inclusion/exclusion, and combined
semantic/keyword ranking. It queries FTS read-only; it does not import this package.
SQLite FTS5 already supports Boolean expressions, phrases, prefixes, and column
filters without an index schema change. No additional Python search dependency is
required. A SQLite build with FTS5 support is required for indexing and searching.

Rebuild with the normal `fragment-indexer build` command to add keyword search to an
existing output directory. Keep the permanent registry; completed older snapshots
are not migrated in place. Restart the API after the new release is published.
The indexer's existing CLI `query` command remains a semantic query helper.

## Compatibility and verification

The existing API can still select `html_heading, html_fragment` from `sections`
using `id`, and Chroma results retain `db_id`, `page_title`, `section_heading`, and
`updated_at`. The local query command deduplicates by `db_id`. Zalgorithm API also deduplicates by `db_id`, preserving distinct fragments that
share heading text.

The existing Hugo hook consumes the generated map unchanged, via
`site.Data.fragments.sections`. Hugo 0.165 emits a deprecation warning for this
access form; the user-requested interface is retained. Lookup keys are canonical
resolved page URLs, optionally with an anchor; query-string variants are not
additional map entries.

Tests include real Hugo builds, the actual ONNX tokenizer/encoder, a persistent
Chroma query, and repeated snapshots. They exercise introductions, headingless
posts, first-heading aliases, slug/bundle routing, drafts, nested heading errors,
inline heading markup, skipped heading levels, lists/tables/code/images/math,
long inputs, permanent IDs, shortened content, restored sections, and failed-build
publication. The integration test skips if Hugo is unavailable. Model tests can
need a model download on an uncached machine.

## Editor setup

`pyrightconfig.json` selects this project's `.venv` and Python 3.13. `uv sync
--locked` also installs the development type stubs for lxml. Open this directory as
the editor project root and restart the Python language server after environment
changes. Run `pyright` from the project root to check the code.
