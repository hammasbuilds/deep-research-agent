"use strict";

(function () {
  var PAGE = 12;
  var state = { report: null, shown: PAGE, samples: [] };

  var $ = function (id) { return document.getElementById(id); };

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "text") node.textContent = attrs[k];
        else if (k === "cls") node.className = attrs[k];
        else node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  /* ---------- theme ---------- */
  var THEMES = ["auto", "light", "dark"];
  function applyTheme(t) {
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    $("theme").textContent = "Theme: " + t;
  }
  function savedTheme() {
    try { return localStorage.getItem("dra-theme") || "auto"; } catch (e) { return "auto"; }
  }
  applyTheme(savedTheme());
  $("theme").addEventListener("click", function () {
    var next = THEMES[(THEMES.indexOf(savedTheme()) + 1) % THEMES.length];
    try { localStorage.setItem("dra-theme", next); } catch (e) { /* private window */ }
    applyTheme(next);
  });

  /* ---------- sources editor ---------- */
  function addSource(url, text) {
    var node = $("source-tpl").content.firstElementChild.cloneNode(true);
    node.querySelector(".src-url").value = url || "";
    node.querySelector(".src-text").value = text || "";
    node.querySelector(".src-remove").addEventListener("click", function () {
      node.remove();
      countSources();
    });
    node.querySelector(".src-url").addEventListener("input", countSources);
    node.querySelector(".src-text").addEventListener("input", countSources);
    $("sources").appendChild(node);
    countSources();
    return node;
  }

  function readSources() {
    return Array.prototype.map.call(document.querySelectorAll("#sources .source"), function (li) {
      return { url: li.querySelector(".src-url").value.trim(), text: li.querySelector(".src-text").value };
    }).filter(function (s) { return s.url || s.text.trim(); });
  }

  function countSources() {
    var n = readSources().length;
    $("source-count").textContent = n ? "(" + n + ")" : "";
  }

  function clearSources() {
    $("sources").innerHTML = "";
    countSources();
  }

  $("add-source").addEventListener("click", function () {
    addSource("", "").querySelector(".src-url").focus();
  });
  $("clear-sources").addEventListener("click", function () {
    clearSources();
    addSource("", "");
  });

  $("upload").addEventListener("change", function (ev) {
    var files = Array.prototype.slice.call(ev.target.files || []);
    if (!files.length) return;
    var empties = Array.prototype.filter.call(document.querySelectorAll("#sources .source"), function (li) {
      return !li.querySelector(".src-url").value.trim() && !li.querySelector(".src-text").value.trim();
    });
    empties.forEach(function (li) { li.remove(); });
    var pending = files.length;
    files.forEach(function (file) {
      if (file.size > 2000000) {
        setStatus(file.name + " is larger than 2 MB and was skipped.", true);
        pending -= 1;
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        addSource(file.name, String(reader.result || ""));
        pending -= 1;
        if (pending === 0) setStatus(files.length + " file(s) added as sources.");
      };
      reader.onerror = function () {
        setStatus("Could not read " + file.name + ".", true);
        pending -= 1;
      };
      reader.readAsText(file);
    });
    ev.target.value = "";
  });

  /* ---------- samples ---------- */
  function loadSample(sample) {
    $("question").value = sample.question;
    clearSources();
    sample.sources.forEach(function (s) { addSource(s.url, s.text); });
    $("sample-note").textContent = sample.note;
    $("sample-note").hidden = false;
    setStatus("Sample loaded. Press Research.");
  }

  fetch("/api/samples").then(function (r) { return r.json(); }).then(function (data) {
    state.samples = data.samples || [];
    state.samples.forEach(function (s) {
      var b = el("button", { type: "button", cls: "ghost sample", "data-sample": s.id, text: s.name });
      b.addEventListener("click", function () { loadSample(s); });
      $("sample-buttons").appendChild(b);
    });
  }).catch(function () {
    $("sample-buttons").textContent = "(samples could not be loaded)";
  });

  /* ---------- run ---------- */
  function setStatus(msg, isError) {
    $("status").textContent = msg || "";
    $("status").className = "status" + (isError ? " error" : "");
  }

  $("ask").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var sources = readSources();
    var question = $("question").value.trim();
    if (!question) { setStatus("Type a question first.", true); $("question").focus(); return; }
    if (!sources.length) { setStatus("Add at least one source: paste text, upload a file, or give a web address.", true); return; }
    var live = sources.filter(function (s) { return !s.text.trim(); }).length;
    $("go").disabled = true;
    setStatus(live ? "Researching: fetching " + live + " page(s) live, this can take a few seconds..." : "Researching...");
    fetch("/api/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: question,
        sources: sources,
        settings: {
          min_authority: Number($("min-authority").value),
          numeric_tolerance: Number($("tolerance").value),
          max_fetches: Number($("max-fetches").value)
        }
      })
    }).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, body: body }; });
    }).then(function (res) {
      if (!res.ok) { setStatus(res.body.error || "The request failed.", true); return; }
      state.report = res.body;
      state.shown = PAGE;
      render();
      setStatus("Done in " + res.body.seconds + " s.");
      $("results").scrollIntoView({ behavior: "smooth", block: "start" });
    }).catch(function () {
      setStatus("Could not reach the server. Is it still running?", true);
    }).then(function () {
      $("go").disabled = false;
    });
  });

  /* ---------- render ---------- */
  var BADGE = {
    "disputed": "disputed",
    "well-corroborated": "well",
    "corroborated": "corr",
    "single-source": "single"
  };

  function fmt(v) {
    if (v === null || v === undefined) return "";
    if (Math.abs(v) >= 1e5) return v.toLocaleString("en-US");
    return String(v);
  }

  function hostOf(url) {
    var m = /^[a-z]+:\/\/([^/]+)/i.exec(url || "");
    return m ? m[1].replace(/^www\./, "") : url;
  }

  function shortUrl(url) {
    var m = /^[a-z]+:\/\/([^/?#]+)([^?#]*)/i.exec(url || "");
    if (!m) return url;
    var path = decodeURIComponent(m[2] || "").replace(/\/$/, "");
    if (path.length > 40) path = path.slice(0, 18) + "…" + path.slice(-18);
    return m[1].replace(/^www\./, "") + path;
  }

  function sourceLink(url) {
    if (/^https?:\/\//i.test(url)) {
      return el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: shortUrl(url), title: url });
    }
    return el("span", { cls: "dom", text: hostOf(url) });
  }

  function authorityOf(url) {
    var rows = (state.report && state.report.sources) || [];
    for (var i = 0; i < rows.length; i++) if (rows[i].url === url) return rows[i];
    return null;
  }

  function citation(c) {
    var a = authorityOf(c.source);
    return el("li", { cls: "cite" }, [
      el("div", { cls: "cite-src" }, [
        sourceLink(c.source),
        a ? el("span", { cls: "meta", text: a["class"].replace("_", " ") + " " + a.authority }) : null,
        el("span", { cls: "badge ok", text: "quote verified" })
      ]),
      el("q", { text: c.quote })
    ]);
  }

  function card(f, dispute) {
    var head = el("div", { cls: "card-head" }, [
      el("span", { cls: "subject", text: f.subject }),
      el("span", { cls: "badge " + (BADGE[f.confidence] || "single"), text: f.confidence })
    ]);
    var parts = [head];
    if (f.values && f.values.length) {
      var UNITS = { gw: "GW", mw: "MW", kw: "kW", twh: "TWh", gwh: "GWh", pkr: "PKR", usd: "USD" };
      var unit = f.unit ? (f.unit === "%" ? "%" : " " + (UNITS[f.unit] || f.unit)) : "";
      parts.push(el("div", { cls: "values" }, f.values.map(function (v) {
        return el("span", { cls: "value", text: fmt(v) + unit });
      })));
      var bits = [];
      if (f.values.length > 1) bits.push("median " + fmt(f.consensus) + unit);
      if (f.spread) bits.push("spread " + Math.round(f.spread * 100) + "%");
      bits.push(f.independent_sources + " independent source" + (f.independent_sources === 1 ? "" : "s"));
      if (f.copied_sentences) bits.push(copyNote(f.copied_sentences));
      parts.push(el("div", { cls: "meta", text: bits.join(" · ") }));
    } else {
      var stance = f.negated && f.negated.length > 1 ? "one source says it is, another says it is not" : "";
      var line = (stance ? stance + " · " : "") + f.independent_sources + " independent source" + (f.independent_sources === 1 ? "" : "s");
      if (f.copied_sentences) line += " · " + copyNote(f.copied_sentences);
      parts.push(el("div", { cls: "meta", text: line }));
    }
    parts.push(el("ul", { cls: "cites" }, (f.citations || []).map(citation)));
    return el("article", { cls: "card" + (dispute ? " dispute" : "") }, parts);
  }

  function copyNote(n) {
    return "the same sentence on " + n + " more page" + (n === 1 ? "" : "s") + ", counted once";
  }

  function tile(value, label) {
    return el("div", { cls: "tile" }, [el("b", { text: String(value) }), el("span", { text: label })]);
  }

  function filtered() {
    var r = state.report;
    var rel = $("only-relevant").checked, num = $("only-numbers").checked, two = $("only-corroborated").checked;
    var kept = r.findings.filter(function (f) {
      if (rel && !(f.relevance > 0)) return false;
      if (num && !(f.values && f.values.length)) return false;
      if (two && f.independent_sources < 2) return false;
      return true;
    });
    // Equally relevant claims: the ones carrying a figure first, then the better supported.
    return kept.map(function (f, i) { return [f, i]; }).sort(function (a, b) {
      var fa = a[0], fb = b[0];
      if (fb.relevance !== fa.relevance) return fb.relevance - fa.relevance;
      var na = fa.values && fa.values.length ? 1 : 0, nb = fb.values && fb.values.length ? 1 : 0;
      if (nb !== na) return nb - na;
      if (fb.independent_sources !== fa.independent_sources) return fb.independent_sources - fa.independent_sources;
      return a[1] - b[1];
    }).map(function (p) { return p[0]; });
  }

  function renderFindings() {
    var list = filtered();
    var box = $("findings");
    box.innerHTML = "";
    list.slice(0, state.shown).forEach(function (f) { box.appendChild(card(f, false)); });
    if (!list.length) {
      box.appendChild(el("p", { cls: "empty", text: state.report.findings.length ? "No claim matches these filters. Untick a filter to see the rest." : "No claims were found in these sources." }));
    }
    $("findings-count").textContent = "(showing " + Math.min(state.shown, list.length) + " of " + list.length + (list.length !== state.report.findings.length ? ", " + state.report.findings.length + " in all" : "") + ")";
    $("more").hidden = list.length <= state.shown;
  }

  var STATUS_TEXT = {
    "read": "read (pasted text)",
    "fetched": "fetched live",
    "fetched, no text": "fetched live, but no readable text",
    "fetch failed": "fetch failed",
    "merged as a copy": "merged as a copy of another source",
    "no query matched it": "not read: no query matched it",
    "below the authority floor": "not read: below the authority setting",
    "not searched": "not read: the budget ran out first"
  };

  function renderSources() {
    var r = state.report;
    var rows = $("src-rows");
    rows.innerHTML = "";
    r.inputs.forEach(function (inp) {
      var a = authorityOf(inp.url);
      var st = STATUS_TEXT[inp.status] || inp.status;
      if (inp.error) st += ": " + inp.error;
      if (inp.chars && inp.status !== "fetch failed") st += " (" + inp.chars.toLocaleString("en-US") + " characters)";
      var stCls = inp.status === "fetch failed" ? "st-failed" : (inp.status === "merged as a copy" ? "st-merged" : "");
      var copies = a && a.copies && a.copies.length ? a.copies.map(hostOf).join(", ") : "";
      var titleBits = [sourceLink(inp.url)];
      if (inp.title) titleBits.push(el("div", { cls: "meta", text: inp.title }));
      rows.appendChild(el("tr", null, [
        el("td", null, titleBits),
        el("td", { cls: stCls, text: st }),
        el("td", { text: a ? a["class"].replace("_", " ") + " (" + a.authority + ")" : "" }),
        el("td", { text: copies })
      ]));
    });
  }

  function render() {
    var r = state.report;
    var s = r.summary;
    $("results").hidden = false;
    $("res-question").textContent = r.question;
    var tiles = $("tiles");
    tiles.innerHTML = "";
    tiles.appendChild(tile(s.independent_sources + " / " + s.documents_fetched, "independent sources / pages read"));
    tiles.appendChild(tile(Math.round(s.independence_ratio * 100) + "%", "independence ratio"));
    tiles.appendChild(tile(s.disputed, "disputed"));
    tiles.appendChild(tile(s.findings, "claims"));
    tiles.appendChild(tile(s.unverified_quotes_dropped, "quotes dropped (not found in source)"));
    $("stopped").textContent = "Stopped because: " + s.stopped_because + ".";

    var dbox = $("disputed");
    dbox.innerHTML = "";
    r.disputed.forEach(function (f) { dbox.appendChild(card(f, true)); });
    if (!r.disputed.length) dbox.appendChild(el("p", { cls: "empty", text: "No disagreement between these sources." }));
    $("disputed-count").textContent = "(" + r.disputed.length + ")";

    renderFindings();
    renderSources();
    var q = $("queries");
    q.innerHTML = "";
    r.queries_run.forEach(function (x) { q.appendChild(el("li", { text: x })); });
  }

  ["only-relevant", "only-numbers", "only-corroborated"].forEach(function (id) {
    $(id).addEventListener("change", function () {
      if (!state.report) return;
      state.shown = PAGE;
      renderFindings();
    });
  });
  $("more").addEventListener("click", function () {
    state.shown += PAGE;
    renderFindings();
  });

  addSource("", "");
})();
