import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

class TestCircularBuffer(unittest.TestCase):
    def test_basic(self):
        try:
            from solution import CircularBuffer, BufferFullException, BufferEmptyException
        except ImportError as e:
            self.fail(f"Could not import solution elements: {e}")

        buf = CircularBuffer(3)
        self.assertRaises(BufferEmptyException, buf.read)
        buf.write(1)
        buf.write(2)
        self.assertEqual(buf.read(), 1)
        buf.write(3)
        buf.write(4)
        self.assertEqual(buf.read(), 2)
        self.assertEqual(buf.read(), 3)
        self.assertEqual(buf.read(), 4)
        self.assertRaises(BufferEmptyException, buf.read)

    def test_full_and_overwrite(self):
        try:
            from solution import CircularBuffer, BufferFullException, BufferEmptyException
        except ImportError as e:
            self.fail(f"Could not import solution elements: {e}")

        buf = CircularBuffer(2)
        buf.write(1)
        buf.write(2)
        self.assertRaises(BufferFullException, buf.write, 3)

        buf.overwrite(3)
        self.assertEqual(buf.read(), 2)
        self.assertEqual(buf.read(), 3)

if __name__ == '__main__':
    unittest.main()
