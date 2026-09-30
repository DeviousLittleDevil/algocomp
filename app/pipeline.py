"""The comparison pipeline.

This module owns the *scientific* story of the application:

1. build a test suite from the chosen strategies,
2. run both implementations against exactly the same inputs in the sandbox,
3. compare their outputs and report what that does - and does not - prove,
4. if and only if they agreed, benchmark them under matched conditions.

The wording of the verdict is deliberate throughout: agreement on a finite set
of inputs is *empirical evidence*, never a proof of equivalence.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from . import benchmark as bench
from .compare import (
    AccuracyReport,
    compare_suites,
    score_accuracy,
    score_against_expected,
)
from .config import clamp_limits
from .models import RunRequest
from .sandbox import (
    Limits,
    SuiteResult,
    probe_memory_baseline,
    run_function_suite,
    run_stdio_suite,
)
from .sandbox.capabilities import best_isolation, probe_sandbox
from .sandbox.protocol import describe
from .testgen import GenerationParams, case_stats, generate_tests, get_schema

ProgressFn = Callable[[str, float, str], None]

#: Progress checkpoints (fraction complete when the stage *starts*), so the bar
#: moves sensibly no matter which optional stages are enabled.
_STAGES = {
    "generating": 0.02,
    "baseline": 0.08,
    "running": 0.12,
    "comparing": 0.55,
    "reference": 0.62,
    "benchmarking": 0.74,
}


def _fingerprint(code: str) -> dict:
    return {
        "lines": len(code.splitlines()) if code else 0,
        "bytes": len(code.encode("utf-8", "replace")),
        "sha256": hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()[:16],
    }


def _run_suite(
    code: str,
    cases: list[dict],
    request: RunRequest,
    limits: Limits,
) -> SuiteResult:
    if request.protocol == "stdio":
        return run_stdio_suite(code, cases, limits=limits, isolation=best_isolation())
    return run_function_suite(
        code,
        cases,
        entry_point=request.entry_point,
        limits=limits,
        spread_args=request.spread_args,
        isolation=best_isolation(),
    )


def _suite_dict(suite: SuiteResult, limit: int = 400) -> dict:
    return {
        "status": suite.status,
        "error": (suite.error or "")[:limit] or None,
        "error_type": suite.error_type,
        "wall_ms": round(suite.wall_ms, 3),
        "process_cpu_ms": round(suite.process_cpu_ms, 3),
        "process_peak_rss_kb": suite.process_peak_rss_kb,
        "memory_attribution": suite.memory_attribution,
        "isolation": suite.isolation,
        "tests_run": len(suite.tests),
        "tests_ok": sum(1 for case in suite.tests if case.ok),
        "tests_errored": sum(1 for case in suite.tests if case.error),
    }


def _verdict(
    comparison,
    request: RunRequest,
    suite_a: SuiteResult,
    suite_b: SuiteResult,
    test_summary: dict,
) -> dict:
    """Turn the numbers into an honest, explicitly empirical statement."""
    total = comparison.total
    passed = comparison.passed
    coverage = _coverage_phrase(request.tests)

    fatal = []
    for name, suite in (("A", suite_a), ("B", suite_b)):
        if suite.status in ("error", "crash", "timeout"):
            fatal.append(
                {
                    "side": name,
                    "status": suite.status,
                    "message": (suite.error or "the program could not be executed")[:600],
                }
            )

    if fatal:
        return {
            "status": "inconclusive",
            "headline": "The comparison could not be completed",
            "statement": (
                "At least one implementation failed to run, so no conclusion about "
                "behaviour can be drawn from this run."
            ),
            "disclaimer": DISCLAIMER,
            "fatal": fatal,
        }

    if total == 0:
        return {
            "status": "inconclusive",
            "headline": "No test cases were generated",
            "statement": "There was nothing to run, so no conclusion can be drawn.",
            "disclaimer": DISCLAIMER,
            "fatal": [],
        }

    if passed == total:
        return {
            "status": "empirically_equivalent",
            "headline": "Identical output on every test case",
            "statement": (
                f"The algorithms produced identical outputs for {passed:,}/{total:,} test cases "
                f"({coverage}). This provides empirical evidence that they behave equivalently "
                f"for the tested inputs, but does not mathematically prove equivalence for all "
                f"possible inputs."
            ),
            "disclaimer": DISCLAIMER,
            "fatal": [],
        }

    return {
        "status": "different",
        "headline": "The implementations disagree",
        "statement": (
            f"The algorithms produced identical outputs for {passed:,}/{total:,} test cases "
            f"({comparison.failed:,} test case(s) disagreed). On the tested inputs they do not "
            f"behave equivalently, so they are very likely implementing different behaviour - "
            f"though a bug confined to untested inputs cannot be ruled out."
        ),
        "disclaimer": DISCLAIMER,
        "fatal": [],
    }


DISCLAIMER = (
    "Matching output on a finite set of tests is evidence, not proof. Two algorithms can agree "
    "on every input you try and still differ somewhere else; conversely they can differ on the "
    "benchmarked inputs and still be equivalent in general. Use these results to guide you, not "
    "to conclude anything mathematical."
)


def _format_bound(value) -> str:
    """Render a range bound without a pointless ``.0`` for integer schemas."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else repr(number)


def _coverage_phrase(test_config) -> str:
    parts = []
    if test_config.count:
        parts.append(f"{test_config.count} random")
    if test_config.size:
        parts.append(f"typical input size {test_config.size}")
    if test_config.min_value != test_config.max_value:
        parts.append(
            f"values in [{_format_bound(test_config.min_value)}, "
            f"{_format_bound(test_config.max_value)}]"
        )
    if test_config.seed:
        parts.append(f"seed {test_config.seed}")
    return ", ".join(parts) if parts else "as configured"


def _category_breakdown(comparison) -> list[dict]:
    buckets: dict[str, dict] = {}
    for record in comparison.per_case:
        key = record.get("category") or "other"
        bucket = buckets.setdefault(key, {"category": key, "passed": 0, "failed": 0, "error": 0})
        status = record.get("status")
        if status == "pass":
            bucket["passed"] += 1
        elif status == "error":
            bucket["error"] += 1
        else:
            bucket["failed"] += 1
    for bucket in buckets.values():
        bucket["total"] = bucket["passed"] + bucket["failed"] + bucket["error"]
    order = {"random": 0, "edge": 1, "boundary": 2, "duplicates": 3, "empty": 4, "large": 5, "user": 6}
    return sorted(buckets.values(), key=lambda b: order.get(b["category"], 99))


def _build_warnings(
    request: RunRequest,
    comparison,
    suite_a: SuiteResult,
    suite_b: SuiteResult,
    cases: list[dict],
) -> list[dict]:
    warnings: list[dict] = []
    stats = case_stats(cases)

    if comparison.total and comparison.total < 20:
        warnings.append(
            {
                "level": "warning",
                "message": (
                    f"Only {comparison.total} test cases were run. A small suite makes it much "
                    f"easier for two different algorithms to agree by accident."
                ),
            }
        )
    if comparison.total and comparison.total >= 1000:
        warnings.append(
            {
                "level": "info",
                "message": f"{comparison.total:,} test cases were run - strong empirical evidence, still not proof.",
            }
        )
    if len(request.tests.strategies) == 1:
        warnings.append(
            {
                "level": "warning",
                "message": (
                    "Only one test strategy was used. Adding edge, boundary and large inputs "
                    "explores behaviour a single strategy can miss."
                ),
            }
        )
    for name, suite in (("Algorithm A", suite_a), ("Algorithm B", suite_b)):
        if suite.status == "completed_with_errors":
            warnings.append(
                {
                    "level": "warning",
                    "message": f"{name} raised errors on some test cases; those count as mismatches.",
                }
            )
    skipped = [case for case in cases if case.get("status") == "skipped"]
    if skipped:
        warnings.append(
            {
                "level": "warning",
                "message": f"{len(skipped)} test case(s) were skipped because the time budget ran out.",
            }
        )
    if stats["largest"] and stats["largest"] < 8:
        warnings.append(
            {
                "level": "info",
                "message": (
                    f"The largest input had {stats['largest']} element(s). Timing differences on "
                    f"inputs this small are dominated by interpreter overhead."
                ),
            }
        )
    if not any(case.get("size", 0) > 1000 for case in cases):
        warnings.append(
            {
                "level": "info",
                "message": (
                    "No large input was included, so scaling behaviour (O(n log n) vs O(n²)) "
                    "was not exercised."
                ),
            }
        )
    return warnings


def run_comparison(
    request: RunRequest,
    run_id: str,
    progress: ProgressFn | None = None,
) -> dict:
    """Execute a full comparison and return the report the dashboard renders."""

    def report(stage: str, fraction: float, message: str = "") -> None:
        if progress:
            progress(stage, fraction, message)

    started = time.time()
    warnings: list[dict] = []
    limits = clamp_limits(request.limits.model_dump() if request.limits else None)

    # -- 1. test generation ----------------------------------------------
    report("generating", _STAGES["generating"], "Building the test suite")
    params = GenerationParams(
        schema=request.tests.input_schema,
        count=request.tests.count,
        size=request.tests.size,
        min_value=request.tests.min_value,
        max_value=request.tests.max_value,
        seed=request.tests.seed,
        strategies=request.tests.strategies,
        large_factor=request.tests.large_factor,
        large_count=request.tests.large_count,
    )
    try:
        cases, test_summary = generate_tests(params, request.tests.user_cases)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    if not cases:
        raise ValueError(
            "The test configuration produced no test cases. Increase the number of tests, "
            "add a strategy, or provide your own test cases."
        )
    previews = {case["id"]: describe(case["value"], limit=120) for case in cases}

    # -- 2. interpreter baseline -----------------------------------------
    report("baseline", _STAGES["baseline"], "Measuring the interpreter baseline")
    baseline = probe_memory_baseline(limits)
    baseline_kb = baseline.get("peak_rss_kb", 0)

    # -- 3. run both implementations -------------------------------------
    report("running", 0.15, "Running Algorithm A")
    suite_a = _run_suite(request.algorithm_a.code, cases, request, limits)
    report("running", 0.35, "Running Algorithm B")
    suite_b = _run_suite(request.algorithm_b.code, cases, request, limits)

    # Align on cases both sides actually produced.
    common = min(len(suite_a.tests), len(suite_b.tests), len(cases))
    tests_a = suite_a.tests[:common]
    tests_b = suite_b.tests[:common]
    cases = cases[:common]

    # -- 4. compare -------------------------------------------------------
    report("comparing", _STAGES["comparing"], "Comparing outputs")
    comparison = compare_suites(
        tests_a,
        tests_b,
        mode=request.comparison_mode,
        rel_tol=request.rel_tolerance,
        abs_tol=request.abs_tolerance,
        input_previews=previews,
    )
    comparison.total = common

    verdict = _verdict(comparison, request, suite_a, suite_b, test_summary)
    warnings.extend(_build_warnings(request, comparison, suite_a, suite_b, cases))

    # -- 5. accuracy against a reference ----------------------------------
    accuracy: dict[str, Any] | None = None
    report("reference", _STAGES["reference"], "Running the reference implementation")
    if request.reference_code and request.reference_code.strip():
        suite_ref = _run_suite(request.reference_code, cases, request, limits)
        reference_common = min(len(tests_a), len(tests_b), len(suite_ref.tests), len(cases))
        if suite_ref.status in ("ok", "completed_with_errors") and reference_common:
            report_a = score_accuracy(
                tests_a[:reference_common],
                suite_ref.tests[:reference_common],
                rel_tolerance=request.rel_tolerance,
                abs_tolerance=request.abs_tolerance,
                label="Algorithm A",
            )
            report_b = score_accuracy(
                tests_b[:reference_common],
                suite_ref.tests[:reference_common],
                rel_tolerance=request.rel_tolerance,
                abs_tolerance=request.abs_tolerance,
                label="Algorithm B",
            )
            report_a.reference_side = "a"
            report_b.reference_side = "b"
            accuracy = {
                "a": report_a.to_dict(),
                "b": report_b.to_dict(),
                "reference": _fingerprint(request.reference_code),
                "reference_suite": _suite_dict(suite_ref),
            }
        else:
            accuracy = {
                "a": AccuracyReport().to_dict(),
                "b": AccuracyReport().to_dict(),
                "error": suite_ref.error or "the reference implementation could not be run",
            }
            warnings.append(
                {
                    "level": "error",
                    "message": "The reference implementation failed to run, so no accuracy score is available.",
                }
            )
    elif request.tests.expected.strip():
        try:
            expected = json.loads(request.tests.expected)
        except ValueError as exc:
            expected = None
            warnings.append(
                {
                    "level": "error",
                    "message": f"Could not parse the expected-output JSON: {exc.msg} (line {exc.lineno}).",
                }
            )
        if isinstance(expected, dict) and expected:
            report_a = score_against_expected(
                tests_a, expected, rel_tolerance=request.rel_tolerance,
                abs_tolerance=request.abs_tolerance
            )
            report_b = score_against_expected(
                tests_b, expected, rel_tolerance=request.rel_tolerance,
                abs_tolerance=request.abs_tolerance
            )
            report_a.reference_side = "a"
            report_b.reference_side = "b"
            accuracy = {
                "a": report_a.to_dict(),
                "b": report_b.to_dict(),
                "expected_count": len(expected),
            }
    if accuracy is None:
        report("reference", 0.74, "No reference implementation - skipping accuracy scoring")

    # -- 6. benchmark (only when the outputs agreed) ----------------------
    performance: dict[str, Any] | None = None
    equivalent = comparison.equivalent and verdict["status"] == "empirically_equivalent"
    if equivalent and request.benchmark.enabled:
        report("benchmarking", _STAGES["benchmarking"], "Benchmarking both implementations")
        performance = bench.benchmark(
            request.algorithm_a.code,
            request.algorithm_b.code,
            cases,
            config=bench.BenchmarkConfig(
                iterations=request.benchmark.iterations,
                warmup_iterations=request.benchmark.warmup_iterations,
                max_tests=request.benchmark.max_tests,
                min_relative_gap=request.benchmark.min_relative_gap,
            ),
            limits=limits,
            entry_point=request.entry_point,
            spread_args=request.spread_args,
            protocol=request.protocol,
            baseline_kb=baseline_kb,
            label_a=request.algorithm_a.name,
            label_b=request.algorithm_b.name,
            on_progress=lambda stage, fraction: report(
                "benchmarking", _STAGES["benchmarking"] + fraction * (0.97 - _STAGES["benchmarking"]), stage
            ),
        )
        performance["per_test_pairs"] = bench.per_test_pairs(
            performance["per_test"]["a"], performance["per_test"]["b"]
        )
        performance["baseline"] = baseline
    elif not equivalent and request.benchmark.enabled:
        warnings.append(
            {
                "level": "info",
                "message": (
                    "Benchmarking was skipped because the implementations do not produce the "
                    "same output. Comparing the speed of two different algorithms is not "
                    "informative."
                ),
            }
        )
    elif not request.benchmark.enabled:
        warnings.append(
            {"level": "info", "message": "Benchmarking was disabled for this run."}
        )

    capabilities = probe_sandbox()
    report("done", 1.0, "Done")

    return {
        "run_id": run_id,
        "created_at": started,
        "duration_seconds": round(time.time() - started, 3),
        "language": request.language,
        "protocol": request.protocol,
        "entry_point": request.entry_point if request.protocol == "function" else None,
        "algorithms": {
            "a": {"name": request.algorithm_a.name or "Algorithm A", **_fingerprint(request.algorithm_a.code)},
            "b": {"name": request.algorithm_b.name or "Algorithm B", **_fingerprint(request.algorithm_b.code)},
            "reference": _fingerprint(request.reference_code) if request.reference_code else None,
        },
        "verdict": verdict,
        "correctness": {
            **comparison.to_dict(),
            "by_category": _category_breakdown(comparison),
            "a": _suite_dict(suite_a),
            "b": _suite_dict(suite_b),
        },
        "accuracy": accuracy,
        "performance": performance,
        "test_generation": {**test_summary, "stats": case_stats(cases)},
        "sandbox": {
            "capabilities": capabilities.to_dict(),
            "limits": limits.to_dict(),
            "isolation_level": best_isolation(),
        },
        "warnings": warnings,
    }
