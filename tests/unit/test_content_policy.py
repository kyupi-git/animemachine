import contextlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from animemachine.torrents import content_policy


class ContentPolicyTests(unittest.TestCase):
    def test_explicit_flags_and_release_markers_avoid_title_false_positives(self):
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE anime_work(id INTEGER,is_nsfw INTEGER)')
            db.executemany('INSERT INTO anime_work VALUES(?,?)',[(1,1),(2,0)])
            self.assertTrue(content_policy.blocked(db,1))
            self.assertFalse(content_policy.blocked(db,2))
        for text,expected in [('[R-18] Release',True),('【NSFW】 Release',True),
                              ('The Hentai Prince',False),('[Group] TV [1080p]',False)]:
            self.assertEqual(expected,content_policy.excluded_text(text))

    def test_cached_archive_backfill_is_complete_idempotent_and_generation_safe(self):
        with tempfile.TemporaryDirectory() as raw:
            database=Path(raw)/'catalog.sqlite3';archive=Path(raw)/'dump.zip'
            with contextlib.closing(sqlite3.connect(database)) as db,db:
                db.execute('CREATE TABLE anime_work(id INTEGER,bgm_id INTEGER,is_nsfw INTEGER DEFAULT 0)')
                db.executemany('INSERT INTO anime_work(id,bgm_id) VALUES(?,?)',[(1,11),(2,22)])
                db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
                db.execute("INSERT INTO metadata VALUES('archive_name','dump.zip')")
            with zipfile.ZipFile(archive,'w') as target:
                target.writestr('subject.jsonlines','\n'.join(json.dumps(item) for item in [{'id':11,'nsfw':True},{'id':22,'nsfw':False}]))
            self.assertTrue(content_policy.backfill_archive(database,archive))
            self.assertFalse(content_policy.backfill_archive(database,archive))
            with contextlib.closing(sqlite3.connect(database)) as db:
                self.assertTrue(content_policy.blocked(db,1))
                self.assertFalse(content_policy.blocked(db,2))
            with self.assertRaisesRegex(ValueError,'content_excluded'):
                content_policy.require_plan_allowed(database,{'assessments':[{'animeIds':[1]}]})
            with contextlib.closing(sqlite3.connect(database)) as db,db:
                db.execute("DELETE FROM metadata WHERE key='resource_content_archive'")
            with zipfile.ZipFile(archive,'w') as target:
                target.writestr('subject.jsonlines',json.dumps({'id':11,'nsfw':False}))
            with self.assertRaisesRegex(ValueError,'archive_subjects_incomplete'):
                content_policy.backfill_archive(database,archive)
            with contextlib.closing(sqlite3.connect(database)) as db:
                self.assertTrue(content_policy.blocked(db,1))
