# Methodology

Everything on this page is also printed in the UI, under the results. If the
application and this document ever disagree, the application is wrong and
should be fixed — that is the point of writing it down.

## 1. What a passing run does and does not mean

A run that produces `1000/1000` matching outputs establishes:

> The two implementations returned the same value, under this comparison mode,
> for these 1000 inputs, in this environment.

It does **not** establish that they compute the same function. An algorithm that
returns the wrong answer on every input matches a correct one perfectly. So the
application:

* always labels the result **empirical equivalence**, never "equivalent";
* always prints the caveat sentence, in full, next to the verdict;
* prints the seed, the mode and the case count, because "they agree" means
  nothing without knowing *on what*.

The classic illustration: `return sum(xs)` and `return sum(xs[:len(xs)//2])`
agree on every input of length 1. Adding a duplicate-value case and a
large-input case is what breaks the illusion — which is why `duplicates` and
`large` are first-class generation strategies and not extras.

## 2. Test generation

Ten input schemas (integer lists, float pairs, strings, matrices, pairs…) each
declare a valid range. A strategy produces candidates, and the schema filters
them, so an "edge case" like `0` is only emitted for a schema where `0` is in
range. Results are deterministic given the seed.

The edge categories exist to attack different classes of bug:

| Category | Typical bug it finds |
|---|---|
| random | Wrong formula on typical data |
| edge | Off-by-one at `0`/`1`/`-1`, empty string, single element |
| boundary | Range errors: `<` vs `<=`, missing last element |
| duplicates | Accidental set-like behaviour, unstable sorting, dedup bugs |
| empty | Missing base case; unguarded `max()` on an empty sequence |
| large | Quadratic blow-ups, recursion limits, integer overflow assumptions |
| user | The case *you* thought of |

## 3. Output comparison

Modes, in the order the UI presents them:

* **auto** — infer from the observed values. Report what was inferred.
* **strict** — exact equality *and* exact type. `1` ≠ `1.0`; `True` ≠ `1`;
  `NaN` ≠ `NaN` (per IEEE-754, and the way `sorted()` and `set()` behave).
* **float** — numbers equal if `|a-b| ≤ max(atol, rtol·max(|a|,|b|))`.
* **numeric** — as `float`, but numeric strings (`"3.0"`) compare numerically.
* **unordered** — equal as multisets, ignoring order.

For non-numeric values, auto falls back to a normalized comparison: floats are
formatted to a fixed precision before equality so that the same number produced
by two routes (`0.1+0.2` vs `0.3`) does not read as a bug.

When values differ, the report gives the **path** to the first difference using
JSON-path syntax (`$.result[3].total`), both values, and the input that
produced them — which is usually enough to diagnose the bug without re-running.

## 4. Accuracy against a reference

For numerical algorithms, matching is not correctness: two implementations can
be wrong in the same way. Supplying a reference implementation switches the
scoring to:

* max absolute error, mean absolute error
* RMSE
* max relative error
* count of inputs within tolerance, and the worst offender

Inputs are drawn from the same generated suite, so the reference sees exactly
what the submissions see.

## 5. Benchmarking

Benchmarking runs **only when every comparison passed**, and the report says so
when it is skipped. Speed is only meaningful between two things that do the
same work.

The procedure:

1. Both implementations are loaded in a fresh child process, under identical
   resource limits.
2. **Warm-up iterations run first and are discarded** — they populate caches,
   trigger imports and let the CPU settle.
3. Remaining iterations **alternate A, B, A, B…**. If the machine gets busy
   halfway through, the noise is shared instead of being attributed to
   whichever side happened to run during the spike.
4. Per-run wall time comes from `time.perf_counter_ns()` around the call
   itself, so process start-up is excluded.
5. Memory is the child's own `VmHWM` (peak resident set) minus the interpreter
   baseline, reported as *peak* and *added* memory. See
   [ARCHITECTURE.md](ARCHITECTURE.md#why-measure-memory-from-inside-the-child).

Reported statistics per side: **mean, min, max, median, standard deviation**,
plus the iteration count. Mean alone hides a bimodal distribution; the spread
statistics are what tell you whether the measurement is trustworthy at all.

### The noise band

> A 4% difference measured on a shared CI runner is not a 4% difference.

The comparison uses two signals, and requires both before it will call a winner:

1. **Relative spread** — the observed spread of repeated runs of the *same*
   implementation. If a run varies by 15% run to run, a 4% gap between A and B
   is noise. This is `noise_floor_percent`.
2. **A gap threshold** — a `min_relative_gap` (default 5%) below which a
   difference is not reported as meaningful, regardless of how stable the
   measurements looked.

When the gap between the two means falls inside the band, the verdict is
**"no significant difference"** and no winner is named. The exact band, the
observed spread and the gap are all printed, so a reader can disagree with the
threshold and still see the data.

The band is deliberately conservative. Over-reporting noise as a win is the
most common way a benchmark lies.

### Two known sources of error we do not correct for

* **CPU frequency scaling and thermal behaviour** — not modelled.
* **JIT-like warm-up** — irrelevant for CPython, relevant if a second runtime
  is ever added.

Both are handled by the same response: discard warm-ups, interleave, and refuse
to call small gaps.

## 6. Reproducibility

Every run records the seed, the schema, the strategies, the case count, the
comparison mode, the tolerances, the iteration count and the limits. Re-running
with the same configuration re-generates byte-identical inputs, so any
difference in the result is a real difference and not a different test suite.
