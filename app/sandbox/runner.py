"""Parent-side half of the execution layer.

Responsibilities
----------------
* build a throw-away directory per run (the child's whole world),
* write the submitted program and a JSON specification into it,
* fork/exec the bootstrap with hard resource limits applied *before* ``exec``,
* supervise with a wall-clock deadline and kill the whole process group,
* read back structured results and never trust the submitted program's stdout.

The child is always a separate process - the web server never executes
submitted code inside its own interpreter.
"""

from __future__ import annotations

import ctypes
import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from ..config import PYTHON_BIN, TEMP_ROOT, Limits
from . import protocol

BOOTSTRAP_PATH = Path(__file__).resolve().parent / "bootstrap.py"

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000

#: UID/GID used when we can genuinely drop privileges.
NOBODY_UID = 65534
NOBODY_GID = 65534


class SandboxError(RuntimeError):
    """Raised when the sandbox itself fails (as opposed to user code failing)."""


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class ChildResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    wall_ms: float
    cpu_ms: float
    peak_rss_kb: int
    timed_out: bool = False
    killed_by_signal: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None

    @property
    def signal_name(self) -> str | None:
        if self.killed_by_signal is None:
            return None
        try:
            return signal.Signals(self.killed_by_signal).name
        except ValueError:  # pragma: no cover
            return f"SIG{self.killed_by_signal}"


@dataclass
class TestOutcome:
    index: int
    id: str
    label: str
    category: str
    ok: bool
    output: Any = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    wall_ms: float = 0.0
    cpu_ms: float = 0.0
    peak_rss_kb: int = 0
    added_kb: int = 0
    error: str | None = None
    error_type: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        return "pass" if self.ok else "fail"


@dataclass
class SuiteResult:
    status: str
    tests: list[TestOutcome] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    wall_ms: float = 0.0
    process_cpu_ms: float = 0.0
    process_peak_rss_kb: int = 0
    memory_attribution: str = "process_total"
    isolation: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    protocol: str = "function"
    entry_point: str = "solve"

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---------------------------------------------------------------------------
# Sandbox directory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxPaths:
    """The two directories that make up one sandbox.

    ``root`` stays owned by the server process; ``work`` is the child's entire
    writable world.  Keeping them apart is what lets the parent keep writing
    control files (stdout capture, stdin payload) *after* it has handed the
    work directory over to the unprivileged child.
    """

    root: str
    work: str

    @property
    def drops_privileges(self) -> bool:
        return os.stat(self.work).st_uid == NOBODY_UID


def make_sandbox_dir(prefix: str = "run-", drop_privileges: bool = False) -> SandboxPaths:
    """Create the control/work directory pair for one child process.

    Permission layout, chosen so both sides can do their job:

    ===============  ==============  ==========================================
    path             mode/owner     why
    ===============  ==============  ==========================================
    ``root``         o+x, we        the child must traverse it by exact path,
                                    but must not be able to list our files
    ``work``         nobody, 0755   child owns and writes it; we can still read
                                    its results back
    ===============  ==============  ==========================================
    """
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(TEMP_ROOT, 0o755)
    except OSError:  # pragma: no cover - depends on filesystem
        pass
    root = tempfile_mkdtemp(prefix=prefix, dir=str(TEMP_ROOT))
    work = os.path.join(root, "work")
    os.mkdir(work, 0o700)

    if drop_privileges:
        os.chmod(root, 0o711)  # traversable, not listable
        try:
            os.chown(work, NOBODY_UID, NOBODY_GID)
            os.chmod(work, 0o755)
        except (OSError, PermissionError):
            os.chmod(work, 0o700)
    else:
        os.chmod(root, 0o700)
        os.chmod(work, 0o700)
    return SandboxPaths(root=root, work=work)


def tempfile_mkdtemp(prefix: str, dir: str) -> str:
    import tempfile

    return tempfile.mkdtemp(prefix=prefix, dir=dir)


def _handover(paths: SandboxPaths) -> None:
    """Give the work directory to ``nobody`` once the parent has finished
    writing the files the child needs to read."""
    try:
        os.chown(paths.work, NOBODY_UID, NOBODY_GID)
        os.chmod(paths.work, 0o755)
    except (OSError, PermissionError):
        pass


def cleanup(path: str | None) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Isolation performed between fork() and exec()
# ---------------------------------------------------------------------------


def _apply_rlimits(limits: Limits) -> None:
    import resource

    cpu = max(1, int(limits.cpu_seconds))
    memory = max(64, int(limits.memory_bytes))
    file_size = max(1, int(limits.file_size_mb)) * 1024 * 1024
    pairs = [
        (resource.RLIMIT_CPU, (cpu, cpu + 1)),
        (resource.RLIMIT_AS, (memory, memory)),
        (resource.RLIMIT_FSIZE, (file_size, file_size)),
        (resource.RLIMIT_NOFILE, (limits.max_open_files, limits.max_open_files)),
        (resource.RLIMIT_CORE, (0, 0)),
    ]
    if hasattr(resource, "RLIMIT_NPROC"):
        pairs.append((resource.RLIMIT_NPROC, (limits.max_processes, limits.max_processes)))
    for which, (soft, hard) in pairs:
        try:
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError):
            # Some limits cannot be lowered by an unprivileged process; the
            # other layers still apply.
            pass


def _enter_user_namespace() -> None:
    """Create a user + network namespace (no network interfaces at all).

    Requires unprivileged user namespaces.  The parent process does not need
    any special capability: the kernel maps our own uid into the new namespace.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.unshare(CLONE_NEWUSER | CLONE_NEWNET) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"unshare(CLONE_NEWUSER|CLONE_NEWNET) failed: {os.strerror(err)}")
    uid, gid = os.getuid(), os.getgid()
    with open("/proc/self/setgroups", "w") as handle:
        handle.write("deny")
    with open("/proc/self/uid_map", "w") as handle:
        handle.write(f"{uid} {uid} 1\n")
    with open("/proc/self/gid_map", "w") as handle:
        handle.write(f"{gid} {gid} 1\n")


def _drop_to_nobody() -> None:
    os.setgroups([])
    os.setresgid(NOBODY_GID, NOBODY_GID, NOBODY_GID)
    os.setresuid(NOBODY_UID, NOBODY_UID, NOBODY_UID)
    if os.getuid() != NOBODY_UID or os.getgid() != NOBODY_GID:
        raise OSError("could not drop privileges to nobody")


def _make_preexec(limits: Limits, isolation: str):
    """Build the ``preexec_fn`` executed in the child after fork."""

    def _preexec() -> None:  # pragma: no cover - runs in the forked child
        # Nothing here may allocate much or raise: an exception aborts exec.
        try:
            if isolation in ("namespace", "namespace_nobody"):
                _enter_user_namespace()
            _apply_rlimits(limits)
            if isolation == "namespace_nobody":
                _drop_to_nobody()
            elif limits.drop_privileges and os.getuid() == 0:
                # Running as real root: always give it up, even without a
                # namespace.
                _drop_to_nobody()
        except BaseException:
            if os.getuid() == 0:
                raise
            # Degrade gracefully for unprivileged hosts; the audit hook and
            # rlimits still apply.  The capability report tells the user.
            try:
                _apply_rlimits(limits)
            except BaseException:
                pass

    return _preexec


def _child_environment(workdir: str) -> dict:
    """A minimal, reproducible environment for the child."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ARENA_SANDBOX": "1",
    }


def _kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Spawning
# ---------------------------------------------------------------------------


def spawn_child(
    argv: Sequence[str],
    *,
    workdir: str,
    limits: Limits,
    timeout: float,
    isolation: str = "none",
    stdin_data: bytes | None = None,
    control_dir: str | None = None,
) -> ChildResult:
    """Start a child, supervise it, and collect structured usage data.

    We use ``os.wait4`` so the kernel hands us the rusage of *this specific
    child* (peak RSS, CPU time) rather than an aggregate over all children.
    """
    # Capture files live in the control directory, which the parent still
    # owns even after the work directory has been handed to `nobody`.
    control = control_dir or workdir
    out_path = os.path.join(control, f".arena-stdout-{os.getpid()}")
    err_path = os.path.join(control, f".arena-stderr-{os.getpid()}")
    in_path = os.path.join(control, f".arena-stdin-{os.getpid()}")
    if stdin_data is not None:
        with open(in_path, "wb") as handle:
            handle.write(stdin_data)
        os.chmod(in_path, 0o444)
        stdin = open(in_path, "rb")
    else:
        stdin = subprocess.DEVNULL

    started = time.perf_counter()
    error: str | None = None
    returncode: int | None = None
    rusage = None
    timed_out = False

    with open(out_path, "wb") as out_file, open(err_path, "wb") as err_file:
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=stdin,
                stdout=out_file,
                stderr=err_file,
                cwd=workdir,
                env=_child_environment(workdir),
                preexec_fn=_make_preexec(limits, isolation),
                close_fds=True,
                start_new_session=True,  # own process group -> we can kill the tree
            )
        except OSError as exc:
            return ChildResult(
                returncode=None,
                stdout=b"",
                stderr=b"",
                wall_ms=(time.perf_counter() - started) * 1000,
                cpu_ms=0.0,
                peak_rss_kb=0,
                error=f"could not start the sandbox process: {exc}",
            )

        timer = threading.Timer(timeout, _kill_group, args=(proc.pid,))
        timer.daemon = True
        timer.start()
        try:
            _pid, status, rusage = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(status)
            returncode = proc.returncode
        except ChildProcessError:  # pragma: no cover - already reaped
            proc.poll()
            returncode = proc.returncode
        finally:
            timer.cancel()
    if stdin is not subprocess.DEVNULL:
        stdin.close()
    wall_ms = (time.perf_counter() - started) * 1000.0
    captured_out = _read_capped(out_path, limits.max_output_bytes)
    captured_err = _read_capped(err_path, limits.max_output_bytes)
    for scratch in (out_path, err_path, in_path):
        try:
            os.unlink(scratch)
        except OSError:
            pass

    timed_out = returncode is not None and returncode < 0 and -returncode in (
        signal.SIGKILL,
        signal.SIGXCPU,
    )
    killed_signal = -returncode if (returncode is not None and returncode < 0) else None
    if timed_out:
        error = f"execution exceeded the {timeout:g}s wall-clock limit and was killed"

    cpu_ms = 0.0
    peak_rss_kb = 0
    if rusage is not None:
        cpu_ms = (rusage.ru_utime + rusage.ru_stime) * 1000.0
        peak_rss_kb = int(rusage.ru_maxrss)

    return ChildResult(
        returncode=returncode,
        stdout=captured_out,
        stderr=captured_err,
        wall_ms=wall_ms,
        cpu_ms=cpu_ms,
        peak_rss_kb=peak_rss_kb,
        timed_out=timed_out,
        killed_by_signal=killed_signal,
        error=error,
    )


def _read_capped(path: str, limit: int) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


# ---------------------------------------------------------------------------
# Public API: function protocol
# ---------------------------------------------------------------------------


def run_function_suite(
    code: str,
    tests: list[dict],
    *,
    entry_point: str = "solve",
    limits: Limits | None = None,
    spread_args: bool = False,
    isolation: str | None = None,
    measure_only: bool = False,
    max_tests: int | None = None,
) -> SuiteResult:
    """Execute ``code`` against every test case in a single sandboxed process."""
    limits = limits or Limits()
    if isolation is None:
        from .capabilities import best_isolation

        isolation = best_isolation()
    cases = tests if max_tests is None else tests[:max_tests]

    drop = isolation == "namespace_nobody" or (limits.drop_privileges and os.getuid() == 0)
    sandbox = make_sandbox_dir(prefix="fn-", drop_privileges=drop)
    workdir = sandbox.work
    try:
        program = Path(workdir) / "program.py"
        program.write_text(code, encoding="utf-8")
        results_path = Path(workdir) / "results.json"
        spec_path = Path(workdir) / "spec.json"

        os.chmod(program, 0o644)

        per_test_timeout = min(float(limits.wall_seconds), max(0.25, float(limits.cpu_seconds) * 0.9))
        # The per-test SIGALRM fires first and yields a clean, attributable
        # timeout; RLIMIT_CPU is the harder backstop for the whole process, so
        # it is scaled by the number of test cases rather than set to the
        # per-test budget (otherwise one slow case would kill the whole suite
        # and throw away every result we had already collected).
        process_cpu = int(min(float(limits.cpu_seconds) * max(1, len(cases)) + 5, 900))
        child_limits = replace(limits, cpu_seconds=process_cpu)
        spec = {
            "protocol": 1,
            "program_file": str(program),
            "results_file": str(results_path),
            "workdir": workdir,
            "entry_point": entry_point,
            "spread_args": spread_args,
            "measure_only": measure_only,
            "tests": [{"id": case.get("id"), "label": case.get("label", ""),
                       "category": case.get("category", ""), "value": case.get("value")}
                      for case in cases],
            "per_test_timeout": per_test_timeout,
            "cpu_budget_seconds": float(limits.cpu_seconds),
            "total_time_budget": float(limits.suite_wall_seconds) * 0.85,
            "max_output_bytes": limits.max_output_bytes,
            "measure_memory_per_test": True,
            "deny_network": limits.deny_network,
            "deny_subprocess": limits.deny_subprocess,
            "allow_ctypes": limits.allow_ctypes,
            "recursion_limit": 4000,
        }
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        os.chmod(spec_path, 0o644)

        # Everything the child must read is in place; now hand the directory
        # over so the child cannot write anywhere we still control.
        if drop:
            _handover(sandbox)

        child = spawn_child(
            [sys.executable, "-I", str(BOOTSTRAP_PATH), str(spec_path)],
            workdir=workdir,
            control_dir=sandbox.root,
            limits=child_limits,
            timeout=limits.suite_wall_seconds,
            isolation=isolation,
        )

        reported = limits.to_dict()
        reported["process_cpu_seconds"] = process_cpu
        reported["per_test_wall_seconds"] = per_test_timeout
        return _collect_function_result(child, results_path, reported, isolation, entry_point)
    finally:
        cleanup(sandbox.root)


def _collect_function_result(
    child: ChildResult,
    results_path: Path,
    reported_limits: dict,
    isolation: str,
    entry_point: str,
) -> SuiteResult:
    meta = _parse_meta(child.stderr)
    result = SuiteResult(
        status="error",
        wall_ms=child.wall_ms,
        process_cpu_ms=child.cpu_ms,
        # The child's own VmHWM, not the rusage the parent collected: a forked
        # child inherits the parent's ru_maxrss, which would swamp the real
        # figure.  See bootstrap._peak_rss_kb for the full explanation.
        process_peak_rss_kb=int(meta.get("peak_rss_kb") or 0) or child.peak_rss_kb,
        isolation={"level": isolation, "returncode": child.returncode,
                   "timed_out": child.timed_out, "signal": child.signal_name,
                   "peak_rss_source": meta.get("peak_rss_source", "ru_maxrss")},
        limits=reported_limits,
        entry_point=entry_point,
    )

    payload = None
    if results_path.exists():
        try:
            with open(results_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            payload = None

    if payload is None:
        result.status = "timeout" if child.timed_out else "crash"
        result.error = child.error or _describe_crash(child, reported_limits)
        return result

    result.memory_attribution = str(payload.get("meta", {}).get("memory_attribution", "process_total"))
    if payload.get("status") != "ok":
        result.status = "timeout" if child.timed_out else "error"
        result.error = payload.get("error") or child.error or "the submitted program could not be loaded"
        result.error_type = payload.get("error_type")
        return result

    for record in payload.get("tests", []):
        result.tests.append(
            TestOutcome(
                index=int(record.get("index", 0)),
                id=str(record.get("id", "")),
                label=str(record.get("label", "")),
                category=str(record.get("category", "")),
                ok=bool(record.get("ok")),
                output=protocol.decode(record.get("output")),
                stdout=str(record.get("stdout", ""))[: reported_limits["max_output_bytes"]],
                stderr=str(record.get("stderr", ""))[: reported_limits["max_output_bytes"]],
                truncated=bool(record.get("truncated")),
                wall_ms=float(record.get("wall_ms", 0.0)),
                cpu_ms=float(record.get("cpu_ms", 0.0)),
                peak_rss_kb=int(record.get("peak_rss_kb", 0)),
                added_kb=int(record.get("added_kb", 0)),
                error=record.get("error"),
                error_type=record.get("error_type"),
            )
        )
    result.status = "ok"
    return result


def _parse_meta(stderr: bytes) -> dict:
    """Recover the trusted metadata block the bootstrap appended to stderr."""
    text = stderr.decode("utf-8", "replace")
    index = text.rfind("\x1eARENA-META:")
    if index < 0:
        return {}
    tail = text[index + len("\x1eARENA-META:") :]
    end = tail.find("\x1e")
    blob = tail[:end] if end >= 0 else tail.splitlines()[0] if tail else ""
    try:
        return json.loads(blob)
    except ValueError:
        return {}


def _strip_meta(text: str) -> str:
    """Remove our internal metadata block from output shown to the user."""
    marker = "\x1eARENA-META:"
    while marker in text:
        head, _, rest = text.partition(marker)
        _, newline, tail = rest.partition("\n")
        text = head + tail
    return text


def _describe_crash(child: ChildResult, limits: "Limits | dict") -> str:
    memory_mb = limits["memory_mb"] if isinstance(limits, dict) else limits.memory_mb
    cpu_seconds = limits["cpu_seconds"] if isinstance(limits, dict) else limits.cpu_seconds
    stderr = child.stderr.decode("utf-8", "replace").strip()
    parts = []
    if child.signal_name in ("SIGKILL", "SIGXCPU"):
        parts.append(
            "the process was killed - it most likely exceeded the "
            f"{memory_mb} MB memory limit or the {cpu_seconds}s CPU limit"
        )
    elif child.returncode not in (0, None):
        parts.append(f"the process exited with code {child.returncode}")
    if stderr:
        parts.append(stderr[-600:])
    return "\n".join(parts) or "the process produced no diagnostic output"


# ---------------------------------------------------------------------------
# Public API: stdin/stdout protocol
# ---------------------------------------------------------------------------


def run_stdio_suite(
    code: str,
    tests: list[dict],
    *,
    limits: Limits | None = None,
    isolation: str | None = None,
    max_tests: int | None = None,
) -> SuiteResult:
    """Execute a plain script once per test case, feeding JSON on stdin.

    This mirrors how algorithms are submitted to judges, so it is the most
    familiar option - but it costs one interpreter start-up per test case.
    """
    limits = limits or Limits()
    if isolation is None:
        from .capabilities import best_isolation

        isolation = best_isolation()
    cases = tests if max_tests is None else tests[:max_tests]

    result = SuiteResult(
        status="ok",
        isolation={"level": isolation},
        limits=limits.to_dict(),
        protocol="stdio",
    )
    drop = isolation == "namespace_nobody" or (limits.drop_privileges and os.getuid() == 0)
    sandbox = make_sandbox_dir(prefix="io-", drop_privileges=drop)
    workdir = sandbox.work
    try:
        program = Path(workdir) / "program.py"
        program.write_text(code, encoding="utf-8")
        spec_path = Path(workdir) / "spec.json"
        spec = {
            "protocol": 1,
            "mode": "stdio",
            "program_file": str(program),
            "results_file": str(Path(workdir) / "results.json"),
            "workdir": workdir,
            "per_test_timeout": min(float(limits.wall_seconds), max(0.25, float(limits.cpu_seconds) * 0.9)),
            "deny_network": limits.deny_network,
            "deny_subprocess": limits.deny_subprocess,
            "allow_ctypes": limits.allow_ctypes,
        }
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        os.chmod(program, 0o644)
        os.chmod(spec_path, 0o644)
        if drop:
            _handover(sandbox)

        for case in cases:
            outcome = _run_stdio_case(spec_path, case, limits, sandbox, isolation, result)
            result.tests.append(outcome)
            if outcome.error and result.status == "ok":
                result.status = "completed_with_errors"
    finally:
        cleanup(sandbox.root)
    return result


def _run_stdio_case(
    spec_path: Path,
    case: dict,
    limits: Limits,
    sandbox: SandboxPaths,
    isolation: str,
    result: SuiteResult,
) -> TestOutcome:
    payload = json.dumps({"input": case.get("value"), "id": case.get("id")})
    child = spawn_child(
        [sys.executable, "-I", str(BOOTSTRAP_PATH), str(spec_path)],
        workdir=sandbox.work,
        control_dir=sandbox.root,
        limits=limits,
        timeout=min(limits.wall_seconds, max(1.0, limits.cpu_seconds * 2)),
        isolation=isolation,
        stdin_data=payload.encode("utf-8"),
    )
    meta = _parse_meta(child.stderr)
    child.stderr = _strip_meta(child.stderr.decode("utf-8", "replace")).encode("utf-8", "replace")
    # The child's own VmHWM, not the parent's ru_maxrss: see bootstrap._peak_rss_kb.
    peak_kb = int(meta.get("peak_rss_kb") or child.peak_rss_kb)
    result.wall_ms += child.wall_ms
    result.process_cpu_ms += child.cpu_ms
    result.process_peak_rss_kb = max(result.process_peak_rss_kb, peak_kb)

    outcome = TestOutcome(
        index=int(case.get("index", 0)),
        id=str(case.get("id", "")),
        label=str(case.get("label", "")),
        category=str(case.get("category", "")),
        ok=False,
        stdout=child.stdout.decode("utf-8", "replace")[: limits.max_output_bytes],
        stderr=_strip_meta(child.stderr.decode("utf-8", "replace"))[: limits.max_output_bytes],
        wall_ms=child.wall_ms,
        cpu_ms=child.cpu_ms,
        peak_rss_kb=peak_kb,
    )
    if child.timed_out:
        outcome.error = child.error
        outcome.error_type = "Timeout"
        return outcome
    if child.signal_name in ("SIGKILL", "SIGXCPU"):
        outcome.error = (
            f"killed by {child.signal_name} - most likely the {limits.memory_mb} MB memory "
            f"limit or the {limits.cpu_seconds}s CPU limit was exceeded"
        )
        outcome.error_type = child.signal_name
        return outcome
    if child.returncode != 0:
        outcome.error = _describe_crash(child, limits)
        outcome.error_type = "RuntimeError"
        return outcome

    # A well-behaved submission prints JSON on the last line of stdout.
    text = outcome.stdout.strip()
    parsed, is_json = _parse_output(text)
    outcome.output = parsed
    outcome.ok = True
    if not is_json and text:
        outcome.stderr = (outcome.stderr + f"\n[output is not JSON: {text[:200]}]").strip()
    return outcome


def _parse_output(text: str) -> tuple[Any, bool]:
    if not text:
        return None, False
    for candidate in (text.splitlines()[-1] if "\n" in text else text, text):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return protocol.decode(json.loads(candidate)), True
        except ValueError:
            continue
    return text, False


def run_single_process(
    code: str,
    *,
    limits: Limits | None = None,
    isolation: str | None = None,
    payload: Any = None,
    timeout: float | None = None,
) -> ChildResult:
    """Run one script once through the sandbox - used for baseline probes."""
    limits = limits or Limits()
    if isolation is None:
        from .capabilities import best_isolation

        isolation = best_isolation()
    drop = isolation == "namespace_nobody" or (limits.drop_privileges and os.getuid() == 0)
    sandbox = make_sandbox_dir(prefix="one-", drop_privileges=drop)
    try:
        program = Path(sandbox.work) / "program.py"
        program.write_text(code, encoding="utf-8")
        spec_path = Path(sandbox.work) / "spec.json"
        spec_path.write_text(
            json.dumps({
                "protocol": 1,
                "mode": "stdio",
                "program_file": str(program),
                "results_file": str(Path(sandbox.work) / "results.json"),
                "workdir": sandbox.work,
                "per_test_timeout": float(timeout or 20.0),
                "deny_network": limits.deny_network,
                "deny_subprocess": limits.deny_subprocess,
                "allow_ctypes": limits.allow_ctypes,
            }),
            encoding="utf-8",
        )
        os.chmod(program, 0o644)
        os.chmod(spec_path, 0o644)
        if drop:
            _handover(sandbox)
        data = None
        if payload is not None:
            data = json.dumps({"input": payload}).encode("utf-8")
        child = spawn_child(
            [sys.executable, "-I", str(BOOTSTRAP_PATH), str(spec_path)],
            workdir=sandbox.work,
            control_dir=sandbox.root,
            limits=limits,
            timeout=timeout or min(limits.wall_seconds, max(1.0, limits.cpu_seconds * 2)),
            isolation=isolation,
            stdin_data=data,
        )
        meta = _parse_meta(child.stderr)
        child.peak_rss_kb = int(meta.get("peak_rss_kb") or 0) or child.peak_rss_kb
        child.stderr = _strip_meta(child.stderr.decode("utf-8", "replace")).encode("utf-8", "replace")
        return child
    finally:
        cleanup(sandbox.root)


def probe_memory_baseline(limits: Limits | None = None) -> dict:
    """Measure the interpreter's own footprint.

    Peak RSS always includes the Python runtime, so reporting it alone is
    misleading for small algorithms.  Measuring a trivial program gives the
    baseline a reader can subtract to see what the algorithm itself costs.
    """
    child = run_single_process("x = 1\nprint(x)\n", limits=limits, timeout=20.0)
    return {
        "peak_rss_kb": child.peak_rss_kb,
        "wall_ms": child.wall_ms,
        "note": "includes the Python interpreter itself",
    }
