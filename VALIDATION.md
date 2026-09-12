# Validation — 2026-09-12

- All 28 indexer tests passed, including real Hugo/Chroma builds and repeated
  publication, deletion, restoration, and failed-build preservation.
- Pyright passed with zero errors or warnings using the indexer's environment.
- A full isolated build of the Zalgorithm site produced 20 pages, 70 fragments,
  70 FTS rows, 70 embedding chunks, and 80 URL mappings. All 20 generated HTMX
  links were verified.
- FTS tests verify the final text of long fragments is searchable, empty heading
  fragments retain searchable context, canonical page URLs remain retrievable,
  row IDs equal HTML fragment IDs, deleted words/rows disappear on rebuild, and
  read-only queries leave the database unchanged.
- The API integration suite passed all 45 tests against the new snapshot in both
  local and HTTP Chroma modes, including keyword, inclusion/exclusion, and hybrid
  queries with the real MiniLM encoder.
- No source-site templates, content, or deployment scripts were changed.

# Validation — 2026-09-11

Verified locally with Python 3.13, Hugo 0.165.0, Chroma 1.5.9, and the actual
all-MiniLM-L6-v2 ONNX encoder. No deploy or full_deploy script was run.

- 26 tests passed, including real minimal/production Hugo builds and persisted
  Chroma queries against synthetic fixtures.
- The current published Zalgorithm test corpus produced 20 pages, 70 HTML
  fragments, 70 embedding chunks, and 80 URL mappings.
- All 20 generated HTMX fragment links resolved to the expected numeric IDs.
- Every Chroma chunk resolved to a stored HTML fragment. The largest input in the
  final site build was 160 tokens, including heading context and special tokens.
- A second site build preserved all 70 numeric fragment IDs.
- Synthetic oversized prose and Unicode/code inputs produced multiple chunks;
  their complete body text, including the final sentinel, was retained.
- Integration fixtures verified draft exclusion, custom slugs and leaf bundles,
  first-heading aliases, intro mappings, shrinking sections, restored identities,
  failed-build preservation of the active snapshot, and real query-to-HTML lookup.

Three temporary source posts contained Python-style escaped control characters in
LaTeX. Those equations were repaired with filesystem approval so Hugo could build:

- hypotrochoids-from-three-parameters.md
- why-logarithms-compress-large-values.md
- negative-derivatives-and-direction.md

Original bytes were saved under /tmp/fragment-test-post-backups. The new build
command itself does not modify source posts or templates. The old embeddings
project and its databases were not used as output or migration inputs.
