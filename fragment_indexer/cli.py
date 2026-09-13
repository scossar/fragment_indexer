import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


def parser():
    root = argparse.ArgumentParser(
        description="Build local Hugo fragments and embeddings; never deploys"
    )
    commands = root.add_subparsers(dest="command", required=True)
    build = commands.add_parser(
        "build", help="Build and verify an isolated copy of a Hugo site"
    )
    build.add_argument("--site", type=Path, required=True)
    build.add_argument("--output", type=Path, default=Path("output"))
    build.add_argument(
        "--page-template", default="themes/zalgorithm_theme/layouts/page.html"
    )
    build.add_argument("--max-tokens", type=int, default=256)
    build.add_argument("--collection", default="zalgorithm")
    index = commands.add_parser("_index", help=argparse.SUPPRESS)
    index.add_argument("--html", type=Path, required=True)
    index.add_argument("--release", type=Path, required=True)
    index.add_argument("--registry", type=Path, required=True)
    index.add_argument("--max-tokens", type=int, required=True)
    index.add_argument("--collection", required=True)
    query = commands.add_parser(
        "query", help="Query a completed local snapshot and retrieve its HTML"
    )
    query.add_argument("text")
    query.add_argument("--output", type=Path, default=Path("output"))
    query.add_argument("--limit", type=int, default=5)
    return root


def build(args):
    from .hugo import copy_site, render, verify_links

    site, output = args.site.expanduser().resolve(), args.output.expanduser().resolve()
    if output == site or site in output.parents or output in site.parents:
        raise ValueError("Output and source site must be separate directory trees")
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        registry = output / "state/identities.sqlite3"
        releases = output / "releases"
        if not registry.exists() and (output / "current").is_symlink():
            raise ValueError(
                "Permanent ID registry is missing; restore it before rebuilding"
            )
        releases.mkdir(exist_ok=True)
        release = releases / uuid.uuid4().hex
        release.mkdir()
        published = False
        try:
            with tempfile.TemporaryDirectory(prefix="fragment-hugo-") as temporary:
                isolated = Path(temporary) / "site"
                print("Copying Hugo inputs to an isolated local workspace", flush=True)
                copy_site(site, isolated, args.page_template)
                print("Building minimal HTML", flush=True)
                render(isolated, release / "minimal", "minimal")
                print("Extracting fragments and generating embeddings", flush=True)
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "fragment_indexer",
                        "_index",
                        "--html",
                        str(release / "minimal"),
                        "--release",
                        str(release),
                        "--registry",
                        str(registry),
                        "--max-tokens",
                        str(args.max_tokens),
                        "--collection",
                        args.collection,
                    ],
                    check=True,
                )
                mapping_path = release / "data/fragments/sections.json"
                shutil.copy2(mapping_path, isolated / "data/fragments/sections.json")
                print(
                    "Building production HTML locally and verifying HTMX IDs",
                    flush=True,
                )
                render(isolated, release / "public", "production")
                mapping = json.loads(mapping_path.read_text())
                verification = verify_links(release / "public", mapping)
                summary_path = release / "build.json"
                summary = json.loads(summary_path.read_text())
                summary.update(verification)
                summary["hugo_version"] = subprocess.check_output(
                    ["hugo", "version"], text=True
                ).strip()
                summary_path.write_text(json.dumps(summary, indent=2) + "\n")
            # Both Hugo passes and the Chroma subprocess have completed before switch.
            link = output / (".current-" + release.name)
            link.symlink_to(Path("releases") / release.name)
            os.replace(link, output / "current")
            published = True
            print(f"Completed local snapshot: {output / 'current'}", flush=True)
            print(json.dumps(summary, indent=2))
        finally:
            if not published:
                shutil.rmtree(release)


def query(args):
    import sqlite3
    from contextlib import closing

    import chromadb
    from chromadb.config import Settings

    from .embedding import MiniLM

    # Resolve once so a concurrent build cannot mix two snapshots.
    release = (args.output.expanduser() / "current").resolve(strict=True)
    summary = json.loads((release / "build.json").read_text())
    encoder = MiniLM()
    if (
        summary["embedding_model"] != encoder.name
        or summary["tokenizer_sha256"] != encoder.tokenizer_sha256
    ):
        raise ValueError(
            "Query encoder does not match the snapshot's recorded model/tokenizer"
        )
    client = chromadb.PersistentClient(
        path=str(release / "chroma"), settings=Settings(anonymized_telemetry=False)
    )
    collection = client.get_collection(
        summary["collection"], embedding_function=encoder.function
    )
    if args.limit < 1:
        raise ValueError("limit must be positive")
    if not collection.count():
        print("[]")
        return
    result = collection.query(
        query_texts=[args.text], n_results=min(args.limit, collection.count())
    )
    metadata_groups, distance_groups = result["metadatas"], result["distances"]
    if not metadata_groups or not distance_groups:
        raise ValueError("Chroma did not return the requested metadata and distances")
    seen, rows = set(), []
    with closing(sqlite3.connect(release / "sqlite/sections.db")) as db:
        for metadata, distance in zip(
            metadata_groups[0], distance_groups[0], strict=True
        ):
            db_id = metadata["db_id"]
            if db_id in seen:
                continue
            seen.add(db_id)
            record = db.execute(
                "SELECT html_heading, html_fragment FROM sections WHERE id=?", (db_id,)
            ).fetchone()
            if record is None:
                raise ValueError(f"Missing fragment {db_id}")
            rows.append(
                {
                    "db_id": db_id,
                    "url": metadata["fragment_url"],
                    "distance": distance,
                    "html": "".join(record),
                }
            )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main():
    args = parser().parse_args()
    try:
        if args.command == "build":
            build(args)
        elif args.command == "query":
            query(args)
        else:
            from .index import index_html

            index_html(
                args.html, args.release, args.registry, args.max_tokens, args.collection
            )
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0
