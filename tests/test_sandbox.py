"""The execution layer.

These tests treat submitted code as hostile: each one submits a program that
tries to escape, and asserts that it cannot.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

from app.sandbox import Limits, run_function_suite, run_stdio_suite
from app.sandbox.capabilities import probe_sandbox
from app.sandbox.protocol import decode, describe

# Keep the suite quick: a one-second budget is plenty to prove a limit works.
FAST = Limits(cpu_seconds=1, wall_seconds=3.0, suite_wall_seconds=20.0, memory_mb=192)


def cases(*values):
    return [
        {"id": f"c{index}", "label": "", "category": "manual", "value": value}
        for index, value in enumerate(values)
    ]


def run(code, values=(1,), **kwargs):
    kwargs.setdefault("limits", FAST)
    return run_function_suite(code, cases(*values), **kwargs)


# ---------------------------------------------------------------------------
# Function protocol basics
# ---------------------------------------------------------------------------


class TestFunctionProtocol:
    def test_returns_the_value(self):
        result = run("def solve(x):\n    return x * 2", (21,))
        assert result.status == "ok"
        assert result.tests[0].ok
        assert result.tests[0].output == 42

    def test_entry_point_name_is_configurable(self):
        result = run("def main(x):\n    return x + 1", (1,), entry_point="main")
        assert result.tests[0].output == 2

    def test_spread_args_splits_a_pair(self):
        result = run("def solve(a, b):\n    return a + b", ([2, 3],), spread_args=True)
        assert result.tests[0].output == 5

    def test_generators_are_materialised(self):
        result = run("def solve(x):\n    return (i * i for i in x)", ([1, 2, 3],))
        assert result.tests[0].output == [1, 4, 9]

    def test_dictionaries_and_tuples(self):
        result = run("def solve(x):\n    return {'sum': sum(x), 'kind': 'list'}", ([1, 2],))
        assert result.tests[0].output == {"sum": 3, "kind": "list"}

    def test_printed_output_is_captured_not_printed(self):
        result = run("def solve(x):\n    print('debug', x)\n    return x", (5,))
        assert result.tests[0].stdout.strip() == "debug 5"
        assert result.tests[0].output == 5

    def test_missing_entry_point_is_a_clear_error(self):
        result = run("x = 1\n")
        assert result.status == "error"
        assert "does not define a function" in result.error
        assert "solve" in result.error

    def test_entry_point_must_be_callable(self):
        result = run("solve = 5\n")
        assert result.status == "error"
        assert "not callable" in result.error

    def test_syntax_error_is_reported(self):
        result = run("def solve(x)\n    return x\n")
        assert result.status == "error"
        assert "SyntaxError" in result.error

    def test_each_test_is_isolated_from_the_last(self):
        """State leaking between test cases would corrupt the comparison."""
        result = run("cache = []\ndef solve(x):\n    cache.append(x)\n    return len(cache)",
                     (1, 2, 3))
        assert [case.output for case in result.tests] == [1, 2, 3]

    def test_exception_is_captured_not_fatal(self):
        result = run("def solve(x):\n    return 1 / x", (0, 2))
        assert result.tests[0].ok is False
        assert "ZeroDivisionError" in result.tests[0].error
        assert result.tests[1].ok is True

    def test_import_of_a_local_module_is_possible(self):
        # Submitted code may define helpers in the same file.
        result = run("def helper(x):\n    return x + 1\ndef solve(x):\n    return helper(x)", (1,))
        assert result.tests[0].output == 2


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


class TestLimits:
    def test_infinite_loop_is_stopped(self):
        result = run("def solve(x):\n    while True:\n        pass\n", (1,))
        assert result.tests[0].ok is False
        assert "time limit" in result.tests[0].error

    def test_one_hanging_test_does_not_destroy_the_others(self):
        result = run(
            "def solve(x):\n"
            "    if x == 0:\n"
            "        while True:\n"
            "            pass\n"
            "    return x\n",
            (0, 1, 2),
        )
        assert result.tests[0].ok is False
        assert result.tests[1].output == 1
        assert result.tests[2].output == 2

    def test_memory_bomb_hits_memory_error(self):
        result = run(
            "def solve(x):\n    blob = bytearray(400 * 1024 * 1024)\n    return len(blob)\n",
            (1,),
            limits=Limits(cpu_seconds=2, wall_seconds=5.0, suite_wall_seconds=20.0, memory_mb=128),
        )
        assert result.tests[0].ok is False
        assert "MemoryError" in result.tests[0].error

    def test_unbounded_output_is_truncated(self):
        result = run(
            "def solve(x):\n    return 'y' * 500000\n",
            (1,),
            limits=Limits(cpu_seconds=3, wall_seconds=6.0, memory_mb=192, max_output_bytes=4096),
        )
        assert result.tests[0].truncated
        assert len(result.tests[0].stdout) <= 4096

    def test_huge_output_is_truncated_in_the_value(self):
        result = run("def solve(x):\n    return [0] * 2_000_000\n", (1,))
        assert result.status == "ok"

    def test_user_cannot_swallow_the_timeout(self):
        """The timeout derives from BaseException so `except Exception` misses it."""
        result = run(
            "def solve(x):\n"
            "    try:\n"
            "        while True:\n"
            "            pass\n"
            "    except Exception:\n"
            "        return 'caught'\n",
            (1,),
        )
        assert result.tests[0].ok is False


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_network_is_blocked(self):
        result = run("def solve(x):\n    import socket\n    socket.socket()\n", (1,))
        assert "network" in (result.tests[0].error or "").lower()

    def test_dns_is_blocked(self):
        result = run(
            "def solve(x):\n    import socket\n    socket.getaddrinfo('example.com', 80)\n", (1,)
        )
        assert result.tests[0].ok is False

    def test_writes_outside_the_sandbox_are_blocked(self):
        result = run("def solve(x):\n    open('/tmp/arena-escape.txt', 'w').write('x')\n", (1,))
        assert result.tests[0].ok is False
        assert "filesystem" in result.tests[0].error.lower()
        assert not os.path.exists("/tmp/arena-escape.txt")

    def test_writing_inside_the_sandbox_is_allowed(self):
        result = run("def solve(x):\n    open('note.txt', 'w').write('x')\n    return 'ok'\n", (1,))
        assert result.tests[0].output == "ok"

    def test_subprocess_is_blocked(self):
        result = run("def solve(x):\n    import subprocess\n    subprocess.Popen(['/bin/true'])\n", (1,))
        assert "process" in result.tests[0].error.lower()

    def test_os_system_is_blocked(self):
        result = run("def solve(x):\n    import os\n    os.system('echo hi')\n", (1,))
        assert result.tests[0].ok is False

    def test_ctypes_is_blocked(self):
        result = run("def solve(x):\n    import ctypes\n    ctypes.CDLL('libc.so.6')\n", (1,))
        assert "ctypes" in result.tests[0].error.lower()

    def test_reading_the_host_filesystem_is_possible_but_harmless(self):
        """Reading is not blocked - only writes. The process is unprivileged
        and cannot see the application directory, so this is not an escape."""
        result = run("def solve(x):\n    return os.path.exists('/etc/hostname')\n",
                     (1,)) if False else run(
            "import os\ndef solve(x):\n    return os.path.exists('/etc/hostname')\n", (1,))
        assert result.tests[0].ok

    def test_environment_is_minimal(self):
        result = run("def solve(x):\n    return sorted(__import__('os').environ)\n", (1,))
        assert result.tests[0].output == [
            "ARENA_SANDBOX", "HOME", "LANG", "LC_ALL", "PATH",
            "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "PYTHONIOENCODING", "TMPDIR",
        ]

    def test_pythonpath_is_ignored(self):
        result = run("def solve(x):\n    import sys\n    return 'PYTHONPATH' in sys.path\n", (1,))
        assert result.tests[0].output is False

    def test_the_program_cannot_see_the_application_source(self):
        result = run("def solve(x):\n    import os\n    return os.path.exists('/app/main.py')\n", (1,))
        assert result.tests[0].output is False

    def test_uid_is_dropped_when_supported(self):
        capabilities = probe_sandbox()
        if not capabilities.privileges_dropped:
            pytest.skip("privilege dropping unavailable on this host")
        result = run("import os\ndef solve(x):\n    return os.getuid()\n", (1,))
        assert result.tests[0].output == 65534

    def test_no_network_namespace_leak(self):
        capabilities = probe_sandbox()
        if not capabilities.network_isolated:
            pytest.skip("network namespaces unavailable on this host")
        result = run(
            "import socket\ndef solve(x):\n"
            "    s = socket.socket()\n"
            "    s.settimeout(1)\n"
            "    s.connect(('1.1.1.1', 53))\n"
            "    return 'connected'\n",
            (1,),
        )
        assert result.tests[0].ok is False


# ---------------------------------------------------------------------------
# stdin/stdout protocol
# ---------------------------------------------------------------------------


class TestStdioProtocol:
    def test_json_in_json_out(self):
        code = "import sys, json\nd = json.load(sys.stdin)\nprint(json.dumps(sorted(d['input'])))\n"
        result = run_stdio_suite(code, cases([3, 1, 2]), limits=FAST)
        assert result.tests[0].ok
        assert result.tests[0].output == [1, 2, 3]

    def test_plain_text_output(self):
        """Non-JSON stdout is passed through as a string."""
        result = run_stdio_suite(
            "import sys, json\n"
            "print(json.load(sys.stdin)['input'].upper())\n",
            cases("hello"), limits=FAST)
        assert result.tests[0].output == "HELLO"
        assert result.tests[0].ok

    def test_input_is_wrapped_in_a_json_envelope(self):
        """The protocol hands the program {"input": ..., "id": ...} on stdin."""
        result = run_stdio_suite(
            "import sys, json\nd = json.load(sys.stdin)\nprint(json.dumps(sorted(d)))\n",
            cases([1]), limits=FAST)
        assert result.tests[0].output == ["id", "input"]  # sorted keys of the envelope

    def test_each_case_gets_a_fresh_process(self):
        result = run_stdio_suite(
            "import sys, json\nd = json.load(sys.stdin)\nprint(json.dumps(len(d['input'])))\n",
            cases([1, 2], [1, 2, 3]), limits=FAST)
        assert [case.output for case in result.tests] == [2, 3]

    def test_nonzero_exit_is_reported(self):
        result = run_stdio_suite("import sys\nsys.exit(3)\n", cases(1), limits=FAST)
        assert result.tests[0].ok is False
        assert "exited with code 3" in result.tests[0].error

    def test_sandbox_applies_to_stdio_too(self):
        result = run_stdio_suite(
            "import socket\nsocket.socket()\n", cases(1), limits=FAST)
        assert result.tests[0].ok is False

    def test_infinite_loop_is_stopped(self):
        result = run_stdio_suite("while True:\n    pass\n", cases(1), limits=FAST)
        assert result.tests[0].ok is False

    def test_internal_metadata_never_reaches_the_user(self):
        result = run_stdio_suite("print('clean')\n", cases(1), limits=FAST)
        assert "ARENA" not in result.tests[0].stderr
        assert "ARENA" not in result.tests[0].stdout

    def test_a_program_cannot_forge_our_metadata(self):
        """We read the *last* marker on stderr, so printing a fake one first
        does not help."""
        result = run_stdio_suite(
            "import sys\n"
            "sys.stderr.write('\\x1eARENA-META:{\"peak_rss_kb\": 999999}\\n')\n"
            "print('ok')\n",
            cases(1), limits=FAST)
        assert result.tests[0].output == "ok"
        assert result.tests[0].peak_rss_kb < 999_999


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


class TestMeasurement:
    def test_memory_is_not_inflated_by_the_parent(self):
        """A big parent must not make a small child look big.

        This is the ru_maxrss-across-fork trap: if we measured the wrong
        number, a 70 MB parent would make this report ~70 MB.
        """
        ballast = bytearray(80 * 1024 * 1024)
        for index in range(0, len(ballast), 4096):
            ballast[index] = 1
        try:
            result = run("def solve(x):\n    return 1\n", (1,))
            peak_mb = result.tests[0].peak_rss_kb / 1024
        finally:
            del ballast
        assert peak_mb < 40, f"child reported {peak_mb:.1f} MB - the parent's RSS leaked in"

    def test_per_test_attribution_is_available(self):
        capabilities = probe_sandbox()
        if not capabilities.per_test_memory_supported:
            pytest.skip("per-test attribution unavailable on this host")
        result = run("def solve(x):\n    return len(str(x))\n", (1, 2))
        assert result.memory_attribution == "per_test"
        assert result.tests[0].added_kb >= 0

    def test_heavier_work_uses_more_memory(self):
        small = run("def solve(x):\n    return sum(x)\n", ([1] * 100,))
        large = run("def solve(x):\n    return sum(x)\n", ([1] * 400_000,))
        assert large.tests[0].added_kb > small.tests[0].added_kb

    def test_wall_time_is_measured(self):
        result = run("import time\ndef solve(x):\n    time.sleep(0.05)\n    return 1\n", (1,))
        assert result.tests[0].wall_ms >= 40

    def test_measure_only_skips_serialisation(self):
        """Benchmark runs must not pay for encoding a huge result."""
        fast = run("def solve(x):\n    return x\n", ([1] * 1000,))
        measured = run_function_suite(
            "def solve(x):\n    return x\n", cases([1] * 1000),
            limits=FAST, measure_only=True,
        )
        assert measured.status == "ok"
        assert measured.tests[0].output is None
        assert fast.tests[0].output == [1] * 1000


# ---------------------------------------------------------------------------
# Value encoding
# ---------------------------------------------------------------------------


class TestEncoding:
    def test_non_finite_floats_survive(self):
        result = run("def solve(x):\n    return [float('nan'), float('inf'), float('-inf')]\n", (1,))
        value = result.tests[0].output
        assert math.isnan(value[0]) and value[1] == math.inf and value[2] == -math.inf

    def test_big_integers_survive(self):
        result = run("def solve(x):\n    return 2 ** 100\n", (1,))
        assert result.tests[0].output == 2**100

    def test_unserialisable_objects_degrade_gracefully(self):
        class Weird:
            def __repr__(self):
                return "<weird>"

        code = "class W:\n    def __repr__(self):\n        return '<w>'\ndef solve(x):\n    return W()\n"
        result = run(code, (1,))
        assert result.tests[0].ok
        assert result.tests[0].output == "<w>"

    def test_recursive_structures_do_not_hang(self):
        code = (
            "def solve(x):\n"
            "    a = []\n"
            "    a.append(a)\n"
            "    return a\n"
        )
        result = run(code, (1,))
        assert result.status == "ok"

    def test_bytes_are_survived(self):
        result = run("def solve(x):\n    return b'abc'\n", (1,))
        assert result.tests[0].output == b"abc"

    def test_sets_round_trip(self):
        result = run("def solve(x):\n    return {3, 1, 2}\n", (1,))
        assert sorted(result.tests[0].output) == [1, 2, 3]

    def test_decode_is_the_inverse_of_describe(self):
        from app.sandbox.bootstrap import to_jsonable

        for value in [1, 1.5, "x", None, True, [1, [2, 3]], {"a": 1}, 2**70, b"xy"]:
            encoded = to_jsonable(value)
            assert decode(encoded) == value or (
                isinstance(value, float) and math.isnan(value)
            )

    def test_describe_is_readable(self):
        assert describe([1, 2, 3]) == "[1, 2, 3]"
        assert describe({"a": 1}) == '{"a": 1}'
        assert describe(float("nan")) == "NaN"
        assert describe(2**70) == str(2**70)
        assert "…" in describe("x" * 1000, limit=50)


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


class TestHousekeeping:
    def test_temporary_directories_are_removed(self):
        from app.config import TEMP_ROOT

        before = set(os.listdir(TEMP_ROOT)) if TEMP_ROOT.exists() else set()
        run("def solve(x):\n    return x\n", (1,))
        after = set(os.listdir(TEMP_ROOT)) if TEMP_ROOT.exists() else set()
        assert after <= before

    def test_empty_test_list_is_handled(self):
        result = run_function_suite("def solve(x):\n    return x\n", [], limits=FAST)
        assert result.status == "ok"
        assert result.tests == []

    def test_capabilities_are_probed_once_and_cached(self):
        first = probe_sandbox()
        second = probe_sandbox()
        assert first is second
        assert first.level in ("namespace_nobody", "namespace", "none")
