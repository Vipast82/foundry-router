/* Foundry UI kit (v0.181.120): behaviour shared by /ui, /ui/control and /ui/automation.
   - FK.tables(): every data table can be sorted by a column (click the header) and
     its columns resized (drag a header edge; double-click the edge to reset).
     Tables are picked up automatically as pages re-render.
   - A table of 30 rows or more gets a bar above it: a find box and paging (the first
     50 rows, "show more", "show all"). <table class="no-tools"> opts a table out;
     <table data-fk-find="off"> keeps paging only (the page has its own search).
   - FK.onResize(fn): run fn after the window's WIDTH changes, so a page can redraw
     charts and maps at the new size.
   - FK.flow(...): the flow map used for routes and activity. */
(function () {
  "use strict";
  const FK = window.FK = window.FK || {};
  // <script src="/ui/kit.js" data-no-sort="1">: tables on that page resize but do not sort
  const NO_SORT = !!(document.currentScript && document.currentScript.dataset.noSort);
  // <script ... data-no-tools="1">: no find-and-paging bar on that page's long tables
  const NO_TOOLS = !!(document.currentScript && document.currentScript.dataset.noTools);
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  FK.esc = esc;

  // ---- resize -------------------------------------------------------------------------
  FK.onResize = function (fn, ms) {
    let w = window.innerWidth, t = 0;
    window.addEventListener("resize", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        if (Math.abs(window.innerWidth - w) < 24) return;
        w = window.innerWidth;
        try { fn(w); } catch (e) { /* a redraw must never break the page */ }
      }, ms || 220);
    });
  };
  // true while the person is typing or a dialog is open: not the moment to redraw
  FK.busy = function () {
    const a = document.activeElement;
    if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.type !== "checkbox") return true;
    return !!document.querySelector("dialog[open], .modal.open, #modal.open, .modal-back.show, .drawer.show");
  };

  // ---- tables ---------------------------------------------------------------------------
  const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
  const sortState = {};                                   // table signature -> {col, dir}
  const MULT = {k: 1e3, m: 1e6, b: 1e9};

  function cellValue(td) {
    if (!td) return "";
    const inp = td.querySelector("input:not([type=checkbox]):not([type=radio]), select");
    const s = (inp ? inp.value : td.textContent || "").trim();
    const m = /^[-+]?[$€£]?\s*(\d[\d,]*\.?\d*|\.\d+)\s*([kKmMbB])?(?=$|[\s%a-zA-Z/×(·])/.exec(s);
    if (m) {
      const n = parseFloat(m[1].replace(/,/g, "")) * (MULT[(m[2] || "").toLowerCase()] || 1);
      if (isFinite(n)) return s[0] === "-" ? -n : n;
    }
    return s;
  }
  function compare(a, b) {
    const na = typeof a === "number", nb = typeof b === "number";
    if (na && nb) return a - b;
    if (na !== nb) return na ? -1 : 1;                     // numbers before text
    return collator.compare(a, b);
  }
  function headerRow(table) {
    const rows = Array.from(table.rows);
    const withTh = rows.filter(r => r.querySelector("th"));
    if (withTh.length !== 1 || withTh[0] !== rows[0]) return null;      // key/value tables etc.
    if (Array.from(rows[0].cells).some(c => c.colSpan > 1 || c.rowSpan > 1)) return null;
    return rows[0];
  }
  function signature(head) {
    return location.pathname + "|" + Array.from(head.cells).map(c => (c.textContent || "").trim()).join("¦");
  }
  function sortTable(table, head, col, dir) {
    const body = head.parentElement;
    const rows = Array.from(table.rows).filter(r => r !== head && r.parentElement === body);
    if (rows.some(r => Array.from(r.cells).some(c => c.colSpan > 1))) return false;
    const keyed = rows.map((r, i) => ({r, i, v: cellValue(r.cells[col])}));
    keyed.sort((x, y) => (compare(x.v, y.v) * dir) || (x.i - y.i));
    const frag = document.createDocumentFragment();
    keyed.forEach(k => frag.appendChild(k.r));
    body.appendChild(frag);
    Array.from(head.cells).forEach((c, i) => {
      if (i === col) c.setAttribute("aria-sort", dir > 0 ? "ascending" : "descending");
      else c.removeAttribute("aria-sort");
    });
    return true;
  }
  function loadWidths(sig) {
    try { return JSON.parse(localStorage.getItem("fk.w." + sig) || "null"); } catch (e) { return null; }
  }
  function saveWidths(sig, ws) {
    try { ws ? localStorage.setItem("fk.w." + sig, JSON.stringify(ws)) : localStorage.removeItem("fk.w." + sig); }
    catch (e) { /* private window: widths just aren't remembered */ }
  }
  function freeze(table, head, widths) {
    const ws = widths || Array.from(head.cells).map(c => Math.round(c.getBoundingClientRect().width));
    Array.from(head.cells).forEach((c, i) => { c.style.width = ws[i] + "px"; });
    table.style.width = ws.reduce((a, b) => a + b, 0) + "px";
    table.classList.add("fk-fixed");
    return ws;
  }
  function unfreeze(table, head) {
    Array.from(head.cells).forEach(c => { c.style.width = ""; });
    table.style.width = "";
    table.classList.remove("fk-fixed");
  }
  function enhance(table) {
    if (table.classList.contains("no-fk")) return;
    const head = headerRow(table);
    if (!head) { if (table.rows.length) table.classList.add("no-fk"); return; }
    if (head._fk) { tools(table, head); return; }           // already done: only the rows changed
    head._fk = true;
    table.classList.add("fk");
    const sig = signature(head);
    const dataRows = table.rows.length - 1;
    Array.from(head.cells).forEach((th, col) => {
      const label = (th.textContent || "").trim();
      if (!NO_SORT && dataRows > 1 && label && !th.querySelector("input, button, select")) {
        th.classList.add("fk-sort");
        if (!th.title) th.title = "Sort by " + label;
        th.addEventListener("click", e => {
          if (e.target.closest(".fk-grip")) return;
          const cur = sortState[sig];
          const dir = cur && cur.col === col ? -cur.dir : 1;
          if (sortTable(table, head, col, dir)) { sortState[sig] = {col, dir}; tools(table, head); }
        });
      }
      const grip = document.createElement("span");
      grip.className = "fk-grip";
      grip.title = "Drag to resize the column. Double-click to reset.";
      grip.addEventListener("click", e => e.stopPropagation());
      grip.addEventListener("dblclick", e => { e.stopPropagation(); unfreeze(table, head); saveWidths(sig, null); });
      grip.addEventListener("pointerdown", e => {
        e.preventDefault(); e.stopPropagation();
        const ws = freeze(table, head, table.classList.contains("fk-fixed")
          ? Array.from(head.cells).map(c => parseFloat(c.style.width) || c.offsetWidth) : null);
        const x0 = e.clientX, w0 = ws[col];
        grip.classList.add("on"); document.body.classList.add("fk-dragging");
        const move = ev => {
          ws[col] = Math.max(36, Math.round(w0 + ev.clientX - x0));
          th.style.width = ws[col] + "px";
          table.style.width = ws.reduce((a, b) => a + b, 0) + "px";
        };
        const up = () => {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          grip.classList.remove("on"); document.body.classList.remove("fk-dragging");
          saveWidths(sig, ws);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      });
      th.appendChild(grip);
    });
    const saved = loadWidths(sig);
    if (Array.isArray(saved) && saved.length === head.cells.length) freeze(table, head, saved);
    const st = sortState[sig];                             // a page that redraws keeps its order
    if (st && st.col < head.cells.length) sortTable(table, head, st.col, st.dir);
    tools(table, head);
  }

  // ---- long tables: find and paging -------------------------------------------------------
  // A table of LONG rows or more shows PAGE rows at a time under a bar with a find box.
  // The state is kept per table signature, so it survives the page re-rendering the table.
  const LONG = 30, PAGE = 50;
  const toolState = {};                                   // signature -> {q, limit, focus}
  const toolBars = new WeakMap();                         // table -> {bar, find, n, more, all, sig}
  FK.LONG = LONG; FK.PAGE = PAGE;
  // a row made of one cell spanning the table (a detail row) stays with the row above it
  function rowGroups(table, head) {
    const out = [];
    Array.from(table.rows).forEach(r => {
      if (r === head) return;
      const span = r.cells.length === 1 && r.cells[0].colSpan > 1;
      if (span && out.length) out[out.length - 1].push(r); else out.push([r]);
    });
    return out;
  }
  function applyTools(table, head) {
    const t = toolBars.get(table);
    if (!t) return;
    const st = toolState[t.sig], q = (st.q || "").trim().toLowerCase();
    const groups = rowGroups(table, head);
    let match = 0, shown = 0;
    groups.forEach(g => {
      const ok = !q || g.some(r => (r.textContent || "").toLowerCase().includes(q));
      if (ok) match++;
      const show = ok && (!st.limit || shown < st.limit);
      if (show) shown++;
      g.forEach(r => { if (r.classList.contains("fk-hide") === show) r.classList.toggle("fk-hide", !show); });
    });
    const total = groups.length;
    t.bar.hidden = (total < LONG && !q) || (!t.find && total <= PAGE);
    const txt = q ? (match ? (shown < match ? `showing ${shown} of ${match} that match` : `${match} of ${total} match`) : "nothing matches")
                  : (shown < total ? `showing ${shown} of ${total}` : `all ${total} shown`);
    if (t.n.textContent !== txt) t.n.textContent = txt;
    const left = match - shown;
    t.more.hidden = left <= 0;
    t.all.hidden = left <= PAGE;
    t.less.hidden = !(left <= 0 && match > PAGE);
    const moreTxt = `Show ${Math.min(PAGE, left)} more`;
    if (t.more.textContent !== moreTxt) t.more.textContent = moreTxt;
    const allTxt = `Show all ${match}`;
    if (t.all.textContent !== allTxt) t.all.textContent = allTxt;
  }
  function tools(table, head) {
    if (NO_TOOLS || table.classList.contains("no-tools")) return;
    let t = toolBars.get(table);
    if (!t) {
      if (table.rows.length - 1 < LONG) return;
      const sig = signature(head) + "#" + (table.id || "");
      const st = toolState[sig] = toolState[sig] || {q: "", limit: PAGE, focus: false};
      const bar = document.createElement("div");
      bar.className = "fk-tools";
      const noFind = table.dataset.fkFind === "off";
      bar.innerHTML = (noFind ? "" : '<input type="search" class="fk-find" placeholder="Find in this table" aria-label="Find in this table">')
        + '<span class="fk-n" aria-live="polite"></span>'
        + '<button type="button" class="fk-more"></button><button type="button" class="fk-all"></button>'
        + `<button type="button" class="fk-less">Show the first ${PAGE}</button>`;
      t = {bar, sig, find: bar.querySelector(".fk-find"), n: bar.querySelector(".fk-n"),
           more: bar.querySelector(".fk-more"), all: bar.querySelector(".fk-all"), less: bar.querySelector(".fk-less")};
      toolBars.set(table, t);
      if (t.find) {
        t.find.value = st.q;
        t.find.addEventListener("input", () => { st.q = t.find.value; applyTools(table, headerRow(table) || head); });
        t.find.addEventListener("focus", () => { st.focus = true; });
        t.find.addEventListener("blur", () => { if (t.find.isConnected) st.focus = false; });
      } else st.q = "";
      const again = () => applyTools(table, headerRow(table) || head);
      t.more.addEventListener("click", () => { st.limit = (st.limit || 0) + PAGE; again(); });
      t.all.addEventListener("click", () => { st.limit = 0; again(); });
      t.less.addEventListener("click", () => { st.limit = PAGE; again(); });
      // inside the table's scroll box the bar keeps its place while the table scrolls sideways
      const par = table.parentElement;
      const stale = table.previousElementSibling;           // left by a table that was replaced
      if (stale && stale.classList.contains("fk-tools")) stale.remove();
      par.insertBefore(bar, table);
      if (par.classList.contains("fk-scroll") || par.classList.contains("tscroll")) bar.classList.add("in-box");
      // the page redrew the table while its find box was in use: the caret goes back
      if (t.find && st.focus && (!document.activeElement || document.activeElement === document.body)) {
        t.find.focus(); t.find.setSelectionRange(st.q.length, st.q.length);
      }
    }
    applyTools(table, head);
  }
  // A table wider than its place scrolls sideways inside its own box; it never pushes
  // the page (or its last columns) past the edge of the window.
  function boxed(table) {
    const par = table.parentElement;
    if (!par || table.dataset.fkBox) return;
    table.dataset.fkBox = "1";
    if (par.classList.contains("fk-scroll") || par.classList.contains("tscroll")) return;
    const ox = getComputedStyle(par).overflowX;
    if (ox === "auto" || ox === "scroll") return;
    const box = document.createElement("div");
    box.className = "fk-scroll";
    par.insertBefore(box, table);
    box.appendChild(table);
  }
  FK.tables = function (root) {
    (root || document).querySelectorAll("table").forEach(t => {
      try { boxed(t); enhance(t); } catch (e) { t.classList.add("no-fk"); }
    });
  };
  let pending = 0;
  function schedule() { if (!pending) pending = setTimeout(() => { pending = 0; FK.tables(document); }, 80); }
  function start() {
    FK.tables(document);
    new MutationObserver(schedule).observe(document.body, {childList: true, subtree: true});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();

  // ---- flow map ----------------------------------------------------------------------------
  // FK.flow({columns:[{key,title}], nodes:[{id,col,label,sub,count,status,live,err,dim,gates,tip}],
  //          edges:[{from,to,kind,count,label,tip,onclick}], kinds:{kind:{color,dash,live,thin}},
  //          width}) -> SVG markup. Call FK.flowWire(container) after inserting it.
  FK.flow = function (g) {
    const cols = g.columns, n = cols.length;
    const W = Math.max(900, Math.round(g.width || 1100));
    const NW = Math.max(150, Math.min(250, Math.round((W - 20) / n * 0.56)));
    const NH = 46, GAP = 12, TOP = 30;
    const colX = i => 6 + i * ((W - NW - 12) / Math.max(1, n - 1));
    const byCol = {}; cols.forEach(c => { byCol[c.key] = []; });
    g.nodes.forEach(nd => { if (byCol[nd.col]) byCol[nd.col].push(nd); });
    const pos = {};
    cols.forEach((c, i) => byCol[c.key].forEach((nd, j) => { pos[nd.id] = {x: colX(i), y: TOP + j * (NH + GAP)}; }));
    const H = TOP + Math.max(1, ...cols.map(c => byCol[c.key].length)) * (NH + GAP) + 6;
    const maxChars = Math.floor((NW - 58) / 6.6);
    const clip = (s, k) => { s = String(s || ""); return s.length > k ? s.slice(0, Math.max(1, k - 1)) + "…" : s; };
    const widthOf = e => (g.kinds[e.kind] || {}).thin ? 1.4 : Math.min(15, 1.8 + Math.log2((e.count || 0) + 1) * 1.7);
    const edges = g.edges.filter(e => pos[e.from] && pos[e.to]).map(e => {
      const k = g.kinds[e.kind] || {}, a = pos[e.from], b = pos[e.to];
      const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
      const d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
      const w = widthOf(e);
      return `<g class="fk-edge${e.onclick ? " click" : ""}${k.live ? " live" : ""}${k.thin ? " thin" : ""}" data-a="${esc(e.from)}" data-b="${esc(e.to)}"${e.onclick ? ` onclick="${esc(e.onclick)}"` : ""}>
        <title>${esc(e.tip || "")}</title>
        <path class="line" d="${d}" style="stroke:${k.color || "var(--dim)"};stroke-width:${w.toFixed(1)};opacity:${k.thin ? .4 : .8}${k.dash ? ";stroke-dasharray:" + k.dash : ""}"/>
        <path d="${d}" fill="none" stroke="transparent" stroke-width="${Math.max(14, w + 8)}"/>
        ${e.label ? `<text x="${mx}" y="${(y1 + y2) / 2 - 5}" class="fk-lbl">${esc(e.label)}</text>` : ""}</g>`;
    }).join("");
    const nodes = g.nodes.filter(nd => pos[nd.id]).map(nd => {
      const p = pos[nd.id];
      const cls = [nd.status || "", nd.err ? "err" : "", nd.live ? "live" : ""].filter(Boolean).join(" ");
      return `<g class="fk-node ${cls}" data-id="${esc(nd.id)}"${nd.dim ? ' style="opacity:.45"' : ""}>
        <title>${esc(nd.tip || nd.label)}</title>
        <rect class="box" x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="6"/>
        <rect class="bar" x="${p.x}" y="${p.y}" width="4" height="${NH}" rx="2"/>
        <text x="${p.x + 13}" y="${p.y + 19}" class="fk-name">${esc(clip(nd.label, maxChars))}</text>
        ${nd.count ? `<text x="${p.x + NW - 9}" y="${p.y + 19}" class="fk-count">${esc(nd.count)}</text>` : ""}
        <text x="${p.x + 13}" y="${p.y + 36}" class="fk-sub">${esc(clip(nd.sub, maxChars + 6))}</text>
        ${nd.gates ? `<text x="${p.x + NW - 9}" y="${p.y + 36}" class="fk-gate">${esc(nd.gates)}</text>` : ""}</g>`;
    }).join("");
    const heads = cols.map((c, i) => `<text x="${colX(i)}" y="16" class="fk-col">${esc(c.title)}</text>`).join("");
    return `<svg class="fk-flow" viewBox="0 0 ${W} ${H}" style="min-width:${W <= 900 ? 900 : 760}px" role="img" aria-label="${esc(g.label || "flow map")}">${heads}${edges}${nodes}</svg>`;
  };
  // hovering a node or a link lights up everything connected to it
  FK.flowWire = function (root) {
    const svg = root && root.querySelector(".fk-flow");
    if (!svg) return;
    const edges = Array.from(svg.querySelectorAll(".fk-edge")), nodes = Array.from(svg.querySelectorAll(".fk-node"));
    const light = ids => {
      svg.classList.add("focus");
      edges.forEach(e => e.classList.toggle("on", ids.edges.has(e)));
      nodes.forEach(nd => nd.classList.toggle("on", ids.nodes.has(nd.dataset.id)));
    };
    const clear = () => { svg.classList.remove("focus"); };
    const reach = start => {                        // everything up- and downstream of a node
      const out = {edges: new Set(), nodes: new Set([start])};
      const walk = (id, fwd) => edges.forEach(e => {
        const from = fwd ? e.dataset.a : e.dataset.b, to = fwd ? e.dataset.b : e.dataset.a;
        if (from === id && !out.edges.has(e)) { out.edges.add(e); out.nodes.add(to); walk(to, fwd); }
      });
      walk(start, true); walk(start, false);
      return out;
    };
    nodes.forEach(nd => {
      nd.addEventListener("mouseenter", () => light(reach(nd.dataset.id)));
      nd.addEventListener("mouseleave", clear);
    });
    edges.forEach(e => {
      e.addEventListener("mouseenter", () => light({edges: new Set([e]), nodes: new Set([e.dataset.a, e.dataset.b])}));
      e.addEventListener("mouseleave", clear);
    });
  };
})();
