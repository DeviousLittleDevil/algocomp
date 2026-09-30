"""Test-case generation."""

from __future__ import annotations

import pytest

from app.testgen import (
    MAX_TEST_CASES,
    GenerationParams,
    case_stats,
    generate_tests,
    get_schema,
    parse_user_cases,
)


def categories(cases):
    return {case["category"] for case in cases}


def values_for(cases, category):
    return [case["value"] for case in cases if case["category"] == category]


class TestSchemas:
    def test_every_schema_is_constructible(self):
        for schema_id in get_schema_ids():
            params = GenerationParams(
                schema=schema_id,
                count=3,
                size=5,
                strategies=["random", "edge", "boundary", "duplicates", "empty", "large"],
            )
            cases, summary = generate_tests(params)
            assert cases, f"{schema_id} produced no cases"
            assert summary["schema"] == schema_id
            for case in cases:
                assert conforms(case["value"], schema_id), f"{schema_id}: {case['value']!r}"

    def test_unknown_schema_falls_back(self):
        params = GenerationParams(schema="nonsense", count=2, strategies=["random"])
        cases, summary = generate_tests(params)
        assert summary["schema"] == "int_list"

    def test_int_range_is_respected(self):
        cases, _ = generate_tests(
            GenerationParams(schema="int", count=60, min_value=5, max_value=9, strategies=["random"])
        )
        assert all(5 <= case["value"] <= 9 for case in cases)

    def test_float_range_is_respected(self):
        cases, _ = generate_tests(
            GenerationParams(
                schema="float", count=60, min_value=-2.5, max_value=-0.5, strategies=["random"]
            )
        )
        assert all(-2.5 <= case["value"] <= -0.5 for case in cases)

    def test_min_greater_than_max_is_swapped(self):
        cases, summary = generate_tests(
            GenerationParams(schema="int", count=10, min_value=100, max_value=-100, strategies=["random"])
        )
        assert summary["min_value"] < summary["max_value"]
        assert all(-100 <= case["value"] <= 100 for case in cases)

    def test_conformable_matrix_pairs(self):
        """A matrix pair must be a valid multiplication, i.e. A is r x k, B is k x c."""
        cases, _ = generate_tests(
            GenerationParams(schema="matrix_pair", count=10, size=6, strategies=["random", "large"])
        )
        for case in cases:
            left, right = case["value"]
            assert len(left[0]) == len(right), "inner dimensions must match"
            assert all(len(row) == len(left[0]) for row in left)
            assert all(len(row) == len(right[0]) for row in right)


class TestStrategies:
    def test_empty_only_where_allowed(self):
        cases, _ = generate_tests(
            GenerationParams(schema="int_list", count=0, strategies=["empty"])
        )
        assert [case["value"] for case in cases] == [[]]

        cases, _ = generate_tests(GenerationParams(schema="int", count=0, strategies=["empty"]))
        assert cases == [], "a scalar schema has no empty input"

    def test_empty_string_for_text_schema(self):
        cases, _ = generate_tests(GenerationParams(schema="str", count=0, strategies=["empty"]))
        assert [case["value"] for case in cases] == [""]

    def test_duplicates_actually_contain_duplicates(self):
        cases, _ = generate_tests(
            GenerationParams(schema="int_list", count=0, size=20, strategies=["duplicates"])
        )
        for value in values_for(cases, "duplicates"):
            assert len(value) != len(set(value)), f"no duplicates in {value[:10]}"

    def test_large_is_bigger_than_typical(self):
        cases, _ = generate_tests(
            GenerationParams(
                schema="int_list", count=2, size=10, large_factor=20, large_count=1,
                strategies=["random", "large"],
            )
        )
        random_len = len(values_for(cases, "random")[0])
        large_len = len(values_for(cases, "large")[0])
        assert large_len == random_len * 20

    def test_boundary_hits_the_extremes(self):
        cases, _ = generate_tests(
            GenerationParams(schema="int", count=0, min_value=3, max_value=77, strategies=["boundary"])
        )
        values = set(values_for(cases, "boundary"))
        assert 3 in values and 77 in values
        assert 4 in values and 76 in values

    def test_edge_values_stay_inside_a_positive_range(self):
        """A square-root algorithm must not be handed -1 as an 'edge case'."""
        cases, _ = generate_tests(
            GenerationParams(schema="float", count=0, min_value=0.1, max_value=100, strategies=["edge"])
        )
        assert cases
        assert all(case["value"] >= 0.1 for case in cases)

    def test_unknown_strategy_is_dropped(self):
        """An unrecognised strategy is ignored rather than crashing the run."""
        cases, summary = generate_tests(
            GenerationParams(count=2, strategies=["random", "not-a-strategy"])
        )
        assert summary["strategies"] == ["random"]
        assert cases and all(case["category"] == "random" for case in cases)


class TestReproducibility:
    def test_same_seed_same_cases(self):
        params = GenerationParams(count=10, size=8, seed=777, strategies=["random", "edge", "boundary"])
        first, _ = generate_tests(params)
        second, _ = generate_tests(GenerationParams(**{**params.__dict__}))
        assert [case["value"] for case in first] == [case["value"] for case in second]

    def test_different_seed_different_cases(self):
        a, _ = generate_tests(GenerationParams(count=20, size=8, seed=1, strategies=["random"]))
        b, _ = generate_tests(GenerationParams(count=20, size=8, seed=2, strategies=["random"]))
        assert [case["value"] for case in a] != [case["value"] for case in b]

    def test_ids_are_unique(self):
        cases, _ = generate_tests(
            GenerationParams(
                count=5, size=4, strategies=["random", "edge", "boundary", "duplicates", "large"],
                large_count=2,
            )
        )
        ids = [case["id"] for case in cases]
        assert len(ids) == len(set(ids))


class TestLimits:
    def test_total_is_capped(self):
        cases, summary = generate_tests(
            GenerationParams(count=MAX_TEST_CASES, strategies=["random", "edge", "boundary", "large"],
                             large_count=5)
        )
        assert len(cases) <= MAX_TEST_CASES
        assert summary["truncated"] is True
        assert summary["cap"] == MAX_TEST_CASES

    def test_size_is_capped(self):
        cases, summary = generate_tests(
            GenerationParams(count=1, size=10**7, strategies=["random"])
        )
        assert summary["size"] <= 50_000

    def test_negative_count_becomes_zero(self):
        cases, _ = generate_tests(GenerationParams(count=-5, strategies=["random"]))
        assert cases == []


class TestUserCases:
    def test_one_json_value_per_line(self):
        cases, error = parse_user_cases('[3, 1, 2]\n"hello"\n42')
        assert error is None
        assert [case["value"] for case in cases] == [[3, 1, 2], "hello", 42]
        assert {case["category"] for case in cases} == {"user"}

    def test_pretty_printed_array_of_cases(self):
        cases, error = parse_user_cases('[\n  [1, 2],\n  [3, 4]\n]')
        assert error is None
        assert [case["value"] for case in cases] == [[1, 2], [3, 4]]

    def test_blank_lines_ignored(self):
        cases, error = parse_user_cases('\n\n1\n\n2\n\n')
        assert error is None
        assert [case["value"] for case in cases] == [1, 2]

    def test_bad_json_reports_the_line(self):
        cases, error = parse_user_cases('1\n2\nnot json\n4')
        assert cases == []
        assert "line 3" in error

    def test_empty_input(self):
        assert parse_user_cases("") == ([], None)
        assert parse_user_cases("   \n  ") == ([], None)

    def test_user_cases_merged_with_generated(self):
        cases, summary = generate_tests(
            GenerationParams(count=2, strategies=["random"]),
            user_cases="[9, 9]",
        )
        assert summary["per_strategy"]["user"] == 1
        assert any(case["category"] == "user" for case in cases)


class TestStats:
    def test_case_stats_sums_sizes(self):
        cases, _ = generate_tests(
            GenerationParams(schema="int_list", count=3, size=4, strategies=["random", "edge"])
        )
        stats = case_stats(cases)
        assert stats["count"] == len(cases)
        assert stats["largest"] == max(case["size"] for case in cases)
        assert stats["total_elements"] == sum(case["size"] for case in cases)

    def test_matrix_pair_size_counts_every_element(self):
        cases, _ = generate_tests(
            GenerationParams(schema="matrix_pair", count=1, size=8, strategies=["random"])
        )
        left, right = cases[0]["value"]
        assert cases[0]["size"] == (len(left) * len(left[0])) + (len(right) * len(right[0]))


# --- helpers -------------------------------------------------------------


def get_schema_ids():
    from app.testgen import SCHEMAS

    return list(SCHEMAS)


def conforms(value, schema_id):
    """Loose structural check used by the schema coverage test."""
    if schema_id == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_id == "float":
        return isinstance(value, float)
    if schema_id == "str":
        return isinstance(value, str)
    if schema_id in ("int_list", "float_list", "str_list"):
        expected = {"int_list": int, "float_list": float, "str_list": str}[schema_id]
        return isinstance(value, list) and all(isinstance(item, expected) for item in value)
    if schema_id == "int_matrix":
        return (
            isinstance(value, list)
            and value
            and all(isinstance(row, list) and all(isinstance(x, int) for x in row) for row in value)
            and len({len(row) for row in value}) == 1
        )
    if schema_id in ("pair_int", "pair_float"):
        expected = int if schema_id == "pair_int" else float
        return isinstance(value, list) and len(value) == 2 and all(isinstance(x, expected) for x in value)
    if schema_id == "matrix_pair":
        return (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(m, list) and m and all(isinstance(r, list) for r in m) for m in value)
        )
    return True
