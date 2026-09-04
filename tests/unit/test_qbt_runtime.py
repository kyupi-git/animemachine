import unittest

from animemachine.integrations import qbt_runtime


class QbtRuntimeTests(unittest.TestCase):
    def test_lifecycle(self):
        self.assertEqual(qbt_runtime.lifecycle({"state":"stoppedDL","progress":0}), "queued")
        self.assertEqual(qbt_runtime.lifecycle({"state":"downloading","progress":0,"downloaded":0}), "downloading")
        self.assertEqual(qbt_runtime.lifecycle({"state":"pausedDL","progress":0,"downloaded":0}), "queued")
        self.assertEqual(qbt_runtime.lifecycle({"state":"pausedDL","progress":0.4,"downloaded":400}), "downloading")
        self.assertEqual(qbt_runtime.lifecycle({"state":"stoppedUP","progress":1}), "existing")


if __name__ == "__main__":
    unittest.main()

