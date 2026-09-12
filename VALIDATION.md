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
