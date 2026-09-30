# Architecture

## The one rule

**The web server never runs anybody's code.**

Everything else follows from that. `app/main.py` handles HTTP, stores reports in
memory and returns JSON. It imports `app.pipeline`, which coordinates work, and
`app/sandbox`, which is the *only* thing that ever touches submitted source. A
bug in comparison logic or a bad regex cannot execute user code; the worst it
can do is produce a wrong report.

## Layers

```
app/main.py        HTTP. routing, validation, static files. no business logic.
app/jobs.py        thread-pool + in-memory registry; owns job lifecycle.
app/pipeline.py    orchestrates one run: generate → execute → compare → benchmark.
app/testgen.py     pure functions: schema → list of JSON inputs.
app/compare.py     pure functions: two JSON values → verdict.
app/benchmark.py   pure functions: measurements → statistics.
app/examples.py    data only: six (a, b, schema) triples.
app/config.py      every default limit, timeout and path, in one place.
app/sandbox/       the trust boundary.
```

`testgen`, `compare` and `benchmark` are pure and have no idea a sandbox
exists — they take values in and give values out. That is what makes them
cheap to test (91 of the 204 tests never spawn a process at all) and what makes them
easy to reason about.

The dependency graph is acyclic and shallow:

```
main → jobs → pipeline → {testgen, sandbox, compare, benchmark}
                     → examples
```

`testgen`, `compare`, `benchmark` and `examples` import nothing but each other
and `config`. `sandbox` imports `config` and its own three modules. Nothing
imports `main`. Adding a second language means adding a runtime to `sandbox`
and a field to the config — not a new service, not a message queue.

## The sandbox package

Four modules, split by which side of the process boundary they live on.

| Module | Side | Responsibility |
|---|---|---|
| `capabilities.py` | parent | Probe, once, what this host can actually enforce |
| `bootstrap.py` | child | The *trusted* harness. The only untrusted code it loads is yours, in a fresh namespace |
| `runner.py` | parent | Fork, set up isolation, supervise, reap, collect |
| `protocol.py` | parent | Decode a child's JSON result strictly, render previews |

`bootstrap.py` is the interesting design point. It cannot be allowed to crash
because a submission crashed, and it cannot afford to be clever. So it does the
minimum: read a spec, import the user's module once, call the entry point per
test, and print a single JSON line. Everything dangerous is either impossible
(its own allocation limit) or a dictionary lookup (`sys.modules` gets a fake
`__main__`). Any exception in user code becomes a data structure, not a crash.

### Why measure memory from inside the child

`getrusage(RUSAGE_CHILDREN).ru_maxrss` and `os.wait4().rusage` both report the
**maximum over all reaped children**, so on a forked/exec'd worker they include
the parent's page tables through the fork. They also cannot be read *between*
test cases. `VmHWM` from `/proc/self/status`, sampled by the child itself, is
per-test and unaffected by the fork. The runner still records `ru_maxrss` for
the whole process as a cross-check, and the report shows the interpreter
baseline separately from the memory the submission actually added.

### Why the results are JSON

The protocol is deliberately boring: one line of strict JSON on stdout, read
back and validated. The costs of this choice are paid knowingly — arbitrary
objects do not round-trip, and the writer is bounded and truncates deliberately
— in exchange for a result that cannot be corrupted by a stray `print()` in the
submission.

## Jobs

A `ThreadPoolExecutor` with a bounded queue. The HTTP request returns `202` and
an id immediately; the browser polls `GET /api/runs/{id}` and renders whatever
`progress` says. A job is a `Thread` only because the work is *waiting* on
child processes — there is no CPU-bound work in the server, and all the real
parallelism is in the sandbox, one process per implementation.

State lives in a `dict` behind an `RLock`, and expired jobs are reaped
opportunistically. No Redis, no database, no Celery: the job lives as long as
the process, which is exactly as long as a single-server university project
needs it.

## Frontend

Five static files, no build step, no CDN, no framework:

| File | Role |
|---|---|
| `index.html` | Structure and semantics |
| `styles.css` | One dark theme, CSS custom properties, no preprocessor |
| `editor.js` | `ArenaEditor` — highlighted `<pre>` under a transparent `<textarea>` |
| `charts.js` | `ArenaCharts` — bar charts as inline SVG |
| `app.js` | State, API calls, rendering, progress polling |

The editor technique is the classic one: a `<textarea>` with `color: transparent`
and a matching caret sits on top of a `<pre>` that contains the same text
wrapped in `<span class="tok-*">`. The browser handles selection, undo,
IME and accessibility for free; the highlighter is 120 lines of regex.

The two halves of the report — **correctness** and **performance** — are
separate DOM sections with separate headings, because "they agree" and "one is
faster" are different claims and a reader should never have to guess which one
a number belongs to. The headline verdict is rendered in a banner that always
includes the empirical-evidence caveat.

## Deliberate non-goals

* **No microservices.** One process, four packages.
* **No database.** In-memory state, lost on restart, which is fine.
* **No build step.** A project you can clone and `run.sh` is a project people
  can actually read.
* **No plugin system.** If the runner ever needs to be replaced, that happens
  by editing `sandbox/runner.py` and keeping the same three functions.

## Testing

| Suite | Tests | What it covers |
|---|---|---|
| `test_testgen.py` | 27 | Schemas, strategies, seeding, type fidelity, edge shapes |
| `test_compare.py` | 42 | Tolerance modes, mismatch paths, NaN, accuracy scoring |
| `test_benchmark.py` | 23 | Statistics, noise band, winner selection |
| `test_sandbox.py` | 56 | Real subprocesses: escapes, hangs, bombs, encodings, isolation |
| `test_api.py` | 26 | The HTTP contract end to end |
| `test_examples.py` | 30 | Every bundled pair actually behaves as advertised |

`tests/ui_smoke_test.js` additionally drives the real DOM under jsdom against
a live server (~50 assertions). It needs jsdom, which is why it is separate
from the pytest suite: the application itself has no JavaScript dependencies.
