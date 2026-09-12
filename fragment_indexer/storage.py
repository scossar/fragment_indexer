"""Permanent allocation history lives outside replaceable build snapshots."""

import sqlite3
from contextlib import closing
from pathlib import Path

from .extraction import Fragment
from .keyword import write_keyword_index


def allocate(path: Path, fragments: list[Fragment]) -> dict[str, int]:
    keys = [f.identity for f in fragments]
    if len(keys) != len(set(keys)):
        raise ValueError("Boundary policy produced duplicate fragment identities")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section_id TEXT NOT NULL UNIQUE
        )""")
        # Never delete rows, including identities absent from this build.
        db.executemany(
            "INSERT OR IGNORE INTO identities(section_id) VALUES (?)",
            [(key,) for key in sorted(keys)],
        )
        all_ids = dict(db.execute("SELECT section_id, id FROM identities"))
        return {key: all_ids[key] for key in keys}


def write_sections(path: Path, fragments: list[Fragment], ids: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE sections (
            id INTEGER PRIMARY KEY,
            section_id TEXT NOT NULL UNIQUE,
            post_id TEXT NOT NULL,
            section_heading_slug TEXT NOT NULL,
            html_heading TEXT NOT NULL,
            html_fragment TEXT NOT NULL,
            updated_at REAL NOT NULL,
            page_url TEXT NOT NULL,
            source TEXT NOT NULL
        )""")
        db.executemany(
            "INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    ids[f.identity],
                    f.identity,
                    f.page.post_id,
                    f.anchor or "",
                    f.html_heading,
                    f.html_fragment,
                    f.page.updated_at,
                    f.page.url,
                    f.page.source,
                )
                for f in fragments
            ],
        )
        db.execute("CREATE INDEX sections_post_id ON sections(post_id)")
        write_keyword_index(db, fragments, ids)


def make_map(
    fragments: list[Fragment], ids: dict[str, int]
) -> dict[str, dict[str, int]]:
    mapping = {}
    for fragment in fragments:
        value = {"db_id": ids[fragment.identity]}
        if fragment.url in mapping and mapping[fragment.url] != value:
            raise ValueError(f"Conflicting fragment URL {fragment.url}")
        mapping[fragment.url] = value
        # First fragment is the page default, whether introductory or headed.
        mapping.setdefault(fragment.page.url, value)
    return dict(sorted(mapping.items()))
