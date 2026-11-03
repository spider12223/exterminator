import unittest

from shopkit.profile import display_name


class ProfileTests(unittest.TestCase):
    def test_display_name_is_title_cased(self):
        self.assertEqual(display_name("u1"), "Ada Lovelace")


if __name__ == "__main__":
    unittest.main()
