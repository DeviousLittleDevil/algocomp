"""Built-in example algorithm pairs.

These exist so the application is useful the moment it loads, and so each
generation strategy and comparison mode has a case that demonstrates it.
Each entry carries the settings that make the example meaningful (input
schema, comparison mode, tolerances) so the UI can apply them in one click.
"""

from __future__ import annotations

from typing import Any

SORT_BUILTIN = '''\
def solve(data):
    """Return the input in ascending order."""
    return sorted(data)
'''

SORT_MERGE = '''\
def solve(data):
    """Bottom-up merge sort - the same result as sorted(), O(n log n)."""
    values = list(data)
    if len(values) < 2:
        return values
    width = 1
    while width < len(values):
        merged = []
        for start in range(0, len(values), 2 * width):
            left = values[start:start + width]
            right = values[start + width:start + 2 * width]
            i = j = 0
            while i < len(left) and j < len(right):
                if left[i] <= right[j]:
                    merged.append(left[i])
                    i += 1
                else:
                    merged.append(right[j])
                    j += 1
            merged.extend(left[i:])
            merged.extend(right[j:])
        values = merged
        width *= 2
    return values
'''

SORT_BUGGY = '''\
def solve(data):
    """A buggy "sort" that silently drops repeated values."""
    seen = set()
    out = []
    for value in data:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
'''

GCD_EUCLID = '''\
def solve(data):
    """Greatest common divisor by the Euclidean algorithm."""
    a, b = abs(int(data[0])), abs(int(data[1]))
    while b:
        a, b = b, a % b
    return a
'''

GCD_BINARY = '''\
def solve(data):
    """Same result via Stein's binary GCD - shifts and subtraction only."""
    a, b = abs(int(data[0])), abs(int(data[1]))
    if a == 0:
        return b
    if b == 0:
        return a
    # Pull out the factors of two that both numbers share.
    common_twos = 0
    while a % 2 == 0 and b % 2 == 0:
        a //= 2
        b //= 2
        common_twos += 1
    while a % 2 == 0:
        a //= 2
    while b != 0:
        while b % 2 == 0:
            b //= 2
        if a > b:
            a, b = b, a
        b -= a
    return a << common_twos
'''

SIEVE = '''\
def solve(data):
    """All primes up to n, using the Sieve of Eratosthenes."""
    n = int(data)
    if n < 2:
        return []
    flags = bytearray([1]) * (n + 1)
    flags[0:2] = b"\\x00\\x00"
    limit = int(n ** 0.5)
    for candidate in range(2, limit + 1):
        if flags[candidate]:
            flags[candidate * candidate :: candidate] = bytearray(
                len(range(candidate * candidate, n + 1, candidate))
            )
    return [i for i, is_prime in enumerate(flags) if is_prime]
'''

TRIAL_DIVISION = '''\
def solve(data):
    """Same output as the sieve, by trial division - much slower."""
    n = int(data)
    primes = []
    for candidate in range(2, n + 1):
        limit = int(candidate ** 0.5)
        for divisor in range(2, limit + 1):
            if candidate % divisor == 0:
                break
        else:
            primes.append(candidate)
    return primes
'''

MATRIX_NAIVE = '''\
def solve(data):
    """Multiply two matrices with the textbook triple loop."""
    left, right = data
    rows, shared = len(left), len(right)
    cols = len(right[0]) if right else 0
    result = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        for k in range(shared):
            factor = left[i][k]
            if factor == 0:
                continue
            for j in range(cols):
                result[i][j] += factor * right[k][j]
    return result
'''

MATRIX_TRANSPOSED = '''\
def solve(data):
    """Same product, but iterating the inner loop over a transposed copy.

    Identical results; better cache locality, so it is measurably faster on
    larger inputs.
    """
    left, right = data
    rows, cols = len(left), len(right[0]) if right else 0
    transposed = [list(column) for column in zip(*right)]
    result = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        row = left[i]
        out = result[i]
        for j in range(cols):
            column = transposed[j]
            total = 0
            for k in range(len(column)):
                total += row[k] * column[k]
            out[j] = total
    return result
'''

SQRT_NEWTON = '''\
def solve(data):
    """Square root by Newton's method - iterative, and approximate."""
    value = float(data)
    if value < 0:
        raise ValueError("square root is undefined for negative numbers")
    if value == 0:
        return 0.0
    guess = value / 2 if value < 1 else value
    for _ in range(60):
        guess = 0.5 * (guess + value / guess)
    return guess
'''

SQRT_REFERENCE = '''\
import math

def solve(data):
    """The reference: the standard library's correctly-rounded result."""
    return math.sqrt(float(data))
'''

MATMUL_REFERENCE = '''\
def solve(data):
    """Reference implementation for validating a numerical algorithm."""
    left, right = data
    return [
        [sum(a * b for a, b in zip(row, column)) for column in zip(*right)]
        for row in left
    ]
'''


EXAMPLES: list[dict[str, Any]] = [
    {
        "id": "sorting-equivalent",
        "title": "Sorting: library vs. hand-written",
        "blurb": "sorted() against a bottom-up merge sort. Identical results, so the run "
        "goes on to compare their speed.",
        "category": "Deterministic",
        "protocol": "function",
        "entry_point": "solve",
        "comparison_mode": "strict",
        "rel_tolerance": 1e-9,
        "abs_tolerance": 1e-12,
        "tests": {
            "schema": "int_list",
            "count": 25,
            "size": 300,
            "min_value": -1000,
            "max_value": 1000,
            "seed": 4242,
            "strategies": ["random", "edge", "boundary", "duplicates", "empty", "large"],
            "large_factor": 20,
            "large_count": 1,
            "user_cases": "",
        },
        "benchmark": {"enabled": True, "iterations": 7, "warmup_iterations": 1, "max_tests": 40},
        "code_a": SORT_BUILTIN,
        "code_b": SORT_MERGE,
        "reference_code": "",
        "names": ["sorted()", "Merge sort"],
    },
    {
        "id": "sorting-buggy",
        "title": "Sorting: spotting a bug",
        "blurb": "A 'sort' that drops duplicates. Run this to see how mismatches, per-category "
        "results and example differences are reported.",
        "category": "Bug hunt",
        "protocol": "function",
        "entry_point": "solve",
        "comparison_mode": "strict",
        "rel_tolerance": 1e-9,
        "abs_tolerance": 1e-12,
        "tests": {
            "schema": "int_list",
            "count": 20,
            "size": 40,
            "min_value": -50,
            "max_value": 50,
            "seed": 7,
            "strategies": ["random", "duplicates", "edge", "empty"],
            "large_factor": 10,
            "large_count": 1,
            "user_cases": "",
        },
        "benchmark": {"enabled": True, "iterations": 3, "warmup_iterations": 1, "max_tests": 20},
        "code_a": SORT_BUILTIN,
        "code_b": SORT_BUGGY,
        "reference_code": "",
        "names": ["sorted()", "Dedup 'sort'"],
    },
    {
        "id": "gcd-equivalent",
        "title": "GCD: two very different algorithms",
        "blurb": "Euclid's algorithm against Stein's binary GCD. Same answer, quite different "
        "work - ideal for exercising the pairwise input schema.",
        "category": "Deterministic",
        "protocol": "function",
        "entry_point": "solve",
        "comparison_mode": "strict",
        "rel_tolerance": 1e-9,
        "abs_tolerance": 1e-12,
        "tests": {
            "schema": "pair_int",
            "count": 40,
            "size": 8,
            "min_value": -10**6,
            "max_value": 10**6,
            "seed": 99,
            "strategies": ["random", "edge", "boundary", "duplicates"],
            "large_factor": 10,
            "large_count": 1,
            "user_cases": "",
        },
        "benchmark": {"enabled": True, "iterations": 7, "warmup_iterations": 1, "max_tests": 60},
        "code_a": GCD_EUCLID,
        "code_b": GCD_BINARY,
        "reference_code": "",
        "names": ["Euclid", "Binary GCD"],
    },
    {
        "id": "primes-speed",
        "title": "Primes: sieve vs. trial division",
        "blurb": "The same list of primes, produced by a sieve and by trial division. Expect a "
        "clear, large performance gap - far outside the noise band.",
        "category": "Performance",
        "protocol": "function",
        "entry_point": "solve",
        "comparison_mode": "strict",
        "rel_tolerance": 1e-9,
        "abs_tolerance": 1e-12,
        "tests": {
            "schema": "int",
            "count": 8,
            "size": 8,
            "min_value": 2,
            "max_value": 4000,
            "seed": 11,
            "strategies": ["random", "edge", "boundary"],
            "large_factor": 5,
            "large_count": 2,
            "user_cases": "",
        },
        "benchmark": {"enabled": True, "iterations": 5, "warmup_iterations": 1, "max_tests": 12},
        "code_a": SIEVE,
        "code_b": TRIAL_DIVISION,
        "reference_code": "",
        "names": ["Sieve", "Trial division"],
    },
    {
        "id": "matrix-multiplication",
        "title": "Matrix multiplication: cache-friendly variant",
        "blurb": "Identical products from two different loop orders. Shows how a performance "
        "win shows up without changing a single output.",
        "category": "Performance",
        "protocol": "function",
        "entry_point": "solve",
        "comparison_mode": "strict",
        "rel_tolerance": 1e-9,
        "abs_tolerance": 1e-12,
        "tests": {
            "schema": "matrix_pair",
            "count": 4,
            "size": 60,
            "min_value": -9,
            "max_value": 9,
            "seed": 5,
            "strategies": ["random", "edge"],
            "large_factor": 3,
            "large_count": 2,
            "user_cases": "",
        },
        "benchmark": {"enabled": True, "iterations": 5, "warmup_iterations": 1, "max_tests": 8},
        "code_a": MATRIX_NAIVE,
        "code_b": MATRIX_TRANSPOSED,
        "reference_code": MATMUL_REFERENCE,
        "names": ["Triple loop", "Transposed"],
    },
    {
        "id": "sqrt-accuracy",
        "title": "Square root: accuracy against a reference",
        "blurb": "Newton's method against math.sqrt. Outputs differ slightly, so this one needs "
        "a float tolerance - and a reference implementation to score error against.",
        "category": "Numerical",
        "protocol": "function",
        "entry_point": "solve",
        "comparison_mode": "float",
        "rel_tolerance": 1e-9,
        "abs_tolerance": 1e-12,
        "tests": {
            "schema": "float",
            "count": 20,
            "size": 8,
            "min_value": 1e-3,
            "max_value": 1e6,
            "seed": 2024,
            "strategies": ["random", "edge", "boundary"],
            "large_factor": 5,
            "large_count": 1,
            "user_cases": "",
        },
        "benchmark": {"enabled": True, "iterations": 5, "warmup_iterations": 1, "max_tests": 30},
        "code_a": SQRT_NEWTON,
        "code_b": SQRT_REFERENCE,
        "reference_code": SQRT_REFERENCE,
        "names": ["Newton's method", "math.sqrt"],
    },
]


def list_examples() -> list[dict[str, Any]]:
    """Metadata only - the UI fetches the full payload for one example."""
    return [
        {
            key: value
            for key, value in example.items()
            if key not in ("code_a", "code_b", "reference_code")
        }
        for example in EXAMPLES
    ]


def get_example(example_id: str) -> dict[str, Any] | None:
    for example in EXAMPLES:
        if example["id"] == example_id:
            return example
    return None


DEFAULT_CODE_A = SORT_BUILTIN
DEFAULT_CODE_B = SORT_MERGE
