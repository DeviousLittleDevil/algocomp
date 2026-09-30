/* A small syntax-highlighting code editor with zero dependencies.
 *
 * The technique is the classic one: a <pre> with the highlighted HTML sits
 * behind a transparent <textarea>. The textarea owns input, selection and the
 * caret; the <pre> is only ever painted. Both layers must share identical
 * font metrics, padding and scroll offsets or the text visibly drifts.
 *
 * Using this instead of CodeMirror/Monaco keeps the project dependency-free
 * and the app fully functional offline.
 */
(function (global) {
  "use strict";

  var KEYWORDS = new Set([
    "False", "None", "True", "and", "as", "assert", "async", "await", "break",
    "class", "continue", "def", "del", "elif", "else", "except", "finally",
    "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
    "match", "case"
  ]);

  var BUILTINS = new Set([
    "abs", "all", "any", "bin", "bool", "bytes", "chr", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "getattr", "hash",
    "hex", "id", "int", "isinstance", "iter", "len", "list", "map", "max",
    "min", "next", "object", "oct", "open", "ord", "pow", "print", "range",
    "repr", "reversed", "round", "set", "setattr", "slice", "sorted", "str",
    "sum", "super", "tuple", "type", "zip", "Exception", "ValueError",
    "TypeError", "IndexError", "KeyError", "ZeroDivisionError", "NotImplementedError"
  ]);

  // One sticky regex with named alternatives: tokenising a few thousand lines
  // on every keystroke has to stay cheap.
  var TOKEN_RE = new RegExp(
    [
      "(?<comment>#[^\\n]*)",
      "(?<string>\"\"\"[\\s\\S]*?\"\"\"|'''[\\s\\S]*?'''|\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*')",
      "(?<decorator>@[A-Za-z_][\\w.]*)",
      "(?<number>\\b(?:0[xXbBoO][0-9a-fA-F_]+|\\d[\\d_]*\\.?\\d*(?:[eE][+-]?\\d+)?j?)\\b)",
      "(?<name>[A-Za-z_]\\w*)",
      "(?<op>[+\\-*/%=<>!&|^~@,:.;()\\[\\]{}]+)"
    ].join("|"),
    "gy"
  );

  var WS_RE = /\s+/y;
  var IDENT_RE = /[A-Za-z_]\w*/y;

  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  /** Classify a bare identifier using the context around it. */
  function classifyName(name, afterToken) {
    if (KEYWORDS.has(name)) return "tok-key";
    if (BUILTINS.has(name)) return "tok-blt";
    if (afterToken === "." ) return null;               // attribute access
    if (/^[A-Z]/.test(name)) return "tok-dec";            // class-like
    return null;
  }

  function highlightPython(source) {
    var out = [];
    var index = 0;
    var length = source.length;
    var lastSignificant = "";
    TOKEN_RE.lastIndex = 0;

    while (index < length) {
      TOKEN_RE.lastIndex = index;
      var match = TOKEN_RE.exec(source);
      var text;
      if (match) {
        index = TOKEN_RE.lastIndex;
        var groups = match.groups;
        if (groups.comment) {
          out.push('<span class="tok-com">' + escapeHtml(groups.comment) + "</span>");
          lastSignificant = "";
        } else if (groups.string) {
          out.push('<span class="tok-str">' + escapeHtml(groups.string) + "</span>");
          lastSignificant = '"';
        } else if (groups.decorator) {
          out.push('<span class="tok-dec">' + escapeHtml(groups.decorator) + "</span>");
          lastSignificant = "";
        } else if (groups.number) {
          out.push('<span class="tok-num">' + escapeHtml(groups.number) + "</span>");
          lastSignificant = "0";
        } else if (groups.name) {
          text = groups.name;
          // Look ahead: `name(` is a call, so colour it like a function.
          var probe = index;
          while (probe < length && (source[probe] === " " || source[probe] === "\t")) probe++;
          var cls = source[probe] === "(" ? "tok-fn" : classifyName(text, lastSignificant);
          out.push(cls ? '<span class="' + cls + '">' + escapeHtml(text) + "</span>" : escapeHtml(text));
          lastSignificant = text;
        } else if (groups.op) {
          out.push('<span class="tok-op">' + escapeHtml(groups.op) + "</span>");
          lastSignificant = groups.op.trim().slice(-1) || lastSignificant;
        }
        continue;
      }

      // Whitespace and anything the tokenizer does not recognise.
      WS_RE.lastIndex = index;
      var ws = WS_RE.exec(source);
      if (ws) {
        out.push(escapeHtml(ws[0]));
        index = WS_RE.lastIndex;
        continue;
      }
      IDENT_RE.lastIndex = index;
      var ident = IDENT_RE.exec(source);
      if (ident) {
        var cls = classifyName(ident[0], lastSignificant);
        out.push(cls ? '<span class="' + cls + '">' + escapeHtml(ident[0]) + "</span>" : escapeHtml(ident[0]));
        lastSignificant = ident[0];
        index = IDENT_RE.lastIndex;
        continue;
      }
      out.push(escapeHtml(source[index]));
      index += 1;
    }
    // A trailing newline keeps the <pre> the same height as the textarea.
    return out.join("");
  }

  function highlight(source, language) {
    if (language === "python") return highlightPython(source);
    return escapeHtml(source);
  }

  function Editor(host, options) {
    options = options || {};
    this.host = host;
    this.language = options.language || "python";
    this.onChange = options.onChange || function () {};
    this.placeholder = options.placeholder || "";
    this._pending = null;

    host.classList.add("ed");
    host.innerHTML =
      '<div class="ed-gutter" aria-hidden="true">1</div>' +
      '<div class="ed-surface">' +
        '<pre class="ed-hl" aria-hidden="true"><code></code></pre>' +
        '<textarea class="ed-input" wrap="off" spellcheck="false" autocapitalize="off"' +
        ' autocorrect="off" autocomplete="off"></textarea>' +
      "</div>";

    this.gutter = host.querySelector(".ed-gutter");
    this.highlightLayer = host.querySelector(".ed-hl");
    this.code = host.querySelector(".ed-hl code");
    this.input = host.querySelector(".ed-input");
    if (this.placeholder) this.input.placeholder = this.placeholder;

    var self = this;
    this.input.addEventListener("input", function () { self._onInput(); });
    this.input.addEventListener("scroll", function () { self._syncScroll(); });
    this.input.addEventListener("keydown", function (event) { self._onKeyDown(event); });

    this.setValue(options.value || "", true);
  }

  Editor.prototype.value = function () {
    return this.input.value;
  };

  Editor.prototype.setValue = function (text, silent) {
    this.input.value = text;
    this._render();
    if (!silent) this.onChange(this.input.value);
  };

  Editor.prototype._onInput = function () {
    this._render();
    this.onChange(this.input.value);
  };

  Editor.prototype._render = function () {
    var self = this;
    // Coalesce rapid typing into one paint per frame.
    if (this._pending) return;
    this._pending = requestAnimationFrame(function () {
      self._pending = null;
      var source = self.input.value;
      self.code.innerHTML = highlight(source, self.language) + "\n";
      var lines = source.split("\n").length;
      self.gutter.textContent = Array.from({ length: lines }, function (_, i) { return i + 1; }).join("\n");
      self._syncScroll();
    });
  };

  Editor.prototype._syncScroll = function () {
    this.highlightLayer.scrollTop = this.input.scrollTop;
    this.highlightLayer.scrollLeft = this.input.scrollLeft;
    this.gutter.scrollTop = this.input.scrollTop;
  };

  Editor.prototype._onKeyDown = function (event) {
    if (event.key === "Tab") {
      event.preventDefault();
      var start = this.input.selectionStart;
      var end = this.input.selectionEnd;
      var value = this.input.value;

      if (event.shiftKey) {
        // Outdent every line touched by the selection.
        var from = value.lastIndexOf("\n", start - 1) + 1;
        var to = end;
        var block = value.slice(from, to);
        var dedented = block.replace(/^[ \t]{1,4}/gm, "");
        this.input.setRangeText(dedented, from, to, "select");
      } else if (start !== end) {
        var blockFrom = value.lastIndexOf("\n", start - 1) + 1;
        var block = value.slice(blockFrom, end);
        var indented = block.replace(/^/gm, "    ");
        this.input.setRangeText(indented, blockFrom, end, "select");
      } else {
        this.input.setRangeText("    ", start, end, "end");
      }
      this._onInput();
      return;
    }

    if (event.key === "Enter") {
      // Keep the current indentation, and add one level after a colon.
      var position = this.input.selectionStart;
      var before = this.input.value.slice(0, position);
      var lineStart = before.lastIndexOf("\n") + 1;
      var line = before.slice(lineStart);
      var indent = (line.match(/^[ \t]*/) || [""])[0];
      if (/:\s*$/.test(line)) indent += "    ";
      if (!indent) return;
      event.preventDefault();
      this.input.setRangeText("\n" + indent, position, this.input.selectionEnd, "end");
      this._onInput();
    }
  };

  Editor.prototype.focus = function () {
    this.input.focus();
  };

  global.ArenaEditor = { create: function (host, options) { return new Editor(host, options); } };
})(window);
