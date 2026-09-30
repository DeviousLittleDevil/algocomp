"""The bundled examples must actually demonstrate what they claim.

An example that stops working is worse than no example at all, because it is
the first thing a reader tries.  These tests run every pair for real.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from app.examples import EXAMPLES, get_example
from app.models import BenchmarkOptions, LimitsRequest, RunRequest, TestConfig
from app.pipeline import run_comparison
from app.sandbox import Limits

FAST_LIMITS = Limits(cpu_seconds=3, wall_seconds=6.0, suite_wall_seconds=30.0, memory_mb=256)


def build_request(example, **overrides) -> RunRequest:
    tests = TestConfig(**example["tests"])
    payload = dict(
        algorithm_a={"name": example["names"][0], "code": example["code_a"]},
        algorithm_b={"name": example["names"][1], "code": example["code_b"]},
        language="python",
        protocol=example["protocol"],
        entry_point=example["entry_point"],
        tests=tests,
        comparison_mode=example["comparison_mode"],
        rel_tolerance=example["rel_tolerance"],
        abs_tolerance=example["abs_tolerance"],
        reference_code=example.get("reference_code") or None,
        benchmark=BenchmarkOptions(
            enabled=example["benchmark"]["enabled"],
            iterations=example["benchmark"]["iterations"],
            warmup_iterations=example["benchmark"]["warmup_iterations"],
            max_tests=example["benchmark"]["max_tests"],
        ),
        limits=LimitsRequest(**asdict(FAST_LIMITS)),
    )
    payload.update(overrides)
    return RunRequest(**payload)


@pytest.mark.parametrize(
    "example",
    [e for e in EXAMPLES if e["id"] != "sorting-buggy"],
    ids=[e["id"] for e in EXAMPLES if e["id"] != "sorting-buggy"],
)
def test_example_agrees_with_its_own_claim(example):
    """Each example shows off two implementations that should agree."""
    report = run_comparison(build_request(example), "test")

    assert report["correctness"]["errored"] == 0, (
        f"{example['id']}: an implementation raised errors - {report['correctness']['mismatches'][:1]}"
    )
    assert report["verdict"]["status"] == "empirically_equivalent", (
        f"{example['id']} should agree with itself; first mismatch: "
        f"{report['correctness']['mismatches'][:1]}"
    )
    assert report["verdict"]["statement"].endswith("for all possible inputs.")
    assert "empirical evidence" in report["verdict"]["statement"]


@pytest.mark.parametrize("example", EXAMPLES, ids=[e["id"] for e in EXAMPLES])
def test_example_actually_generates_tests(example):
    """Every example, including the deliberately broken one, builds a suite."""
    report = run_comparison(build_request(example), "test")
    assert report["correctness"]["total"] > 0
    assert report["test_generation"]["per_strategy"]


@pytest.fixture(scope="module")
def buggy_report():
    return run_comparison(build_request(get_example("sorting-buggy")), "test")


class TestTheBugHuntExample:
    """The point of this example is that it *fails*, loudly and usefully."""

    def test_verdict_is_a_difference(self, buggy_report):
        assert buggy_report["verdict"]["status"] == "different"
        assert "do not behave equivalently" in buggy_report["verdict"]["statement"]

    def test_mismatches_are_reported_with_context(self, buggy_report):
        mismatches = buggy_report["correctness"]["mismatches"]
        assert mismatches
        assert all(m["reason"] for m in mismatches)
        assert all(m["a_output"] and m["b_output"] for m in mismatches)
        assert any(m["input_preview"] for m in mismatches)

    def test_duplicate_strategy_is_where_it_fails(self, buggy_report):
        by_category = {b["category"]: b for b in buggy_report["correctness"]["by_category"]}
        assert by_category["duplicates"]["failed"] > 0
        assert by_category["empty"]["failed"] == 0

    def test_no_benchmark_when_different(self, buggy_report):
        assert buggy_report["performance"] is None
        assert any("Benchmarking was skipped" in w["message"] for w in buggy_report["warnings"])


class TestPerformanceExamples:
    def test_sieve_beats_trial_division(self):
        """A 100x algorithmic gap must not be written off as noise."""
        report = run_comparison(build_request(get_example("primes-speed")), "test")
        performance = report["performance"]
        assert performance is not None
        assert performance["verdict"]["winner"] == "a", performance["verdict"]["explanation"]
        assert performance["verdict"]["within_noise"] is False
        # difference_percent is (B - A): positive means B is the slower side.
        assert performance["verdict"]["difference_percent"] > 50

    def test_transposed_multiplication_is_faster(self):
        report = run_comparison(build_request(get_example("matrix-multiplication")), "test")
        performance = report["performance"]
        assert performance is not None
        assert performance["verdict"]["winner"] in ("a", "b")
        assert performance["verdict"]["within_noise"] is False
        assert performance["iterations"] == 5

    def test_sorted_beats_handwritten_merge_sort(self):
        report = run_comparison(build_request(get_example("sorting-equivalent")), "test")
        assert report["performance"]["verdict"]["winner"] == "a"

    def test_memory_is_measured_for_both_sides(self):
        report = run_comparison(build_request(get_example("sorting-equivalent")), "test")
        memory = report["performance"]["memory"]
        assert memory["a"]["peak_kb"] > 0
        assert memory["b"]["peak_kb"] > 0
        assert memory["a"]["baseline_kb"] > 0


class TestAccuracyExample:
    def test_newton_matches_math_sqrt_within_tolerance(self):
        report = run_comparison(build_request(get_example("sqrt-accuracy")), "test")
        assert report["accuracy"] is not None
        for side in ("a", "b"):
            accuracy = report["accuracy"][side]
            assert accuracy["compared"] > 0, "the reference was never consulted"
            assert accuracy["outside_tolerance"] == 0
            assert accuracy["max_abs_error"] < 1e-9

    def test_accuracy_uses_float_mode_not_strict(self):
        report = run_comparison(build_request(get_example("sqrt-accuracy")), "test")
        assert report["correctness"]["resolved_mode"] == "float"


class TestExampleMetadata:
    def test_ids_are_unique(self):
        ids = [example["id"] for example in EXAMPLES]
        assert len(ids) == len(set(ids))

    def test_every_example_defines_the_entry_point(self):
        for example in EXAMPLES:
            assert f"def {example['entry_point']}" in example["code_a"]
            assert f"def {example['entry_point']}" in example["code_b"]
            if example.get("reference_code"):
                assert f"def {example['entry_point']}" in example["reference_code"]

    def test_every_example_uses_a_known_schema_and_strategies(self):
        from app.testgen import SCHEMAS, STRATEGIES

        for example in EXAMPLES:
            assert example["tests"]["schema"] in SCHEMAS
            assert set(example["tests"]["strategies"]) <= set(STRATEGIES)

    def test_list_examples_hides_the_code(self):
        from app.examples import list_examples

        for entry in list_examples():
            assert "code_a" not in entry and "code_b" not in entry


class TestVerdictWording:
    """The headline claim is the part of the product that must not lie."""

    @pytest.mark.parametrize(
        "example_id", ["sorting-equivalent", "primes-speed", "sqrt-accuracy"], ids=str
    )
    def test_statement_carries_the_caveat(self, example_id):
        report = run_comparison(build_request(get_example(example_id)), "test")
        statement = report["verdict"]["statement"]
        assert "does not mathematically prove equivalence" in statement
        assert "empirical" in statement
        assert report["verdict"]["disclaimer"]

    def test_integer_ranges_are_not_printed_as_floats(self):
        report = run_comparison(build_request(get_example("sorting-equivalent")), "test")
        # int_list is an integer schema; [-1000.0, 1000.0] would look wrong.
        assert "values in [-1000, 1000]" in report["verdict"]["statement"]
        assert ".0]" not in report["verdict"]["statement"]

    def test_benchmark_only_runs_after_the_outputs_match(self):
        for example in EXAMPLES:
            report = run_comparison(build_request(example), "test")
            if report["verdict"]["status"] == "different":
                assert report["performance"] is None, example["id"]
                assert any(
                    "Benchmarking was skipped" in w["message"] for w in report["warnings"]
                ), example["id"]
            else:
                assert report["performance"] is not None, example["id"]
