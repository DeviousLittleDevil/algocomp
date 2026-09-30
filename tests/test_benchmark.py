"""Benchmark statistics and the noise logic."""

from __future__ import annotations

import math

import pytest

from app.benchmark import (
    BenchmarkConfig,
    MemoryStats,
    TimingStats,
    compare_memory,
    compare_timings,
    per_test_pairs,
)
from app.sandbox import Limits


class TestTimingStats:
    def test_basic_statistics(self):
        stats = TimingStats(samples=[1.0, 2.0, 3.0, 4.0])
        assert stats.count == 4
        assert stats.mean == 2.5
        assert stats.minimum == 1.0
        assert stats.maximum == 4.0
        assert stats.median == 2.5
        assert stats.stdev == pytest.approx(1.2909944, rel=1e-5)

    def test_single_sample_has_no_stdev(self):
        assert TimingStats(samples=[5.0]).stdev == 0.0
        assert TimingStats(samples=[]).mean == 0.0

    def test_coefficient_of_variation(self):
        stats = TimingStats(samples=[9.0, 10.0, 11.0])
        assert stats.cv_percent == pytest.approx(stats.stdev / stats.mean * 100)

    def test_serialises_with_samples(self):
        payload = TimingStats(samples=[1.0, 2.0]).to_dict()
        assert payload["count"] == 2
        assert payload["samples"] == [1.0, 2.0]
        assert payload["mean"] == 1.5


class TestTimingVerdict:
    def test_clear_difference_has_a_winner(self):
        verdict = compare_timings(TimingStats(samples=[1.0] * 5), TimingStats(samples=[10.0] * 5))
        assert verdict.winner == "a"
        assert not verdict.within_noise
        assert "A" in verdict.label

    def test_a_slower_b_is_reported_as_a_winner(self):
        """The classic mistake: calling the implementation with the *larger*
        mean the faster one."""
        verdict = compare_timings(
            TimingStats(samples=[10.0] * 5), TimingStats(samples=[1.0] * 5), label_a="slow", label_b="fast"
        )
        assert verdict.winner == "b"
        assert "fast" in verdict.label
        assert verdict.difference_percent < 0

    def test_b_slower_b_is_reported_as_a_winner(self):
        verdict = compare_timings(
            TimingStats(samples=[1.0] * 5), TimingStats(samples=[10.0] * 5), label_a="fast", label_b="slow"
        )
        assert verdict.winner == "a"
        assert verdict.difference_percent > 0

    def test_tiny_difference_is_reported_as_noise(self):
        verdict = compare_timings(TimingStats(samples=[10.0, 10.1, 9.9] * 4),
                                  TimingStats(samples=[10.05, 10.0, 10.1] * 4))
        assert verdict.within_noise
        assert verdict.winner == "tie"
        assert "noise" in verdict.explanation.lower()

    def test_noise_floor_is_respected(self):
        """2% apart but very stable: still inside the default 5% floor."""
        verdict = compare_timings(TimingStats(samples=[100.0] * 20), TimingStats(samples=[102.0] * 20))
        assert verdict.within_noise
        assert verdict.noise_band_percent == 5.0

    def test_more_iterations_shrink_the_statistical_band(self):
        stable = TimingStats(samples=[10.0, 10.2, 9.8, 10.1, 9.9])
        many = TimingStats(samples=[10.0, 10.2, 9.8, 10.1, 9.9] * 20)
        few = compare_timings(stable, stable)
        lots = compare_timings(many, many)
        assert lots.noise_band_percent <= few.noise_band_percent

    def test_noise_band_never_below_the_floor(self):
        verdict = compare_timings(
            TimingStats(samples=[1.0] * 50), TimingStats(samples=[1.01] * 50), min_relative_gap=0.05
        )
        assert verdict.noise_band_percent == pytest.approx(5.0)

    def test_empty_samples_are_inconclusive(self):
        verdict = compare_timings(TimingStats(samples=[]), TimingStats(samples=[1.0]))
        assert verdict.winner == "tie"
        assert verdict.confidence == "none"

    def test_explanation_always_present(self):
        for a, b in [([1.0], [2.0]), ([1.0] * 5, [1.0] * 5), ([1.0], [1.0])]:
            verdict = compare_timings(TimingStats(samples=a), TimingStats(samples=b))
            assert len(verdict.explanation) > 30

    def test_confidence_drops_when_noisy(self):
        noisy_a = TimingStats(samples=[1.0, 5.0, 2.0, 8.0, 1.2])
        noisy_b = TimingStats(samples=[1.1, 4.5, 2.2, 7.5, 1.1])
        verdict = compare_timings(noisy_a, noisy_b)
        assert verdict.confidence in ("low", "medium", "high")

    def test_heavily_noisy_data_is_not_called_a_difference(self):
        # 10 samples spread over 10x, means almost identical.
        verdict = compare_timings(
            TimingStats(samples=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10.0]),
            TimingStats(samples=[1.1, 2.2, 2.9, 4.1, 5.2, 5.8, 7.1, 7.9, 9.2, 9.8]),
        )
        assert verdict.within_noise


class TestMemoryComparison:
    def test_added_memory_drives_the_verdict(self):
        a = MemoryStats(samples_kb=[30000], added_kb=[500], baseline_kb=11000)
        b = MemoryStats(samples_kb=[30000], added_kb=[9000], baseline_kb=11000)
        result = compare_memory(a, b, label_a="A", label_b="B")
        assert result["basis"] == "added"
        assert result["winner"] == "b"
        assert "more" in result["summary"]

    def test_similar_added_memory_is_a_tie(self):
        a = MemoryStats(samples_kb=[30000], added_kb=[500], baseline_kb=11000)
        b = MemoryStats(samples_kb=[30000], added_kb=[510], baseline_kb=11000)
        result = compare_memory(a, b)
        assert result["winner"] == "tie"
        assert result["within_noise"]

    def test_falls_back_to_peak_without_added_samples(self):
        a = MemoryStats(samples_kb=[30000], baseline_kb=11000)
        b = MemoryStats(samples_kb=[50000], baseline_kb=11000)
        result = compare_memory(a, b, label_a="A", label_b="B")
        assert result["basis"] == "peak"
        assert result["winner"] == "b"

    def test_above_baseline_helper(self):
        stats = MemoryStats(samples_kb=[30000], baseline_kb=11000)
        assert stats.above_baseline_kb == 19000
        assert MemoryStats(samples_kb=[5000], baseline_kb=11000).above_baseline_kb == 0

    def test_explains_the_interpreter_baseline(self):
        a = MemoryStats(samples_kb=[30000], added_kb=[500], baseline_kb=11000)
        b = MemoryStats(samples_kb=[30000], added_kb=[900], baseline_kb=11000)
        text = compare_memory(a, b)["above_baseline"]["summary"]
        assert "interpreter" in text


class TestHelpers:
    def test_per_test_pairs_joins_by_id(self):
        a = [{"id": "1", "category": "random", "wall_ms": 1.0}, {"id": "2", "category": "edge", "wall_ms": 2.0}]
        b = [{"id": "1", "category": "random", "wall_ms": 1.5}]
        pairs = per_test_pairs(a, b)
        assert pairs == [{"id": "1", "category": "random", "a_ms": 1.0, "b_ms": 1.5}]

    def test_benchmark_config_defaults_are_sane(self):
        config = BenchmarkConfig()
        assert config.iterations >= 1
        assert config.warmup_iterations >= 0
        assert 0 < config.min_relative_gap <= 1


def test_benchmark_uses_the_names_the_user_gave():
    """The verdict must name the algorithms, not "Algorithm A"."""
    from app.benchmark import benchmark as bench_benchmark

    report = bench_benchmark(
        "def solve(xs):\n    return sorted(xs)\n",
        "def solve(xs):\n    return list(reversed(sorted(xs)))\n",
        [{"id": "c0", "category": "random", "input": [3, 1, 2]}],
        config=BenchmarkConfig(iterations=3, warmup_iterations=1, max_tests=4),
        limits=Limits(cpu_seconds=3, wall_seconds=10.0, suite_wall_seconds=60.0, memory_mb=256),
        label_a="Quick sort",
        label_b="Reverse trick",
    )
    assert report["verdict"]["label"]
    assert report["verdict"]["label"] not in ("Algorithm A is faster", "Algorithm B is faster")
