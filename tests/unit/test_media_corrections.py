from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from animemachine.library import corrections, external
from animemachine.integrations import playback


class MediaCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.database=self.root/'catalog.sqlite3'
        shutil.copyfile(Path(__file__).parents[1]/'fixtures/anime-catalog.sqlite3',self.database)
        self.media=self.root/'media';folder=self.media/'Unrecognized Collection';folder.mkdir(parents=True)
        self.file=folder/'video 01.mkv';self.file.write_bytes(b'test media')
        self.source={'id':'test-source','kind':'generic','path':str(self.media),'enabled':True,'readOnly':True}
        external.scan(self.database,[self.source],force=True)
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            self.anime_id,self.bgm_id=db.execute('SELECT id,bgm_id FROM anime_work ORDER BY id LIMIT 1').fetchone()

    def item(self):
        return corrections.folders(self.database,all_files=True)['items'][0]

    def request(self,episode=3):
        row=self.item()
        return {'bgmId':self.bgm_id,'files':[{'sourceId':row['source_id'],'path':row['absolute_path'],
            'size':row['size'],'modified':row['mtime_ns'],'revision':row['revision'] or 0,'episode':episode}]}

    def test_association_and_episode_survive_rescan_and_can_be_undone(self):
        original=self.item()
        corrections.update(self.database,self.request())
        self.assertEqual(self.anime_id,self.item()['anime_id']);self.assertEqual(3,self.item()['episode_number'])
        external.scan(self.database,[self.source],force=True)
        self.assertEqual(self.anime_id,self.item()['anime_id']);self.assertEqual(3,self.item()['episode_number'])
        request=self.request();request['undo']=True
        corrections.update(self.database,request)
        self.assertEqual(original['anime_id'],self.item()['anime_id'])
        self.assertEqual(original['episode_number'],self.item()['episode_number'])
        self.assertIsNone(self.item()['revision'])

    def test_stale_or_forged_rows_roll_back_entire_selection(self):
        request=self.request();stale=json.loads(json.dumps(request['files'][0]));stale['path']=str(self.root/'other.mkv')
        request['files'].append(stale)
        with self.assertRaisesRegex(ValueError,'media_inventory_changed'):
            corrections.update(self.database,request)
        self.assertIsNone(self.item()['revision'])
        request=self.request();corrections.update(self.database,request)
        with self.assertRaisesRegex(ValueError,'media_correction_conflict'):
            corrections.update(self.database,request)

    def test_replaced_file_does_not_inherit_previous_file_correction(self):
        corrections.update(self.database,self.request())
        self.file.write_bytes(b'completely different media')
        external.scan(self.database,[self.source],force=True)
        self.assertNotEqual(3,self.item()['episode_number'])
        corrections.update(self.database,self.request(7))
        self.assertEqual(7,self.item()['episode_number'])

    def test_manual_episode_and_work_override_duplicate_local_inventory(self):
        config={'deployment':{'libraryUncRoot':str(self.media)}}
        with contextlib.closing(sqlite3.connect(self.database)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS runtime_local_file(absolute_path TEXT,size INTEGER,file_kind TEXT,owner_path TEXT)')
            db.execute("""INSERT INTO runtime_work(private_work_id,anime_id,target_unc,directory_name,official_title,date_code,
                library_state,scope_state,relation_state,origin,mapping_method,evidence_json,updated_at)
                VALUES(-1,?,?, 'Test','Test','2026_10','existing','in_scope','main','test','test','{}','2026-10-27')""",
                (self.anime_id,str(self.file.parent)))
            db.execute("INSERT INTO runtime_local_file(absolute_path,size,file_kind,owner_path) VALUES(?,?,'main_video',?)",(str(self.file),self.file.stat().st_size,str(self.file.parent)))
            other_id,other_bgm=db.execute('SELECT id,bgm_id FROM anime_work WHERE id!=? LIMIT 1',(self.anime_id,)).fetchone()
        corrections.update(self.database,self.request(7))
        items=playback.collect_items(self.database,self.anime_id,config,cached_only=True)
        self.assertEqual([7],[item.episode for item in items])
        corrections.update(self.database,self.request(None))
        self.assertEqual([None],[item.episode for item in playback.collect_items(self.database,self.anime_id,config,cached_only=True)])
        request=self.request(5);request['bgmId']=other_bgm
        corrections.update(self.database,request)
        self.assertEqual([],playback.collect_items(self.database,self.anime_id,config,cached_only=True))
        self.assertEqual([5],[item.episode for item in playback.collect_items(self.database,other_id,config,cached_only=True)])

    def test_nan_and_non_numeric_episodes_are_rejected(self):
        for value in (float('nan'),float('inf'),-1,True,'3'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    corrections.update(self.database,self.request(value))


if __name__=='__main__':
    unittest.main()
