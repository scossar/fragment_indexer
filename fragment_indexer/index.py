"""Run in a child process so Chroma is quiescent before publishing a snapshot."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from importlib.metadata import version

import chromadb
from chromadb.config import Settings

from .embedding import MiniLM, chunk_fragment
from .extraction import NextHeadingPolicy, discover_pages, extract
from .storage import allocate, make_map, write_sections
from .keyword import verify_keyword_index


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def index_html(html_dir: Path, release: Path, registry: Path, max_tokens: int, collection_name: str):
    policy = NextHeadingPolicy()
    pages = discover_pages(html_dir)
    fragments = [f for page in pages for f in extract(page, policy)]
    if not fragments:
        raise ValueError("No fragments found; refusing to replace the current index")
    encoder = MiniLM()
    prepared = [(f, chunk_fragment(f, encoder, max_tokens)) for f in fragments]
    # Allocate only after extraction and tokenization have succeeded. Failed later
    # builds may reserve IDs; gaps are harmless and IDs are never reused.
    ids = allocate(registry, fragments)
    write_sections(release / "sqlite/sections.db", fragments, ids)
    mapping = make_map(fragments, ids)
    write_json(release / "data/fragments/sections.json", mapping)
    client = chromadb.PersistentClient(path=str(release / "chroma"),
                                       settings=Settings(anonymized_telemetry=False))
    collection = client.create_collection(collection_name, embedding_function=encoder.function)
    records = []
    audit = []
    for fragment, chunks in prepared:
        db_id = ids[fragment.identity]
        for ordinal, chunk in enumerate(chunks):
            chunk_id = f"{db_id}:{ordinal}"
            metadata = {
                "db_id": db_id,
                "page_title": fragment.page.title,
                "section_heading": fragment.headings[-1] if fragment.headings else fragment.page.title,
                "section_id": fragment.identity,
                "page_url": fragment.page.url,
                "fragment_url": fragment.url,
                "updated_at": fragment.page.updated_at,
                "chunk_index": ordinal,
                "token_count": chunk.token_count,
            }
            records.append((chunk_id, chunk.text, metadata))
            audit.append({"id": chunk_id, "text": chunk.text, "body": chunk.body, **metadata})
    for start in range(0, len(records), 64):
        batch = records[start:start + 64]
        collection.add(ids=[r[0] for r in batch], documents=[r[1] for r in batch],
                       metadatas=[r[2] for r in batch])
    write_json(release / "chunks.json", audit)
    # Verify stored references, documents, and full token lengths, not just input counts.
    stored = collection.get(include=["metadatas", "documents"])
    expected = {r[0]: r for r in records}
    if set(stored["ids"]) != set(expected):
        raise ValueError("Stored Chroma IDs do not match generated chunks")
    stored_metadata, stored_documents = stored["metadatas"], stored["documents"]
    if stored_metadata is None or stored_documents is None:
        raise ValueError("Chroma did not return the requested metadata and documents")
    for key, metadata, document in zip(stored["ids"], stored_metadata, stored_documents, strict=True):
        if metadata != expected[key][2] or document != expected[key][1]:
            raise ValueError(f"Stored Chroma record differs: {key}")
        if encoder.count(document) > max_tokens:
            raise ValueError(f"Stored chunk exceeds token budget: {key}")
    with closing(sqlite3.connect(release / "sqlite/sections.db")) as db, db:
        sql_ids = {row[0] for row in db.execute("SELECT id FROM sections")}
        keyword_count = verify_keyword_index(db)
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
    if sql_ids != set(ids.values()) or not all(v["db_id"] in sql_ids for v in mapping.values()):
        raise ValueError("Fragment references do not resolve")
    if not all(r[2]["db_id"] in sql_ids for r in records):
        raise ValueError("Chroma references missing HTML fragments")
    if set(v["db_id"] for v in mapping.values()) != sql_ids:
        raise ValueError("A fragment has no Hugo URL mapping")
    if records:
        query = collection.query(query_texts=[records[0][1]], n_results=min(3, len(records)))
        query_metadata = query["metadatas"]
        if not query["ids"][0] or not query_metadata or not all(m["db_id"] in sql_ids for m in query_metadata[0]):
            raise ValueError("Real Chroma query failed to resolve fragments")
    summary = {
        "schema_version": 1, "boundary_policy": policy.name, "chunker_version": 1,
        "keyword_index": {"version": 1, "tokenizer": "unicode61", "fragments": keyword_count},
        "embedding_model": encoder.name, "max_tokens": max_tokens,
        "tokenizer_sha256": encoder.tokenizer_sha256,
        "collection": collection_name,
        "versions": {name: version(name) for name in ("chromadb", "lxml", "tokenizers")},
        "pages": len(pages), "fragments": len(fragments), "chunks": len(records),
        "url_mappings": len(mapping), "max_observed_tokens": max((r[2]["token_count"] for r in records), default=0),
        "fragment_content_sha256": hashlib.sha256("".join(f.identity + f.html_heading + f.html_fragment for f in fragments).encode()).hexdigest(),
    }
    write_json(release / "build.json", summary)
    print(json.dumps(summary, indent=2), flush=True)
