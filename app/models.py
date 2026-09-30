"""Request and response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Hard caps that keep a single request from becoming a denial of service.
MAX_CODE_BYTES = 200_000
MAX_USER_CASES = 20_000
MAX_ENTRY_POINT_LENGTH = 60

SUPPORTED_LANGUAGES = {"python"}


def _check_code(value: str, label: str) -> str:
    if value is None:
        return ""
    if len(value.encode("utf-8", "replace")) > MAX_CODE_BYTES:
        raise ValueError(f"{label} is too large (limit {MAX_CODE_BYTES // 1024} KB)")
    return value


class AlgorithmSpec(BaseModel):
    """One submitted implementation."""

    name: str = Field(default="", max_length=80)
    code: str = Field(default="", description="Source code of the implementation")

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        return _check_code(value, "The submitted code")


class TestConfig(BaseModel):
    """How the test suite is built."""

    # `schema` is the natural name in the API, but it collides with
    # BaseModel.schema(), so the field is called input_schema internally and
    # exposed as "schema" on the wire.
    model_config = ConfigDict(populate_by_name=True)

    input_schema: str = Field(default="int_list", alias="schema")
    count: int = Field(default=20, ge=0, le=5000, description="Random cases to generate")
    size: int = Field(default=16, ge=0, le=50_000, description="Typical input length")
    min_value: float = Field(default=-1000, ge=-10**12, le=10**12)
    max_value: float = Field(default=1000, ge=-10**12, le=10**12)
    seed: int = Field(default=12345, ge=0, le=2**31 - 1)
    strategies: list[str] = Field(
        default_factory=lambda: ["random", "edge", "boundary", "duplicates"]
    )
    large_factor: int = Field(default=25, ge=2, le=500)
    large_count: int = Field(default=2, ge=0, le=8)
    user_cases: str = Field(default="", max_length=MAX_USER_CASES)
    expected: str = Field(
        default="",
        max_length=MAX_USER_CASES,
        description='JSON object mapping test-case id to the expected output',
    )

    @field_validator("strategies")
    @classmethod
    def _validate_strategies(cls, value: list[str]) -> list[str]:
        from .testgen import STRATEGIES

        unknown = [item for item in value if item not in STRATEGIES]
        if unknown:
            raise ValueError(
                f"unknown test strategies: {', '.join(unknown)}. "
                f"Valid options: {', '.join(STRATEGIES)}"
            )
        return value


class BenchmarkOptions(BaseModel):
    enabled: bool = True
    iterations: int = Field(default=5, ge=1, le=50)
    warmup_iterations: int = Field(default=1, ge=0, le=5)
    max_tests: int = Field(default=50, ge=1, le=500)
    #: Differences below this percentage are always reported as noise.
    min_relative_gap: float = Field(default=0.05, ge=0.0, le=1.0)


class LimitsRequest(BaseModel):
    cpu_seconds: int | None = Field(default=None, ge=1, le=30)
    wall_seconds: float | None = Field(default=None, ge=0.1, le=60)
    memory_mb: int | None = Field(default=None, ge=64, le=2048)
    max_output_bytes: int | None = Field(default=None, ge=1024, le=262_144)
    allow_ctypes: bool | None = None


class RunRequest(BaseModel):
    algorithm_a: AlgorithmSpec
    algorithm_b: AlgorithmSpec
    language: str = "python"
    protocol: Literal["function", "stdio"] = "function"
    entry_point: str = Field(default="solve", max_length=MAX_ENTRY_POINT_LENGTH)
    spread_args: bool = Field(
        default=False,
        description="Call the entry point as f(*value) instead of f(value)",
    )
    tests: TestConfig = Field(default_factory=TestConfig)
    comparison_mode: Literal["auto", "strict", "float", "numeric", "unordered"] = "auto"
    rel_tolerance: float = Field(default=1e-9, ge=0.0, le=1.0)
    abs_tolerance: float = Field(default=1e-12, ge=0.0, le=1.0)
    reference_code: str | None = None
    benchmark: BenchmarkOptions = Field(default_factory=BenchmarkOptions)
    limits: LimitsRequest = Field(default_factory=LimitsRequest)

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str) -> str:
        lowered = value.strip().lower()
        if lowered not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"language {value!r} is not supported yet. "
                "Algorithm Arena currently executes Python only."
            )
        return lowered

    @field_validator("reference_code")
    @classmethod
    def _validate_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        checked = _check_code(value, "The reference implementation")
        return checked or None

    @field_validator("entry_point")
    @classmethod
    def _validate_entry(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return "solve"
        if not cleaned.isidentifier():
            raise ValueError("the entry point name must be a valid Python identifier")
        return cleaned


class JobAccepted(BaseModel):
    run_id: str
    status: str
    created_at: float


class JobState(BaseModel):
    run_id: str
    status: str  # queued | running | done | error
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    result: dict[str, Any] | None = None
    error: str | None = None
