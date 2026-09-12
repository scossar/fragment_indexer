"""Build a full-fragment FTS index independently of embedding chunk boundaries."""

import sqlite3

from .extraction import Fragment


def write_keyword_index(
    db: sqlite3.Connection, fragments: list[Fragment], ids: dict[str, int]
) -> None:
    db.execute("""CREATE VIRTUAL TABLE sections_fts USING fts5(
        page_title, headings, body, tokenize='unicode61'
    )""")
    db.executemany(
        "INSERT INTO sections_fts(rowid, page_title, headings, body) VALUES (?, ?, ?, ?)",
        [
            (ids[f.identity], f.page.title, " > ".join(f.headings), "\n".join(f.text_blocks))
            for f in fragments
        ],
    )


def verify_keyword_index(db: sqlite3.Connection) -> int:
    # The FTS integrity command validates the inverted index as well as content.
    db.execute("INSERT INTO sections_fts(sections_fts, rank) VALUES ('integrity-check', 1)")
    keyword_ids = {row[0] for row in db.execute("SELECT rowid FROM sections_fts")}
    section_ids = {row[0] for row in db.execute("SELECT id FROM sections")}
    if keyword_ids != section_ids:
        raise ValueError("Keyword index IDs differ from HTML fragment IDs")
    return len(keyword_ids)
