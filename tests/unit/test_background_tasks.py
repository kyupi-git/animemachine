import unittest
from animemachine.api.tasks import snapshot


class BackgroundTaskTests(unittest.TestCase):
    def test_aggregate_keeps_failures_actionable_and_does_not_echo_provider_urls(self):
        value=snapshot({'library':{'state':'failed','error':'ConnectError: https://key@example.test'},
                        'images':{'state':'idle'},'torrentSearch':{'42':{'state':'running','done':2,'total':3}}},
                       {'state':'checking'},{'state':'warm','overallDone':12,'overallTotal':12},{},{})
        self.assertEqual(2,value['running'])
        self.assertEqual('library',value['items'][0]['id'])
        self.assertTrue(value['items'][0]['retryable'])
        self.assertEqual('ConnectError',value['items'][0]['error'])
        self.assertNotIn('example.test',str(value))
        self.assertEqual('complete',next(row for row in value['items'] if row['id']=='preload')['state'])


if __name__=='__main__':
    unittest.main()
