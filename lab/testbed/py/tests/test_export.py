import unittest

from shopkit.export import export_csv


class ExportTests(unittest.TestCase):
    def test_rows(self):
        out = export_csv([{"id": "o1", "customer": "ada", "total": 12.5}])
        self.assertEqual(out.splitlines(), ["id,customer,total", "o1,ada,12.50"])

    def test_empty(self):
        self.assertEqual(export_csv([]).splitlines(), ["id,customer,total"])


if __name__ == "__main__":
    unittest.main()
