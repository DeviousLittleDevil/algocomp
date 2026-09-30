/* Algorithm Arena - dashboard controller.
 *
 * Responsibilities: build the request from the form, start a job, poll it for
 * progress, and render the report. No framework, no build step.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    reference: null,
    editors: {},
    runId: null,
    pollTimer: null,
    cancelled: false,
    lastReport: null
  };

  var STORAGE_KEY = "algorithm-arena.v1";

  /* ==================================================================
   * Boot
   * ================================================================== */

  function boot() {
    state.editors.a = ArenaEditor.create($("editor-a"), {
      value: "",
      onChange: function (value) { updateEditorMeta("a", value); persist(); }
    });
    state.editors.b = ArenaEditor.create($("editor-b"), {
      value: "",
      onChange: function (value) { updateEditorMeta("b", value); persist(); }
    });

    fetch("/api/reference").then(function (r) { return r.json(); }).then(function (data) {
      state.reference = data;
      populateSchemas(data.schemas);
      populateModes(data.comparison_modes);
      populateStrategies(data.strategies);
      renderExamples(data.examples);
      applyDefaults(data);
      restore();
      syncConditionalFields();
    }).catch(function (err) {
      showError("Could not load the server configuration: " + err.message);
    });

    fetch("/api/health").then(function (r) { return r.json(); }).then(renderHealth).catch(function () {});

    wireEvents();
  }

  function applyDefaults(data) {
    var tolerances = data.default_tolerances;
    $("rel-tolerance").value = String(tolerances.rel);
    $("abs-tolerance").value = String(tolerances.abs);
  }

  function wireEvents() {
    $("run-button").addEventListener("click", runComparison);
    $("stop-button").addEventListener("click", stopWaiting);
    $("reseed").addEventListener("click", function () {
      $("seed").value = Math.floor(Math.random() * 1000000);
      persist();
    });

    ["protocol", "entry-point", "spread-args", "schema", "comparison-mode", "name-a", "name-b"].forEach(function (id) {
      $(id).addEventListener("change", function () { syncConditionalFields(); persist(); });
    });
    ["min-value", "max-value", "test-count", "test-size", "seed", "large-factor", "large-count",
     "user-cases", "rel-tolerance", "abs-tolerance", "reference-code", "expected",
     "benchmark-enabled", "iterations", "warmup", "bench-tests", "min-gap",
     "limit-cpu", "limit-wall", "limit-memory", "limit-output"].forEach(function (id) {
      $(id).addEventListener("change", persist);
      $(id).addEventListener("input", persist);
    });

    $("strategy-list").addEventListener("change", function () { syncConditionalFields(); persist(); });
    document.addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        runComparison();
      }
    });
  }

  function syncConditionalFields() {
    var isFunction = $("protocol").value === "function";
    $("entry-point-field").hidden = !isFunction;
    $("spread-field").hidden = !isFunction;
    $("protocol-hint").innerHTML = isFunction
      ? 'Your code should define <code>solve(data)</code> and return the result. It is called once per test ' +
        'case with the generated input; anything it prints is captured but ignored.'
      : 'Your code is run as a script once per test case. It receives <code>{"input": …}</code> as JSON on stdin ' +
        'and should print the result as JSON on stdout.';

    $("large-options").hidden = !selectedStrategies().includes("large");

    var mode = $("comparison-mode").value;
    $("tolerance-fields").hidden = !(mode === "auto" || mode === "float" || mode === "numeric");
    var modeInfo = (state.reference && state.reference.comparison_modes || [])
      .filter(function (m) { return m.id === mode; })[0];
    $("mode-hint").textContent = modeInfo ? modeInfo.description : "";

    var schema = (state.reference && state.reference.schemas || [])
      .filter(function (s) { return s.id === $("schema").value; })[0];
    $("schema-hint").textContent = schema
      ? schema.description + (schema.example ? "  Example: " + schema.example : "")
      : "";
  }

  function selectedStrategies() {
    return Array.prototype.slice
      .call(document.querySelectorAll('#strategy-list input:checked'))
      .map(function (input) { return input.value; });
  }

  /* ==================================================================
   * Form population
   * ================================================================== */

  function populateSchemas(schemas) {
    var select = $("schema");
    select.innerHTML = "";
    schemas.forEach(function (schema) {
      var option = document.createElement("option");
      option.value = schema.id;
      option.textContent = schema.label;
      select.appendChild(option);
    });
    select.value = "int_list";
  }

  function populateModes(modes) {
    var select = $("comparison-mode");
    select.innerHTML = "";
    modes.forEach(function (mode) {
      var option = document.createElement("option");
      option.value = mode.id;
      option.textContent = mode.label;
      select.appendChild(option);
    });
    select.value = "auto";
  }

  function populateStrategies(strategies) {
    var host = $("strategy-list");
    host.innerHTML = "";
    var defaults = { random: true, edge: true, boundary: true, duplicates: true, empty: false, large: false, user: false };
    strategies.forEach(function (strategy) {
      var label = document.createElement("label");
      label.className = "strategy";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.value = strategy.id;
      input.checked = !!defaults[strategy.id];
      var body = document.createElement("div");
      body.className = "strategy-body";
      var strong = document.createElement("strong");
      strong.textContent = strategy.label;
      var span = document.createElement("span");
      span.textContent = strategy.description;
      body.appendChild(strong);
      body.appendChild(span);
      label.appendChild(input);
      label.appendChild(body);
      host.appendChild(label);
    });
  }

  function renderExamples(examples) {
    var host = $("example-list");
    host.innerHTML = "";
    examples.forEach(function (example) {
      var card = document.createElement("button");
      card.type = "button";
      card.className = "example-card";
      var title = document.createElement("strong");
      title.textContent = example.title;
      var blurb = document.createElement("span");
      blurb.textContent = example.blurb;
      var tag = document.createElement("span");
      tag.className = "example-tag";
      tag.textContent = example.category;
      card.appendChild(title);
      card.appendChild(blurb);
      card.appendChild(tag);
      card.addEventListener("click", function () { loadExample(example.id); });
      host.appendChild(card);
    });
  }

  function loadExample(id) {
    fetch("/api/examples/" + encodeURIComponent(id))
      .then(function (r) { return r.json(); })
      .then(function (example) {
        state.editors.a.setValue(example.code_a);
        state.editors.b.setValue(example.code_b);
        $("name-a").value = example.names[0];
        $("name-b").value = example.names[1];
        $("reference-code").value = example.reference_code || "";
        $("protocol").value = example.protocol;
        $("entry-point").value = example.entry_point;
        $("spread-args").checked = !!example.spread_args;
        $("comparison-mode").value = example.comparison_mode;
        $("rel-tolerance").value = String(example.rel_tolerance);
        $("abs-tolerance").value = String(example.abs_tolerance);
        $("benchmark-enabled").checked = example.benchmark.enabled;
        $("iterations").value = example.benchmark.iterations;
        $("warmup").value = example.benchmark.warmup_iterations;
        $("bench-tests").value = example.benchmark.max_tests;

        var tests = example.tests;
        $("schema").value = tests.schema;
        $("min-value").value = tests.min_value;
        $("max-value").value = tests.max_value;
        $("test-count").value = tests.count;
        $("test-size").value = tests.size;
        $("seed").value = tests.seed;
        $("large-factor").value = tests.large_factor;
        $("large-count").value = tests.large_count;
        $("user-cases").value = tests.user_cases || "";
        document.querySelectorAll('#strategy-list input').forEach(function (input) {
          input.checked = tests.strategies.indexOf(input.value) >= 0;
        });
        syncConditionalFields();
        persist();
        scrollTo($("run-button"), { block: "center" });
        $("run-button").focus();
      });
  }

  function updateEditorMeta(side, value) {
    var lines = value ? value.split("\n").length : 0;
    var bytes = new Blob([value]).size;
    $("meta-" + side).textContent = lines + (lines === 1 ? " line · " : " lines · ") + bytes + " B";
  }

  /* ==================================================================
   * Persistence
   * ================================================================== */

  function snapshot() {
    return {
      name_a: $("name-a").value, name_b: $("name-b").value,
      code_a: state.editors.a.value(), code_b: state.editors.b.value(),
      protocol: $("protocol").value, entry_point: $("entry-point").value,
      spread_args: $("spread-args").checked,
      schema: $("schema").value, min_value: $("min-value").value, max_value: $("max-value").value,
      count: $("test-count").value, size: $("test-size").value, seed: $("seed").value,
      large_factor: $("large-factor").value, large_count: $("large-count").value,
      strategies: selectedStrategies(), user_cases: $("user-cases").value,
      comparison_mode: $("comparison-mode").value,
      rel_tolerance: $("rel-tolerance").value, abs_tolerance: $("abs-tolerance").value,
      reference_code: $("reference-code").value, expected: $("expected").value,
      benchmark_enabled: $("benchmark-enabled").checked, iterations: $("iterations").value,
      warmup: $("warmup").value, bench_tests: $("bench-tests").value, min_gap: $("min-gap").value,
      limits: {
        cpu_seconds: $("limit-cpu").value, wall_seconds: $("limit-wall").value,
        memory_mb: $("limit-memory").value, max_output_bytes: String(Number($("limit-output").value) * 1024)
      }
    };
  }

  var persistTimer = null;
  function persist() {
    clearTimeout(persistTimer);
    persistTimer = setTimeout(function () {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot())); } catch (e) { /* quota */ }
    }, 400);
  }

  function restore() {
    var raw;
    try { raw = localStorage.getItem(STORAGE_KEY); } catch (e) { return; }
    if (!raw) return;
    var data;
    try { data = JSON.parse(raw); } catch (e) { return; }
    if (!data) return;

    state.editors.a.setValue(data.code_a || "");
    state.editors.b.setValue(data.code_b || "");
    setValue("name-a", data.name_a); setValue("name-b", data.name_b);
    setValue("protocol", data.protocol); setValue("entry-point", data.entry_point);
    $("spread-args").checked = !!data.spread_args;
    setValue("schema", data.schema); setValue("min-value", data.min_value);
    setValue("max-value", data.max_value); setValue("test-count", data.count);
    setValue("test-size", data.size); setValue("seed", data.seed);
    setValue("large-factor", data.large_factor); setValue("large-count", data.large_count);
    setValue("user-cases", data.user_cases);
    if (data.strategies) {
      document.querySelectorAll("#strategy-list input").forEach(function (input) {
        input.checked = data.strategies.indexOf(input.value) >= 0;
      });
    }
    setValue("comparison-mode", data.comparison_mode);
    setValue("rel-tolerance", data.rel_tolerance); setValue("abs-tolerance", data.abs_tolerance);
    setValue("reference-code", data.reference_code); setValue("expected", data.expected);
    $("benchmark-enabled").checked = data.benchmark_enabled !== false;
    setValue("iterations", data.iterations); setValue("warmup", data.warmup);
    setValue("bench-tests", data.bench_tests); setValue("min-gap", data.min_gap);
    if (data.limits) {
      setValue("limit-cpu", data.limits.cpu_seconds);
      setValue("limit-wall", data.limits.wall_seconds);
      setValue("limit-memory", data.limits.memory_mb);
      if (data.limits.max_output_bytes) {
        setValue("limit-output", Math.round(Number(data.limits.max_output_bytes) / 1024));
      }
    }
    syncConditionalFields();
  }

  function setValue(id, value) {
    if (value === undefined || value === null) return;
    var node = $(id);
    if (node) node.value = value;
  }

  /* ==================================================================
   * Running
   * ================================================================== */

  function buildRequest() {
    var data = snapshot();
    return {
      algorithm_a: { name: data.name_a, code: data.code_a },
      algorithm_b: { name: data.name_b, code: data.code_b },
      language: $("language").value,
      protocol: data.protocol,
      entry_point: data.entry_point,
      spread_args: data.spread_args,
      tests: {
        schema: data.schema,
        count: Number(data.count),
        size: Number(data.size),
        min_value: Number(data.min_value),
        max_value: Number(data.max_value),
        seed: Number(data.seed),
        strategies: data.strategies,
        large_factor: Number(data.large_factor),
        large_count: Number(data.large_count),
        user_cases: data.user_cases,
        expected: data.expected
      },
      comparison_mode: data.comparison_mode,
      rel_tolerance: parseFloat(data.rel_tolerance) || 0,
      abs_tolerance: parseFloat(data.abs_tolerance) || 0,
      reference_code: data.reference_code || null,
      benchmark: {
        enabled: data.benchmark_enabled,
        iterations: Number(data.iterations),
        warmup_iterations: Number(data.warmup),
        max_tests: Number(data.bench_tests),
        min_relative_gap: (Number(data.min_gap) || 0) / 100
      },
      limits: {
        cpu_seconds: Number(data.limits.cpu_seconds),
        wall_seconds: Number(data.limits.wall_seconds),
        memory_mb: Number(data.limits.memory_mb),
        max_output_bytes: Number(data.limits.max_output_bytes)
      }
    };
  }

  function runComparison() {
    var payload = buildRequest();
    hideError();
    state.cancelled = false;

    if (!payload.algorithm_a.code.trim() || !payload.algorithm_b.code.trim()) {
      showError("Both editors need code before you can run a comparison.");
      return;
    }

    setRunning(true);
    $("results").hidden = true;
    $("run-summary").hidden = true;
    $("progress-wrap").hidden = false;
    setProgress(0.02, "Submitting…");

    fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (response) {
      return response.json().then(function (body) { return { ok: response.ok, body: body }; });
    }).then(function (result) {
      if (!result.ok) {
        var detail = result.body && result.body.detail ? result.body.detail : "The server rejected the request.";
        throw new Error(detail);
      }
      state.runId = result.body.run_id;
      setProgress(0.05, "Queued — id " + state.runId.slice(0, 8));
      poll();
    }).catch(function (err) {
      setRunning(false);
      showError(err.message || String(err));
    });
  }

  function stopWaiting() {
    state.cancelled = true;
    clearTimeout(state.pollTimer);
    setRunning(false);
    $("run-summary").hidden = false;
    $("run-summary").innerHTML =
      "<span class='chip chip-warn'><span class='chip-dot'></span>Stopped waiting — the comparison is still running on the server.</span>";
  }

  function poll() {
    if (state.cancelled || !state.runId) return;
    fetch("/api/runs/" + state.runId).then(function (r) { return r.json(); }).then(function (job) {
      if (state.cancelled) return;
      setProgress(Math.max(0.03, job.progress || 0), job.message || job.stage || "Working…");

      if (job.status === "error") {
        setRunning(false);
        showError(job.error || "The comparison failed.");
        return;
      }
      if (job.status === "done") {
        setProgress(1, "Done");
        setTimeout(function () {
          setRunning(false);
          state.lastReport = job.result;
          render(job.result);
        }, 220);
        return;
      }
      state.pollTimer = setTimeout(poll, 400);
    }).catch(function (err) {
      if (state.cancelled) return;
      setRunning(false);
      showError("Lost contact with the server: " + err.message);
    });
  }

  function setRunning(running) {
    $("run-button").disabled = running;
    $("run-button").innerHTML = running
      ? "<span class='spinner'></span> Comparing…"
      : "<span class='btn-icon' aria-hidden='true'>▶</span> Run Comparison";
    $("stop-button").hidden = !running;
    $("progress-wrap").hidden = !running;
  }

  function setProgress(fraction, message) {
    $("progress-bar").style.width = Math.round(fraction * 100) + "%";
    $("progress-pct").textContent = Math.round(fraction * 100) + "%";
    $("progress-stage").textContent = message;
  }

  function showError(message) {
    var banner = $("error-banner");
    banner.hidden = false;
    banner.innerHTML = "<strong>Could not run the comparison</strong>" + escapeHtml(message);
    scrollTo(banner, { block: "center" });
  }

  function hideError() { $("error-banner").hidden = true; }

  function renderHealth(health) {
    var chip = $("sandbox-chip");
    var label = $("sandbox-label");
    var sandbox = health.sandbox || {};
    if (sandbox.level === "namespace_nobody") {
      chip.className = "chip chip-ok";
      label.textContent = "sandboxed · no network · unprivileged";
      chip.title = "User + network namespace, running as uid 65534 (" + (sandbox.notes || []).join("; ") + ")";
    } else if (sandbox.level === "namespace") {
      chip.className = "chip chip-ok";
      label.textContent = "sandboxed · no network";
      chip.title = "Network namespace isolation active";
    } else if (sandbox.level === "none") {
      chip.className = "chip chip-warn";
      label.textContent = "sandboxed · limits only";
      chip.title = "Namespaces unavailable on this host; CPU/memory/time limits and the audit hook still apply";
    }
  }

  /* ==================================================================
   * Rendering
   * ================================================================== */

  function render(report) {
    state.lastReport = report;
    var host = $("results");
    host.innerHTML = "";
    host.hidden = false;
    host.appendChild(renderVerdict(report));
    host.appendChild(renderStatGrid(report));
    host.appendChild(renderCorrectness(report));
    if (report.accuracy) host.appendChild(renderAccuracy(report));
    if (report.performance) host.appendChild(renderPerformance(report));
    if (report.warnings && report.warnings.length) host.appendChild(renderWarnings(report));
    host.appendChild(renderRunDetails(report));
    scrollTo(host, { behavior: "smooth", block: "start" });
  }

  function renderVerdict(report) {
    var verdict = report.verdict || {};
    var kind = verdict.status === "empirically_equivalent" ? "ok"
      : verdict.status === "different" ? "diff" : "unknown";
    var icon = kind === "ok" ? "✓" : kind === "diff" ? "✗" : "?";

    var section = node("section", { className: "verdict verdict-" + kind });
    var label = node("div", { className: "verdict-label" }, icon + "  " + labelFor(verdict.status));
    section.appendChild(label);
    section.appendChild(node("h2", {}, verdict.headline || "Result"));
    section.appendChild(node("p", { className: "verdict-statement" }, verdict.statement || ""));

    if (verdict.fatal && verdict.fatal.length) {
      var list = node("div", { className: "warnings" });
      verdict.fatal.forEach(function (item) {
        list.appendChild(warning("error", "Algorithm " + item.side + " (" + item.status + "): " + item.message));
      });
      section.appendChild(list);
    }

    var caveat = node("div", { className: "verdict-caveat" });
    caveat.appendChild(node("b", {}, "What this does and does not mean: "));
    caveat.appendChild(document.createTextNode(verdict.disclaimer || ""));
    section.appendChild(caveat);
    return section;
  }

  function labelFor(status) {
    if (status === "empirically_equivalent") return "Empirically equivalent on the tested inputs";
    if (status === "different") return "Not equivalent on the tested inputs";
    return "Inconclusive";
  }

  function renderStatGrid(report) {
    var correctness = report.correctness || {};
    var perf = report.performance;
    var grid = node("div", { className: "stat-grid" });
    var total = correctness.total || 0;
    var passed = correctness.passed || 0;
    var failed = (correctness.failed || 0) + (correctness.errored || 0);

    grid.appendChild(statCard("Tests passed", passed + " / " + total,
      total ? Math.round((passed / total) * 100) + "% match rate" : "no tests",
      passed === total && total > 0 ? "ok" : failed ? "err" : ""));

    grid.appendChild(statCard("Tests failed / errored", String(failed),
      (correctness.errored || 0) + " raised an error",
      failed ? "err" : "ok"));

    if (perf) {
      var a = perf.timing.a, b = perf.timing.b;
      var verdict = perf.verdict || {};
      grid.appendChild(statCard(
        "Average runtime · A", ArenaCharts.formatMsUnit(a.mean),
        "min " + ArenaCharts.formatMs(a.min) + " · max " + ArenaCharts.formatMs(a.max) +
        " · σ " + ArenaCharts.formatMs(a.stdev), "stat-a"));
      grid.appendChild(statCard(
        "Average runtime · B", ArenaCharts.formatMsUnit(b.mean),
        "min " + ArenaCharts.formatMs(b.min) + " · max " + ArenaCharts.formatMs(b.max) +
        " · σ " + ArenaCharts.formatMs(b.stdev), "stat-b"));
      // difference_percent is (B - A): positive means B took longer.
      var diff = verdict.difference_percent;
      var slower = diff === null || diff === undefined ? "" : (diff > 0
        ? report.algorithms.b.name + " is " + diff.toFixed(1) + "% slower"
        : report.algorithms.a.name + " is " + Math.abs(diff).toFixed(1) + "% slower");
      grid.appendChild(statCard(
        "Relative runtime difference",
        diff === null || diff === undefined ? "–" : (diff > 0 ? "+" : "−") + Math.abs(diff).toFixed(1) + "%",
        (slower || "") + (verdict.within_noise
          ? " · inside the ±" + (verdict.noise_band_percent || 0).toFixed(1) + "% noise band"
          : " · outside the ±" + (verdict.noise_band_percent || 0).toFixed(1) + "% noise band"),
        verdict.within_noise ? "warn" : "ok"));

      var mem = perf.memory || {};
      grid.appendChild(statCard(
        "Memory used by the algorithm",
        (mem.a && mem.a.added_kb ? (mem.a.added_kb / 1024).toFixed(2) : "–") + " / " +
        (mem.b && mem.b.added_kb ? (mem.b.added_kb / 1024).toFixed(2) : "–") + " MB",
        "peak resident set " + (mem.a ? (mem.a.peak_kb / 1024).toFixed(1) : "–") + " / " +
        (mem.b ? (mem.b.peak_kb / 1024).toFixed(1) : "–") + " MB, interpreter included",
        ""));
    } else {
      grid.appendChild(statCard("Comparison mode", correctness.resolved_mode || correctness.mode || "–",
        "how outputs were compared", ""));
      var gen = report.test_generation || {};
      grid.appendChild(statCard("Test cases built", String(gen.total || total),
        (gen.strategies || []).join(", "), ""));
    }
    return grid;
  }

  function statCard(label, value, sub, extra) {
    var card = node("div", { className: "stat " + (extra || "") });
    card.appendChild(node("div", { className: "stat-label" }, label));
    card.appendChild(node("div", { className: "stat-value" }, value));
    if (sub) card.appendChild(node("div", { className: "stat-sub" }, sub));
    return card;
  }

  function renderCorrectness(report) {
    var correctness = report.correctness || {};
    var section = node("section", { className: "result-section" });
    section.appendChild(node("h3", {}, "Correctness"));
    section.appendChild(node("p", { className: "result-note" },
      "Outputs were compared with the “" + (correctness.resolved_mode || correctness.mode) +
      "” mode" + (correctness.resolved_mode && correctness.mode === "auto" ? " (chosen automatically)" : "") +
      ". A test only passes when both implementations finished without an error and returned equal values."));

    var split = node("div", { className: "split" });

    var byCategory = node("div", { className: "card" });
    byCategory.appendChild(node("h4", {}, "Results by test strategy"));
    byCategory.appendChild(node("p", { className: "card-sub" },
      "Coverage matters: a suite that only generates random inputs will not catch boundary bugs."));
    var chartHost = node("div", {});
    byCategory.appendChild(chartHost);
    split.appendChild(byCategory);

    var suiteCard = node("div", { className: "card" });
    suiteCard.appendChild(node("h4", {}, "Execution summary"));
    suiteCard.appendChild(node("p", { className: "card-sub" }, "How each implementation ran inside the sandbox."));
    suiteCard.appendChild(suiteTable(correctness.a, correctness.b, report));
    split.appendChild(suiteCard);

    section.appendChild(split);

    if (correctness.mismatches && correctness.mismatches.length) {
      section.appendChild(node("h3", {}, "Example differences (" + correctness.mismatches.length + ")"));
      var shown = correctness.mismatches.slice(0, 12);
      shown.forEach(function (mismatch) { section.appendChild(renderMismatch(mismatch)); });
      if (correctness.mismatches.length > shown.length) {
        section.appendChild(node("p", { className: "result-note" },
          "…and " + (correctness.mismatches.length - shown.length) + " more. The full report below contains every case."));
      }
    }

    if (correctness.per_case && correctness.per_case.length) {
      var details = node("details", { className: "raw" });
      details.appendChild(node("summary", {}, "Per-test results (" + correctness.per_case.length + " cases)"));
      details.appendChild(perCaseTable(correctness.per_case));
      section.appendChild(details);
    }
    return section;
  }

  function suiteTable(a, b, report) {
    var table = node("table");
    var head = node("thead");
    var headRow = node("tr");
    ["", report.algorithms.a.name, report.algorithms.b.name].forEach(function (label) {
      headRow.appendChild(node("th", {}, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    var rows = [
      ["Process", a && a.status, b && b.status],
      ["Tests run", (a && a.tests_run) || 0, (b && b.tests_run) || 0],
      ["Completed without error", (a && a.tests_ok) || 0, (b && b.tests_ok) || 0],
      ["Raised an error", (a && a.tests_errored) || 0, (b && b.tests_errored) || 0],
      ["Wall-clock (ms)", a && a.wall_ms, b && b.wall_ms],
      ["Process CPU (ms)", a && a.process_cpu_ms, b && b.process_cpu_ms],
      ["Peak RSS (MB)", mb(a && a.process_peak_rss_kb), mb(b && b.process_peak_rss_kb)],
      ["Memory attribution", a && a.memory_attribution, b && b.memory_attribution]
    ];
    var body = node("tbody");
    rows.forEach(function (row) {
      var tr = node("tr");
      tr.appendChild(node("td", {}, row[0]));
      tr.appendChild(node("td", { className: "mono" }, String(row[1])));
      tr.appendChild(node("td", { className: "mono" }, String(row[2])));
      body.appendChild(tr);
    });
    table.appendChild(body);
    return wrapTable(table);
  }

  function mb(kb) { return kb ? (kb / 1024).toFixed(1) + " MB" : "–"; }

  function perCaseTable(cases) {
    var table = node("table");
    var head = node("thead");
    var headRow = node("tr");
    ["#", "Category", "Result", "Reason"].forEach(function (label, i) {
      headRow.appendChild(node("th", { className: i === 0 ? "num" : "" }, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);
    var body = node("tbody");
    var limit = Math.min(cases.length, 300);
    for (var i = 0; i < limit; i++) {
      var record = cases[i];
      var tr = node("tr");
      tr.appendChild(node("td", { className: "num" }, String(record.index + 1)));
      tr.appendChild(node("td", {}, record.category || "–"));
      tr.appendChild(node("td", { className: record.status }, record.status));
      tr.appendChild(node("td", { className: "wide mono" }, record.reason || "outputs matched"));
      body.appendChild(tr);
    }
    table.appendChild(body);
    return wrapTable(table);
  }

  function renderMismatch(mismatch) {
    var details = node("details", { className: "mismatch" });
    var summary = node("summary");
    summary.appendChild(node("span", { className: "mismatch-label" },
      "Test " + (mismatch.index + 1) + " · " + (mismatch.category || "case") +
      (mismatch.label ? " · " + mismatch.label : "")));
    summary.appendChild(node("span", { className: "mismatch-reason" },
      (mismatch.path ? mismatch.path + " — " : "") + (mismatch.reason || "")));
    details.appendChild(summary);

    var body = node("div", { className: "mismatch-body" });
    if (mismatch.input_preview) {
      var inputBox = node("div", { className: "diff-input" });
      inputBox.appendChild(node("h5", {}, "Input"));
      inputBox.appendChild(node("pre", {}, mismatch.input_preview));
      body.appendChild(inputBox);
    }
    var grid = node("div", { className: "diff-grid" });
    grid.appendChild(side("Algorithm A", mismatch.a_output, mismatch.a_error, "diff-a"));
    grid.appendChild(side("Algorithm B", mismatch.b_output, mismatch.b_error, "diff-b"));
    body.appendChild(grid);
    details.appendChild(body);
    return details;
  }

  function side(title, output, error, extraClass) {
    var box = node("div", { className: "diff-side " + extraClass });
    box.appendChild(node("h5", {}, title));
    box.appendChild(node("pre", {}, error ? "⚠ " + error : (output || "(no output)")));
    return box;
  }

  function renderAccuracy(report) {
    var accuracy = report.accuracy;
    var section = node("section", { className: "result-section" });
    section.appendChild(node("h3", {}, "Accuracy vs. reference"));
    section.appendChild(node("p", { className: "result-note" },
      "Matching each other is not the same as being right. Where a reference was supplied, every number in the " +
      "output is compared against it and the error is reported here."));

    if (accuracy.error) {
      section.appendChild(warning("error", accuracy.error));
      return section;
    }

    var split = node("div", { className: "split" });
    split.appendChild(accuracyCard(report.algorithms.a.name, accuracy.a));
    split.appendChild(accuracyCard(report.algorithms.b.name, accuracy.b));
    section.appendChild(split);
    return section;
  }

  function accuracyCard(name, report_) {
    var card = node("div", { className: "card" });
    card.appendChild(node("h4", {}, name));
    if (!report_ || !report_.available) {
      card.appendChild(node("p", { className: "hint" }, "No accuracy data."));
      return card;
    }
    card.appendChild(node("p", { className: "card-sub" },
      (report_.compared || 0) + " inputs scored against the reference; tolerance " +
      "rel " + report_.rel_tolerance + " / abs " + report_.abs_tolerance + "."));

    var table = node("table");
    var rows = [
      ["Inputs scored", report_.compared],
      ["Within tolerance", report_.within_tolerance],
      ["Outside tolerance", report_.outside_tolerance],
      ["Max absolute error", fmtError(report_.max_abs_error)],
      ["Mean absolute error", fmtError(report_.mean_abs_error)],
      ["Root mean square error", fmtError(report_.rmse)],
      ["Max relative error", fmtError(report_.max_rel_error)]
    ];
    var body = node("tbody");
    rows.forEach(function (row) {
      var tr = node("tr");
      tr.appendChild(node("td", {}, row[0]));
      var cell = node("td", { className: "num" }, String(row[1]));
      if (row[0] === "Outside tolerance" && row[1] > 0) cell.className += " fail";
      if (row[0] === "Within tolerance" && row[1] > 0) cell.className += " pass";
      tr.appendChild(cell);
      body.appendChild(tr);
    });
    table.appendChild(body);
    card.appendChild(wrapTable(table));

    (report_.notes || []).forEach(function (note) {
      card.appendChild(node("p", { className: "hint hint-tight" }, note));
    });
    return card;
  }

  function fmtError(value) {
    if (value === null || value === undefined) return "–";
    if (value === 0) return "0 (exact)";
    if (Math.abs(value) < 1e-4 || Math.abs(value) >= 1e6) return value.toExponential(3);
    return value.toPrecision(4);
  }

  function renderPerformance(report) {
    var perf = report.performance;
    var section = node("section", { className: "result-section" });
    section.appendChild(node("h3", {}, "Performance"));
    section.appendChild(node("p", { className: "result-note" },
      "Measured over " + perf.iterations + " iterations of " + perf.test_count +
      " test cases, after " + perf.warmup_iterations + " discarded warm-up iteration(s). " +
      "Iterations alternate A, B, A, B… so drift in machine load hits both sides equally."));

    var verdict = perf.verdict || {};
    var banner = node("div", { className: "verdict " + (verdict.within_noise ? "verdict-unknown" : "verdict-ok") });
    banner.style.marginBottom = "14px";
    banner.appendChild(node("div", { className: "verdict-label" },
      (verdict.within_noise ? "≈" : "⚡") + "  " + (verdict.label || "Performance")));
    banner.appendChild(node("p", { className: "verdict-statement" }, verdict.explanation || ""));
    var meta = node("p", { className: "verdict-caveat" },
      "Noise band ±" + (verdict.noise_band_percent || 0).toFixed(1) + "% · observed variation up to " +
      (verdict.noise_percent || 0).toFixed(1) + "% (coefficient of variation) · confidence: " +
      (verdict.confidence || "–") + ".");
    banner.appendChild(meta);
    section.appendChild(banner);

    var split = node("div", { className: "split" });

    var timing = node("div", { className: "card" });
    timing.appendChild(node("h4", {}, "Average runtime"));
    timing.appendChild(node("p", { className: "card-sub" }, "Mean per iteration, with the observed min–max range."));
    var timingHost = node("div", {});
    timing.appendChild(timingHost);
    split.appendChild(timing);

    var samples = node("div", { className: "card" });
    samples.appendChild(node("h4", {}, "Iteration samples"));
    samples.appendChild(node("p", { className: "card-sub" },
      "Every individual measurement. Flat, well-separated lines mean the result is trustworthy."));
    var sampleHost = node("div", {});
    samples.appendChild(sampleHost);
    split.appendChild(samples);
    section.appendChild(split);

    var perTest = node("div", { className: "card" });
    perTest.appendChild(node("h4", {}, "Per-test-case runtime"));
    perTest.appendChild(node("p", { className: "card-sub" },
      "Where the time actually goes, and which implementation wins on each individual input."));
    var perTestHost = node("div", {});
    perTest.appendChild(perTestHost);
    section.appendChild(perTest);

    var memory = node("div", { className: "card" });
    memory.appendChild(node("h4", {}, "Memory"));
    memory.appendChild(node("p", { className: "card-sub" }, (perf.memory && perf.memory.summary) || ""));
    var memoryHost = node("div", {});
    memory.appendChild(memoryHost);
    section.appendChild(memory);

    // Charts are drawn after the nodes are in the document.
    setTimeout(function () {
      ArenaCharts.timingBars(timingHost, {
        a: perf.timing.a, b: perf.timing.b, iterations: perf.iterations,
        labelA: report.algorithms.a.name, labelB: report.algorithms.b.name
      });
      ArenaCharts.iterationLines(sampleHost, [
        { label: report.algorithms.a.name, samples: perf.timing.a.samples, cls: "series-a" },
        { label: report.algorithms.b.name, samples: perf.timing.b.samples, cls: "series-b" }
      ]);
      ArenaCharts.perTestScatter(perTestHost, perf.per_test_pairs || [],
        { labelA: report.algorithms.a.name, labelB: report.algorithms.b.name });
      var mem = perf.memory || {};
      if (mem.a && mem.b) {
        var onAdded = mem.basis === "added" && mem.a.added_kb && mem.b.added_kb;
        ArenaCharts.metricBars(memoryHost, {
          entries: [
            { label: report.algorithms.a.name,
              value: onAdded ? mem.a.added_kb : mem.a.peak_kb,
              display: ((onAdded ? mem.a.added_kb : mem.a.peak_kb) / 1024).toFixed(2) + " MB", cls: "bar-a" },
            { label: report.algorithms.b.name,
              value: onAdded ? mem.b.added_kb : mem.b.peak_kb,
              display: ((onAdded ? mem.b.added_kb : mem.b.peak_kb) / 1024).toFixed(2) + " MB", cls: "bar-b" },
            { label: "Python interpreter",
              value: mem.a.baseline_kb,
              display: (mem.a.baseline_kb / 1024).toFixed(2) + " MB", cls: "baseline-bar" }
          ],
          note: onAdded
            ? "Memory each algorithm added on top of what was already resident. The interpreter's own " +
              (mem.a.baseline_kb / 1024).toFixed(1) + " MB is identical on both sides and excluded."
            : ((mem.above_baseline && mem.above_baseline.summary) || "")
        });
      }
    }, 0);

    var method = node("details", { className: "raw" });
    method.appendChild(node("summary", {}, "Benchmarking methodology — how these numbers were obtained"));
    var list = node("ul", { className: "method-list" });
    (perf.methodology || []).forEach(function (item) { list.appendChild(node("li", {}, item)); });
    method.appendChild(list);
    section.appendChild(method);

    return section;
  }

  function renderWarnings(report) {
    var section = node("section", { className: "result-section" });
    section.appendChild(node("h3", {}, "Things worth knowing"));
    var host = node("div", { className: "warnings" });
    report.warnings.forEach(function (item) { host.appendChild(warning(item.level, item.message)); });
    section.appendChild(host);
    return section;
  }

  function warning(level, message) {
    var box = node("div", { className: "warning warning-" + (level || "info") });
    box.appendChild(node("span", { className: "warning-icon" },
      level === "error" ? "✕" : level === "warning" ? "!" : "i"));
    box.appendChild(node("span", {}, message));
    return box;
  }

  function renderRunDetails(report) {
    var section = node("section", { className: "result-section" });
    section.appendChild(node("h3", {}, "Run details"));

    var gen = report.test_generation || {};
    var stats = gen.stats || {};
    var testCard = node("div", { className: "card" });
    testCard.appendChild(node("h4", {}, "Test suite"));
    var dl = node("dl", { className: "kv" });
    [
      ["Input shape", gen.schema_label || gen.schema],
      ["Strategies", (gen.strategies || []).join(", ") || "–"],
      ["Per strategy", JSON.stringify(gen.per_strategy || {})],
      ["Seed", gen.seed],
      ["Value range", "[" + gen.min_value + ", " + gen.max_value + "]"],
      ["Typical size", gen.size],
      ["Test cases", stats.count + " (" + (stats.total_elements || 0) + " elements, largest " + (stats.largest || 0) + ")"],
      ["Language", report.language + (report.protocol === "stdio" ? " · stdin/stdout" : " · function " + (report.entry_point || ""))],
      ["Duration", report.duration_seconds + " s"]
    ].forEach(function (row) {
      dl.appendChild(node("dt", {}, row[0]));
      dl.appendChild(node("dd", {}, String(row[1])));
    });
    testCard.appendChild(dl);
    section.appendChild(testCard);

    var sandbox = report.sandbox || {};
    var capabilities = sandbox.capabilities || {};
    var limits = sandbox.limits || {};
    var sandboxCard = node("div", { className: "card" });
    sandboxCard.appendChild(node("h4", {}, "Sandbox"));
    sandboxCard.appendChild(node("p", { className: "card-sub" },
      "Submitted code never runs inside the web server. Each implementation executes in its own process with " +
      "hard resource limits, a minimal environment, no network, and write access to a single throw-away directory."));
    var sdl = node("dl", { className: "kv" });
    [
      ["Isolation level", capabilities.level || "–"],
      ["Network namespace", capabilities.network_isolated ? "yes (no interfaces at all)" : "not available"],
      ["Socket creation", capabilities.network_blocked_by_hook ? "blocked" : "not blocked"],
      ["Privileges dropped", capabilities.privileges_dropped ? "yes (uid 65534)" : "no"],
      ["Writes outside the sandbox", capabilities.filesystem_restricted ? "blocked" : "not blocked"],
      ["Subprocess creation", capabilities.subprocess_blocked ? "blocked" : "not blocked"],
      ["ctypes", capabilities.ctypes_blocked ? "blocked" : "allowed"],
      ["Per-test memory attribution", capabilities.per_test_memory_supported ? "yes (VmHWM reset per test)" : "no (cumulative only)"],
      ["CPU limit / test", limits.cpu_seconds + " s"],
      ["Wall limit / test", limits.wall_seconds + " s"],
      ["Memory limit", limits.memory_mb + " MB"],
      ["Network", limits.deny_network ? "blocked" : "allowed"],
      ["Subprocesses", limits.deny_subprocess ? "blocked" : "allowed"],
      ["ctypes", limits.allow_ctypes ? "allowed" : "blocked"]
    ].forEach(function (row) {
      sdl.appendChild(node("dt", {}, row[0]));
      sdl.appendChild(node("dd", {}, String(row[1])));
    });
    sandboxCard.appendChild(sdl);
    section.appendChild(sandboxCard);

    var raw = node("details", { className: "raw" });
    raw.appendChild(node("summary", {}, "Full report as JSON"));
    raw.appendChild(node("pre", {}, JSON.stringify(report, null, 2)));
    section.appendChild(raw);
    return section;
  }

  /* ==================================================================
   * Small DOM helpers
   * ================================================================== */

  /** scrollIntoView is not available in every environment (tests, older WebViews). */
  function scrollTo(element, options) {
    if (element && typeof element.scrollIntoView === "function") {
      element.scrollIntoView(options);
    }
  }

  function node(tag, attrs, text) {
    var element = document.createElement(tag);
    if (attrs) {
      for (var key in attrs) {
        if (key === "className") element.className = attrs[key];
        else if (key === "innerHTML") element.innerHTML = attrs[key];
        else if (attrs[key] !== null && attrs[key] !== undefined) element.setAttribute(key, attrs[key]);
      }
    }
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function wrapTable(table) {
    var host = node("div", { className: "table-scroll" });
    host.appendChild(table);
    return host;
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = String(text);
    return div.innerHTML;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
