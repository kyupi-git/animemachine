from __future__ import annotations

import datetime as dt
import re
import sqlite3
import unittest
from unittest import mock

from animemachine.torrents import season_guard
from animemachine.torrents.variants import release_variant


class ReleaseStreamTests(unittest.TestCase):
    def test_empty_resource_list_does_not_read_catalog_or_provider(self):
        with mock.patch.object(season_guard, 'live_frontier', side_effect=AssertionError('provider called')):
            self.assertEqual([], season_guard.filter_streams(None, 1, [], lambda _: (), {}))

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript('''CREATE TABLE anime_work(id INTEGER PRIMARY KEY,title_ja TEXT,title_en TEXT,
            media_code TEXT,start_month TEXT,episode_count INTEGER,episode_start_number INTEGER);
            CREATE TABLE anime_title(anime_id INTEGER,title TEXT);
            INSERT INTO anime_work VALUES(1,'夜曲 第3期','Nocturne Season 3','tv','2026-10',12,25);''')

    def stream(self, group, episodes, name='Nocturne S03', variant='Baha'):
        return {'resourceGroup':group,'episodeSequence':episodes,'name':name,'variant':variant,'sourceFamily':'serial'}

    def filter(self, rows, frontier=5):
        with mock.patch.object(season_guard,'live_frontier',return_value=frontier):
            return season_guard.filter_streams(self.db,1,rows,lambda item:(item['resourceGroup'],item['variant']),{},
                                               today=dt.date(2026,10,20))

    def test_only_contiguous_current_or_one_behind_streams_are_kept(self):
        valid = [self.stream('current',[1,2,3]),self.stream('current',[4,5]),self.stream('one-behind',[1,2,3,4])]
        excluded = [self.stream('missing-start',[2,3,4,5]),self.stream('gap',[1,2,4,5]),
                    self.stream('old',[1,2,3]),self.stream('ahead',[1,2,3,4,5,6]),
                    self.stream('wrong-season',[1,2,3,4,5],name='Nocturne S01-S02'),
                    self.stream('unknown',[])]
        self.assertEqual(valid,self.filter(valid+excluded))

    def test_variants_never_fill_each_others_gaps(self):
        baha=self.stream('same',[1,2,3,4],variant='Baha')
        cr=self.stream('same',[5],variant='CR')
        self.assertEqual([baha],self.filter([baha,cr]))

    def test_excluded_or_invalid_episodes_do_not_fill_a_selectable_stream(self):
        first = {**self.stream('same',[1]), 'eligible':False, 'canOverride':False}
        rest = {**self.stream('same',[2,3,4,5]), 'eligible':True}
        self.assertEqual([], self.filter([first,rest]))
        first['canOverride'] = True
        self.assertEqual([], self.filter([first,rest]))

    def test_verified_cumulative_episode_numbers_are_normalized(self):
        stream=self.stream('cumulative',[25,26,27,28,29])
        self.assertEqual([stream],self.filter([stream]))
        self.assertEqual([1,2,3,4,5],stream['episodeSequence'])

    def test_old_and_completed_works_keep_backfill_but_reject_wrong_season(self):
        self.db.execute("UPDATE anime_work SET start_month='2020-10'")
        full=self.stream('archive',list(range(1,13)))
        wrong=self.stream('old-season',list(range(1,13)),name='Nocturne S01')
        self.assertEqual([full],self.filter([full,wrong]))
        self.db.execute("UPDATE anime_work SET start_month='2026-10'")
        self.assertEqual([full],self.filter([full],frontier=12))

    def test_native_discovery_still_works_without_ani_rss(self):
        native=self.stream('native',[1,2,3])
        self.assertEqual([native],self.filter([native],frontier=None))

    def test_long_series_require_24_recent_contiguous_episodes_with_one_episode_lag(self):
        self.db.execute("UPDATE anime_work SET episode_count=1500,episode_start_number=1,start_month='1999-10'")
        current = self.stream('current',list(range(1077,1101)))
        previous = self.stream('one-behind',list(range(1076,1100)))
        incomplete = self.stream('missing',list(range(1078,1101)))
        old = self.stream('old',list(range(1075,1099)))
        gap = self.stream('gap',[value for value in range(1077,1101) if value != 1080])
        wrong = self.stream('wrong',list(range(1077,1101)),name='Nocturne S02')
        self.assertEqual([current,previous],self.filter([current,previous,incomplete,old,gap,wrong],frontier=1100))

    def test_long_series_native_search_and_archive_backfill_remain_available(self):
        self.db.execute("UPDATE anime_work SET episode_count=1500,episode_start_number=1,start_month='1999-10'")
        current = self.stream('current',list(range(1077,1101)))
        older = self.stream('older',list(range(1050,1074)))
        archive = {**self.stream('BD',[1,2,3]),'sourceFamily':'archive'}
        self.assertEqual([current,archive],self.filter([current,older,archive],frontier=None))
        self.assertEqual([current,older,archive],self.filter([current,older,archive],frontier=1500))

    def test_long_running_work_without_total_uses_recent_release_evidence(self):
        self.db.executescript("""CREATE TABLE ani_rss_release_state(anime_id INTEGER,current_episode INTEGER,last_episode_update_at TEXT);
            INSERT INTO ani_rss_release_state VALUES(1,1100,'2026-10-18T12:00:00+00:00');
            UPDATE anime_work SET episode_count=0,episode_start_number=NULL,start_month='1999-10';""")
        valid = self.stream('current',list(range(1077,1101)))
        gap = self.stream('gap',list(range(1078,1101)))
        self.assertEqual([valid],self.filter([valid,gap],frontier=1100))
        self.db.execute("UPDATE ani_rss_release_state SET last_episode_update_at='2020-10-18T12:00:00+00:00'")
        self.assertEqual([valid,gap],self.filter([valid,gap],frontier=1100))

    def test_variant_filters_match_their_own_releases_and_exclude_siblings(self):
        titles=['[ANi] Example - 01 [Baha][1080p][CHS][HEVC-10bit]',
                '[ANi] Example - 02 [CR][1080p][HEVC-10bit]',
                '[ANi] Example - 01 [Baha][720p][CHS][AVC-8bit]',
                '[ANi] Example - 01 [Baha][1080p][CHT][HEVC-10bit]',
                '[ANi] Example - 01 [B-Global][1080p][CHS][AVC-8bit][MP4][内嵌]',
                '[ANi] Example - 01 [B-Global][1080p][CHS][AVC-8bit][MKV][内封]']
        for title in titles:
            variant=release_variant(title)
            patterns=variant.patterns()
            self.assertTrue(all(re.search(pattern,title) for pattern in patterns),(title,patterns))
            for other in titles:
                self.assertEqual(title==other,all(re.search(pattern,other) for pattern in patterns),(title,other))
        self.assertEqual(release_variant(titles[0]).key,release_variant(titles[0].replace('01','02')).key)
        self.assertNotEqual(release_variant(titles[0]).key,release_variant(titles[1]).key)

    def test_season_ordinals_cover_japanese_chinese_and_release_tokens(self):
        for title in ('第3期','第三季','Season 3','S03E05','3rd Season'):
            self.assertEqual({3},season_guard.season_numbers(title))


if __name__ == '__main__':
    unittest.main()
