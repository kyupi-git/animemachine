"""Release intent safety across future dates, restarts and user changes."""
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from animemachine.catalog import service
from animemachine.config.loader import torrent_policy_eligible
from animemachine.integrations import ani_rss, follows


class ReleaseFollowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / 'catalog.sqlite3'
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE anime_work(id INTEGER PRIMARY KEY,bgm_id INTEGER,start_month TEXT)')
            db.execute("INSERT INTO anime_work VALUES(7,123,'2028-01')")
        self.store = follows.Store(self.root / 'follows.sqlite3')
        self.config = json.loads(service.EXAMPLE_CONFIG.read_text(encoding='utf-8'))
        self.row = {'provider':'ani-rss','resourceId':'ar-test','kind':'follow','eligible':True,
                    'sourceFamily':'serial','resourceGroup':'ANi','variantTitle':'[ANi] Work [Baha][CHT]',
                    'releaseVariant':{'platform':'Baha'}}

    def check(self, **kwargs):
        return follows.check_one(self.store, self.database, self.config, enabled_users={1},
                                 can_submit=lambda: True, now=100, **kwargs)

    def test_future_subscription_uses_exact_child_and_survives_store_restart(self):
        self.store.save(1, 123, 'serial', {'ani':['Baha']}, revision=0)
        self.store = follows.Store(self.store.path)
        wrong = {**self.row,'resourceId':'wrong','releaseVariant':{'platform':'CR'}}
        with mock.patch.object(ani_rss, 'state', return_value={}), \
             mock.patch.object(ani_rss, 'state_available', return_value=True), \
             mock.patch.object(ani_rss, 'search') as search, \
             mock.patch.object(ani_rss, 'resources', return_value=[wrong,self.row]), \
             mock.patch.object(ani_rss, 'subscribe') as subscribe:
            search.return_value = {}
            self.assertTrue(self.check())
            search.assert_called_once_with(self.database, 7, self.config)
            subscribe.assert_called_once_with(self.database, 'ar-test', self.config, expected_bgm_id=123, create_only=True)
            self.assertFalse(self.check())
        self.assertEqual('subscribed', self.store.snapshot(1)[0]['state'])

    def test_archive_tracking_only_notifies_without_downloading(self):
        self.store.save(1, 123, 'archive', {}, revision=0)
        release = {**self.row, 'kind':'collection', 'sourceFamily':'archive'}
        with mock.patch.object(ani_rss, 'state', return_value={}), \
             mock.patch.object(ani_rss, 'state_available', return_value=True), \
             mock.patch.object(ani_rss, 'search', return_value={}), \
             mock.patch.object(ani_rss, 'resources', return_value=[release]), \
             mock.patch.object(ani_rss, 'subscribe') as subscribe:
            self.check()
            subscribe.assert_not_called()
        self.assertEqual('available', self.store.snapshot(1)[0]['state'])

    def test_cancel_while_searching_never_subscribes_or_resurrects_intent(self):
        self.store.save(1, 123, 'serial', {}, revision=0)
        def search(*_args):
            self.store.cancel(1, 123, 'serial', 1)
            return {}
        with mock.patch.object(ani_rss, 'state', return_value={}), \
             mock.patch.object(ani_rss, 'state_available', return_value=True), \
             mock.patch.object(ani_rss, 'search', side_effect=search), \
             mock.patch.object(ani_rss, 'resources', return_value=[self.row]), \
             mock.patch.object(ani_rss, 'subscribe') as subscribe:
            self.check()
            subscribe.assert_not_called()
        self.assertEqual('cancelled', self.store.snapshot(1)[0]['state'])

    def test_claim_is_exclusive_and_recovers_after_restart_timeout(self):
        self.store.save(1, 123, 'serial', {}, revision=0)
        job = self.store.claim(now=100, enabled_users={1})
        self.assertIsNotNone(job)
        self.assertIsNone(follows.Store(self.store.path).claim(now=200, enabled_users={1}))
        next_job = follows.Store(self.store.path).claim(now=2000, enabled_users={1})
        self.assertIsNotNone(next_job)
        self.assertFalse(self.store.current(job))
        self.assertTrue(self.store.current(next_job))

    def test_disabled_account_and_stale_form_cannot_change_intents(self):
        self.store.save(1, 123, 'serial', {}, revision=0)
        self.assertIsNone(self.store.claim(now=100, enabled_users={2}))
        with self.assertRaises(follows.Conflict):
            self.store.save(1, 123, 'serial', {}, revision=0)
        with self.assertRaises(follows.Conflict):
            self.store.cancel(1, 123, 'serial', 0)

    def test_offline_provider_preserves_intent_and_backs_off(self):
        self.store.save(1, 123, 'serial', {}, revision=0)
        with mock.patch.object(ani_rss, 'state', return_value={}), \
             mock.patch.object(ani_rss, 'state_available', return_value=False), \
             mock.patch.object(ani_rss, 'subscribe') as subscribe:
            self.check()
            subscribe.assert_not_called()
        row = self.store.snapshot(1)[0]
        self.assertEqual('pending', row['state'])
        self.assertEqual('ani_rss_unavailable', row['error'])
        self.assertGreater(row['nextCheckAt'], 100)

    def test_child_restrictions_apply_to_native_and_ani_rss_policy(self):
        policy = self.config['torrentPolicy']
        policy['groupVariants'] = {'serial': {'ani':['Baha']}, 'archive': {'vcb-studio':['Baha']}}
        for family, group, source in [('serial','ANi','webrip'),('archive','VCB-Studio','bdrip')]:
            for platform, expected in [('Baha',True),('CR',False)]:
                title = f'[{group}] Work [{platform}][CHT][1080p]'
                with self.subTest(family=family, platform=platform):
                    self.assertEqual(expected, torrent_policy_eligible(policy,source,group,'1080p','CHT',title))
                    self.assertEqual(expected, ani_rss._policy_eligibility_and_rank(title,group,source,1080,self.config)[0])

    def test_language_specific_child_preferences_do_not_overwrite_each_other(self):
        policy = self.config['torrentPolicy']
        policy['groupVariants'] = {'serial': {'zh':{'kirara-fantasia':['Baha']},
                                              'en':{'kirara-fantasia':['CR']}}}
        for language, platform, expected in [('zh','Baha',True),('zh','CR',False),
                                              ('en','CR',True),('en','Baha',False)]:
            with self.subTest(language=language, platform=platform):
                title = f'[Kirara] Work [{platform}][CHT][ENG][1080p]'
                self.assertEqual(expected,torrent_policy_eligible(policy,'webrip','Kirara Fantasia',
                    '1080p','CHT',title,ui_language=language))

    def test_generic_platform_rule_does_not_bypass_selected_publisher_children(self):
        policy = self.config['torrentPolicy']
        policy['groupVariants'] = {'serial': {'ja': {'kirara-fantasia': ['Baha']}}}
        self.assertFalse(torrent_policy_eligible(policy, 'webrip', 'Kirara', '1080p', 'unknown',
                                                '[Kirara] Work [ABEMA][1080p]', ui_language='ja'))
        self.assertTrue(torrent_policy_eligible(policy, 'webrip', 'Unlisted', '1080p', 'unknown',
                                               '[Unlisted] Work [ABEMA][1080p]', ui_language='ja'))

    def test_replaced_catalog_identity_never_subscribes_the_old_numeric_id(self):
        self.store.save(1, 123, 'serial', {}, revision=0)
        def replace(*_args):
            with contextlib.closing(sqlite3.connect(self.database)) as db, db:
                db.execute('UPDATE anime_work SET bgm_id=456 WHERE id=7')
            return {}
        with mock.patch.object(ani_rss, 'state', return_value={}), \
             mock.patch.object(ani_rss, 'state_available', return_value=True), \
             mock.patch.object(ani_rss, 'search', side_effect=replace), \
             mock.patch.object(ani_rss, 'resources', return_value=[self.row]), \
             mock.patch.object(ani_rss, 'subscribe') as subscribe:
            self.check()
            subscribe.assert_not_called()
        self.assertEqual('resource_check_failed', self.store.snapshot(1)[0]['error'])

    def test_owner_disabled_during_search_cannot_start_a_download(self):
        self.store.save(1, 123, 'serial', {}, revision=0)
        with mock.patch.object(ani_rss, 'state', return_value={}), \
             mock.patch.object(ani_rss, 'state_available', return_value=True), \
             mock.patch.object(ani_rss, 'search', return_value={}), \
             mock.patch.object(ani_rss, 'resources', return_value=[self.row]), \
             mock.patch.object(ani_rss, 'subscribe') as subscribe:
            self.check(owner_enabled=lambda _owner: False)
            subscribe.assert_not_called()
        self.assertEqual('available', self.store.snapshot(1)[0]['state'])

    def test_archive_pool_tracking_survives_optional_provider_failure(self):
        self.store.save(1, 123, 'archive', {}, revision=0)
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE runtime_torrent(id INTEGER)')
        release = {**self.row, 'kind': 'collection', 'sourceFamily': 'archive'}
        with mock.patch.object(ani_rss, 'state', side_effect=RuntimeError('optional provider unavailable')), \
             mock.patch.object(follows.runtime, 'torrents_for_anime', return_value=[release]), \
             mock.patch.object(ani_rss, 'subscribe') as subscribe:
            self.check()
            subscribe.assert_not_called()
        self.assertEqual('available', self.store.snapshot(1)[0]['state'])
        self.assertIsNone(self.store.snapshot(1)[0]['error'])


if __name__ == '__main__':
    unittest.main()
