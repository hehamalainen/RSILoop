import unittest

class TestMatrix(unittest.TestCase):
    def setUp(self):
        from solution import Matrix
        self.Matrix = Matrix

    def test_add(self):
        a = self.Matrix([[1, 2], [3, 4]])
        b = self.Matrix([[5, 6], [7, 8]])
        c = a + b
        self.assertEqual(c.data, [[6, 8], [10, 12]])

    def test_add_dimension_mismatch(self):
        a = self.Matrix([[1, 2]])
        b = self.Matrix([[1, 2], [3, 4]])
        with self.assertRaises(ValueError):
            _ = a + b

    def test_mul(self):
        a = self.Matrix([[1, 2], [3, 4]])
        b = self.Matrix([[2, 0], [1, 2]])
        c = a * b
        self.assertEqual(c.data, [[4, 4], [10, 8]])

    def test_mul_dimension_mismatch(self):
        a = self.Matrix([[1, 2, 3]])
        b = self.Matrix([[1, 2]])
        with self.assertRaises(ValueError):
            _ = a * b

    def test_transpose(self):
        a = self.Matrix([[1, 2, 3], [4, 5, 6]])
        t = a.transpose()
        self.assertEqual(t.data, [[1, 4], [2, 5], [3, 6]])

if __name__ == '__main__':
    unittest.main()
