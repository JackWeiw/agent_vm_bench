"""
Unit tests for vm_bench utils module

Tests:
- calc_percentiles function
- calc_p99 function
- calc_avg function
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm_bench.utils import calc_percentiles, calc_p99, calc_avg


class TestCalcPercentiles(unittest.TestCase):
    """Test calc_percentiles function"""

    def test_empty_list(self):
        result = calc_percentiles([])
        self.assertEqual(result['min'], 0.0)
        self.assertEqual(result['max'], 0.0)
        self.assertEqual(result['avg'], 0.0)
        self.assertEqual(result['p50'], 0.0)
        self.assertEqual(result['p95'], 0.0)
        self.assertEqual(result['p99'], 0.0)
        self.assertEqual(result['count'], 0)

    def test_single_value(self):
        result = calc_percentiles([5.0])
        self.assertEqual(result['min'], 5.0)
        self.assertEqual(result['max'], 5.0)
        self.assertEqual(result['avg'], 5.0)
        self.assertEqual(result['p50'], 5.0)
        self.assertEqual(result['p95'], 5.0)
        self.assertEqual(result['p99'], 5.0)
        self.assertEqual(result['count'], 1)

    def test_multiple_values(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = calc_percentiles(values)
        self.assertEqual(result['min'], 1.0)
        self.assertEqual(result['max'], 5.0)
        self.assertEqual(result['avg'], 3.0)
        self.assertEqual(result['count'], 5)

    def test_large_dataset(self):
        # Create 100 values from 0 to 99
        values = [float(i) for i in range(100)]
        result = calc_percentiles(values)
        self.assertEqual(result['min'], 0.0)
        self.assertEqual(result['max'], 99.0)
        self.assertEqual(result['avg'], 49.5)
        # P50 should be around 50
        self.assertGreater(result['p50'], 49)
        self.assertLess(result['p50'], 51)
        # P95 should be around 95
        self.assertGreater(result['p95'], 94)
        self.assertLess(result['p95'], 96)
        # P99 should be around 99
        self.assertGreater(result['p99'], 98)

    def test_unsorted_input(self):
        # Input unsorted, function should still work
        values = [5.0, 1.0, 3.0, 2.0, 4.0]
        result = calc_percentiles(values)
        self.assertEqual(result['min'], 1.0)
        self.assertEqual(result['max'], 5.0)


class TestCalcP99(unittest.TestCase):
    """Test calc_p99 function"""

    def test_empty_list(self):
        self.assertEqual(calc_p99([]), 0.0)

    def test_single_value(self):
        self.assertEqual(calc_p99([5.0]), 5.0)

    def test_small_sample(self):
        # Less than 100 samples, return max
        values = [1.0, 2.0, 3.0]
        self.assertEqual(calc_p99(values), 3.0)

    def test_large_sample(self):
        # 100 values, P99 should be 99th percentile
        values = [float(i) for i in range(100)]
        self.assertGreater(calc_p99(values), 98)

    def test_exact_100_values(self):
        values = list(range(1, 101))  # 1 to 100
        p99 = calc_p99(values)
        # For 100 values, P99 should be near the 99th value
        self.assertGreater(p99, 98)


class TestCalcAvg(unittest.TestCase):
    """Test calc_avg function"""

    def test_empty_list(self):
        self.assertEqual(calc_avg([]), 0.0)

    def test_single_value(self):
        self.assertEqual(calc_avg([5.0]), 5.0)

    def test_multiple_values(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(calc_avg(values), 3.0)

    def test_large_values(self):
        values = [1000.0, 2000.0, 3000.0]
        self.assertEqual(calc_avg(values), 2000.0)


if __name__ == '__main__':
    unittest.main()