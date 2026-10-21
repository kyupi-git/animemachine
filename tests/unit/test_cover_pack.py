import contextlib
import hashlib
import io
import json
import runpy
import sqlite3
import tempfile
import os
import sys
import unittest
import zipfile
import datetime as dt
from pathlib import Path
from unittest import mock

from PIL import Image
from animemachine.catalog import cover_pack
from animemachine.catalog import service


class CoverPackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root/'catalog.sqlite3'
        buffer = io.BytesIO()
        Image.new('RGB',(16,24),'green').save(buffer,'PNG')
        self.image = buffer.getvalue()
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER);'
                'CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,mime_type TEXT,image_blob BLOB,'
                'source_url TEXT,etag TEXT,fetched_at TEXT,error TEXT);'
                'INSERT INTO anime_work VALUES(101,646154);INSERT INTO anime_work VALUES(102,2);')
        self.pack = self.root/'season-covers.zip'

    def tearDown(self):
        self.temporary.cleanup()

    def write_pack(self, *, identity=646154, corrupt=False, filename=None):
        name = filename or f'covers/{identity}.png'
        payload = {'schemaVersion':1,'covers':[{'bgmId':identity,'file':name,
            'sha256':hashlib.sha256(self.image).hexdigest(),'mime':'image/png',
            'sourceUrl':'https://lain.bgm.tv/r/800/pic/cover/l/test.jpg',
            'fetchedAt':'2026-10-20T12:00:00+00:00'}]}
        with zipfile.ZipFile(self.pack,'w') as archive:
            archive.writestr('manifest.json',json.dumps(payload))
            archive.writestr(name,b'corrupt' if corrupt else self.image)
        digest = hashlib.sha256(self.pack.read_bytes()).hexdigest()
        self.pack.with_name(self.pack.name+'.sha256').write_text(digest+'  '+self.pack.name,encoding='ascii')

    def test_matching_uses_bangumi_identity_and_is_idempotent(self):
        self.write_pack()
        self.assertEqual(1,cover_pack.seed(self.db,self.pack))
        self.assertEqual(0,cover_pack.seed(self.db,self.pack))
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual([(101,self.image)],db.execute('SELECT anime_id,image_blob FROM anime_image').fetchall())

    def test_existing_and_concurrently_fetched_covers_are_preserved(self):
        self.write_pack()
        existing=b'existing native cover'
        def concurrent_image(data,mime):
            with contextlib.closing(sqlite3.connect(self.db)) as db, db:
                db.execute('INSERT INTO anime_image(anime_id,image_blob) VALUES(101,?)',(existing,))
            return data,mime
        with mock.patch.object(cover_pack,'image_bytes',side_effect=concurrent_image):
            self.assertEqual(0,cover_pack.seed(self.db,self.pack))
        self.assertEqual(0,cover_pack.seed(self.db,self.pack))
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(existing,db.execute('SELECT image_blob FROM anime_image').fetchone()[0])

    def test_pack_or_image_corruption_writes_nothing(self):
        for corrupt_container in (False,True):
            with self.subTest(container=corrupt_container):
                self.write_pack(corrupt=True)
                if corrupt_container:
                    with self.pack.open('ab') as output:
                        output.write(b'changed')
                with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                    cover_pack.seed(self.db,self.pack)
                with contextlib.closing(sqlite3.connect(self.db)) as db:
                    self.assertEqual(0,db.execute('SELECT COUNT(*) FROM anime_image').fetchone()[0])

    def test_absent_unknown_and_unsafe_entries_do_not_seed_other_works(self):
        self.assertEqual(0,cover_pack.seed(self.db,self.pack))
        self.write_pack(identity=999)
        self.assertEqual(0,cover_pack.seed(self.db,self.pack))
        self.write_pack(filename='../cover.png')
        with self.assertRaises(ValueError):
            cover_pack.seed(self.db,self.pack)
        self.assertFalse((self.root.parent/'cover.png').exists())

    def test_release_window_matches_existing_radar_and_year_rollover(self):
        build = runpy.run_path(str(Path(__file__).parents[2]/'scripts/build_cover_pack.py'))
        window = build['season_range']
        self.assertEqual(('2026-09','2026-11'),window(dt.date(2026,10,21)))
        self.assertEqual(('2026-12','2027-02'),window(dt.date(2026,12,28)))
        self.assertEqual(('2026-09','2026-11'),window(dt.date(2026,12,1)))

    def test_builder_packages_cached_original_bytes_and_repairs_incomplete_output(self):
        build = runpy.run_path(str(Path(__file__).parents[2]/'scripts/build_cover_pack.py'))
        lower, _upper = build['season_range'](dt.datetime.now().astimezone().date())
        archive = self.root/'archive.zip'
        with zipfile.ZipFile(archive,'w') as source:
            source.writestr('subject.jsonlines',json.dumps({'id':646154,'type':2,'date':lower+'-01'})+'\n')
        cache = self.root/'builder';cache.mkdir()
        with contextlib.closing(sqlite3.connect(cache/'covers.sqlite3')) as db, db:
            db.executescript('CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER);'
                'CREATE TABLE anime_image(anime_id INTEGER PRIMARY KEY,mime_type TEXT,image_blob BLOB,'
                'source_url TEXT,etag TEXT,fetched_at TEXT,error TEXT);')
            db.execute('INSERT INTO anime_image(anime_id,mime_type,image_blob,source_url,fetched_at) VALUES(?,?,?,?,?)',
                (646154,'image/png',self.image,'https://lain.bgm.tv/r/800/pic/cover/l/test.png','2026-10-20T12:00:00+00:00'))
        work = service.BuildItem({'_inferred_countries':[('JP','test')]},{'bgm_id':646154},[],[],[],[],[])
        self.pack.write_bytes(b'incomplete')
        with mock.patch.object(sys,'argv',['build_cover_pack.py','--archive',str(archive),'--cache',str(cache),'--output',str(self.pack)]), \
                mock.patch.dict(os.environ,{},clear=False), \
                mock.patch.object(service,'ensure_archive',return_value=(archive,{'name':'archive.zip'})), \
                mock.patch.object(service,'build_items_from_archive',return_value=[work]), \
                mock.patch.object(service,'get_anime_image',return_value=(self.image,'image/png')), \
                mock.patch.object(service,'IMAGE_LIMITER'), \
                mock.patch.object(service.transport,'configure_host_limit'):
            self.assertEqual(0,build['main']())
        payload = cover_pack.manifest(self.pack)
        self.assertEqual('image/png',payload['covers'][0]['mime'])
        with zipfile.ZipFile(self.pack) as pack:
            self.assertEqual(self.image,pack.read('covers/646154.png'))
        self.assertEqual(1,cover_pack.seed(self.db,self.pack))


if __name__ == '__main__':
    unittest.main()
