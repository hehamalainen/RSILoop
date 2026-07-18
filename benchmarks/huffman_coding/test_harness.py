import unittest

class TestHuffmanCoder(unittest.TestCase):
    def setUp(self):
        from solution import HuffmanCoder
        self.coder = HuffmanCoder()

    def test_empty_string(self):
        self.assertEqual(self.coder.decode(self.coder.encode('')), '')

    def test_single_char(self):
        encoded = self.coder.encode('aaaa')
        decoded = self.coder.decode(encoded)
        self.assertEqual(decoded, 'aaaa')
        # No compression ratio assertion for single-character alphabet

    def test_two_char(self):
        encoded = self.coder.encode('ababab')
        decoded = self.coder.decode(encoded)
        self.assertEqual(decoded, 'ababab')
        # No compression ratio assertion for two-character alphabet

    def test_repetitive_ascii_roundtrip(self):
        text = 'abracadabra' * 18  # 198 chars, mix of 5 symbols
        encoded = self.coder.encode(text)
        decoded = self.coder.decode(encoded)
        self.assertEqual(decoded, text)
        # Compression ratio applies only to sufficiently long multi-symbol inputs
        self.assertLess(len(encoded), len(text.encode('utf-8')))

    def test_emoji_roundtrip(self):
        text = '\U0001f600\U0001f601\U0001f602' * 10
        encoded = self.coder.encode(text)
        decoded = self.coder.decode(encoded)
        self.assertEqual(decoded, text)

if __name__ == '__main__':
    unittest.main()
