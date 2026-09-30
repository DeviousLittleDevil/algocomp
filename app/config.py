"""Central configuration: every tunable default lives here.

Keeping the limits in one module makes the security posture of the app easy to
audit and easy to change.  Values are deliberately conservative defaults; the
UI can ask for different ones per run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict, replace
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
STATIC_DIR = BASE_DIR / "static"

#: Interpreter used to run submitted (untrusted) Python programs.
PYTHON_BIN = os.environ.get("ARENA_PYTHON", "python3")


@dataclass(frozen=True)
class Limits:
    """Resource limits applied to every submitted program.

    These are enforced twice: in the parent process via ``setrlimit`` before
    ``exec`` (so interpreter start-up counts too) and again inside the sandbox
    bootstrap for defence in depth.
    """

    #: Hard CPU seconds for a single test case (SIGXCPU / SIGKILL).
    cpu_seconds: int = 5
    #: Wall-clock seconds for a single test case; the real backstop on timeout.
    wall_seconds: float = 10.0
    #: Wall-clock seconds for a whole suite invocation.
    suite_wall_seconds: float = 120.0
    #: Address-space limit in MiB (RLIMIT_AS).
    memory_mb: int = 256
    #: RLIMIT_FSIZE in MiB - prevents runaway disk writes.
    file_size_mb: int = 8
    #: RLIMIT_NPROC - limits forked children.
    max_processes: int = 64
    #: RLIMIT_NOFILE - keeps file-descriptor usage bounded.
    max_open_files: int = 64
    #: Maximum captured stdout/stderr per test case (bytes).
    max_output_bytes: int = 64 * 1024
    #: Block socket creation / connections from submitted code.
    deny_network: bool = True
    #: Block subprocess spawning from submitted code.
    deny_subprocess: bool = True
    #: Block the ``ctypes`` module (it can call arbitrary native code).
    allow_ctypes: bool = False
    #: Drop to an unprivileged uid inside a user namespace when possible.
    drop_privileges: bool = True

    @property
    def memory_bytes(self) -> int:
        return self.memory_mb * 1024 * 1024

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_LIMITS = Limits()

#: Hard ceilings a client may request through the API (defence in depth so a
#: caller cannot ask the server for an unbounded sandbox).
MAX_ALLOWED_LIMITS = Limits(
    cpu_seconds=30,
    wall_seconds=60.0,
    suite_wall_seconds=600.0,
    memory_mb=2048,
    file_size_mb=32,
    max_processes=256,
    max_open_files=256,
    max_output_bytes=256 * 1024,
)

#: How many jobs we keep in memory before evicting the oldest finished ones.
MAX_RETAINED_JOBS = 50

#: How long a finished job stays retrievable.
JOB_TTL_SECONDS = 60 * 60

#: Worker threads used to run comparisons.  One per core, capped, because the
#: real work happens in child processes and we want fair CPU allocation.
JOB_WORKERS = max(1, min(4, (os.cpu_count() or 2)))

#: Where temporary sandboxes are created.
TEMP_ROOT = Path(os.environ.get("ARENA_TMP", "/tmp/algorithm-arena"))


def clamp_limits(requested: dict | None) -> Limits:
    """Merge user-supplied limits with defaults, clamped to the hard ceilings."""
    if not requested:
        return DEFAULT_LIMITS
    clean = {}
    for key, value in requested.items():
        if key not in asdict(DEFAULT_LIMITS) or value is None:
            continue
        clean[key] = value
    limits = replace(DEFAULT_LIMITS, **clean)
    hard = asdict(MAX_ALLOWED_LIMITS)
    clamped = {}
    for key, value in asdict(limits).items():
        ceiling = hard[key]
        if isinstance(value, bool):
            clamped[key] = bool(value)
        elif isinstance(value, int):
            clamped[key] = max(1, min(int(value), int(ceiling)))
        elif isinstance(value, float):
            clamped[key] = max(0.1, min(float(value), float(ceiling)))
        else:
            clamped[key] = value
    return Limits(**clamped)
