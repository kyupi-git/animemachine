import unittest

from animemachine.catalog.service import subject_image_urls
from animemachine.network.validators import bangumi_subject


class ImageSourcesTests(unittest.TestCase):
    def test_upgraded_defaults_follow_the_current_cache_and_custom_sources_remain(self):
        urls = subject_image_urls(646154, {'bangumiSubjectCacheEndpoints':[
            'https://bgm-cache.wushuo.top', 'https://custom.example/{bucket}/{id}.json',
            'https://legacy-custom.example'], 'bangumiApiEndpoints':['https://official.example']})
        self.assertIn('https://cache.wushuo.top/bgm/subjects/646154', urls)
        self.assertFalse(any('bgm-cache.wushuo.top' in url for url in urls))
        self.assertIn('https://custom.example/6/646154.json', urls)
        self.assertIn('https://legacy-custom.example/6/646154.json', urls)
        self.assertIn('https://official.example/v0/subjects/646154', urls)
        self.assertEqual(len(urls),len(set(urls)))

    def test_cover_metadata_rejects_wrong_titles_and_error_documents(self):
        subject = {'id': '646154', 'name': 'Animation', 'images': {'large': 'https://lain.bgm.tv/cover.jpg'}}
        self.assertEqual(subject, bangumi_subject({'data': subject}, 646154))
        self.assertEqual({'id': 646154, 'name': 'No cover'}, bangumi_subject({'id': 646154, 'name': 'No cover'}, 646154))
        for bad in ({'message': 'upstream failed'}, {**subject, 'id': 123}, {**subject, 'images': []}):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                bangumi_subject(bad, 646154)


if __name__ == '__main__':
    unittest.main()
