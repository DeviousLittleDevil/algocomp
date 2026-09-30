"""Benchmarking and statistics.

Two ideas drive this module:

**Fairness.**  A and B are measured in the *same* process pool, with the same
limits, the same test inputs and - crucially - in *interleaved* order
(A, B, A, B, ...).  Interleaving cancels out slow drift from other load on the
machine far better than measuring all of A and then all of B.

**Honesty.**  Wall-clock differences on a shared machine are noisy.  A 3%
difference between two runs is usually measurement noise, not a finding, and
reporting it as "B is 3% faster" would be misleading.  Every comparison
therefore carries an explicit noise band and an explicit verdict.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .sandbox import Limits, run_function_suite, run_single_process
from .sandbox.capabilities import best_isolation


@dataclass
class TimingStats:
    """Summary of a set of repeated measurements."""

    samples: list[float] = field(default_factory=list)
    unit: str = "ms"

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples) if self.samples else 0.0

    @property
    def minimum(self) -> float:
        return min(self.samples) if self.samples else 0.0

    @property
    def maximum(self) -> float:
        return max(self.samples) if self.samples else 0.0

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.samples) if len(self.samples) > 1 else 0.0

    @property
    def relative_spread(self) -> float:
        """Standard deviation as a fraction of the mean - our noise proxy."""
        if not self.samples or self.mean == 0:
            return 0.0
        return self.stdev / self.mean

    @property
    def cv_percent(self) -> float:
        return self.relative_spread * 100.0

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "unit": self.unit,
            "mean": self.mean,
            "min": self.minimum,
            "max": self.maximum,
            "median": self.median,
            "stdev": self.stdev,
            "cv_percent": self.cv_percent,
            "samples": [round(value, 6) for value in self.samples],
        }


@dataclass
class MemoryStats:
    samples_kb: list[int] = field(default_factory=list)
    #: Memory each implementation added on top of what was already resident
    #: when its test case started - i.e. the algorithm's own footprint,
    #: excluding the interpreter, the harness and the test inputs.
    added_kb: list[int] = field(default_factory=list)
    baseline_kb: int = 0
    attribution: str = "process_total"

    @property
    def peak_kb(self) -> int:
        return max(self.samples_kb) if self.samples_kb else 0

    @property
    def peak_added_kb(self) -> int:
        return max(self.added_kb) if self.added_kb else 0

    @property
    def mean_kb(self) -> float:
        return statistics.fmean(self.samples_kb) if self.samples_kb else 0.0

    @property
    def above_baseline_kb(self) -> int:
        """Peak minus the interpreter's own footprint.

        This is the number that actually says something about the algorithm:
        the interpreter baseline is identical for both implementations and
        would otherwise dominate small programs.
        """
        return max(0, self.peak_kb - self.baseline_kb)

    def to_dict(self) -> dict:
        return {
            "peak_kb": self.peak_kb,
            "mean_kb": self.mean_kb,
            "added_kb": self.peak_added_kb,
            "added_samples_kb": list(self.added_kb),
            "baseline_kb": self.baseline_kb,
            "above_baseline_kb": self.above_baseline_kb,
            "attribution": self.attribution,
            "samples_kb": list(self.samples_kb),
        }


@dataclass
class Verdict:
    """A comparison that refuses to over-claim."""

    winner: str  # "a" | "b" | "tie"
    label: str
    difference_percent: float | None
    noise_band_percent: float
    within_noise: bool
    confidence: str
    explanation: str
    noise_percent: float
    signal_to_noise: float | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def compare_timings(
    a: TimingStats,
    b: TimingStats,
    *,
    label_a: str = "Algorithm A",
    label_b: str = "Algorithm B",
    min_relative_gap: float = 0.05,
) -> Verdict:
    """Decide whether the timing difference is meaningful.

    The noise band combines two things:

    * ``min_relative_gap`` - an absolute floor, because no wall-clock
      measurement on a shared machine is trustworthy below a few percent;
    * the *standard error* of both means, propagated in quadrature.  With n
      independent samples the uncertainty of each mean is ``sd / sqrt(n)``, so
      doubling the iterations genuinely shrinks the band.

    A difference smaller than the band is reported as **within noise**, not as
    a winner.
    """
    if not a.samples or not b.samples:
        return Verdict(
            winner="tie",
            label="Inconclusive",
            difference_percent=None,
            noise_band_percent=100.0,
            within_noise=True,
            confidence="none",
            noise_percent=100.0,
            explanation="Not enough successful measurements to compare.",
        )

    mean_a, mean_b = a.mean, b.mean
    diff = mean_b - mean_a
    reference = max(abs(mean_a), abs(mean_b), 1e-12)
    diff_percent = diff / reference * 100.0

    se_a = a.stdev / math.sqrt(a.count) if a.count else 0.0
    se_b = b.stdev / math.sqrt(b.count) if b.count else 0.0
    combined_se = math.sqrt(se_a * se_a + se_b * se_b)
    statistical_band = combined_se / reference * 100.0 if reference else 0.0
    # `min_relative_gap` arrives as a fraction (0.05 = 5%); everything below is
    # in percent, so convert before taking the maximum.
    floor_percent = max(0.0, float(min_relative_gap)) * 100.0
    noise_band = max(floor_percent, statistical_band)

    noise_percent = max(a.cv_percent, b.cv_percent)
    signal_to_noise = abs(diff) / combined_se if combined_se > 0 else (None if diff == 0 else math.inf)

    within_noise = abs(diff_percent) <= noise_band
    if within_noise:
        winner = "tie"
        label = "No significant difference"
        if abs(diff_percent) < 0.5:
            explanation = (
                f"The two implementations differ by only {abs(diff_percent):.2f}%, which is smaller "
                f"than the {noise_band:.1f}% noise band for this setup. Treat them as equally fast."
            )
        else:
            explanation = (
                f"The measured difference ({abs(diff_percent):.1f}%) is inside the "
                f"{noise_band:.1f}% noise band, so it cannot be distinguished from run-to-run "
                f"variation. Run more iterations or use a quieter machine to resolve it."
            )
        confidence = "low"
    else:
        # diff = mean_b - mean_a, so a positive diff means B took *longer*.
        faster, slower = (label_a, label_b) if diff > 0 else (label_b, label_a)
        winner = "a" if diff > 0 else "b"
        label = f"{faster} is faster"
        if signal_to_noise is not None and signal_to_noise != math.inf and signal_to_noise < 3:
            confidence = "low"
        elif a.cv_percent > 15 or b.cv_percent > 15:
            confidence = "medium"
        else:
            confidence = "high"
        explanation = (
            f"{faster} averages {abs(diff_percent):.1f}% less time than {slower} "
            f"({a.mean:.3f} ms vs {b.mean:.3f} ms over {a.count} and {b.count} iterations). "
            f"That exceeds the {noise_band:.1f}% noise band"
            + (
                f" and the gap is {signal_to_noise:.1f}x the combined standard error."
                if signal_to_noise not in (None, math.inf)
                else "."
            )
        )

    return Verdict(
        winner=winner,
        label=label,
        difference_percent=diff_percent,
        noise_band_percent=noise_band,
        within_noise=within_noise,
        confidence=confidence,
        explanation=explanation,
        noise_percent=noise_percent,
        signal_to_noise=None if signal_to_noise == math.inf else signal_to_noise,
    )


def compare_memory(a: MemoryStats, b: MemoryStats, *, label_a="A", label_b="B") -> dict:
    """Compare peak memory with the same 'is this difference real?' caution.

    Two figures are reported.  The *peak* is the child's maximum resident set
    size, which necessarily includes the Python interpreter and - because the
    harness holds the whole test suite in memory - the test inputs themselves.
    The *added* figure is the memory each algorithm put on top of what was
    already resident when its test case started, and that is the one that
    actually characterises the implementation, so the verdict is based on it.
    """
    peak_a, peak_b = a.peak_kb, b.peak_kb
    over_a, over_b = a.above_baseline_kb, b.above_baseline_kb
    baseline = max(a.baseline_kb, b.baseline_kb)
    added_a, added_b = a.peak_added_kb, b.peak_added_kb
    has_added = bool(a.added_kb and b.added_kb)

    if has_added:
        difference = added_b - added_a
        reference = max(added_a, added_b, 1)
        # 64 KB is roughly the granularity at which the allocator hands out or
        # reuses arenas, so a smaller gap is not worth calling a difference.
        band_kb = max(64.0, 0.02 * max(reference, 512))
    else:
        difference = peak_b - peak_a
        reference = max(peak_a, peak_b, 1)
        band_kb = max(128.0, baseline * 0.01)

    within_noise = abs(difference) <= band_kb
    unit = 1024.0
    value_a = (added_a if has_added else peak_a) / unit
    value_b = (added_b if has_added else peak_b) / unit
    # "0.51 MB allocated during its test cases" vs "a 30.3 MB peak resident set"
    measure = "allocated during its test cases" if has_added else "peak resident set size"

    if within_noise:
        winner = "tie"
        summary = (
            f"The two implementations are indistinguishable on memory: {value_a:.2f} MB vs "
            f"{value_b:.2f} MB {measure} - a {abs(difference) / unit:.2f} MB gap, inside the "
            f"{band_kb / unit:.2f} MB noise band."
        )
    else:
        winner = "b" if difference > 0 else "a"
        heavier, lighter = (label_b, label_a) if difference > 0 else (label_a, label_b)
        summary = (
            f"{heavier} uses {abs(difference) / unit:.2f} MB more than {lighter} "
            f"({value_b:.2f} MB vs {value_a:.2f} MB {measure}, "
            f"{abs(difference) / reference * 100:.0f}% more)."
        )

    return {
        "winner": winner,
        "within_noise": within_noise,
        "summary": summary,
        "noise_band_kb": band_kb,
        "basis": "added" if has_added else "peak",
        "a": a.to_dict(),
        "b": b.to_dict(),
        "above_baseline": {
            "a_kb": over_a,
            "b_kb": over_b,
            "summary": (
                f"Peak resident set size is {peak_a / unit:.1f} MB and {peak_b / unit:.1f} MB, of which "
                f"about {baseline / unit:.1f} MB is the Python interpreter itself - identical for both "
                f"sides, which is why the verdict above uses the memory each algorithm actually added."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Running the benchmark
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkConfig:
    iterations: int = 5
    warmup_iterations: int = 1
    max_tests: int = 50
    enabled: bool = True
    min_relative_gap: float = 0.05


def _methodology(config: BenchmarkConfig, isolation: str, attribution: str, protocol: str) -> list[str]:
    return [
        f"{config.iterations} timed iterations per implementation, after "
        f"{config.warmup_iterations} discarded warm-up iteration(s) to avoid measuring "
        f"one-off costs such as module import and first-call allocation.",
        "Iterations are interleaved (A, B, A, B, ...) so that drift in machine load "
        "affects both implementations equally instead of favouring whichever ran first.",
        "Both implementations run in separate sandboxed processes with identical CPU, "
        "memory and wall-clock limits, the same Python interpreter, the same environment "
        "and the same test inputs.",
        f"Each iteration executes up to {config.max_tests} test cases back to back in one "
        f"process; the reported time is the sum of the per-test times measured inside the "
        f"child, so interpreter start-up is excluded from both sides.",
        (
            "Peak memory is the kernel's maximum resident set size for the child process. "
            "On Linux the high-water mark is reset before each test case, so the figure is "
            "per test rather than cumulative."
            if attribution == "per_test"
            else "Peak memory is the maximum resident set size of the whole process; this "
            "platform does not support per-test attribution, so figures are cumulative."
        ),
        f"Isolation level in force: {isolation}.",
        (
            "For stdin/stdout submissions the measured time includes interpreter start-up, "
            "because that is what such a program actually costs."
            if protocol == "stdio"
            else "Timing is measured around the call to the entry point only; any output the "
            "program prints is captured but not serialised, so the harness does not distort "
            "the measurement."
        ),
    ]


def benchmark(
    code_a: str,
    code_b: str,
    tests: list[dict],
    *,
    config: BenchmarkConfig,
    limits: Limits,
    entry_point: str = "solve",
    spread_args: bool = False,
    protocol: str = "function",
    baseline_kb: int = 0,
    label_a: str = "Algorithm A",
    label_b: str = "Algorithm B",
    on_progress: Callable[[str, float], None] | None = None,
) -> dict:
    """Benchmark both implementations over the same inputs."""
    isolation = best_isolation()
    subset = tests[: max(1, config.max_tests)]

    def progress(stage: str, fraction: float) -> None:
        if on_progress:
            on_progress(stage, fraction)

    def run_one(code: str, measured: bool) -> tuple[TimingStats, MemoryStats, list[dict]]:
        """One full pass over the subset, executed in a single sandbox process."""
        if protocol == "stdio":
            from .sandbox import run_stdio_suite

            suite = run_stdio_suite(
                code, subset, limits=limits, isolation=isolation, max_tests=len(subset)
            )
        else:
            suite = run_function_suite(
                code,
                subset,
                entry_point=entry_point,
                limits=limits,
                spread_args=spread_args,
                isolation=isolation,
                measure_only=measured,
            )
        usable = [case for case in suite.tests if case.ok]
        total = sum(case.wall_ms for case in usable)
        memory = MemoryStats(
            samples_kb=[case.peak_rss_kb for case in usable],
            added_kb=[case.added_kb for case in usable],
            baseline_kb=baseline_kb,
            attribution=suite.memory_attribution,
        )
        return TimingStats(samples=[total], unit="ms"), memory, usable

    samples_a: list[float] = []
    samples_b: list[float] = []
    memory_a: list[int] = []
    memory_b: list[int] = []
    added_a: list[int] = []
    added_b: list[int] = []
    per_test_a: list[dict] = []
    per_test_b: list[dict] = []
    attribution = "process_total"
    failures: list[str] = []

    total_steps = config.warmup_iterations + config.iterations
    step = 0
    for iteration in range(config.warmup_iterations + config.iterations):
        measured = iteration >= config.warmup_iterations
        for label, code, times, memories, per_test in (
            ("A", code_a, samples_a, memory_a, per_test_a),
            ("B", code_b, samples_b, memory_b, per_test_b),
        ):
            step += 1
            progress(f"benchmarking {label} (iteration {iteration + 1}/{total_steps})", step / (total_steps * 2))
            try:
                timing, memory, usable = run_one(code, measured)
            except Exception as exc:  # noqa: BLE001 - one side may be broken
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
                if measured:
                    continue
                continue
            attribution = memory.attribution or attribution
            if measured:
                times.append(timing.mean)
                memories.extend(memory.samples_kb)
                if label == "A":
                    added_a.extend(memory.added_kb)
                else:
                    added_b.extend(memory.added_kb)
            if label == "A":
                per_test_a = [
                    {"id": case.id, "category": case.category, "wall_ms": case.wall_ms}
                    for case in usable
                ]
            else:
                per_test_b = [
                    {"id": case.id, "category": case.category, "wall_ms": case.wall_ms}
                    for case in usable
                ]

    timing_a = TimingStats(samples=samples_a)
    timing_b = TimingStats(samples=samples_b)
    mem_a = MemoryStats(samples_kb=memory_a, added_kb=added_a, baseline_kb=baseline_kb, attribution=attribution)
    mem_b = MemoryStats(samples_kb=memory_b, added_kb=added_b, baseline_kb=baseline_kb, attribution=attribution)

    verdict = compare_timings(
        timing_a,
        timing_b,
        label_a=label_a,
        label_b=label_b,
        min_relative_gap=config.min_relative_gap,
    )
    memory_verdict = compare_memory(mem_a, mem_b, label_a=label_a, label_b=label_b)

    return {
        "iterations": config.iterations,
        "warmup_iterations": config.warmup_iterations,
        "test_count": len(subset),
        "timing": {"a": timing_a.to_dict(), "b": timing_b.to_dict()},
        "memory": memory_verdict,
        "verdict": verdict.to_dict(),
        "per_test": {"a": per_test_a, "b": per_test_b},
        "methodology": _methodology(config, isolation, attribution, protocol),
        "errors": failures,
        "protocol": protocol,
    }


def per_test_pairs(a: list[dict], b: list[dict]) -> list[dict]:
    """Join per-test timings by case id for the scatter chart."""
    index_b = {row["id"]: row for row in b}
    pairs = []
    for row in a:
        other = index_b.get(row["id"])
        if other is None:
            continue
        pairs.append(
            {
                "id": row["id"],
                "category": row["category"],
                "a_ms": row["wall_ms"],
                "b_ms": other["wall_ms"],
            }
        )
    return pairs


def summarise_series(values: Sequence[float]) -> dict:
    """Small helper used by the API to expose raw iteration samples."""
    if not values:
        return {"min": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
    }
