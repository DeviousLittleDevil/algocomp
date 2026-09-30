# Security

## Threat model in one paragraph

The input is a chunk of Python written by a stranger, and the promise is that
running it will not harm the machine, the server, or anyone else's data. The
cost model is a hostile, patient user: they know the implementation is
`app/sandbox/` and they will read it. The asset is the host and the service's
own files.

**Out of scope:** protecting the machine from a user who has already obtained
root, or from a kernel-level exploit. **Also out of scope:** running genuinely
hostile code from untrusted strangers on a shared machine. See *Honest limits*
below.

## Defence in depth, from outermost to innermost

### 1. Separate process

User code never runs in the FastAPI process. It is loaded by
`app/sandbox/bootstrap.py` inside a freshly spawned child. The parent holds no
reference to the user's module and never imports it. A child that segfaults,
exits, allocates until it is killed, or calls `os._exit` is just a child that
went away.

### 2. OS namespaces (Linux)

At the strongest isolation level, `namespace_nobody`, the child is placed in
**fresh user and network namespaces** and drops to an unprivileged uid:

* a network namespace with only a loopback interface — there is no route to
  anywhere, so exfiltration fails even if the Python-level checks are bypassed;
* a user namespace that maps the child's uid/gid, so a file owned by the
  service user is not owned by the child;
* after `setresuid`, files in the service's own directories become
  inaccessible to the child.

### 3. Resource limits

`setrlimit` in the child, before `exec`:

| Limit | Default | Stops |
|---|---|---|
| `RLIMIT_CPU` | 5 s | An infinite loop (`SIGXCPU` → kill) |
| `RLIMIT_AS` | 256 MiB | `[0] * 10**10` (`MemoryError`) |
| `RLIMIT_FSIZE` | 8 MiB | Writing a huge file to disk |
| `RLIMIT_NPROC` | 64 | Fork bombs |
| `RLIMIT_NOFILE` | 64 | Descriptor exhaustion |
| wall clock | 10 s per test, 120 s per suite | Programs that sleep instead of spinning |
| captured output | 64 KiB per stream, per test | `print("x" * 10**9)` |

The client can ask for *less* than these defaults (a 1-second timeout for a
quick check), never for more: `MAX_ALLOWED_LIMITS` in `app/config.py` caps
every requestable limit, so the API cannot be used to request an unbounded
sandbox.

The wall-clock killer is the parent: the child is placed in its own process
group, and on timeout the parent sends `SIGTERM`, then `SIGKILL` to the whole
group. Sleeping is caught; `SIGSTOP`-ing itself is caught by the group kill.

### 4. Filesystem

Each child gets a private directory, created with `mkdtemp` at mode `0o700`
and removed afterwards. The child's environment is rebuilt from scratch —
`HOME`, `PATH`, `PYTHONPATH` and friends point into that directory, and
variables like `PYTHONSTARTUP` that would inject code are removed. When
privileges are dropped, the directory is handed over to the unprivileged uid.

### 5. `sys.addaudithook` (Python level)

A CPython audit hook, installed **before** the user's module is imported, denies
network, process control, and filesystem mutation outside the sandbox
directory, and blocks `ctypes`:

* `_NETWORK_EVENTS` — `socket.__new__`, `bind`, `connect`, `getaddrinfo`,
  `sendto`, `urllib.Request`, `http.client.connect`, `ftplib.connect`, …
* `_PROCESS_EVENTS` — `subprocess.Popen`, `os.system`, `os.exec*`,
  `posix_spawn`, `os.fork`, `os.clone`, `os.kill`, …
* filesystem writes — `open` with a writing mode, `os.remove`, `os.rename`,
  `os.mkdir`, `os.link`, …
* `ctypes` / `_ctypes` imports, which would bypass everything above.

Audit hooks **cannot be uninstalled** once added, so this holds even if user
code reaches into `sys.modules` or calls `sys.addaudithook` itself.

A violation raises `SandboxViolation` inside the child, which the harness
catches and reports as a *policy* failure — visibly attributed to the sandbox
rather than silently swallowed, so a user can tell that they hit a wall.

## Capability probing: what is real versus what is hoped for

Not every host allows every mechanism (containers, Windows, restricted CI). So
`app/sandbox/capabilities.py` **probes** — it actually runs a child at each
level and tries the forbidden things — and reports what is genuinely enforced.
The UI shows this; a run on a weak host says so instead of claiming protection
it does not have.

| Level | Requires |
|---|---|
| `namespace_nobody` | user + network namespaces, and privilege drop |
| `network_namespace` | network namespace only |
| `rlimits` | POSIX rlimits only |
| `none` | nothing; the child still gets limits where supported |

The probe is cached per process and recorded in every report, so a run tells you
the isolation level it was performed under.

## Honest limits

This design is strong against the things a *submission* realistically does —
escaping, forking, networking, filling memory or disk, hanging — and it is not
a security boundary for hosting genuinely hostile code.

Known gaps, stated plainly:

* **Pure-Python sandbox escapes.** A determined attacker can find a CPython
  primitive that reaches the C level without an audit event. The namespace and
  rlimit layers are the real defence here; the audit hook is defence in depth.
* **Kernel vulnerabilities** are not mitigated. No in-process control can fix
  one.
* **Shared-machine side channels** (timing, cache) are not addressed.
* **DoS by volume.** Each run spawns processes; a flood of requests can exhaust
  CPU. There is a job cap and a bounded pool, which raises the bar but is not a
  rate limiter.

**Therefore: do not expose this to untrusted strangers on a machine you care
about.** For a real multi-tenant deployment, keep this design and replace the
innermost layer:

| Option | Effort | Isolation |
|---|---|---|
| Docker / Podman with `--network=none`, `--read-only`, `--memory`, `--cpus`, `--pids-limit` | Low | Strong process-level boundary |
| gVisor (`runsc`) | Low | Intercepts syscalls; designed for untrusted code |
| Firecracker microVM per run | Medium | Hardware-virtualised boundary |
| A dedicated machine | — | The only option that is genuinely simple |

## Upgrading the isolation

`app/sandbox/` is the only place that spawns anything, and it is already a
self-contained package. To move to containers:

1. Keep `run_function_suite` / `run_stdio_suite` as the interface — the
   pipeline does not know or care how a child is made.
2. In `runner.py`, replace `os.posix_spawn` + `_make_preexec` with a
   `docker run` invocation, passing the same `Limits` as container flags.
3. In `capabilities.py`, add a level and a probe.
4. Leave `bootstrap.py`, `protocol.py` and everything above untouched.

The layers above were built to make that a one-file change.

## Responsible use

The application is for comparing algorithms you are studying, and for teaching
why testing is hard. Do not point it at other people's code without permission,
and do not use it to attack anything.
