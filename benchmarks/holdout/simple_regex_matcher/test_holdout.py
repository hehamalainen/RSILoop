import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

class TestSimpleRegexMatcherHoldout(unittest.TestCase):
    def test_complex_patterns(self):
        try:
            from solution import SimpleRegexMatcher
        except ImportError as e:
            self.fail(f"Could not import solution: {e}")
            
        # Multiple wildcards
        matcher = SimpleRegexMatcher("*a*b?d")
        self.assertTrue(matcher.match("xxayybzd"))
        self.assertTrue(matcher.match("abxd"))
        self.assertFalse(matcher.match("ab"))
        self.assertFalse(matcher.match("xaxbd"))

    def test_edge_cases(self):
        try:
            from solution import SimpleRegexMatcher
        except ImportError as e:
            self.fail(f"Could not import solution: {e}")
            
        # Empty patterns/inputs
        matcher_empty = SimpleRegexMatcher("")
        self.assertTrue(matcher_empty.match(""))
        self.assertFalse(matcher_empty.match("a"))
        
        matcher_star = SimpleRegexMatcher("*")
        self.assertTrue(matcher_star.match(""))
        self.assertTrue(matcher_star.match("anything"))

if __name__ == "__main__":
    unittest.main()
