import unittest

from af.nums import intish


class Intish(unittest.TestCase):
    def test_plain_digits(self):
        self.assertEqual(intish("5", 0), 5)
        self.assertEqual(intish("0", 7), 0)          # 0 is a value, not junk

    def test_junk_falls_back(self):
        self.assertEqual(intish("", 3), 3)
        self.assertEqual(intish("abc", 3), 3)
        self.assertEqual(intish(None, 3), 3)
        self.assertEqual(intish("-5", 3), 3)          # no sign
        self.assertEqual(intish("5x", 3), 3)

    def test_unicode_digit_does_not_crash(self):
        # The whole reason this helper exists: '²'.isdigit() is True but int('²') raises.
        self.assertEqual(intish("²", 99), 99)
        self.assertEqual(intish("५", 99), 99)         # Devanagari 5

    def test_positive(self):
        self.assertEqual(intish("0", 7, positive=True), 7)
        self.assertEqual(intish("3", 7, positive=True), 3)

    def test_whitespace_stripped_but_not_internal(self):
        self.assertEqual(intish("  4 ", 0), 4)
        self.assertEqual(intish("4 0", 0), 0)


if __name__ == "__main__":
    unittest.main()
