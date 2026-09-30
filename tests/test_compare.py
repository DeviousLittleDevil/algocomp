"""Output comparison and accuracy scoring."""

from __future__ import annotations

import math

import pytest

from app.compare import (
    compare_suites,
    compare_values,
    score_accuracy,
    score_against_expected,
)
from app.sandbox.runner import TestOutcome


def outcome(index=0, output=None, error=None, ok=True, case_id="c", category="random"):
    return TestOutcome(
        index=index,
        id=case_id,
        label="",
        category=category,
        ok=ok,
        output=output,
        error=error,
    )


class TestExactComparison:
    def test_identical_scalars(self):
        assert compare_values(3, 3)[0]

    def test_different_scalars(self):
        equal, reason, _path = compare_values(3, 4)
        assert not equal
        assert "differ" in reason

    def test_type_is_part_of_strict_equality(self):
        assert not compare_values(1, 1.0, mode="strict")[0]
        assert not compare_values(True, 1, mode="strict")[0]

    def test_lists(self):
        assert compare_values([1, 2, 3], [1, 2, 3])[0]
        assert not compare_values([1, 2, 3], [1, 2])[0]
        assert not compare_values([1, 2, 3], [3, 2, 1])[0]

    def test_nested_path_points_at_the_difference(self):
        equal, _reason, path = compare_values({"a": [1, 2, 3]}, {"a": [1, 9, 3]})
        assert not equal
        assert path == "$.a[1]"

    def test_dict_key_difference_is_explained(self):
        equal, reason, _ = compare_values({"a": 1, "b": 2}, {"a": 1})
        assert not equal
        assert "only in" in reason

    def test_none_versus_value(self):
        assert not compare_values(None, 5)[0]
        assert compare_values(None, None)[0]

    def test_deeply_nested_structures(self):
        left = {"r": [{"v": [1, 2, {"deep": (1, 2)}]}]}
        right = {"r": [{"v": [1, 2, {"deep": (1, 2)}]}]}
        assert compare_values(left, right)[0]


class TestFloatComparison:
    def test_within_absolute_tolerance(self):
        assert compare_values(1.0, 1.0 + 1e-15, mode="float", abs_tol=1e-12)[0]

    def test_outside_tolerance(self):
        equal, reason, _ = compare_values(1.0, 1.1, mode="float", rel_tol=1e-9, abs_tol=1e-12)
        assert not equal
        assert "differ by" in reason

    def test_relative_tolerance_scales_with_magnitude(self):
        assert compare_values(1e9, 1e9 + 1, mode="float", rel_tol=1e-6, abs_tol=0)[0]
        assert not compare_values(1.0, 1.1, mode="float", rel_tol=1e-6, abs_tol=0)[0]

    def test_zero_uses_absolute_tolerance(self):
        assert compare_values(0.0, 1e-13, mode="float", abs_tol=1e-12, rel_tol=0)[0]
        assert not compare_values(0.0, 1e-6, mode="float", abs_tol=1e-12, rel_tol=0)[0]

    def test_nan_matches_nan_only_in_float_mode(self):
        assert compare_values(float("nan"), float("nan"), mode="float")[0]
        assert not compare_values(float("nan"), float("nan"), mode="strict")[0]
        assert not compare_values(float("nan"), 1.0, mode="float")[0]

    def test_infinity_sign_matters(self):
        assert compare_values(float("inf"), float("inf"), mode="float")[0]
        assert not compare_values(float("inf"), float("-inf"), mode="float")[0]

    def test_floats_inside_a_structure(self):
        assert compare_values([1.0, 2.5], [1.0, 2.5 + 1e-15], mode="float", abs_tol=1e-12)[0]
        assert not compare_values([1.0, 2.5], [1.0, 2.6], mode="float")[0]

    def test_auto_mode_tolerates_floats(self):
        assert compare_values([1.0, 2.0], [1.0, 2.0 + 1e-15], mode="auto", abs_tol=1e-12)[0]


class TestNumericMode:
    def test_numeric_strings_compare_numerically(self):
        assert compare_values("3.0", "3", mode="numeric")[0]
        assert not compare_values("3.0", "4", mode="numeric")[0]

    def test_non_numeric_strings_still_compare_textually(self):
        assert compare_values("abc", "abc", mode="numeric")[0]
        assert not compare_values("abc", "abd", mode="numeric")[0]

    def test_float_mode_does_not_coerce_strings(self):
        assert not compare_values("3.0", 3.0, mode="float")[0]


class TestUnorderedMode:
    def test_order_does_not_matter(self):
        assert compare_values([3, 1, 2], [1, 2, 3], mode="unordered")[0]

    def test_multiset_matters(self):
        assert not compare_values([1, 1, 2], [1, 2, 2], mode="unordered")[0]

    def test_length_still_matters(self):
        assert not compare_values([1, 2], [1, 2, 3], mode="unordered")[0]

    def test_mixed_types_do_not_crash(self):
        assert compare_values([1, "a", None], [None, "a", 1], mode="unordered")[0]

    def test_works_inside_a_dict(self):
        assert compare_values({"s": [1, 2]}, {"s": [2, 1]}, mode="unordered")[0]


class TestCompareSuites:
    def test_all_pass(self):
        result = compare_suites([outcome(i, i) for i in range(5)], [outcome(i, i) for i in range(5)])
        assert result.equivalent
        assert result.passed == 5 and result.failed == 0
        assert result.mismatches == []

    def test_one_mismatch_is_recorded_with_context(self):
        a = [outcome(0, [1, 2, 3], case_id="x", category="edge")]
        b = [outcome(0, [1, 2], case_id="x", category="edge")]
        result = compare_suites(a, b, input_previews={"x": "[1, 2, 3]"})
        assert not result.equivalent
        assert result.failed == 1
        mismatch = result.mismatches[0]
        assert mismatch.a_output == "[1, 2, 3]"
        assert mismatch.b_output == "[1, 2]"
        assert "different lengths" in mismatch.reason
        assert mismatch.input_preview == "[1, 2, 3]"

    def test_an_error_on_one_side_counts_as_a_failure(self):
        a = [outcome(0, None, error="boom")]
        b = [outcome(0, 5)]
        result = compare_suites(a, b)
        assert result.errored == 1
        assert not result.equivalent
        assert "boom" in result.mismatches[0].reason

    def test_mismatch_list_is_capped(self):
        pairs = 100
        a = [outcome(i, i, case_id=f"c{i}") for i in range(pairs)]
        b = [outcome(i, i + 1, case_id=f"c{i}") for i in range(pairs)]
        result = compare_suites(a, b, max_mismatches=5)
        assert result.failed == pairs
        assert len(result.mismatches) == 5

    def test_auto_mode_is_explained(self):
        result = compare_suites(
            [outcome(0, 1.0)], [outcome(0, 1.0)], mode="auto"
        )
        assert result.resolved_mode == "float"
        result2 = compare_suites(
            [outcome(0, [1, 2])], [outcome(0, [1, 2])], mode="auto"
        )
        assert result2.resolved_mode == "strict"

    def test_match_rate(self):
        result = compare_suites([outcome(i, i) for i in range(4)],
                               [outcome(i, i if i < 2 else 99) for i in range(4)])
        assert result.passed == 2
        assert result.to_dict()["match_rate"] == 0.5


class TestAccuracy:
    def test_identical_output_has_zero_error(self):
        report = score_accuracy(
            [outcome(0, [1.0, 2.0])], [outcome(0, [1.0, 2.0])],
            rel_tolerance=1e-9, abs_tolerance=1e-12,
        )
        assert report.compared == 1
        assert report.max_abs_error == 0
        assert report.outside_tolerance == 0

    def test_error_is_measured(self):
        report = score_accuracy(
            [outcome(0, 1.0)], [outcome(0, 1.5)],
            rel_tolerance=1e-9, abs_tolerance=1e-12,
        )
        assert report.max_abs_error == pytest.approx(0.5)
        assert report.outside_tolerance == 1
        assert report.within_tolerance == 0

    def test_within_tolerance_is_counted(self):
        report = score_accuracy(
            [outcome(0, 1.0)], [outcome(0, 1.0 + 1e-15)],
            rel_tolerance=1e-9, abs_tolerance=1e-12,
        )
        assert report.within_tolerance == 1
        assert report.outside_tolerance == 0

    def test_rmse_of_many_values(self):
        report = score_accuracy(
            [outcome(0, [0.0, 0.0])], [outcome(0, [3.0, 3.0])],
            rel_tolerance=0, abs_tolerance=0,
        )
        assert report.mean_abs_error == pytest.approx(3.0)
        assert report.rmse == pytest.approx(3.0)

    def test_shape_mismatch_is_reported_not_scored(self):
        report = score_accuracy(
            [outcome(0, [1, 2, 3])], [outcome(0, [1, 2])],
            rel_tolerance=1e-9, abs_tolerance=1e-12,
        )
        assert report.compared == 0
        assert report.per_case[0]["status"] == "shape_mismatch"
        assert "expected 2" in report.per_case[0]["detail"]

    def test_non_numeric_output_is_not_scored(self):
        report = score_accuracy(
            [outcome(0, "hello")], [outcome(0, "hello")],
            rel_tolerance=1e-9, abs_tolerance=1e-12,
        )
        assert report.compared == 0
        assert report.per_case[0]["status"] == "not_numeric"

    def test_error_on_one_side_is_recorded(self):
        report = score_accuracy(
            [outcome(0, None, error="boom")], [outcome(0, 1.0)],
            rel_tolerance=1e-9, abs_tolerance=1e-12,
        )
        assert report.per_case[0]["status"] == "error"

    def test_nested_numbers_are_flattened(self):
        report = score_accuracy(
            [outcome(0, {"a": [1.0, 2.0]})], [outcome(0, {"a": [1.0, 2.0]})],
            rel_tolerance=1e-9, abs_tolerance=1e-12,
        )
        assert report.compared == 1
        assert report.max_abs_error == 0

    def test_worst_case_is_identified(self):
        report = score_accuracy(
            [outcome(0, 1.0, case_id="good"), outcome(1, 10.0, case_id="bad")],
            [outcome(0, 1.0, case_id="good"), outcome(1, 1.0, case_id="bad")],
            rel_tolerance=0, abs_tolerance=0,
        )
        assert report.worst["id"] == "bad"

    def test_non_finite_values_are_skipped_not_counted_as_errors(self):
        report = score_accuracy(
            [outcome(0, [math.inf])], [outcome(0, [math.inf])],
            rel_tolerance=1e-9, abs_tolerance=1e-12,
        )
        assert report.per_case[0]["status"] == "degenerate"


class TestExpectedOutputs:
    def test_scores_against_user_expectations(self):
        report = score_against_expected(
            [outcome(0, 2.0, case_id="user-1")], {"user-1": 2.0},
            rel_tolerance=1e-9, abs_tolerance=1e-9,
        )
        assert report.within_tolerance == 1
        assert report.has_expected

    def test_missing_expectation_is_skipped(self):
        report = score_against_expected(
            [outcome(0, 2.0, case_id="user-1")], {"other": 1},
            rel_tolerance=1e-9, abs_tolerance=1e-9,
        )
        assert report.per_case[0]["status"] == "no_expected_value"
        assert report.compared == 0
