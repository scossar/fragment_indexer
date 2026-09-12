from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from lxml import html

from fragment_indexer.extraction import NextHeadingPolicy, Page, extract
from fragment_indexer.keyword import verify_keyword_index
from fragment_indexer.storage import allocate, write_sections


class KeywordIndexTests(unittest.TestCase):
    def test_full_text_context_ids_urls_and_read_only_queries(self):
        page = Page('post', '/custom/path/', 'Learning', 'posts/example.md', 1.0,
                    html.fragment_fromstring('<p>Intro</p><h2 id="gradient">Gradient</h2><p>' + 'padding ' * 1000 + 'finalsentinel</p><h3 id="empty">Empty</h3>', create_parent='div'))
        fragments = extract(page, NextHeadingPolicy())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ids = allocate(root / 'registry.db', fragments)
            path = root / 'sections.db'
            write_sections(path, fragments, ids)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(verify_keyword_index(db), 3)
            before = path.read_bytes()
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                rows = dict(db.execute('SELECT rowid, body FROM sections_fts'))
                self.assertEqual(set(rows), set(ids.values()))
                self.assertTrue(rows[ids[fragments[1].identity]].endswith('finalsentinel'))
                matches = db.execute('SELECT rowid FROM sections_fts WHERE sections_fts MATCH ?', ('finalsentinel',)).fetchall()
                self.assertEqual(matches, [(ids[fragments[1].identity],)])
                self.assertEqual(db.execute('SELECT count(*) FROM sections_fts WHERE sections_fts MATCH ?', ('headings:gradient',)).fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT count(*) FROM sections_fts WHERE sections_fts MATCH ?', ('page_title:learning',)).fetchone()[0], 3)
                self.assertEqual(db.execute('SELECT page_url FROM sections WHERE id=?', matches[0]).fetchone()[0], '/custom/path/')
                self.assertTrue(all('<p>' not in body for body in rows.values()))
            self.assertEqual(path.read_bytes(), before)

            # Rebuild from changed fragments: old words and removed rows disappear.
            remaining = [replace(fragments[0], text_blocks=('replacement',))]
            new_ids = allocate(root / 'registry.db', remaining)
            write_sections(root / 'new.db', remaining, new_ids)
            with closing(sqlite3.connect(root / 'new.db')) as db, db:
                self.assertEqual(verify_keyword_index(db), 1)
                self.assertEqual(db.execute('SELECT rowid FROM sections_fts WHERE sections_fts MATCH ?', ('finalsentinel',)).fetchall(), [])
                self.assertEqual(db.execute('SELECT rowid FROM sections_fts WHERE sections_fts MATCH ?', ('replacement',)).fetchone()[0], ids[fragments[0].identity])

    def test_missing_fts_row_fails_validation(self):
        with closing(sqlite3.connect(':memory:')) as db, db:
            db.execute('CREATE TABLE sections(id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO sections VALUES (1)')
            db.execute('CREATE VIRTUAL TABLE sections_fts USING fts5(page_title, headings, body)')
            with self.assertRaisesRegex(ValueError, 'IDs differ'):
                verify_keyword_index(db)


if __name__ == '__main__':
    unittest.main()
