from __future__ import annotations

import datetime as dt
import re
import sqlite3
import unittest
from unittest import mock

from animemachine.torrents import season_guard
from animemachine.torrents.variants import release_variant


class ReleaseStreamTests(unittest.TestCase):
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
