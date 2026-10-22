import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

class TestSimpleRegexMatcher(unittest.TestCase):
    def test_basic(self):
        try:
            from solution import SimpleRegexMatcher
        except ImportError as e:
            self.fail(f"Could not import solution elements: {e}")
            
        matcher = SimpleRegexMatcher("cat")
        self.assertTrue(matcher.match("cat"))
        self.assertFalse(matcher.match("cats"))
        self.assertFalse(matcher.match("dog"))

    def test_wildcards(self):
        try:
            from solution import SimpleRegexMatcher
        except ImportError as e:
            self.fail(f"Could not import solution elements: {e}")
            
        matcher1 = SimpleRegexMatcher("c?t")
        self.assertTrue(matcher1.match("cat"))
        self.assertTrue(matcher1.match("cot"))
        self.assertFalse(matcher1.match("ct"))
        self.assertFalse(matcher1.match("cart"))

        matcher2 = SimpleRegexMatcher("c*t")
        self.assertTrue(matcher2.match("cat"))
        self.assertTrue(matcher2.match("ct"))
        self.assertTrue(matcher2.match("cart"))
        self.assertFalse(matcher2.match("car"))

if __name__ == "__main__":
    unittest.main()
