"""Output comparison and accuracy scoring.

Two responsibilities live here:

1. **Equivalence checking** - do A and B produce the same output for a given
   input?  Because algorithms differ legitimately in *how* they compute, the
   comparison supports several modes: exact structural equality, float
   tolerance, unordered comparison, and a sensible ``auto`` mode that picks
   based on the data.

2. **Accuracy scoring** - when a reference implementation (or a known expected
   value) is supplied, how far off is a candidate?  This is what makes the
   tool useful for numerical or approximate algorithms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .sandbox.protocol import describe

COMPARISON_MODES: dict[str, dict] = {
    "auto": {
        "label": "Auto",
        "description": "Infer the right comparison from the data (tolerances for floats, order-insensitive for sets).",
    },
    "strict": {
        "label": "Strict",
        "description": "Values must be exactly equal, including type and ordering.",
    },
    "float": {
        "label": "Float tolerance",
        "description": "Numbers may differ within a relative/absolute tolerance; everything else must match exactly.",
    },
    "numeric": {
        "label": "Numeric",
        "description": "Like float tolerance, but strings that parse as numbers are compared numerically too.",
    },
    "unordered": {
        "label": "Unordered",
        "description": "Sequence order is ignored - use for algorithms that return a set or an unordered collection.",
    },
}

DEFAULT_REL_TOL = 1e-9
DEFAULT_ABS_TOL = 1e-12


@dataclass
class Mismatch:
    """One test case where the two implementations disagreed."""

    index: int
    case_id: str
    category: str
    label: str
    input_preview: str
    a_output: str
    b_output: str
    reason: str
    path: str = ""
    a_error: str | None = None
    b_error: str | None = None
    stdout_a: str = ""
    stdout_b: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ComparisonResult:
    equivalent: bool
    passed: int = 0
    failed: int = 0
    errored: int = 0
    total: int = 0
    mode: str = "auto"
    resolved_mode: str = "strict"
    mismatches: list[Mismatch] = field(default_factory=list)
    per_case: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "equivalent": self.equivalent,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "total": self.total,
            "match_rate": (self.passed / self.total) if self.total else None,
            "mode": self.mode,
            "resolved_mode": self.resolved_mode,
            "mismatches": [m.to_dict() for m in self.mismatches],
            "per_case": self.per_case,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_number(value: Any, numeric_mode: bool) -> float | None:
    if _is_number(value):
        return float(value)
    if numeric_mode and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _sortable_key(value: Any):
    """Total order across mixed types so unordered comparison never crashes."""
    if value is None:
        return (0, 0.0, "")
    if isinstance(value, bool):
        return (1, float(value), "")
    if _is_number(value):
        number = float(value)
        if math.isnan(number):
            number = float("inf")
        return (2, number, "")
    if isinstance(value, str):
        return (3, 0.0, value)
    if isinstance(value, (list, tuple)):
        return (4, 0.0, repr(value))
    return (5, 0.0, repr(value))


def _type_name(value: Any) -> str:
    return type(value).__name__


def compare_values(
    a: Any,
    b: Any,
    *,
    mode: str = "auto",
    rel_tol: float = DEFAULT_REL_TOL,
    abs_tol: float = DEFAULT_ABS_TOL,
    numeric_strings: bool = False,
) -> tuple[bool, str, str]:
    """Return ``(equal, reason, path)`` for two outputs.

    ``path`` points at the first difference, e.g. ``result[3].total``.
    """
    equal, reason, path = _compare(a, b, mode, rel_tol, abs_tol, numeric_strings, "$", 0)
    return equal, reason, path


def _compare(
    a: Any,
    b: Any,
    mode: str,
    rel_tol: float,
    abs_tol: float,
    numeric_strings: bool,
    path: str,
    depth: int,
) -> tuple[bool, str, str]:
    if depth > 64:
        return True, "", path

    tolerant = mode in ("auto", "float", "numeric")
    unordered = mode == "unordered"

    # 0. Strict mode also insists on the *type*.  In Python 1 == 1.0 and
    #    True == 1, but an algorithm that returns a float where an integer was
    #    expected is a real difference and should not be silently accepted.
    if mode == "strict":
        int_a = isinstance(a, int) and not isinstance(a, bool)
        int_b = isinstance(b, int) and not isinstance(b, bool)
        if int_a != int_b:
            return False, f"one side returns {_type_name(a)} and the other {_type_name(b)}", path

    # 1. numeric comparison
    if tolerant or mode == "auto":
        number_a = _as_number(a, numeric_strings or mode in ("numeric", "auto"))
        number_b = _as_number(b, numeric_strings or mode in ("numeric", "auto"))
        if number_a is not None and number_b is not None:
            return _compare_numbers(number_a, number_b, rel_tol, abs_tol, path)

    # 2. NaN / infinity.  Under a tolerance two NaNs count as "the same
    #    undefined result"; in strict mode they do not, because NaN != NaN is
    #    the only defensible strict reading.
    if isinstance(a, float) or isinstance(b, float):
        if isinstance(a, float) and isinstance(b, float):
            if math.isnan(a) and math.isnan(b):
                if tolerant:
                    return True, "", path
                return False, "both results are NaN, which is never strictly equal", path
            if math.isinf(a) and math.isinf(b) and (a > 0) == (b > 0):
                return True, "", path

    # 3. None
    if a is None or b is None:
        if a is None and b is None:
            return True, "", path
        return False, f"one side is None and the other is {_type_name(a if a is not None else b)}", path

    # 4. strings
    if isinstance(a, str) or isinstance(b, str):
        if isinstance(a, str) and isinstance(b, str) and a == b:
            return True, "", path
        return False, "different string values", path

    # 5. bytes
    if isinstance(a, (bytes, bytearray)) and isinstance(b, (bytes, bytearray)):
        return (True, "", path) if bytes(a) == bytes(b) else (False, "different byte strings", path)

    # 6. mappings
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            only_a = sorted(set(a) - set(b), key=_sortable_key)[:5]
            only_b = sorted(set(b) - set(a), key=_sortable_key)[:5]
            detail = ""
            if only_a:
                detail += f" only in A: {describe(only_a, 60)}"
            if only_b:
                detail += f" only in B: {describe(only_b, 60)}"
            return False, f"different keys ({len(a)} vs {len(b)}).{detail}", path
        for key in a:
            ok, reason, child = _compare(
                a[key], b[key], mode, rel_tol, abs_tol, numeric_strings, f"{path}.{key}", depth + 1
            )
            if not ok:
                return False, reason, child
        return True, "", path

    # 7. sequences
    if isinstance(a, (list, tuple, set, frozenset)) and isinstance(b, (list, tuple, set, frozenset)):
        left = list(a)
        right = list(b)
        if unordered:
            return _compare_unordered(left, right, mode, rel_tol, abs_tol, numeric_strings, path, depth)
        if len(left) != len(right):
            return False, f"different lengths ({len(left)} vs {len(right)})", path
        for index, (item_a, item_b) in enumerate(zip(left, right)):
            ok, reason, child = _compare(
                item_a, item_b, mode, rel_tol, abs_tol, numeric_strings, f"{path}[{index}]", depth + 1
            )
            if not ok:
                return False, reason, child
        return True, "", path

    # 8. containers of different kinds
    if isinstance(a, (list, tuple, set, frozenset, dict)) != isinstance(b, (list, tuple, set, frozenset, dict)):
        return False, f"different structures ({_type_name(a)} vs {_type_name(b)})", path

    # 9. fall back to equality
    try:
        if a == b:
            return True, "", path
    except Exception:  # noqa: BLE001 - user objects can raise on comparison
        pass
    return False, f"values differ ({describe(a, 60)} vs {describe(b, 60)})", path


def _compare_numbers(a: float, b: float, rel_tol: float, abs_tol: float, path: str) -> tuple[bool, str, str]:
    if math.isnan(a) or math.isnan(b):
        if math.isnan(a) and math.isnan(b):
            return True, "", path
        return False, "one result is NaN and the other is not", path
    if a == b:
        return True, "", path
    if math.isinf(a) or math.isinf(b):
        return False, "infinities differ in sign", path
    difference = abs(a - b)
    if difference <= abs_tol:
        return True, "", path
    scale = max(abs(a), abs(b))
    if scale > 0 and difference / scale <= rel_tol:
        return True, "", path
    return False, f"numbers differ by {difference:.6g} (relative {difference / scale:.3g})", path


def _compare_unordered(
    left: list,
    right: list,
    mode: str,
    rel_tol: float,
    abs_tol: float,
    numeric_strings: bool,
    path: str,
    depth: int,
) -> tuple[bool, str, str]:
    """Multiset comparison: every element of A must match one element of B.

    Greedy matching is not a perfect assignment algorithm, but it is exact for
    the realistic cases and its cost is bounded by the result limit we already
    impose on submitted output.
    """
    if len(left) != len(right):
        return False, f"different lengths ({len(left)} vs {len(right)})", path
    if len(left) > 2000:
        left, right = sorted(left, key=_sortable_key)[:2000], sorted(right, key=_sortable_key)[:2000]
    used = [False] * len(right)
    for index, item in enumerate(left):
        for position, candidate in enumerate(right):
            if used[position]:
                continue
            ok, _reason, _child = _compare(
                item, candidate, mode, rel_tol, abs_tol, numeric_strings, path, depth + 1
            )
            if ok:
                used[position] = True
                break
        else:
            return False, "no matching element in B for this value", f"{path}[{index}]"
    return True, "", path


# ---------------------------------------------------------------------------
# Suite-level comparison
# ---------------------------------------------------------------------------


def compare_suites(
    a_tests: list,
    b_tests: list,
    *,
    mode: str = "auto",
    rel_tol: float = DEFAULT_REL_TOL,
    abs_tol: float = DEFAULT_ABS_TOL,
    max_mismatches: int = 50,
    input_previews: dict[str, str] | None = None,
) -> ComparisonResult:
    """Compare two sandbox suites case by case."""
    result = ComparisonResult(equivalent=True, mode=mode, total=len(a_tests))
    previews = input_previews or {}

    for index, (case_a, case_b) in enumerate(zip(a_tests, b_tests)):
        record = {
            "index": index,
            "id": case_a.id,
            "label": case_a.label,
            "category": case_a.category,
            "status": "pass",
            "reason": "",
        }
        if case_a.error or case_b.error:
            result.errored += 1
            record["status"] = "error"
            record["reason"] = case_a.error or case_b.error or "one implementation raised an error"
            result.equivalent = False
            result.mismatches.append(
                Mismatch(
                    index=index,
                    case_id=case_a.id,
                    category=case_a.category,
                    label=case_a.label,
                    input_preview=previews.get(case_a.id, ""),
                    a_output="",
                    b_output="",
                    reason=record["reason"],
                    a_error=case_a.error,
                    b_error=case_b.error,
                    stdout_a=case_a.stdout[-2000:],
                    stdout_b=case_b.stdout[-2000:],
                )
            )
            result.per_case.append(record)
            continue

        equal, reason, path = compare_values(
            case_a.output, case_b.output, mode=mode, rel_tol=rel_tol, abs_tol=abs_tol
        )
        if equal:
            result.passed += 1
            record["status"] = "pass"
        else:
            result.failed += 1
            result.equivalent = False
            record["status"] = "fail"
            record["reason"] = reason
            if len(result.mismatches) < max_mismatches:
                result.mismatches.append(
                    Mismatch(
                        index=index,
                        case_id=case_a.id,
                        category=case_a.category,
                        label=case_a.label,
                        input_preview=previews.get(case_a.id, ""),
                        a_output=describe(case_a.output),
                        b_output=describe(case_b.output),
                        reason=reason,
                        path=path,
                        stdout_a=case_a.stdout[-2000:],
                        stdout_b=case_b.stdout[-2000:],
                    )
                )
        result.per_case.append(record)

    if mode == "auto":
        result.resolved_mode = _infer_mode(a_tests, b_tests)
    else:
        result.resolved_mode = mode
    return result


def _infer_mode(a_tests: list, b_tests: list) -> str:
    """Explain what ``auto`` decided, so the report is not a black box."""
    saw_float = False
    for case in list(a_tests)[:50] + list(b_tests)[:50]:
        if _contains_float(case.output):
            saw_float = True
            break
    if saw_float:
        return "float"
    return "strict"


def _contains_float(value: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, float):
        return True
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_float(item, depth + 1) for item in list(value)[:100])
    if isinstance(value, dict):
        return any(_contains_float(item, depth + 1) for item in list(value.values())[:100])
    return False


# ---------------------------------------------------------------------------
# Accuracy against a reference implementation
# ---------------------------------------------------------------------------


@dataclass
class AccuracyReport:
    available: bool = False
    has_reference: bool = False
    has_expected: bool = False
    #: Which side a reference was supplied for: "a", "b" or "".
    reference_side: str = ""
    rel_tolerance: float = 0.0
    abs_tolerance: float = 0.0
    metric: str = "absolute error"
    max_abs_error: float | None = None
    mean_abs_error: float | None = None
    rmse: float | None = None
    max_rel_error: float | None = None
    within_tolerance: int = 0
    outside_tolerance: int = 0
    compared: int = 0
    per_case: list[dict] = field(default_factory=list)
    worst: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _flatten_numbers(value: Any, out: list[float], depth: int = 0) -> None:
    """Pull every number out of a (possibly nested) result for scoring."""
    if depth > 12 or len(out) > 200_000:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.append(float(value))
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _flatten_numbers(item, out, depth + 1)
        return
    if isinstance(value, dict):
        for item in list(value.values())[:200]:
            _flatten_numbers(item, out, depth + 1)


def _shape_error(value: Any, expected: Any) -> str | None:
    """Structural check before any numeric scoring - different shapes cannot
    be scored, they can only be reported as a mismatch."""
    if isinstance(expected, (list, tuple, dict)) or isinstance(value, (list, tuple, dict)):
        if isinstance(expected, (list, tuple)) and isinstance(value, (list, tuple)):
            if len(expected) != len(value):
                return f"result has {len(value)} items, expected {len(expected)}"
        elif isinstance(expected, dict) and isinstance(value, dict):
            if set(expected) != set(value):
                return "result has a different set of keys"
        else:
            return f"result is {type(value).__name__}, expected {type(expected).__name__}"
    return None


def score_accuracy(
    candidate_tests: list,
    reference_tests: list,
    *,
    rel_tolerance: float,
    abs_tolerance: float,
    label: str = "candidate",
) -> AccuracyReport:
    """Score one implementation against a reference over the same inputs."""
    report = AccuracyReport(
        available=True,
        has_reference=True,
        rel_tolerance=rel_tolerance,
        abs_tolerance=abs_tolerance,
    )
    all_abs: list[float] = []
    all_sq: list[float] = []
    all_rel: list[float] = []

    for candidate, reference in zip(candidate_tests, reference_tests):
        entry = {"index": candidate.index, "id": candidate.id, "category": candidate.category}
        if candidate.error or reference.error:
            entry["status"] = "error"
            entry["error"] = candidate.error or reference.error
            report.per_case.append(entry)
            continue
        shape_problem = _shape_error(candidate.output, reference.output)
        if shape_problem:
            entry["status"] = "shape_mismatch"
            entry["detail"] = shape_problem
            report.per_case.append(entry)
            continue

        left: list[float] = []
        right: list[float] = []
        _flatten_numbers(candidate.output, left)
        _flatten_numbers(reference.output, right)
        if not left or len(left) != len(right):
            entry["status"] = "not_numeric"
            entry["detail"] = (
                f"{label} produced {len(left)} numeric values, the reference produced {len(right)}"
            )
            report.per_case.append(entry)
            continue

        worst_abs = 0.0
        worst_rel = 0.0
        for value, target in zip(left, right):
            if math.isnan(value) or math.isnan(target) or math.isinf(value) or math.isinf(target):
                continue
            absolute = abs(value - target)
            worst_abs = max(worst_abs, absolute)
            scale = max(abs(value), abs(target))
            if scale > 0:
                relative = absolute / scale
                worst_rel = max(worst_rel, relative)
                all_rel.append(relative)
            all_abs.append(absolute)
            all_sq.append(absolute * absolute)

        if not all_abs:
            entry["status"] = "degenerate"
            report.per_case.append(entry)
            continue

        within = worst_abs <= abs_tolerance or worst_rel <= rel_tolerance
        report.compared += 1
        if within:
            report.within_tolerance += 1
            entry["status"] = "within_tolerance"
        else:
            report.outside_tolerance += 1
            entry["status"] = "outside_tolerance"
        entry["max_abs_error"] = worst_abs
        entry["max_rel_error"] = worst_rel

        case_record = dict(entry)
        if report.worst is None or worst_abs > report.worst.get("max_abs_error", -1):
            report.worst = case_record
        report.per_case.append(entry)

    # Averages are computed over every individual number, not per case, which
    # is the standard convention for numerical error reporting.
    if all_abs:
        report.max_abs_error = max(all_abs)
        report.mean_abs_error = sum(all_abs) / len(all_abs)
        report.rmse = math.sqrt(sum(all_sq) / len(all_sq))
        report.max_rel_error = max(all_rel) if all_rel else None

    report.notes.append(
        f"Error is measured over every number in the output, compared against the reference "
        f"on the same {report.compared} inputs."
    )
    if report.outside_tolerance:
        report.notes.append(
            f"{report.outside_tolerance} of {report.compared} inputs fell outside the "
            f"tolerance (relative {rel_tolerance:g}, absolute {abs_tolerance:g})."
        )
    else:
        report.notes.append(
            f"All {report.compared} scored inputs stayed within the tolerance "
            f"(relative {rel_tolerance:g}, absolute {abs_tolerance:g})."
        )
    return report


def score_against_expected(
    candidate_tests: list,
    expected: dict[str, Any],
    *,
    rel_tolerance: float,
    abs_tolerance: float,
) -> AccuracyReport:
    """Score against user-supplied expected outputs, keyed by test-case id."""
    report = AccuracyReport(
        available=True,
        has_expected=True,
        rel_tolerance=rel_tolerance,
        abs_tolerance=abs_tolerance,
    )
    all_abs: list[float] = []
    all_sq: list[float] = []
    for candidate in candidate_tests:
        entry = {"index": candidate.index, "id": candidate.id, "category": candidate.category}
        if candidate.id not in expected:
            entry["status"] = "no_expected_value"
            report.per_case.append(entry)
            continue
        if candidate.error:
            entry["status"] = "error"
            entry["error"] = candidate.error
            report.per_case.append(entry)
            continue
        shape_problem = _shape_error(candidate.output, expected[candidate.id])
        if shape_problem:
            entry["status"] = "shape_mismatch"
            entry["detail"] = shape_problem
            report.per_case.append(entry)
            continue
        left: list[float] = []
        right: list[float] = []
        _flatten_numbers(candidate.output, left)
        _flatten_numbers(expected[candidate.id], right)
        if len(left) != len(right) or not left:
            entry["status"] = "not_numeric"
            report.per_case.append(entry)
            continue
        worst_abs = max((abs(a - b) for a, b in zip(left, right) if not math.isnan(a - b)), default=0.0)
        all_abs.extend(abs(a - b) for a, b in zip(left, right) if not math.isnan(a - b))
        all_sq.extend((a - b) ** 2 for a, b in zip(left, right) if not math.isnan(a - b))
        within = worst_abs <= abs_tolerance
        report.compared += 1
        report.within_tolerance += int(within)
        report.outside_tolerance += int(not within)
        entry["status"] = "within_tolerance" if within else "outside_tolerance"
        entry["max_abs_error"] = worst_abs
        report.per_case.append(entry)
    if all_abs:
        report.max_abs_error = max(all_abs)
        report.mean_abs_error = sum(all_abs) / len(all_abs)
        report.rmse = math.sqrt(sum(all_sq) / len(all_sq))
    return report
