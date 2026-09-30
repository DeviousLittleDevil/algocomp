"""Capability probing for the sandbox.

Isolation techniques are not available everywhere (Windows, containers without
unprivileged user namespaces, kernels with ``unprivileged_userns_clone=0``,
...).  Rather than guessing, we run a *real* probe once and cache the answer.
The probe is a genuine end-to-end test: it spawns a child with a given
isolation level and checks both that it starts and that it really has no
network.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Limits

#: Isolation levels, from strongest to weakest.
LEVELS = ("namespace_nobody", "namespace", "none")

_PROBE_SOURCE = r"""
import json, os, socket, sys


def vmhwm():
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


out = {"uid": os.getuid(), "gid": os.getgid(), "cwd": os.getcwd()}

# 1. network
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    s.connect(("1.1.1.1", 53))
    s.close()
    out["network"] = "reachable"
except Exception as exc:
    out["network"] = "blocked"
    out["network_error"] = type(exc).__name__

# 2. filesystem: writable inside the sandbox, not outside it
try:
    open(os.path.join(os.getcwd(), "probe-write.txt"), "w").write("ok")
    out["write_workdir"] = "ok"
except Exception as exc:
    out["write_workdir"] = "failed: %s" % exc
try:
    open("/tmp/arena-probe-should-fail.txt", "w").write("x")
    out["write_outside"] = "ok"
    try:
        os.unlink("/tmp/arena-probe-should-fail.txt")
    except OSError:
        pass
except Exception:
    out["write_outside"] = "denied"

# 3. subprocess
try:
    import subprocess
    subprocess.Popen(["/bin/true"]).wait()
    out["subprocess"] = "ok"
except Exception:
    out["subprocess"] = "denied"

# 4. limits actually applied
try:
    resource = __import__("resource")
    out["cpu_limit"] = resource.getrlimit(resource.RLIMIT_CPU)[0]
    out["as_limit"] = resource.getrlimit(resource.RLIMIT_AS)[0]
except Exception:
    pass

# 5. per-test peak-RSS attribution: does a real allocation show up in VmHWM,
#    and does clear_refs actually reset the high-water mark?
before = vmhwm()
ballast = bytearray(48 * 1024 * 1024)
for _i in range(0, len(ballast), 4096):
    ballast[_i] = 1
after = vmhwm()
out["vmhwm_tracks_allocations"] = after > before + 8 * 1024
out["vmhwm_grew_kb"] = after - before
del ballast
try:
    with open("/proc/self/clear_refs", "w") as handle:
        handle.write("5")
    out["peak_reset_works"] = vmhwm() < after
except OSError:
    out["peak_reset_works"] = False

sys.stdout.write(json.dumps(out))
"""


_AUDIT_PROBE_CODE = """\
import os, socket, subprocess

def solve(data):
    results = {}
    try:
        socket.socket()
        results["network"] = "reachable"
    except Exception as exc:
        results["network"] = type(exc).__name__
    try:
        open("/tmp/arena-audit-should-fail.txt", "w").write("x")
        results["write_outside"] = "ok"
    except Exception as exc:
        results["write_outside"] = type(exc).__name__
    try:
        subprocess.Popen(["/bin/true"])
        results["subprocess"] = "ok"
    except Exception as exc:
        results["subprocess"] = type(exc).__name__
    try:
        import ctypes
        ctypes.CDLL("libc.so.6")
        results["ctypes"] = "ok"
    except Exception as exc:
        results["ctypes"] = type(exc).__name__
    try:
        open("inside.txt", "w").write("ok")
        results["write_inside"] = "ok"
    except Exception as exc:
        results["write_inside"] = type(exc).__name__
    return results
"""


def probe_audit_hook(isolation: str = "none") -> dict:
    """Run the real bootstrap and check that the guard rails actually hold.

    The OS-level probe tells us what the kernel allows; this one tells us what
    the *sandbox as a whole* enforces, which is the question that matters.

    The isolation level is passed in explicitly: asking for it here would
    re-enter ``probe_sandbox`` while it is still running.
    """
    from .runner import run_function_suite

    cases = [{"id": "probe", "label": "probe", "category": "probe", "value": None}]
    try:
        suite = run_function_suite(
            _AUDIT_PROBE_CODE, cases, entry_point="solve", limits=Limits(), isolation=isolation
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not suite.tests or not suite.tests[0].ok:
        return {"error": (suite.error or suite.tests[0].error if suite.tests else "unknown")}
    return dict(suite.tests[0].output)


# Re-entrant: the audit-hook probe below runs a real sandboxed program, which
# asks for the best isolation level and therefore re-enters this module.
_lock = threading.RLock()
_cache: dict[str, "CapabilityReport"] = {}


@dataclass
class CapabilityReport:
    """What the current host actually supports."""

    level: str = "none"
    network_isolated: bool = False
    privileges_dropped: bool = False
    rlimits_supported: bool = True
    per_test_memory_supported: bool = False
    filesystem_restricted: bool = False
    subprocess_blocked: bool = False
    network_blocked_by_hook: bool = False
    ctypes_blocked: bool = False
    sandbox_writable: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def strong(self) -> bool:
        return self.level == "namespace_nobody"

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "network_isolated": self.network_isolated,
            "privileges_dropped": self.privileges_dropped,
            "rlimits_supported": self.rlimits_supported,
            "per_test_memory_supported": self.per_test_memory_supported,
            "filesystem_restricted": self.filesystem_restricted,
            "subprocess_blocked": self.subprocess_blocked,
            "network_blocked_by_hook": self.network_blocked_by_hook,
            "ctypes_blocked": self.ctypes_blocked,
            "sandbox_writable": self.sandbox_writable,
            "notes": list(self.notes),
            "strongest_available": LEVELS[0],
        }


def _probe_once(level: str) -> CapabilityReport:
    report = CapabilityReport(level=level)
    if level != "none":
        report.network_isolated = True

    from .runner import make_sandbox_dir, spawn_child  # local import: avoids a cycle

    workdir = None
    try:
        workdir = make_sandbox_dir(prefix="probe-", drop_privileges=False)
        program = Path(workdir.work) / "probe.py"
        program.write_text(_PROBE_SOURCE, encoding="utf-8")
        os.chmod(program, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

        limits = Limits(
            cpu_seconds=5,
            wall_seconds=15.0,
            memory_mb=256,
            drop_privileges=(level == "namespace_nobody"),
        )
        result = spawn_child(
            [sys.executable, "-I", str(program)],
            workdir=workdir.work,
            control_dir=workdir.root,
            limits=limits,
            timeout=15.0,
            isolation=level,
        )
        if result.timed_out or result.returncode != 0:
            report.notes.append(f"probe at level {level!r} failed to run cleanly")
            report.level = "none"
            report.network_isolated = False
            return report
        payload = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
        report.network_isolated = payload.get("network") == "blocked"
        report.privileges_dropped = payload.get("uid") == 65534 or payload.get("uid") == 0
        report.rlimits_supported = payload.get("as_limit", 0) != resource_inf()
        report.filesystem_restricted = payload.get("write_outside") == "denied"
        report.subprocess_blocked = payload.get("subprocess") == "denied"
        # Only claim per-test memory attribution if we actually proved it.
        report.per_test_memory_supported = bool(
            payload.get("vmhwm_tracks_allocations") and payload.get("peak_reset_works")
        )
        report.notes.append(
            f"probe: network={payload.get('network')}, uid={payload.get('uid')}, "
            f"writes outside sandbox={payload.get('write_outside')}, "
            f"VmHWM tracks allocations={payload.get('vmhwm_tracks_allocations')}, "
            f"peak reset works={payload.get('peak_reset_works')}"
        )
    except Exception as exc:  # noqa: BLE001 - probing must never break startup
        report.notes.append(f"probe at level {level!r} raised {type(exc).__name__}: {exc}")
        report.level = "none"
        report.network_isolated = False
    finally:
        if workdir:
            _rmtree(workdir.root)
    return report


def resource_inf() -> int:
    import resource as _r

    return _r.RLIM_INFINITY


def _rmtree(path: str) -> None:
    shutil_rmtree(path)


def shutil_rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


_in_progress = False


def probe_sandbox(force: bool = False) -> CapabilityReport:
    """Detect the strongest isolation this host supports (cached)."""
    global _in_progress
    with _lock:
        if not force and "report" in _cache:
            return _cache["report"]
        if _in_progress:
            # A nested call (from a probe that needs to spawn a child) must not
            # start the whole thing again; the conservative answer is fine.
            return _cache.get("report") or CapabilityReport(level="none")
        report = CapabilityReport(level="none")
        if os.name != "posix":
            report.notes.append("POSIX host required for namespace isolation; using rlimits only")
        else:
            for level in LEVELS:
                candidate = _probe_once(level)
                if candidate.level != "none" and candidate.network_isolated:
                    report = candidate
                    break
                report.notes.extend(candidate.notes)
            else:
                report = CapabilityReport(level="none")
        if not sys.platform.startswith("linux"):
            report.notes.append(
                "Per-test memory attribution needs Linux /proc; reporting cumulative peaks instead."
            )
        _in_progress = True
        try:
            audit = probe_audit_hook(report.level)
        finally:
            _in_progress = False
        if audit.get("error"):
            report.notes.append("audit hook probe failed: " + audit["error"])
        else:
            report.filesystem_restricted = audit.get("write_outside") == "SandboxViolation"
            report.subprocess_blocked = audit.get("subprocess") == "SandboxViolation"
            report.network_blocked_by_hook = audit.get("network") == "SandboxViolation"
            report.ctypes_blocked = audit.get("ctypes") == "SandboxViolation"
            report.sandbox_writable = audit.get("write_inside") == "ok"
            report.notes.append(
                f"audit hook: network={audit.get('network')}, write outside="
                f"{audit.get('write_outside')}, subprocess={audit.get('subprocess')}, "
                f"ctypes={audit.get('ctypes')}, write inside sandbox={audit.get('write_inside')}"
            )
        _cache["report"] = report
        return report


def best_isolation() -> str:
    return probe_sandbox().level


def reset_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = [
    "CapabilityReport",
    "probe_sandbox",
    "best_isolation",
    "reset_cache",
    "LEVELS",
]
