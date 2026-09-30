"""Test-case generation.

A generic algorithm has no declared input type, so the user first picks an
**input schema** (a shape such as "list of integers" or "pair of floats") and
then which **strategies** to apply.  Everything is driven by a seeded RNG so a
run can be reproduced exactly - the seed is reported back in the results.
"""

from __future__ import annotations

import json
import math
import random
import string
from dataclasses import dataclass, field
from typing import Any, Callable

#: Hard cap on generated cases, protecting the server from absurd requests.
MAX_TEST_CASES = 2000

#: Beyond this many elements a single case stops being a *large* test and
#: starts being a denial-of-service on the sandbox.
MAX_ELEMENTS = 2_000_000


@dataclass(frozen=True)
class InputSchema:
    """Describes the kind of value a test case holds."""

    id: str
    label: str
    description: str
    #: scalar | sequence | matrix | pair
    shape: str
    #: int | float | str - the element type (also the scalar type)
    element: str
    allow_empty: bool = True
    example: str = ""
    #: For ``pair`` schemas: the shape of each of the two parts.
    #: ``scalar`` gives [a, b]; ``matrix`` gives [matrixA, matrixB].
    part_shape: str = "scalar"
    #: Conformable pairs build A as r x k and B as k x c, so that operations
    #: like matrix multiplication are actually defined on the generated input.
    conformable: bool = False


SCHEMAS: dict[str, InputSchema] = {
    "int": InputSchema(
        "int", "Integer", "A single integer.", "scalar", "int", allow_empty=False, example="42"
    ),
    "float": InputSchema(
        "float", "Float", "A single floating-point number.", "scalar", "float", allow_empty=False,
        example="3.14159",
    ),
    "str": InputSchema(
        "str", "Text", "A single string.", "scalar", "str", example='"hello world"'
    ),
    "int_list": InputSchema(
        "int_list", "List of integers", "A list of integers - the classic input for sorting and searching.",
        "sequence", "int", example="[3, 1, 4, 1, 5, 9, 2, 6]",
    ),
    "float_list": InputSchema(
        "float_list", "List of floats", "A list of floating-point numbers.", "sequence", "float",
        example="[1.5, -0.25, 3.14159]",
    ),
    "str_list": InputSchema(
        "str_list", "List of strings", "A list of short strings.", "sequence", "str",
        example='["pear", "apple", "fig"]',
    ),
    "int_matrix": InputSchema(
        "int_matrix", "Matrix of integers", "A list of equal-length rows of integers.",
        "matrix", "int", example="[[1, 2], [3, 4]]",
    ),
    "pair_int": InputSchema(
        "pair_int", "Two integers", "A pair [a, b] - the classic input for gcd, division, binary search.",
        "pair", "int", allow_empty=False, example="[48, 18]",
    ),
    "pair_float": InputSchema(
        "pair_float", "Two floats", "A pair [a, b] of floating-point numbers.", "pair", "float",
        allow_empty=False, example="[0.1, 0.2]",
    ),
    "matrix_pair": InputSchema(
        "matrix_pair", "Two matrices", "A pair [A, B] of integer matrices - the input for matrix multiplication.",
        "pair", "int", allow_empty=False, example="[[[1, 2], [3, 4]], [[5, 6], [7, 8]]]",
        part_shape="matrix", conformable=True,
    ),
}


def get_schema(schema_id: str) -> InputSchema:
    return SCHEMAS.get(schema_id, SCHEMAS["int_list"])


def describe_schemas() -> list[dict]:
    return [
        {
            "id": schema.id,
            "label": schema.label,
            "description": schema.description,
            "example": schema.example,
            "allow_empty": schema.allow_empty,
        }
        for schema in SCHEMAS.values()
    ]


# ---------------------------------------------------------------------------
# Element builders
# ---------------------------------------------------------------------------


def _element(rng: random.Random, element: str, low: float, high: float, length: int) -> Any:
    if element == "int":
        return rng.randint(int(low), int(high))
    if element == "float":
        return round(rng.uniform(low, high), 6)
    alphabet = string.ascii_lowercase[: max(2, min(26, length))]
    return "".join(rng.choice(alphabet) for _ in range(max(1, length)))


def _numeric_range(schema: InputSchema, params: GenerationParams) -> tuple[float, float]:
    """Resolve the configured value range for this schema's element type.

    Integer schemas get integer bounds (not just integral floats) so that
    every derived value - the random draws, the boundary extremes, the
    duplicate pool - stays an ``int`` and passes a strict comparison.
    """
    low, high = params.min_value, params.max_value
    if schema.element == "int":
        return int(low), int(high)
    return low, high


EDGE_INTS = (0, 1, -1, 2, -2, 10, -10, 100)
EDGE_FLOATS = (0.0, -0.0, 1.0, -1.0, 0.5, -0.5, 1e-9, 1e9)
EDGE_STRINGS = ("", "a", "A", " ", "ab", "aa", "abc", "0")


def _edge_elements(element: str) -> list[Any]:
    if element == "int":
        return list(EDGE_INTS)
    if element == "float":
        return list(EDGE_FLOATS)
    return list(EDGE_STRINGS)


def _shape_value(
    schema: InputSchema,
    rng: random.Random,
    *,
    low: float,
    high: float,
    size: int,
    elements: list[Any] | None = None,
    rows: int | None = None,
) -> Any:
    """Build one value of the schema's shape."""

    def pick() -> Any:
        if elements:
            return elements[rng.randrange(len(elements))]
        return _element(rng, schema.element, low, high, 6)

    def sequence(count: int) -> Any:
        return [pick() for _ in range(max(0, int(count)))]

    def matrix(row_count: int, col_count: int) -> Any:
        return [sequence(col_count) for _ in range(max(1, int(row_count)))]

    if schema.shape == "pair" and schema.conformable:
        # A is r x k and B is k x c, so A @ B is well defined.
        shared = max(1, int(size))
        outer = max(1, int(rows) if rows else max(1, shared // 4))
        return [matrix(outer, shared), matrix(shared, max(1, int(size)))]

    if schema.shape == "pair":
        if schema.part_shape == "scalar":
            return [pick(), pick()]
        if schema.part_shape == "matrix":
            side = max(1, min(int(size), 8))
            return [matrix(side, side), matrix(side, side)]
        return [sequence(size), sequence(size)]

    if schema.shape == "scalar":
        return pick()
    if schema.shape == "sequence":
        return sequence(size)
    return matrix(rows if rows else max(1, int(size) // 8), size)


def _element_count(value: Any) -> int:
    """Total number of atomic elements, used to report input size honestly."""
    if isinstance(value, list):
        if value and isinstance(value[0], list) and value[0] and isinstance(value[0][0], list):
            return sum(_element_count(row) for row in value)  # a pair/list of matrices
        if value and isinstance(value[0], list):
            return sum(len(row) for row in value)  # a matrix
        return len(value)
    if isinstance(value, str):
        return len(value)
    return 1


# ---------------------------------------------------------------------------
# Generation strategies
# ---------------------------------------------------------------------------


@dataclass
class GenerationParams:
    """Knobs the UI exposes for test generation."""

    schema: str = "int_list"
    count: int = 20
    size: int = 16
    min_value: float = -1000
    max_value: float = 1000
    seed: int = 12345
    strategies: list[str] = field(default_factory=lambda: ["random", "edge", "boundary", "duplicates"])
    large_factor: int = 25
    large_count: int = 2

    def clamped(self) -> "GenerationParams":
        low = float(self.min_value)
        high = float(self.max_value)
        if low > high:
            low, high = high, low
        if high - low < 2:
            high = low + 2
        return GenerationParams(
            schema=self.schema if self.schema in SCHEMAS else "int_list",
            count=max(0, min(int(self.count), MAX_TEST_CASES)),
            size=max(0, min(int(self.size), 50_000)),
            min_value=low,
            max_value=high,
            seed=int(self.seed),
            strategies=[s for s in self.strategies if s in STRATEGIES],
            large_factor=max(2, min(int(self.large_factor), 500)),
            large_count=max(0, min(int(self.large_count), 8)),
        )


STRATEGIES: dict[str, dict] = {
    "random": {
        "label": "Random",
        "description": "Uniformly random values, useful for a broad smoke test.",
    },
    "edge": {
        "label": "Edge cases",
        "description": "Small, awkward values such as 0, 1, -1 and single-character strings.",
    },
    "boundary": {
        "label": "Boundary values",
        "description": "The extremes of the configured value range and of the input length.",
    },
    "duplicates": {
        "label": "Duplicate values",
        "description": "Inputs that repeat a handful of values, which many buggy implementations mishandle.",
    },
    "empty": {
        "label": "Empty input",
        "description": "The empty sequence or empty string, where the schema allows it.",
    },
    "large": {
        "label": "Large input",
        "description": "Inputs several times larger than the configured size, to expose scaling problems.",
    },
    "user": {
        "label": "User-provided",
        "description": "Cases you typed in yourself.",
    },
}


def describe_strategies() -> list[dict]:
    return [{"id": key, **value} for key, value in STRATEGIES.items()]


def _case(case_id: str, category: str, value: Any, label: str | None = None) -> dict:
    return {
        "id": case_id,
        "label": label if label is not None else _short_label(value),
        "category": category,
        "value": value,
        "size": _element_count(value),
    }


def _short_label(value: Any) -> str:
    from .sandbox.protocol import describe

    return describe(value, limit=70)


def _gen_random(schema, rng, params, prefix) -> list[dict]:
    low, high = _numeric_range(schema, params)
    cases = []
    for index in range(params.count):
        value = _shape_value(schema, rng, low=low, high=high, size=params.size)
        cases.append(_case(f"{prefix}-r{index}", "random", value))
    return cases


def _edge_values(schema: InputSchema, params: GenerationParams) -> list[Any]:
    """Edge values that are still *valid* for the configured range.

    An edge case should be an awkward but well-formed input.  Feeding, say,
    ``-1`` to a square-root algorithm just produces an exception and teaches
    us nothing, so numeric edge values outside the user's range are dropped
    and replaced by the range's own endpoints.
    """
    pool = _edge_elements(schema.element)
    if schema.element == "str":
        return pool
    low, high = _numeric_range(schema, params)
    inside = [value for value in pool if low <= value <= high]
    if inside:
        return inside
    if schema.element == "int":
        return [int(low), int(high), 0 if low <= 0 <= high else int(low)]
    return [low, high, 0.0 if low <= 0.0 <= high else low]


def _gen_edge(schema, rng, params, prefix) -> list[dict]:
    elements = _edge_values(schema, params)
    low, high = _numeric_range(schema, params)
    if schema.shape == "scalar":
        return [_case(f"{prefix}-e{i}", "edge", value) for i, value in enumerate(elements)]
    if schema.shape == "pair":
        # Keep the parts small: these are about awkward *values*, not size.
        size = min(params.size, 4)
        return [
            _case(f"{prefix}-e{index}", "edge",
                  _shape_value(schema, rng, low=low, high=high, size=size, elements=[value]))
            for index, value in enumerate(elements[:6])
        ]
    if schema.shape == "sequence":
        # The same awkward value repeated, at a couple of lengths.
        return [
            _case(f"{prefix}-e{index}-{length}", "edge", [value] * length)
            for index, value in enumerate(elements)
            for length in (1, 3)
        ]
    # Matrices have to stay matrices, so build them through _shape_value.
    return [
        _case(f"{prefix}-e{index}", "edge",
              _shape_value(schema, rng, low=low, high=high,
                           size=min(params.size, 4), elements=[value]))
        for index, value in enumerate(elements)
    ]


def _gen_boundary(schema, rng, params, prefix) -> list[dict]:
    low, high = _numeric_range(schema, params)
    if schema.element == "str":
        extremes: list[Any] = [chr(97), chr(122), "a" * 4, ""]
    elif schema.element == "int":
        # Integers must stay integers: returning 0.0 from an int schema would
        # fail a strict comparison for no good reason.
        extremes = [low, high, low + 1, high - 1, 0]
    else:
        extremes = [low, high, low + 1, high - 1, 0.0]

    cases: list[dict] = []
    for index, value in enumerate(extremes):
        cases.append(
            _case(f"{prefix}-b{index}", "boundary",
                  _shape_value(schema, rng, low=low, high=high, size=params.size, elements=[value]))
        )
    if schema.shape == "sequence":
        for length in (0, 1, params.size, params.size + 1):
            if length > MAX_ELEMENTS:
                continue
            cases.append(
                _case(f"{prefix}-len{length}", "boundary",
                      _shape_value(schema, rng, low=low, high=high, size=length))
            )
    return cases


def _gen_duplicates(schema, rng, params, prefix) -> list[dict]:
    if schema.shape == "scalar":
        return []
    low, high = _numeric_range(schema, params)
    if schema.element == "str":
        pool: list[Any] = ["a", "b", "aa"]
    else:
        pool = [low, 0.0 if schema.element == "float" else 0, high]
    cases = []
    for index in range(3):
        length = max(2, params.size * (index + 1))
        cases.append(
            _case(f"{prefix}-d{index}", "duplicates",
                  _shape_value(schema, rng, low=low, high=high, size=length, elements=pool[: 2 + index]))
        )
    return cases


def _gen_empty(schema, rng, params, prefix) -> list[dict]:
    if not schema.allow_empty:
        return []
    if schema.shape == "sequence":
        return [_case(f"{prefix}-empty", "empty", [])]
    if schema.shape == "scalar" and schema.element == "str":
        return [_case(f"{prefix}-empty", "empty", "")]
    return []


def _gen_large(schema, rng, params, prefix) -> list[dict]:
    low, high = _numeric_range(schema, params)
    cases = []
    for index in range(params.large_count):
        size = params.size * params.large_factor
        if schema.shape == "matrix":
            size = min(size, 4000)
        size = min(size, MAX_ELEMENTS)
        value = _shape_value(schema, rng, low=low, high=high, size=size)
        cases.append(_case(f"{prefix}-L{index}", "large", value))
    return cases


_GENERATORS: dict[str, Callable] = {
    "random": _gen_random,
    "edge": _gen_edge,
    "boundary": _gen_boundary,
    "duplicates": _gen_duplicates,
    "empty": _gen_empty,
    "large": _gen_large,
}


def generate_tests(
    params: GenerationParams,
    user_cases: str = "",
) -> tuple[list[dict], dict]:
    """Build the test-case list plus a summary describing exactly how it was
    built (the summary is shown in the report, so a result is reproducible)."""
    params = params.clamped()
    schema = get_schema(params.schema)
    rng = random.Random(params.seed)

    cases: list[dict] = []
    per_strategy: dict[str, int] = {}
    for strategy in params.strategies:
        generator = _GENERATORS.get(strategy)
        if generator is None:
            continue
        produced = generator(schema, rng, params, strategy)
        per_strategy[strategy] = len(produced)
        cases.extend(produced)

    if "user" in params.strategies or user_cases.strip():
        parsed, error = parse_user_cases(user_cases)
        if error:
            raise ValueError(error)
        cases.extend(parsed)
        per_strategy["user"] = len(parsed)

    truncated = len(cases) > MAX_TEST_CASES
    if truncated:
        cases = cases[:MAX_TEST_CASES]

    summary = {
        "schema": schema.id,
        "schema_label": schema.label,
        "seed": params.seed,
        "size": params.size,
        "min_value": params.min_value,
        "max_value": params.max_value,
        "strategies": params.strategies,
        "per_strategy": per_strategy,
        "total": len(cases),
        "truncated": truncated,
        "cap": MAX_TEST_CASES,
    }
    return cases, summary


def parse_user_cases(text: str) -> tuple[list[dict], str | None]:
    """Parse user-written cases: one JSON value per line.

    As a convenience a whole JSON array is also accepted (e.g. when pasted
    from a pretty-printed file).
    """
    text = (text or "").strip()
    if not text:
        return [], None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    values: list[Any] = []
    parsed_all = True
    for index, line in enumerate(lines, start=1):
        try:
            values.append(json.loads(line))
        except ValueError as exc:
            parsed_all = False
            break
    if parsed_all:
        return [
            _case(f"user-{i}", "user", value, label=f"line {i}") for i, value in enumerate(values, start=1)
        ], None

    try:
        whole = json.loads(text)
    except ValueError as exc:
        return [], (
            f"Could not parse your test cases on line {index}: {exc.msg}. "
            "Each case must be one JSON value on its own line, for example "
            '"[3, 1, 2]" or "42".'
        )
    if isinstance(whole, list) and whole and all(isinstance(item, (list, dict)) for item in whole):
        return [
            _case(f"user-{i}", "user", value, label=f"item {i}") for i, value in enumerate(whole, start=1)
        ], None
    return [_case("user-1", "user", whole, label="single case")], None


def case_stats(cases: list[dict]) -> dict:
    """Aggregate size information used for the report's methodology section."""
    if not cases:
        return {"count": 0, "total_elements": 0, "largest": 0, "by_category": {}}
    by_category: dict[str, int] = {}
    total = 0
    largest = 0
    for case in cases:
        by_category[case["category"]] = by_category.get(case["category"], 0) + 1
        total += case.get("size", 0)
        largest = max(largest, case.get("size", 0))
    return {
        "count": len(cases),
        "total_elements": total,
        "largest": largest,
        "by_category": by_category,
    }
