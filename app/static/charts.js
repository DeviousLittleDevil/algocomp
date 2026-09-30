/* Minimal SVG charting for the results dashboard.
 *
 * Every chart is drawn as inline SVG sized to its container via viewBox, so
 * the charts scale with the layout and need no resize handling and no
 * charting library. Kept deliberately small: bar, grouped bar, scatter, line,
 * stacked bar and a reference-line helper.
 */
(function (global) {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";

  function el(name, attrs, text) {
    var node = document.createElementNS(NS, name);
    if (attrs) {
      for (var key in attrs) {
        if (attrs[key] !== null && attrs[key] !== undefined) {
          node.setAttribute(key, attrs[key]);
        }
      }
    }
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function svg(width, height) {
    var node = el("svg", {
      class: "chart",
      viewBox: "0 0 " + width + " " + height,
      preserveAspectRatio: "xMidYMid meet",
      role: "img"
    });
    node.style.width = "100%";
    return node;
  }

  /** Round a maximum up to a readable axis bound (1, 2, 5 x 10^n). */
  function niceMax(value) {
    if (!(value > 0)) return 1;
    var exponent = Math.floor(Math.log10(value));
    var base = Math.pow(10, exponent);
    var scaled = value / base;
    var step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
    return step * base;
  }

  function formatMs(value) {
    if (value === null || value === undefined || isNaN(value)) return "–";
    if (value === 0) return "0";
    if (value < 0.001) return value.toExponential(1);
    if (value < 1) return value.toFixed(3);
    if (value < 1000) return value.toFixed(2);
    return value.toFixed(0);
  }

  function formatMsUnit(value) {
    if (value >= 1000) return formatMs(value / 1000) + " s";
    return formatMs(value) + " ms";
  }

  /* ------------------------------------------------------------------
   * Grouped bar chart with min–max whiskers.
   * Shows mean runtime for A and B on a shared axis, with the observed
   * range drawn so the reader can see the spread behind the average.
   * ---------------------------------------------------------------- */
  function timingBars(host, options) {
    var a = options.a, b = options.b;
    var width = 460, height = 190;
    var pad = { top: 16, right: 16, bottom: 34, left: 58 };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;

    var max = niceMax(Math.max(a.max || 0, b.max || 0, a.mean || 0, b.mean || 0) * 1.1);
    var chart = svg(width, height);

    // horizontal grid + axis labels
    for (var t = 0; t <= 4; t++) {
      var value = (max / 4) * t;
      var y = pad.top + plotH - (value / max) * plotH;
      chart.appendChild(el("line", { class: "grid-line", x1: pad.left, y1: y, x2: width - pad.right, y2: y }));
      chart.appendChild(el("text", { x: pad.left - 8, y: y + 3, "text-anchor": "end" }, formatMs(value)));
    }
    chart.appendChild(el("line", {
      class: "axis-line", x1: pad.left, y1: pad.top, x2: pad.left, y2: pad.top + plotH
    }));

    var rows = [
      { label: options.labelA || "Algorithm A", stats: a, cls: "bar-a" },
      { label: options.labelB || "Algorithm B", stats: b, cls: "bar-b" }
    ];

    rows.forEach(function (row, i) {
      var barH = 30;
      var y = pad.top + 16 + i * (barH + 30);
      var meanW = Math.max(2, (row.stats.mean / max) * plotW);

      chart.appendChild(el("text", {
        x: pad.left, y: y - 5, class: "label-strong"
      }, row.label));

      chart.appendChild(el("rect", {
        class: row.cls, x: pad.left, y: y, width: meanW, height: barH, rx: 3
      }));

      // min–max whisker
      if (row.stats.max > row.stats.min && row.stats.min >= 0) {
        var xMin = pad.left + (row.stats.min / max) * plotW;
        var xMax = pad.left + (row.stats.max / max) * plotW;
        var midY = y + barH / 2;
        chart.appendChild(el("line", { class: "error-bar", x1: xMin, y1: midY, x2: xMax, y2: midY }));
        chart.appendChild(el("line", { class: "error-bar", x1: xMin, y1: midY - 6, x2: xMin, y2: midY + 6 }));
        chart.appendChild(el("line", { class: "error-bar", x1: xMax, y1: midY - 6, x2: xMax, y2: midY + 6 }));
      }

      chart.appendChild(el("text", {
        x: pad.left + meanW + 8, y: y + barH / 2 + 4, class: "label-strong"
      }, formatMsUnit(row.stats.mean) + "  (min " + formatMs(row.stats.min) + " / max " + formatMs(row.stats.max) + ")"));
    });

    chart.appendChild(el("text", {
      x: pad.left, y: height - 6, "text-anchor": "start"
    }, "mean time per iteration, whiskers show min–max over " + (options.iterations || 0) + " iterations"));

    host.appendChild(chart);
    return chart;
  }

  /* ------------------------------------------------------------------
   * Scatter of per-test timings: where does each side win?
   * ---------------------------------------------------------------- */
  function perTestScatter(host, pairs, options) {
    options = options || {};
    if (!pairs || !pairs.length) {
      host.appendChild(emptyNote("No per-test timings were recorded."));
      return;
    }
    var width = 460, height = 210;
    var pad = { top: 14, right: 16, bottom: 32, left: 58 };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;

    // Compress with sqrt so tiny and huge cases are both visible.
    var allTimes = [];
    pairs.forEach(function (p) { allTimes.push(p.a_ms, p.b_ms); });
    var maxTime = Math.max.apply(null, allTimes) || 1;
    var scale = function (v) { return Math.sqrt(Math.max(0, v) / maxTime) * plotH; };

    var chart = svg(width, height);
    for (var t = 0; t <= 4; t++) {
      var fraction = Math.pow(t / 4, 2);
      var value = fraction * maxTime;
      var y = pad.top + plotH - scale(value);
      chart.appendChild(el("line", { class: "grid-line", x1: pad.left, y1: y, x2: width - pad.right, y2: y }));
      chart.appendChild(el("text", { x: pad.left - 8, y: y + 3, "text-anchor": "end" }, formatMs(value)));
    }
    chart.appendChild(el("line", { class: "axis-line", x1: pad.left, y1: pad.top, x2: pad.left, y2: pad.top + plotH }));

    var step = plotW / Math.max(1, pairs.length);
    var radius = Math.max(2, Math.min(5, step * 0.38));

    pairs.forEach(function (pair, i) {
      var x = pad.left + step * (i + 0.5);
      var yA = pad.top + plotH - scale(pair.a_ms);
      var yB = pad.top + plotH - scale(pair.b_ms);
      chart.appendChild(el("circle", { class: "point-a", cx: x, cy: yA, r: radius, "fill-opacity": 0.85 }));
      chart.appendChild(el("circle", { class: "point-b", cx: x, cy: yB, r: radius, "fill-opacity": 0.85 }));
    });

    chart.appendChild(el("text", { x: pad.left, y: height - 6 },
      "per test case · vertical axis is √-scaled · " + pairs.length + " cases"));
    host.appendChild(chart);

    appendLegend(host, [
      { color: "var(--a)", label: options.labelA || "Algorithm A" },
      { color: "var(--b)", label: options.labelB || "Algorithm B" }
    ]);
  }

  /* ------------------------------------------------------------------
   * Line chart of the individual iteration samples - the evidence behind
   * the noise argument.
   * ---------------------------------------------------------------- */
  function iterationLines(host, series, options) {
    options = options || {};
    var all = [];
    series.forEach(function (s) { all = all.concat(s.samples || []); });
    if (!all.length) {
      host.appendChild(emptyNote("No iteration samples were recorded."));
      return;
    }
    var width = 460, height = 200;
    var pad = { top: 16, right: 16, bottom: 32, left: 58 };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;

    var max = niceMax(Math.max.apply(null, all) * 1.1);
    var min = 0;
    var count = Math.max.apply(null, series.map(function (s) { return (s.samples || []).length; })) || 1;
    var chart = svg(width, height);

    for (var t = 0; t <= 4; t++) {
      var value = min + ((max - min) / 4) * t;
      var y = pad.top + plotH - ((value - min) / (max - min)) * plotH;
      chart.appendChild(el("line", { class: "grid-line", x1: pad.left, y1: y, x2: width - pad.right, y2: y }));
      chart.appendChild(el("text", { x: pad.left - 8, y: y + 3, "text-anchor": "end" }, formatMs(value)));
    }
    chart.appendChild(el("line", { class: "axis-line", x1: pad.left, y1: pad.top, x2: pad.left, y2: pad.top + plotH }));

    var xAt = function (i) { return pad.left + (plotW * (count > 1 ? i / (count - 1) : 0.5)); };
    var yAt = function (v) { return pad.top + plotH - ((v - min) / (max - min)) * plotH; };

    series.forEach(function (s) {
      var samples = s.samples || [];
      if (!samples.length) return;
      var path = samples.map(function (value, i) {
        return (i ? "L" : "M") + xAt(i).toFixed(1) + " " + yAt(value).toFixed(1);
      }).join(" ");
      chart.appendChild(el("path", { class: s.cls, d: path }));
      samples.forEach(function (value, i) {
        chart.appendChild(el("circle", { class: s.cls.replace("series", "point"), cx: xAt(i), cy: yAt(value), r: 3 }));
      });
    });

    chart.appendChild(el("text", { x: pad.left, y: height - 6 }, "individual iteration samples (interleaved A, B, A, B, …)"));
    host.appendChild(chart);

    appendLegend(host, series.map(function (s) {
      return { color: s.cls === "series-a" ? "var(--a)" : "var(--b)", label: s.label };
    }));
  }

  /* ------------------------------------------------------------------
   * Horizontal bars for a single metric per side (memory, for example).
   * ---------------------------------------------------------------- */
  function metricBars(host, options) {
    var entries = options.entries;
    var width = 460, height = 44 + entries.length * 42;
    var pad = { top: 8, right: 96, bottom: 26, left: 132 };
    var plotW = width - pad.left - pad.right;
    var max = niceMax(Math.max.apply(null, entries.map(function (e) { return e.value || 0; })) * 1.05);
    var chart = svg(width, height);

    entries.forEach(function (entry, i) {
      var y = pad.top + i * 42;
      var barH = 20;
      var w = Math.max(2, (entry.value / max) * plotW);
      chart.appendChild(el("text", { x: 0, y: y + barH / 2 + 4, class: "label-strong" }, entry.label));
      chart.appendChild(el("rect", { class: entry.cls, x: pad.left, y: y, width: w, height: barH, rx: 3 }));
      chart.appendChild(el("text", { x: pad.left + w + 8, y: y + barH / 2 + 4 }, entry.display));
    });

    if (options.baseline) {
      var bx = pad.left + (options.baseline / max) * plotW;
      chart.appendChild(el("line", { class: "baseline", x1: bx, y1: pad.top - 2, x2: bx, y2: height - pad.bottom + 4 }));
      chart.appendChild(el("text", { x: bx + 4, y: height - 8 }, "interpreter baseline"));
    }
    if (options.note) {
      chart.appendChild(el("text", { x: 0, y: height - 8 }, options.note));
    }
    host.appendChild(chart);
  }

  /* ------------------------------------------------------------------
   * Stacked pass/fail bars per test category.
   * ---------------------------------------------------------------- */
  function categoryBars(host, buckets) {
    if (!buckets || !buckets.length) {
      host.appendChild(emptyNote("No test categories were recorded."));
      return;
    }
    var width = 460;
    var rowH = 38;
    var height = 14 + buckets.length * rowH + 24;
    var pad = { left: 92, right: 54 };
    var plotW = width - pad.left - pad.right;
    var max = Math.max.apply(null, buckets.map(function (b) { return b.total || 0; })) || 1;
    var chart = svg(width, height);

    buckets.forEach(function (bucket, i) {
      var y = 6 + i * rowH;
      var barH = 17;
      var total = bucket.total || 1;
      chart.appendChild(el("text", { x: 0, y: y + barH - 3, class: "label-strong" }, bucket.category));
      var x = pad.left;
      [
        { key: "passed", cls: "bar-ok" },
        { key: "failed", cls: "bar-fail" },
        { key: "error", cls: "bar-error" }
      ].forEach(function (part) {
        var count = bucket[part.key] || 0;
        if (!count) return;
        var w = (count / max) * plotW;
        chart.appendChild(el("rect", { class: part.cls, x: x, y: y, width: w, height: barH }));
        if (w > 26) {
          chart.appendChild(el("text", { x: x + w / 2, y: y + barH - 4, "text-anchor": "middle", fill: "#0b0e14" }, count));
        }
        x += w;
      });
      var rate = Math.round((bucket.passed / total) * 100);
      chart.appendChild(el("text", { x: width - 4, y: y + barH - 3, "text-anchor": "end", class: rate === 100 ? "pass" : "fail" },
        rate + "%"));
    });
    host.appendChild(chart);
  }

  function emptyNote(text) {
    var node = document.createElement("p");
    node.className = "hint";
    node.textContent = text;
    return node;
  }

  function appendLegend(host, items) {
    var legend = document.createElement("div");
    legend.className = "legend";
    items.forEach(function (item) {
      var wrap = document.createElement("span");
      wrap.className = "legend-item";
      var key = document.createElement("span");
      key.className = "legend-key";
      key.style.background = item.color;
      wrap.appendChild(key);
      wrap.appendChild(document.createTextNode(item.label));
      legend.appendChild(wrap);
    });
    host.appendChild(legend);
  }

  global.ArenaCharts = {
    timingBars: timingBars,
    perTestScatter: perTestScatter,
    iterationLines: iterationLines,
    metricBars: metricBars,
    categoryBars: categoryBars,
    formatMs: formatMs,
    formatMsUnit: formatMsUnit,
    emptyNote: emptyNote
  };
})(window);
