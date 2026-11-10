from __future__ import annotations

from unittest import TestCase

from app.evals.stats import Proportion, pass_hat_k, pooled, spread, wilson_interval


class WilsonTests(TestCase):
    def test_known_values(self) -> None:
        cases = {
            (0, 10): (0.0, 0.2775),
            (10, 10): (0.7225, 1.0),
            (5, 10): (0.2366, 0.7634),
            (1, 1): (0.2065, 1.0),
        }
        for (k, n), (low, high) in cases.items():
            with self.subTest(k=k, n=n):
                got_low, got_high = wilson_interval(k, n)
                self.assertAlmostEqual(got_low, low, places=3)
                self.assertAlmostEqual(got_high, high, places=3)

    def test_n_zero_has_no_interval(self) -> None:
        with self.assertRaises(ValueError):
            wilson_interval(0, 0)

    def test_invalid_counts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            wilson_interval(-1, 5)
        with self.assertRaises(ValueError):
            wilson_interval(6, 5)

    def test_interval_always_contains_the_point_estimate_and_stays_in_range(self) -> None:
        for n in range(1, 30):
            for k in range(n + 1):
                low, high = wilson_interval(k, n)
                self.assertGreaterEqual(low, 0.0)
                self.assertLessEqual(high, 1.0)
                self.assertLessEqual(low, k / n + 1e-12)
                self.assertGreaterEqual(high, k / n - 1e-12)

    def test_formatting_always_shows_n_and_an_interval(self) -> None:
        text = Proportion(3, 10).format()
        self.assertIn("3/10", text)
        self.assertIn("Wilson interval", text)
        self.assertIn("n=0", Proportion(0, 0).format())


class AggregationTests(TestCase):
    def test_pass_k_requires_every_run_and_fails_empty_groups(self) -> None:
        self.assertEqual(pass_hat_k([[True, True], [True, False], []]), (1, 3))

    def test_spread_and_pooling_weight_runs_equally(self) -> None:
        stats = spread([1.0, 2.0, 6.0])
        assert stats is not None
        self.assertEqual((stats.n, stats.mean, stats.minimum, stats.maximum), (3, 3.0, 1.0, 6.0))
        self.assertIsNone(spread([]))
        pooled_value = pooled([(1, 2), (9, 10)])
        self.assertEqual((pooled_value.successes, pooled_value.n), (10, 12))
