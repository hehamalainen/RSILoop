import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

class TestJSON(unittest.TestCase):
    def test_basic(self):
        try:
            from solution import parse_json
        except ImportError as e:
            self.fail(f"Could not import parse_json from solution.py: {e}")
        
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json('{"a": true, "b": false, "c": null}'), {"a": True, "b": False, "c": None})
        self.assertEqual(parse_json('[1, 2, "three"]'), [1, 2, "three"])
        self.assertEqual(parse_json('{"x": {"y": [1, 2]}}'), {"x": {"y": [1, 2]}})

if __name__ == '__main__':
    unittest.main()
