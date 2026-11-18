// The interactive PDF reader: pdf.js for display, an SVG ink layer per page for the user's own pen/highlighter strokes.
// Stylus handling is the browser's: we only look at PointerEvent.pointerType and use touch-action; there is no palm
// rejection of our own (the OS/browser does that before events reach the page). Stroke smoothing is perfect-freehand.
import * as pdfjsLib from "/app/static/vendor/pdfjs/build/pdf.min.mjs";
import { EventBus, FindState, PDFViewer, PDFLinkService, PDFFindController } from "/app/static/vendor/pdfjs/web/pdf_viewer.mjs";
import { getStroke } from "/app/static/vendor/perfect-freehand/index.mjs";
import { newId, isTyping, strokeHit, outlineToPath, findAttempts, History, Outbox } from "/app/static/ink-core.mjs";

const D = JSON.parse(document.getElementById("reader-data").textContent);
const $ = id => document.getElementById(id);
const V = "/app/static/vendor/pdfjs/";
const SVGNS = "http://www.w3.org/2000/svg";
const store = {
  get: (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode: the setting just isn't remembered */ } },
};

pdfjsLib.GlobalWorkerOptions.workerSrc = V + "build/pdf.worker.min.mjs";
const container = $("viewerContainer"), viewerEl = $("viewer");
const eventBus = new EventBus();
const linkService = new PDFLinkService({ eventBus });
const findController = new PDFFindController({ linkService, eventBus });
const pdfViewer = new PDFViewer({ container, viewer: viewerEl, eventBus, linkService, findController, textLayerMode: 1 });
linkService.setViewer(pdfViewer);
linkService.externalLinkTarget = 2;            // links inside the PDF to other sites open in a new tab
linkService.externalLinkRel = "noopener noreferrer";

// ---------- strokes ----------
const strokes = new Map();            // id -> stroke (live ones)
const byPage = page => [...strokes.values()].filter(s => s.page === page);
const history = new History();
const outbox = new Outbox();
let tool = "hand", color = store.get("lw-ink-color", "#d62828"), width = 1.6, fingerOK = false;
const TOOLS = { pen: { kind: "pen", opacity: 1, thinning: 0.5 }, highlighter: { kind: "highlighter", opacity: 0.35, thinning: 0 } };
const widths = { pen: 1.6, highlighter: 14 };

const baseView = pv => pv.pdfPage.getViewport({ scale: 1 });   // scale 1, the page's own rotation: top-left origin, CSS px = points
function strokePath(s, base) {
  const toView = (x, y) => base.convertToViewportPoint(x, y);
  let d = "";
  s.paths.forEach((flat, i) => {
    const pts = [];
    for (let j = 0; j < flat.length; j += 2) {
      const [x, y] = toView(flat[j], flat[j + 1]);
      pts.push(s.pressures && s.pressures[i] ? [x, y, s.pressures[i][j / 2]] : [x, y]);
    }
    if (pts.length === 1) pts.push([pts[0][0] + 0.01, pts[0][1]]);
    const t = TOOLS[s.kind] || TOOLS.pen;
    d += outlineToPath(getStroke(pts, { size: s.width, thinning: t.thinning, smoothing: 0.5, streamline: 0.5,
                                         simulatePressure: !s.pressures, last: true })) + " ";
  });
  return d;
}
function strokeEl(s, base) {
  const p = document.createElementNS(SVGNS, "path");
  p.setAttribute("d", strokePath(s, base)); p.setAttribute("fill", s.color); p.setAttribute("fill-opacity", s.opacity);
  if (s.kind === "highlighter") p.style.mixBlendMode = "multiply";
  p.dataset.id = s.id;
  return p;
}

const layers = new Map();             // pageNumber -> {svg, base, pv}
function ensureLayer(pv) {
  const n = pv.id;
  let L = layers.get(n);
  if (L && L.svg.isConnected && L.pv === pv) return L;
  if (!pv.pdfPage || !pv.div) return null;
  const base = baseView(pv);
  const wrap = document.createElement("div"); wrap.className = "inkLayer";
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${base.width} ${base.height}`); svg.setAttribute("preserveAspectRatio", "none");
  wrap.append(svg); pv.div.append(wrap);
  L = { svg, base, pv, wrap };
  layers.set(n, L);
  wrap.addEventListener("pointerdown", ev => onDown(ev, L));
  paintPage(L);
  return L;
}
function paintPage(L) {
  L.svg.querySelectorAll("path:not(.live)").forEach(p => p.remove());
  byPage(L.pv.id - 1).forEach(s => L.svg.append(strokeEl(s, L.base)));
}
const layerFor = page => layers.get(page + 1);
function addLocal(s) { strokes.set(s.id, s); const L = layerFor(s.page); if (L && L.svg.isConnected) L.svg.append(strokeEl(s, L.base)); }
function removeLocal(s) { strokes.delete(s.id); const L = layerFor(s.page); if (L) L.svg.querySelector(`path[data-id="${s.id}"]`)?.remove(); }

// ---------- drawing ----------
function svgPoint(L, ev) {
  const m = L.svg.getScreenCTM(); if (!m) return null;
  const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m.inverse());
  return L.base.convertToPdfPoint(p.x, p.y);        // -> PDF user space
}
let drawing = null;
function allowed(ev) { return ev.pointerType === "pen" || ev.pointerType === "mouse" || (ev.pointerType === "touch" && fingerOK); }
function onDown(ev, L) {
  if (tool === "hand" || !allowed(ev) || (ev.pointerType === "mouse" && ev.button !== 0)) return;
  const eraser = tool === "eraser" || (ev.pointerType === "pen" && (ev.buttons & 32));   // the pen's own eraser end/button
  const pt = svgPoint(L, ev); if (!pt) return;
  ev.preventDefault();
  L.wrap.setPointerCapture(ev.pointerId);
  const page = L.pv.id - 1;
  if (eraser) {
    drawing = { L, page, eraser: true, gone: [], id: ev.pointerId };
    erase(drawing, pt);
  } else {
    const t = TOOLS[tool];
    const live = document.createElementNS(SVGNS, "path"); live.setAttribute("class", "live");
    live.setAttribute("fill", color); live.setAttribute("fill-opacity", t.opacity);
    if (tool === "highlighter") live.style.mixBlendMode = "multiply";
    L.svg.append(live);
    drawing = { L, page, eraser: false, pts: [pt], pr: [ev.pointerType === "pen" ? ev.pressure : null], live, id: ev.pointerId, pen: ev.pointerType === "pen" };
    paintLive(drawing);
  }
  const move = e => {
    if (!drawing || e.pointerId !== drawing.id) return;
    const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    for (const c of (evs.length ? evs : [e])) {
      const q = svgPoint(L, c); if (!q) continue;
      if (drawing.eraser) erase(drawing, q);
      else { drawing.pts.push(q); drawing.pr.push(drawing.pen ? c.pressure : null); }
    }
    if (!drawing.eraser) paintLive(drawing);
  };
  const up = e => {
    if (!drawing || e.pointerId !== drawing.id) return;
    L.wrap.removeEventListener("pointermove", move); L.wrap.removeEventListener("pointerup", up); L.wrap.removeEventListener("pointercancel", up);
    finish(e.type === "pointercancel"); drawing = null;
  };
  L.wrap.addEventListener("pointermove", move); L.wrap.addEventListener("pointerup", up); L.wrap.addEventListener("pointercancel", up);
}
function paintLive(dr) {
  const pts = dr.pts.map((q, i) => { const v = dr.L.base.convertToViewportPoint(q[0], q[1]); return dr.pr[i] ? [v[0], v[1], dr.pr[i]] : v; });
  if (pts.length === 1) pts.push([pts[0][0] + 0.01, pts[0][1]]);
  const t = TOOLS[tool] || TOOLS.pen;
  dr.live.setAttribute("d", outlineToPath(getStroke(pts, { size: width, thinning: t.thinning, smoothing: 0.5, streamline: 0.5, simulatePressure: !dr.pr.some(Boolean) })));
}
function erase(dr, pt) {
  for (const s of byPage(dr.page)) if (strokeHit(s, pt[0], pt[1], 6)) { removeLocal(s); outbox.removed(s); dr.gone.push(s); }
  if (dr.gone.length) setSaved(false);
}
function finish(cancelled) {
  const dr = drawing;
  if (dr.eraser) { if (dr.gone.length) { history.push({ type: "del", items: dr.gone }); scheduleSave(); updateButtons(); } return; }
  dr.live.remove();
  if (cancelled) return;
  const flat = dr.pts.flatMap(q => [q[0], q[1]]);
  const usePr = dr.pr.every(p => p !== null && p !== undefined) && dr.pr.length === dr.pts.length;
  const t = TOOLS[tool];
  const s = { id: newId(), page: dr.page, kind: t.kind, color, width, opacity: t.opacity, paths: [flat], pressures: usePr ? [dr.pr.map(p => +p.toFixed(3))] : null };
  addLocal(s);
  history.push({ type: "add", items: [s] }); outbox.added(s); setSaved(false); scheduleSave(); updateButtons();
}
function applyOp(op) {   // undo/redo
  for (const s of op.items) { if (op.type === "add") { strokes.set(s.id, s); addLocal(s); outbox.added(s); } else { removeLocal(s); outbox.removed(s); } }
  setSaved(false); scheduleSave(); updateButtons();
}

// ---------- saving ----------
let saveTimer = null, saving = false;
function setSaved(ok, msg) { const el = $("savestate"); el.textContent = msg || (ok ? "Saved" : "Unsaved changes"); el.className = ok ? "ok" : "dirty"; }
function scheduleSave() { clearTimeout(saveTimer); saveTimer = setTimeout(save, 1200); }
async function save() {
  if (saving) { scheduleSave(); return; }
  if (!outbox.dirty) { setSaved(true); return; }
  saving = true; const ops = outbox.take();
  try {
    const r = await fetch(D.annUrl, { method: "POST", body: new URLSearchParams({ _csrf: D.csrf, ops: JSON.stringify(ops) }) });
    if (!r.ok) { let m = "Could not save"; try { m = (await r.json()).error || m; } catch (e) { /* not JSON */ } throw new Error(m); }
    setSaved(!outbox.dirty);
  } catch (e) { outbox.requeue(ops); setSaved(false, "Not saved: " + e.message + " (will retry)"); saveTimer = setTimeout(save, 8000); }
  saving = false;
}
addEventListener("pagehide", () => { if (outbox.dirty) navigator.sendBeacon && navigator.sendBeacon(D.annUrl, new URLSearchParams({ _csrf: D.csrf, ops: JSON.stringify(outbox.take()) })); });

// ---------- toolbar ----------
function setTool(t) {
  if (t !== "hand" && !$("showink").checked) { $("showink").checked = true; applyVisible(); }
  tool = t;
  if (TOOLS[t]) { width = widths[t]; $("width").value = width; }
  document.querySelectorAll("[data-tool]").forEach(b => b.classList.toggle("on", b.dataset.tool === t));
  viewerEl.classList.toggle("drawing", t !== "hand");
  $("hint").textContent = t === "hand" ? "Hand: scroll, zoom and select text." :
    "Draw with a stylus or mouse. " + (fingerOK ? "" : "Fingers are ignored; switch to Hand to scroll by touch.");
}
function applyVisible() { const on = $("showink").checked; viewerEl.classList.toggle("ink-hidden", !on); store.set("lw-ink-visible:" + D.stem, on ? "1" : "0"); if (!on && tool !== "hand") setTool("hand"); }
function updateButtons() { $("undo").disabled = !history.canUndo; $("redo").disabled = !history.canRedo; $("count").textContent = strokes.size ? strokes.size + " stroke" + (strokes.size > 1 ? "s" : "") : ""; }
document.querySelectorAll("[data-tool]").forEach(b => b.addEventListener("click", () => setTool(b.dataset.tool)));
$("undo").addEventListener("click", () => { const op = history.undo(); if (op) applyOp(op); });
$("redo").addEventListener("click", () => { const op = history.redo(); if (op) applyOp(op); });
$("showink").addEventListener("change", applyVisible);
$("finger").addEventListener("change", () => { fingerOK = $("finger").checked; setTool(tool); });
$("width").addEventListener("input", () => { width = +$("width").value; widths[tool === "highlighter" ? "highlighter" : "pen"] = width; });
document.querySelectorAll("[data-color]").forEach(b => { b.style.background = b.dataset.color; b.addEventListener("click", () => { color = b.dataset.color; store.set("lw-ink-color", color); document.querySelectorAll("[data-color]").forEach(x => x.classList.toggle("on", x === b)); }); });
addEventListener("keydown", ev => {
  if (isTyping(ev.target)) return;
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); const op = ev.shiftKey ? history.redo() : history.undo(); if (op) applyOp(op); }
  else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "y") { ev.preventDefault(); const op = history.redo(); if (op) applyOp(op); }
});
$("prev").addEventListener("click", () => pdfViewer.previousPage());
$("next").addEventListener("click", () => pdfViewer.nextPage());
$("pageno").addEventListener("change", () => { const n = parseInt($("pageno").value, 10); if (n >= 1 && n <= pdfViewer.pagesCount) pdfViewer.currentPageNumber = n; });
$("zin").addEventListener("click", () => pdfViewer.increaseScale());
$("zout").addEventListener("click", () => pdfViewer.decreaseScale());
$("zfit").addEventListener("click", () => { pdfViewer.currentScaleValue = "page-width"; });
$("chatbtn").addEventListener("click", () => { const on = document.body.classList.toggle("nochat"); store.set("lw-reader-chat", on ? "0" : "1"); });
if (store.get("lw-reader-chat", "1") === "0") document.body.classList.add("nochat");

// ---------- jump to a page / a quote (chat citations) ----------
let findState = null;
const dispatchFind = q => eventBus.dispatch("find", { source: window, type: "", query: q, caseSensitive: false, entireWord: false,
  highlightAll: true, findPrevious: false, matchDiacritics: false, phraseSearch: true });
function gotoQuote(page, quote) {
  if (page >= 1 && page <= pdfViewer.pagesCount) pdfViewer.currentPageNumber = page;
  const tries = findAttempts(quote);
  if (!tries.length) return;
  findState = { tries, i: 0 };
  dispatchFind(tries[0]);
}
eventBus.on("updatefindcontrolstate", e => {   // a search that finds nothing: retry with a shorter start of the quote
  if (!findState || e.state !== FindState.NOT_FOUND) return;
  findState.i += 1;
  if (findState.i < findState.tries.length) dispatchFind(findState.tries[findState.i]);
  else { findState = null; $("hint").textContent = "Couldn't find that passage in the PDF's text; showing the page."; }
});
eventBus.on("updatefindmatchescount", e => { if (e.matchesCount && e.matchesCount.total > 0) $("hint").textContent = "Passage highlighted. Press Esc to clear it."; });
addEventListener("keydown", ev => { if (ev.key === "Escape") eventBus.dispatch("find", { source: window, type: "", query: "", highlightAll: false }); });
addEventListener("lw-goto", ev => {
  const d = ev.detail || {};
  if (d.paper === D.stem) gotoQuote(d.page, d.quote);
  else location.href = d.href;
});

// ---------- go ----------
eventBus.on("pagesinit", () => {
  pdfViewer.currentScaleValue = "page-width";
  $("pages").textContent = pdfViewer.pagesCount;
  const q = new URLSearchParams(location.search);
  const page = parseInt(q.get("page") || "1", 10);
  if (page > 1 || q.get("find")) setTimeout(() => gotoQuote(page, q.get("find") || ""), 300);
});
eventBus.on("pagechanging", e => { $("pageno").value = e.pageNumber; });
eventBus.on("pagerendered", e => { ensureLayer(e.source); });

async function load() {
  setSaved(true, "Loading…");
  const [pdf, ann] = await Promise.all([
    pdfjsLib.getDocument({ url: D.pdfUrl, cMapUrl: V + "cmaps/", cMapPacked: true, standardFontDataUrl: V + "standard_fonts/", wasmUrl: V + "wasm/", iccUrl: V + "iccs/" }).promise,
    fetch(D.annUrl, { cache: "no-store" }).then(r => r.json()),
  ]);
  (ann.items || []).forEach(s => strokes.set(s.id, s));
  outbox.markKnown([...strokes.keys()]);
  $("showink").checked = store.get("lw-ink-visible:" + D.stem, "1") !== "0"; applyVisible();
  pdfViewer.setDocument(pdf); linkService.setDocument(pdf);
  document.querySelector("[data-color='" + color + "']")?.classList.add("on");
  setTool("hand"); updateButtons(); setSaved(true);
}
load().catch(e => { $("hint").textContent = "Could not open the PDF: " + e.message; $("hint").className = "bad"; });
