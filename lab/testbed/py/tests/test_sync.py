import unittest

from shopkit.errors import SyncError
from shopkit.sync import push_orders


class FlakyClient:
    def __init__(self):
        self.uploaded = []

    def upload(self, order):
        if order["id"] == "bad":
            raise ConnectionError("warehouse unreachable")
        self.uploaded.append(order["id"])


class SyncTests(unittest.TestCase):
    def test_all_orders_uploaded(self):
        client = FlakyClient()
        self.assertEqual(push_orders([{"id": "a"}, {"id": "b"}], client), 2)

    def test_upload_failure_is_reported(self):
        with self.assertRaises(SyncError):
            push_orders([{"id": "a"}, {"id": "bad"}], FlakyClient())


if __name__ == "__main__":
    unittest.main()
