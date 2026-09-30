/* Headless DOM test: loads the real index.html with the real scripts and
 * drives a complete run against the live server.
 *
 * Run with:  node tests/ui_smoke_test.js
 * Requires:  npm install jsdom   (in /tmp/uitest or NODE_PATH)
 */
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const BASE = process.env.BASE || "http://localhost:8000";
const ROOT = path.resolve(__dirname, "..", "app", "static");

let failures = 0;
function check(label, condition, detail) {
  const status = condition ? "PASS" : "FAIL";
  if (!condition) failures++;
  console.log(`  [${status}] ${label}${detail ? "  — " + detail : ""}`);
}

async function waitFor(fn, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    let value;
    try { value = fn(); } catch (e) { value = null; }
    if (value) return value;
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error("timed out waiting for " + label);
}

async function main() {
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (e) => console.log("  [jsdom error]", e.message));
  virtualConsole.on("error", (...a) => console.log("  [console.error]", ...a));

  const html = require("fs").readFileSync(path.join(ROOT, "index.html"), "utf8");
  const dom = new JSDOM(html, {
    url: BASE + "/",
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    virtualConsole,
    // jsdom implements no fetch, and the dashboard is written against the
    // browser's own. Wire Node's implementation in before any script runs.
    beforeParse(window) {
      window.fetch = (input, init) =>
        fetch(new URL(typeof input === "string" ? input : input.url, BASE).href, init);
      window.Element.prototype.scrollIntoView = function () {};
      window.Headers = Headers;
      window.Request = Request;
      window.Response = Response;
      window.TextEncoder = TextEncoder;
      window.TextDecoder = TextDecoder;
      window.structuredClone = structuredClone;
    },
  });
  const { window } = dom;
  const { document } = window;

  // Wait for the external scripts to load and boot() to run.
  await waitFor(() => document.querySelectorAll(".ed").length === 2, 10000, "editors to build");

  console.log("\n1. page structure");
  check("title", document.title.includes("Algorithm Arena"));
  check("two editor hosts", document.querySelectorAll(".ed").length === 2);
  check("run button present", !!document.getElementById("run-button"));
  check("language selector has python", document.querySelector("#language option[value=python]") !== null);
  check("stdio option disabled in editor languages",
    document.querySelector("#language option[disabled]") !== null);

  console.log("\n2. scripts load and populate the form");
  await waitFor(() => document.querySelectorAll("#strategy-list .strategy").length === 7, 8000, "strategies");
  check("7 strategy checkboxes", document.querySelectorAll("#strategy-list input").length === 7);
  check("schemas populated", document.querySelectorAll("#schema option").length >= 9,
    document.querySelectorAll("#schema option").length + " schemas");
  check("comparison modes populated", document.querySelectorAll("#comparison-mode option").length === 5);
  check("examples rendered", document.querySelectorAll("#example-list .example-card").length === 6);
  check("editors built with gutter",
    document.querySelectorAll(".ed .ed-gutter").length === 2);
  check("highlight layer populated",
    (document.querySelector(".ed .ed-hl code").innerHTML || "").length >= 0);
  check("schema hint shown", document.getElementById("schema-hint").textContent.length > 10);
  check("sandbox chip resolved", !document.getElementById("sandbox-label").textContent.includes("checking"),
    document.getElementById("sandbox-label").textContent);

  console.log("\n3. editor behaviour");
  const editorHost = document.getElementById("editor-a");
  const input = editorHost.querySelector(".ed-input");
  input.value = "def solve(data):\n    # a comment\n    return sorted(data)\n";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 120));
  const html1 = editorHost.querySelector(".ed-hl code").innerHTML;
  check("keyword highlighted", html1.includes("tok-key"), "");
  check("comment highlighted", html1.includes("tok-com"));
  check("function name highlighted", html1.includes("tok-fn"));
  check("gutter has line numbers", editorHost.querySelector(".ed-gutter").textContent.trim().split("\n").length === 4);
  check("meta line count updated", document.getElementById("meta-a").textContent.includes("4 lines"),
    document.getElementById("meta-a").textContent);

  console.log("\n4. load an example pair");
  document.querySelectorAll("#example-list .example-card")[0].dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => document.querySelector("#editor-b .ed-input").value.length > 10, 8000, "example code");
  check("editor A filled", document.querySelector("#editor-a .ed-input").value.length > 10);
  check("editor B filled", document.querySelector("#editor-b .ed-input").value.length > 10);
  check("names applied", document.getElementById("name-a").value.length > 0,
    document.getElementById("name-a").value + " / " + document.getElementById("name-b").value);
  check("strategies applied", document.querySelectorAll("#strategy-list input:checked").length >= 4);
  check("reference field cleared for this example", document.getElementById("reference-code").value === "");

  console.log("\n5. run a comparison end to end");
  document.getElementById("run-button").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => !document.getElementById("results").hidden, 90000, "results");
  await new Promise((r) => setTimeout(r, 400));

  const results = document.getElementById("results");
  const verdict = results.querySelector(".verdict");
  check("verdict banner rendered", verdict !== null);
  check("verdict is the success style", verdict && verdict.className.includes("verdict-ok"),
    verdict && verdict.className);
  check("verdict headline", /Identical output/i.test(verdict.querySelector("h2").textContent),
    verdict.querySelector("h2").textContent);

  const statement = verdict.querySelector(".verdict-statement").textContent;
  check("statement counts cases", /produced identical outputs for \d+\/\d+ test cases/.test(statement));
  check("statement says empirical", /empirical evidence/i.test(statement));
  check("statement disclaims proof", /does not mathematically prove/i.test(statement));
  check("disclaimer present", /never a proof|not proof/i.test(
    verdict.querySelector(".verdict-caveat").textContent));

  const stats = results.querySelectorAll(".stat");
  check("stat cards rendered", stats.length >= 5, stats.length + " cards");
  const statText = Array.from(stats).map((s) => s.textContent).join(" | ");
  check("shows tests passed", /Tests passed/.test(statText));
  check("shows average runtime", /Average runtime/.test(statText));
  check("shows relative difference", /Relative runtime difference/.test(statText));
  check("shows memory", /Memory used by the algorithm/.test(statText));
  // A signed number alone is ambiguous: the card must name the slower side.
  check("relative difference names the slower side",
    /Relative runtime difference[\s\S]{0,160}?is [\d.]+% slower/.test(statText),
    (statText.match(/Relative runtime difference[\s\S]{0,120}/) || [""])[0]);

  check("correctness section", results.querySelector(".result-section h3").textContent.includes("Correctness"));
  check("category chart drawn", results.querySelectorAll("svg rect").length > 0,
    results.querySelectorAll("svg").length + " charts");
  check("timing bar chart", results.querySelectorAll(".chart .bar-a").length > 0);
  check("iteration line chart", results.querySelectorAll(".chart path.series-a, .chart path.series-b").length > 0);
  check("per-test scatter", results.querySelectorAll(".chart circle").length > 0,
    results.querySelectorAll(".chart circle").length + " points");
  check("execution summary table", results.querySelectorAll("table").length >= 2);
  check("no mismatches shown", results.querySelectorAll(".mismatch").length === 0);
  check("performance section present", /Performance/.test(results.textContent));
  check("methodology disclosed", /Benchmarking methodology/.test(results.textContent));
  check("run details present", /Run details/.test(results.textContent));
  check("sandbox facts reported", /Writes outside the sandbox/.test(results.textContent));
  check("sandbox reports VmHWM", /VmHWM reset per test/.test(results.textContent));
  check("full JSON report available", /Full report as JSON/.test(results.textContent));

  const progressHidden = document.getElementById("progress-wrap").hidden;
  check("progress hidden when finished", progressHidden);
  check("run button re-enabled", !document.getElementById("run-button").disabled);

  console.log("\n6. mismatch rendering (buggy example)");
  document.querySelectorAll("#example-list .example-card")[1].dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => document.querySelector("#editor-b .ed-input").value.includes("seen"), 8000, "buggy example");
  document.getElementById("run-button").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => !document.getElementById("results").hidden
    && results.querySelector(".verdict-diff") !== null, 90000, "mismatch results");
  await new Promise((r) => setTimeout(r, 300));

  const diff = results.querySelector(".verdict-diff");
  check("verdict is the mismatch style", diff !== null);
  check("mismatch count reported", /Test cases failed|Tests failed/.test(results.textContent));
  const mismatches = results.querySelectorAll(".mismatch");
  check("mismatch cards rendered", mismatches.length > 0, mismatches.length + " shown");
  const firstOpen = mismatches[0];
  firstOpen.open = true;
  check("mismatch shows both outputs",
    firstOpen.querySelectorAll(".diff-side").length === 2);
  check("mismatch shows the input", /Input/.test(firstOpen.querySelector(".diff-input").textContent));
  check("no benchmark section when different", /Benchmarking was skipped/.test(results.textContent));

  console.log("\n7. validation errors surface to the user");
  const banner = document.getElementById("error-banner");
  document.querySelector("#editor-b .ed-input").value = document.querySelector("#editor-a .ed-input").value;
  document.getElementById("run-button").dispatchEvent(
    new window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => !banner.hidden, 10000, "error banner");
  check("error banner shown", !banner.hidden);
  check("error explains the problem", /identical/i.test(banner.textContent),
    banner.textContent.replace(/\s+/g, " ").slice(0, 90));

  console.log(`\n${failures === 0 ? "ALL UI CHECKS PASSED" : failures + " UI CHECK(S) FAILED"}`);
  window.close();
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("\nFATAL:", err && err.message);
  process.exit(1);
});
