"""The untrusted-code execution layer.

Nothing in this package imports from the FastAPI application: the sandbox must
stay usable (and testable) on its own, and it is the only place that is
allowed to touch user-supplied source code.
"""

from .runner import (  # noqa: F401
    ChildResult,
    Limits,
    SandboxError,
    SuiteResult,
    TestOutcome,
    make_sandbox_dir,
    probe_memory_baseline,
    run_function_suite,
    run_single_process,
    run_stdio_suite,
    spawn_child,
)
from .capabilities import CapabilityReport, best_isolation, probe_sandbox  # noqa: F401

__all__ = [
    "CapabilityReport",
    "ChildResult",
    "Limits",
    "SandboxError",
    "SuiteResult",
    "TestOutcome",
    "best_isolation",
    "make_sandbox_dir",
    "probe_memory_baseline",
    "probe_sandbox",
    "run_function_suite",
    "run_single_process",
    "run_stdio_suite",
    "spawn_child",
]
