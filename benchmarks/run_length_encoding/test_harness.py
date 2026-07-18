import sys
import os
import unittest

# Ensure the directory of the solution is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

class TestRLE(unittest.TestCase):
    def test_basic(self):
        try:
            from solution import rle_encode
        except ImportError as e:
            self.fail(f"Could not import rle_encode from solution.py: {e}")

        self.assertEqual(rle_encode("AABBBCCCC"), "2A3B4C")
        self.assertEqual(rle_encode("A"), "1A")
        self.assertEqual(rle_encode(""), "")
        self.assertEqual(rle_encode("ABC"), "1A1B1C")
        self.assertEqual(rle_encode("WWWWWWWWWWWWBWWWWWWWWWWWWBBBWWWWWWWWWWWWWWWWWWWWWWWWB"), "12W1B12W3B24W1B")

if __name__ == '__main__':
    unittest.main()
