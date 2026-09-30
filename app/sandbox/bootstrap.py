"""Trusted bootstrap executed *inside* the sandboxed child process.

Why this file exists
--------------------
Submitted code is untrusted.  It must not be able to interfere with the
harness that is measuring it, so the harness lives in this file, which is part
of the repository and is never derived from user input.  The user program is
loaded from a separate file and can only influence *data*, never the harness.

It is started as ``python3 -I bootstrap.py <spec.json>``:

* ``-I`` (isolated mode) ignores ``PYTHONPATH``, user site-packages and any
  ``sitecustomize`` that might be lying around, so the parent process's
  environment cannot inject code into the child.
* The specification is passed as a JSON file so nothing user-controlled ever
  reaches the command line.

Responsibilities
----------------
1. Install an audit hook that denies network access, subprocess creation and
   writes outside the sandbox directory.
2. Re-assert the resource limits (defence in depth behind ``preexec_fn``).
3. Run the requested protocol (call an entry point per test, or feed stdin).
4. Measure per-test wall time, CPU time and peak RSS.
5. Emit machine-readable results plus a metadata block the parent trusts more
   than anything the user program printed.

The module is intentionally self-contained: importing anything from the
``app`` package would be pointless here because ``-I`` strips the script's
directory from ``sys.path`` anyway.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import os
import signal
import sys
import time
import traceback
import types

#: Marker used to hand structured metadata to the parent over stderr.  The
#: parent takes the *last* occurrence on the line, so a submitted program
#: cannot hide ours by printing a fake one earlier.
META_MARKER = "\x1eARENA-META:"

PROTOCOL_VERSION = 1

#: Values that JSON cannot represent natively are wrapped in a single-key
#: object so the wire format stays strict JSON (the browser can parse it).
SPECIAL_FLOAT = "__arena_float__"
BIG_INT = "__arena_bigint__"
UNSERIALIZABLE = "__arena_unserializable__"
BYTES = "__arena_bytes__"

#: JSON's largest exactly-representable integer in JavaScript.
JS_SAFE_INT = 2**53


class SandboxViolation(PermissionError):
    """Raised by the audit hook when submitted code tries something forbidden."""


class TestTimeout(BaseException):
    """Raised inside the child when a single test case exceeds its budget.

    Deliberately derived from :class:`BaseException` and *not* from
    ``Exception``: submitted code frequently wraps its work in
    ``try: ... except Exception: ...`` and must not be able to swallow the
    harness's own timeout.
    """


# ---------------------------------------------------------------------------
# Audit hook
# ---------------------------------------------------------------------------

_NETWORK_EVENTS = frozenset(
    {
        "socket.__new__",
        "socket.bind",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.sendto",
        "urllib.Request",
        "ftplib.connect",
        "http.client.connect",
        "smtplib.connect",
    }
)

_PROCESS_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "os.clone",
        "os.kill",
        "pty.spawn",
    }
)

#: Events that mutate the filesystem.
_WRITE_EVENTS = frozenset(
    {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.truncate", "os.link", "os.symlink"}
)


def install_audit_hook(
    *,
    deny_network: bool = True,
    deny_subprocess: bool = True,
    allow_ctypes: bool = False,
    writable_prefixes: tuple[str, ...] = (),
) -> None:
    """Register a ``sys.addaudithook`` guard around the untrusted program.

    Audit hooks cannot be removed once installed, so the restriction holds even
    if the user program reaches into ``sys.modules``.  This is a strong
    mitigation, **not** a substitute for OS-level isolation: see SECURITY.md.
    """
    allowed = tuple(os.path.abspath(p) for p in writable_prefixes)

    def _path_allowed(path) -> bool:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return False
        try:
            text = os.fspath(path)
        except TypeError:
            return False
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        try:
            resolved = os.path.abspath(text)
        except (ValueError, OSError):
            return False
        return any(resolved == p or resolved.startswith(p + os.sep) for p in allowed)

    def hook(event: str, args) -> None:
        if event in _NETWORK_EVENTS and deny_network:
            raise SandboxViolation(f"network access is disabled in the sandbox ({event})")
        if event in _PROCESS_EVENTS and deny_subprocess:
            raise SandboxViolation(f"process control is disabled in the sandbox ({event})")
        if event in _WRITE_EVENTS and not _path_allowed(args[0] if args else None):
            raise SandboxViolation(f"filesystem writes are restricted to the sandbox ({event})")
        if event == "open":
            path = args[0] if args else None
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            writing = False
            if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
                writing = True
            elif isinstance(mode, int) and (mode & 0o3 or mode & 0o1000 or mode & 0o2000):
                writing = True
            elif isinstance(flags, int) and (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
                writing = True
            if writing and not _path_allowed(path):
                raise SandboxViolation("filesystem writes are restricted to the sandbox directory")
        if event == "import" and not allow_ctypes and args and args[0] in ("ctypes", "_ctypes"):
            raise SandboxViolation("the ctypes module is disabled in the sandbox")

    sys.addaudithook(hook)


# ---------------------------------------------------------------------------
# Per-test timeout + memory helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def time_limit(seconds: float | None):
    """Wall-clock limit for a single test case, enforced with ``SIGALRM``."""
    if not seconds:
        yield
        return

    def _handler(signum, frame):  # pragma: no cover - timing dependent
        raise TestTimeout(f"test case exceeded its {seconds:g}s time limit")

    try:
        previous = signal.signal(signal.SIGALRM, _handler)
    except ValueError:  # not on the main thread
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGALRM, previous)


def reset_peak_rss() -> bool:
    """Reset the kernel's peak-RSS high-water mark (Linux ``clear_refs`` 5).

    Returns ``False`` when the platform does not support it, in which case the
    parent is told memory is only meaningful per *process*.
    """
    try:
        with open("/proc/self/clear_refs", "w") as handle:
            handle.write("5")
        return True
    except OSError:
        return False


def _peak_rss_kb() -> int:
    """Peak resident set size of *this* process, in KiB.

    ``getrusage(RUSAGE_SELF).ru_maxrss`` looks like the obvious choice, but it
    is wrong here: the kernel seeds a child's ``ru_maxrss`` from the parent's
    value across ``fork()`` and never lowers it, so a program running inside a
    70 MB web server reports 70 MB no matter how little it actually allocates.

    ``/proc/self/status:VmHWM`` reports the real high-water mark of this
    address space, and it is what ``clear_refs`` resets - so it is both
    accurate and per-test attributable.  ``ru_maxrss`` stays as the fallback
    for platforms without ``/proc``.
    """
    try:
        with open("/proc/self/status", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return int(usage.ru_maxrss if sys.platform != "darwin" else usage.ru_maxrss / 1024)


def _current_rss_kb() -> int:
    """Resident set size *right now*, in KiB (used as the per-test baseline)."""
    try:
        with open("/proc/self/status", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _peak_rss_source() -> str:
    try:
        with open("/proc/self/status", "r", encoding="ascii", errors="replace") as handle:
            if any(line.startswith("VmHWM:") for line in handle):
                return "VmHWM"
    except OSError:
        pass
    return "ru_maxrss"


def _cpu_seconds() -> float:
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


# ---------------------------------------------------------------------------
# JSON-safe encoding of arbitrary Python values
# ---------------------------------------------------------------------------


def to_jsonable(value, _depth: int = 0, _seen: frozenset = frozenset(), max_string: int = 0):
    """Convert an arbitrary Python value into strict, JSON-encodable data.

    Submitted code can return anything at all, so this function is
    deliberately forgiving: unknown objects degrade to a readable repr instead
    of raising, because losing a test result is worse than a lossy preview.
    """
    if _depth > 64:
        return {UNSERIALIZABLE: True, "repr": "<max depth exceeded>"}

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= JS_SAFE_INT else {BIG_INT: str(value)}
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        name = "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
        return {SPECIAL_FLOAT: name}
    if isinstance(value, str):
        if max_string and len(value) > max_string:
            return value[:max_string] + f"\u2026 [truncated, {len(value)} characters]"
        return value
    if isinstance(value, (bytes, bytearray)):
        return {BYTES: bytes(value[:4096]).hex()}

    marker = id(value)
    if marker in _seen:
        return {UNSERIALIZABLE: True, "repr": "<recursive structure>"}
    _seen = _seen | {marker}

    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:1000]:
            key_text = key if isinstance(key, str) else to_jsonable(key, _depth + 1, _seen)
            out[key_text if isinstance(key_text, str) else repr(key_text)] = to_jsonable(
                item, _depth + 1, _seen, max_string
            )
        return out
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item, _depth + 1, _seen, max_string) for item in list(value)[:10000]]
    if isinstance(value, (set, frozenset)):
        try:
            items = sorted(value)
        except TypeError:
            items = list(value)
        return {"__arena_set__": [to_jsonable(i, _depth + 1, _seen, max_string) for i in items[:10000]]}
    # numpy arrays and similar expose .tolist()
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        with contextlib.suppress(Exception):
            return to_jsonable(tolist(), _depth + 1, _seen, max_string)
    with contextlib.suppress(Exception):
        return repr(value)[:4096]
    return {UNSERIALIZABLE: True, "repr": f"<{type(value).__name__}>"}


class _CappedBuffer(io.StringIO):
    """StringIO that silently stops growing past ``limit`` characters."""

    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit
        self.truncated = False

    def write(self, s: str) -> int:  # type: ignore[override]
        current = self.tell()
        if current >= self.limit:
            self.truncated = True
            return len(s)
        if current + len(s) > self.limit:
            self.truncated = True
            s = s[: self.limit - current] + "\n...[output truncated]"
        return super().write(s)


def _short_repr(value, limit: int = 400) -> str:
    with contextlib.suppress(Exception):
        return repr(value)[:limit]
    return f"<unrepresentable {type(value).__name__}>"


def _materialise(result):
    """Turn a generator/iterator result into a list, bounded."""
    if isinstance(result, types.GeneratorType) or (
        hasattr(result, "__next__") and hasattr(result, "__iter__")
    ):
        return list(itertools_islice(result, 10000))
    return result


def itertools_islice(iterator, limit: int):
    out = []
    for index, item in enumerate(iterator):
        if index >= limit:
            break
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Program loading
# ---------------------------------------------------------------------------


def load_program(path: str, module_name: str = "arena_submission"):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load submitted program from {path!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Function protocol
# ---------------------------------------------------------------------------


def run_function_suite(spec: dict) -> dict:
    """Call ``entry_point(value)`` once per test case, in a single process.

    One process for the whole suite is intentional: process start-up and module
    import happen once, so per-test timings measure the algorithm rather than
    the interpreter.
    """
    program_path = spec["program_file"]
    entry_name = spec.get("entry_point") or "solve"
    tests = spec.get("tests") or []
    per_test_timeout = spec.get("per_test_timeout")
    cpu_budget = float(spec.get("cpu_budget_seconds") or 0)
    total_budget = float(spec.get("total_time_budget") or 0)
    spread_args = bool(spec.get("spread_args"))
    max_output = int(spec.get("max_output_bytes", 65536))
    # Benchmark runs set this: serialising a large return value can cost more
    # than the algorithm itself, which would distort the measurement.
    measure_only = bool(spec.get("measure_only"))

    # `main` probes this *before* the audit hook restricts writes, because
    # /proc/self/clear_refs is outside the sandbox directory by definition.
    can_attribute_memory = bool(spec.get("measure_memory_per_test"))
    memory_attribution = "per_test" if can_attribute_memory else "process_total"

    module = load_program(program_path)
    entry = getattr(module, entry_name, None)
    if entry is None:
        raise AttributeError(
            f"the submitted code does not define a function named {entry_name!r}. "
            f"Available names: {', '.join(sorted(n for n in vars(module) if not n.startswith('_'))[:20])}"
        )
    if not callable(entry):
        raise TypeError(f"{entry_name!r} is not callable (it is a {type(entry).__name__})")

    results: list[dict] = []
    suite_started = time.perf_counter()
    skipped = 0

    for index, case in enumerate(tests):
        record = {
            "index": index,
            "id": case.get("id", f"case-{index}"),
            "label": case.get("label", ""),
            "category": case.get("category", ""),
            "ok": False,
            "output": None,
            "stdout": "",
            "stderr": "",
            "truncated": False,
            "wall_ms": 0.0,
            "cpu_ms": 0.0,
            "peak_rss_kb": 0,
            "rss_before_kb": 0,
            "added_kb": 0,
            "error": None,
            "error_type": None,
        }

        # Stop cleanly instead of letting the parent's hard kill discard every
        # result we already have.
        if total_budget and (time.perf_counter() - suite_started) > total_budget:
            record["error"] = "skipped: the run's total time budget was exhausted"
            record["error_type"] = "Skipped"
            record["status"] = "skipped"
            skipped += 1
            results.append(record)
            continue

        if memory_attribution == "per_test":
            reset_peak_rss()
            # Everything already resident - the interpreter, the harness and
            # the test inputs - is charged to nobody.  Only what the algorithm
            # adds on top of this line counts as its own memory.
            record["rss_before_kb"] = _current_rss_kb()

        buffer = _CappedBuffer(max_output)
        error_buffer = _CappedBuffer(max_output)
        value = case.get("value")
        started_wall = time.perf_counter()
        started_cpu = _cpu_seconds()
        try:
            with time_limit(per_test_timeout):
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(error_buffer):
                    if spread_args and isinstance(value, (list, tuple)):
                        produced = entry(*value)
                    else:
                        produced = entry(value)
                    produced = _materialise(produced)
        except BaseException as exc:  # noqa: BLE001 - we must report anything
            record["error"] = _format_exception(exc, error_buffer.getvalue())
            record["error_type"] = type(exc).__name__
        else:
            record["ok"] = True
            if measure_only:
                record["output"] = None
            else:
                record["output"] = to_jsonable(produced, max_string=max_output)
                if record["output"] != produced:
                    record["truncated"] = True
        finally:
            record["wall_ms"] = (time.perf_counter() - started_wall) * 1000.0
            record["cpu_ms"] = (_cpu_seconds() - started_cpu) * 1000.0
            record["peak_rss_kb"] = _peak_rss_kb()
            record["added_kb"] = max(0, record["peak_rss_kb"] - record["rss_before_kb"])

        # Backstop for code that blocks the SIGALRM handler (long C calls, or
        # a program that deliberately clears its own alarms).
        if record["ok"] and cpu_budget and record["cpu_ms"] > cpu_budget * 1000.0:
            record["ok"] = False
            record["error"] = f"exceeded its {cpu_budget:g}s CPU budget ({record['cpu_ms'] / 1000:.2f}s used)"
            record["error_type"] = "CpuBudgetExceeded"

        record["stdout"] = buffer.getvalue()
        record["stderr"] = error_buffer.getvalue()
        # Note the `or record[...]`: a huge *return value* is truncated too, and
        # that flag must not be lost here.
        record["truncated"] = bool(
            record.get("truncated") or buffer.truncated or error_buffer.truncated
        )
        results.append(record)

    return {
        "status": "ok",
        "tests": results,
        "error": None,
        "meta": {
            "memory_attribution": memory_attribution,
            "entry_point": entry_name,
            "skipped": skipped,
            "suite_wall_ms": (time.perf_counter() - suite_started) * 1000.0,
        },
    }


def _format_exception(exc: BaseException, stderr: str) -> str:
    if isinstance(exc, TestTimeout):
        return str(exc) or "timed out"
    lines = traceback.format_exception_only(type(exc), exc)
    text = "".join(lines).strip()
    if len(text) > 600:
        text = text[:600] + " ..."
    if stderr.strip():
        tail = stderr.strip()[-300:]
        text = f"{text}\n   stderr: {tail}"
    return text


# ---------------------------------------------------------------------------
# stdin/stdout protocol
# ---------------------------------------------------------------------------


def run_stdio_program(spec: dict) -> int:
    """Execute a plain script exactly as if the user had run it themselves.

    stdin and stdout pass straight through - the submitted program owns the
    protocol.  We still run it *inside* this process so that it inherits the
    audit hook, the resource limits and a per-test timeout, and so that we can
    report its peak memory accurately from the same address space.
    """
    program_path = spec["program_file"]
    with open(program_path, "r", encoding="utf-8") as handle:
        source = handle.read()

    code = compile(source, "program.py", "exec")
    globals_dict = {
        "__name__": "__main__",
        "__file__": program_path,
        "__builtins__": __builtins__,
    }
    exit_code = 0
    try:
        with time_limit(spec.get("per_test_timeout")):
            exec(code, globals_dict)  # noqa: S102 - executing user code is the point
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    except TestTimeout:
        sys.stderr.write("\nTestTimeout: the program exceeded its time limit.\n")
        exit_code = 124
    except BaseException as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc(file=sys.stderr)
        sys.stderr.write(f"\n{type(exc).__name__}: {exc}\n")
        exit_code = 1
    return exit_code


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: bootstrap.py <spec.json>\n")
        return 2

    with open(argv[1], "r", encoding="utf-8") as handle:
        spec = json.load(handle)

    results_path = spec["results_file"]
    started = time.time()
    outcome = {"status": "error", "tests": [], "error": None, "meta": {}}

    try:
        os.chdir(spec["workdir"])
    except OSError as exc:
        outcome["error"] = f"could not enter sandbox directory: {exc}"

    if outcome["error"] is None:
        try:
            # Probed *before* the audit hook goes up: /proc/self/clear_refs is
            # outside the sandbox directory, so the hook would refuse the write.
            spec["measure_memory_per_test"] = bool(spec.get("measure_memory_per_test")) and reset_peak_rss()
            # Only the sandbox directory itself is writable from the inside.
            install_audit_hook(
                deny_network=spec.get("deny_network", True),
                deny_subprocess=spec.get("deny_subprocess", True),
                allow_ctypes=spec.get("allow_ctypes", False),
                writable_prefixes=(spec["workdir"],),
            )
            sys.setrecursionlimit(max(1000, int(spec.get("recursion_limit", 3000))))
            if spec.get("mode") == "stdio":
                # stdin/stdout belongs to the user's program, so we hand the
                # process over to it and only report metadata afterwards.
                exit_code = run_stdio_program(spec)
                emit_meta(spec, started, {})
                return exit_code
            outcome = run_function_suite(spec)
        except BaseException as exc:  # noqa: BLE001
            outcome = {
                "status": "error",
                "tests": [],
                "error": _format_exception(exc, ""),
                "error_type": type(exc).__name__,
                "meta": {},
            }

    with open(results_path, "w", encoding="utf-8") as handle:
        json.dump(outcome, handle)
    emit_meta(spec, started, outcome["meta"])
    return 0


def emit_meta(spec: dict, started: float, extra: dict) -> None:
    """Hand the parent a metadata block it can trust.

    It goes to stderr wrapped in a marker, and the parent reads the *last*
    occurrence on the line - a submitted program can print whatever it likes
    on stdout, but it cannot prevent ours from being the final word.
    """
    meta = {
        "protocol": PROTOCOL_VERSION,
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "started_at": started,
        "peak_rss_kb": _peak_rss_kb(),
        "peak_rss_source": _peak_rss_source(),
    }
    meta.update(extra)
    try:
        sys.stderr.write(f"{META_MARKER}{json.dumps(meta)}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
