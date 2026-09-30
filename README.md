# Algorithm Arena

Paste two implementations of the same algorithm, run them against the same
inputs in a sandbox, and find out:

1. **Do they behave the same?** — and, if not, *exactly where* they differ.
2. **Which one is faster and lighter?** — measured fairly enough that the
   answer survives a second run.

> *The algorithms produced identical outputs for 1,000/1,000 test cases. This
> provides empirical evidence that they behave equivalently for the tested
> inputs, but does not mathematically prove equivalence for all possible
> inputs.*

That sentence is the point of the whole project. Agreement on a finite set of
tests is **evidence**, not proof, and the application is careful never to
present it as anything else.

---

## Quick start

```bash
./run.sh                      # creates .venv on first run, then serves on :8000
```

Then open <http://localhost:8000>, click **Load an example pair**, and press
**Run Comparison**.

To install and run manually:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run the tests (204 of them, including live sandbox-escape attempts):

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

---

## How it works

```
   browser  ──POST /api/runs──▶  FastAPI  ──▶ job (background thread)
      ▲                              │
      │  GET /api/runs/{id}          ▼
      └────── progress + report ─── pipeline
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
    testgen                      sandbox                     compare
  (schemas +                 (separate process,           (equivalence +
   strategies)                rlimits, netns,              accuracy)
                               audit hook)
                                     │
                                     ▼
                                 benchmark
                        (interleaved, noise-aware)
```

### Your code

Two ways to submit:

| Mode | What you write | Trade-off |
|---|---|---|
| **Call a function** (default) | `def solve(data): ...` returning the answer | One process for the whole suite, so per-test timings exclude interpreter start-up |
| **stdin → stdout** | A script that reads JSON and prints JSON | Familiar, but each test case pays a full interpreter start-up |

Test inputs are JSON values. In stdin mode the program receives
`{"input": <value>, "id": "<case-id>"}` on stdin.

### Test generation

Pick an **input shape** (a list of integers, a pair of floats, two matrices…)
and any combination of **strategies**:

| Strategy | What it produces |
|---|---|
| Random | Uniformly random values in the configured range |
| Edge cases | Small awkward values (`0`, `1`, `-1`, single characters…) — filtered to stay *valid* for the chosen range |
| Boundary values | The extremes of the range and of the input length |
| Duplicates | Inputs built from a handful of repeated values |
| Empty input | `[]` or `""`, for schemas where that is valid |
| Large input | Several times the configured size, to expose scaling problems |
| User-provided | Whatever you typed in (one JSON value per line) |

Everything is driven by a **seed**, reported back in the results, so any run
can be reproduced exactly.

### Comparing outputs

| Mode | Behaviour |
|---|---|
| **Auto** | Infers the right comparison and tells you what it chose |
| **Strict** | Exact equality, including the *type* (so `1` ≠ `1.0`) and `NaN ≠ NaN` |
| **Float tolerance** | Numbers may differ by a relative/absolute tolerance |
| **Numeric** | Like float tolerance, but numeric strings compare numerically |
| **Unordered** | Order-insensitive, for algorithms that return a set |

Mismatches are reported with the exact path to the first difference
(`$.result[3].total`), both outputs side by side, and the input that caused it.

### Accuracy

For numerical or approximate algorithms, supply a **reference implementation**
(or a JSON object of expected outputs). Both sides are then scored against it
with max / mean absolute error, RMSE and max relative error, plus a count of
how many inputs stayed inside the tolerance. Matching each other is not the
same as being right, and this is where the difference shows up.

### Benchmarking

Benchmarking runs **only if the outputs matched** — comparing the speed of two
algorithms that do different things is not informative. Both implementations
run under identical limits, in the same interpreter, on the same inputs, with
iterations **interleaved** (A, B, A, B…) so that drift in machine load hits both
sides equally. Warm-up iterations are discarded and the methodology is printed
in full under the results.

Differences inside the **noise band** are reported as *"no significant
difference"* rather than as a winner. See [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

---

## Security

Submitted code is treated as hostile. It never runs inside the web server: each
implementation gets its own process, its own throw-away directory, hard CPU /
memory / wall-clock limits, no network, and an audit hook that blocks sockets,
subprocesses, `ctypes` and writes outside its directory. On Linux the child
also runs in a user + network namespace as an unprivileged user.

The application **probes and reports** what the host actually enforces rather
than assuming — the chip in the header and the *Sandbox* table in the results
tell you the real isolation level in force.

Read [docs/SECURITY.md](docs/SECURITY.md) for the threat model, what is *not*
protected against, and how to swap in container-based isolation later.

---

## Project layout

```
app/
  main.py            FastAPI routes
  models.py          request/response schemas
  jobs.py            in-memory job registry with progress
  pipeline.py        orchestration + the verdict wording
  testgen.py         input schemas and generation strategies
  compare.py         equivalence checking and accuracy scoring
  benchmark.py       statistics and the noise analysis
  examples.py        six ready-made algorithm pairs
  config.py          every tunable default, in one place
  sandbox/           ── the execution layer ──
    runner.py        parent side: fork, supervise, collect
    bootstrap.py     child side: the trusted harness
    capabilities.py  what this host can enforce, verified
    protocol.py      how results travel (strict JSON)
  static/            no build step, no CDN, works offline
    index.html  styles.css  editor.js  charts.js  app.js
docs/
  ARCHITECTURE.md  METHODOLOGY.md  SECURITY.md
tests/              204 tests, incl. live escape attempts
```

The frontend is deliberately plain: a syntax-highlighting editor and an SVG
charting helper are ~400 lines each of vanilla JavaScript, so the project has
no build step, no bundler and no CDN dependency. The code editor is the classic
transparent-`<textarea>`-over-highlighted-`<pre>` technique.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for why it is arranged this way.

---

## Known limitations

* **Python only.** Other languages appear in the selector as planned but are
  not implemented; the execution layer is deliberately shaped so a second
  runtime can be added without touching the pipeline.
* **The sandbox is strong but not a security boundary for a hostile
  multi-tenant deployment.** See SECURITY.md. Running untrusted code from
  strangers needs a container, a VM, or a dedicated machine.
* **Wall-clock timing on a shared machine is noisy.** The noise band is honest
  about this, but a quiet machine and more iterations are always better than a
  cleverer statistic.
* Jobs are held in memory and are lost on restart — there is no queue or
  database, by design.

---

## Further reading

* Allen Downey, *Why Testing is Hard* — the essay this project's verdict
  wording is modelled on
* [Steele, *How Not to Write a Microbenchmark*](https://www.coyoteefx.com/talks/quickbrownfox/)
* Python's [`sys.addaudithook`](https://docs.python.org/3/library/sys.html#sys.addaudithook)
  documentation
