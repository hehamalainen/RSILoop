import unittest

class TestStringCalculator(unittest.TestCase):
    def setUp(self):
        from solution import calculate
        self.calc = calculate

    def test_addition(self):
        self.assertAlmostEqual(self.calc('2 + 3'), 5.0)

    def test_division_multiplication(self):
        self.assertAlmostEqual(self.calc('10 / 2 * 3'), 15.0)

    def test_parentheses(self):
        self.assertAlmostEqual(self.calc('(1 + 2) * (3 + 4)'), 21.0)

    def test_float_operands(self):
        self.assertAlmostEqual(self.calc('3.14 * 2'), 6.28, places=5)

    def test_division_by_zero(self):
        with self.assertRaises(ValueError):
            self.calc('1 / 0')

if __name__ == '__main__':
    unittest.main()
