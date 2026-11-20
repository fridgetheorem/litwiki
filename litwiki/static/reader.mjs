// The interactive PDF reader: pdf.js for display, an SVG ink layer per page for the user's own pen/highlighter strokes.
// Stylus handling is the browser's: we only look at PointerEvent.pointerType and use touch-action; there is no palm
// rejection of our own (the OS/browser does that before events reach the page). Stroke smoothing is perfect-freehand.
import * as pdfjsLib from "/app/static/vendor/pdfjs/build/pdf.min.mjs";
import { EventBus, FindState, PDFViewer, PDFLinkService, PDFFindController } from "/app/static/vendor/pdfjs/web/pdf_viewer.mjs";
import { getStroke } from "/app/static/vendor/perfect-freehand/index.mjs";
import { newId, isTyping, TouchGestures, strokeHit, outlineToPath, findAttempts, History, Outbox } from "/app/static/ink-core.mjs";

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
const widths = { pen: 1.6, highlighter: 14, text: 12 };

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
  const tb = document.createElement("div"); tb.className = "tbLayer";
  wrap.append(svg, tb); pv.div.append(wrap);
  L = { svg, base, pv, wrap, tb };
  layers.set(n, L);
  wrap.addEventListener("pointerdown", ev => onDown(ev, L));
  paintPage(L);
  return L;
}
function paintPage(L) {
  L.svg.querySelectorAll("path:not(.live)").forEach(p => p.remove());
  L.tb.querySelectorAll(".tbx:not(.editing)").forEach(p => p.remove());
  byPage(L.pv.id - 1).forEach(s => (s.kind === "text" ? L.tb.append(textEl(s, L)) : L.svg.append(strokeEl(s, L.base))));
}
const layerFor = page => layers.get(page + 1);
function addLocal(s) { strokes.set(s.id, s); const L = layerFor(s.page); if (L && L.svg.isConnected) (s.kind === "text" ? L.tb.append(textEl(s, L)) : L.svg.append(strokeEl(s, L.base))); }
function removeLocal(s) { strokes.delete(s.id); const L = layerFor(s.page); if (L) { L.svg.querySelector(`path[data-id="${s.id}"]`)?.remove(); L.tb.querySelector(`.tbx[data-id="${s.id}"]`)?.remove(); } }

// ---------- drawing ----------
function svgPoint(L, ev) {
  const m = L.svg.getScreenCTM(); if (!m) return null;
  const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m.inverse());
  return L.base.convertToPdfPoint(p.x, p.y);        // -> PDF user space
}
let drawing = null;
function allowed(ev) { return ev.pointerType === "pen" || ev.pointerType === "mouse" || (ev.pointerType === "touch" && fingerOK); }
function onDown(ev, L) {
  if (ev.target.closest && ev.target.closest(".tbx")) return;      // a text box handles its own events
  if (tool === "text") { textDown(ev, L); return; }
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
  if (op.type === "edit") { removeLocal(op.from); outbox.removed(op.from); strokes.set(op.to.id, op.to); addLocal(op.to); outbox.added(op.to); setSaved(false); scheduleSave(); updateButtons(); return; }
  for (const s of op.items) { if (op.type === "add") { strokes.set(s.id, s); addLocal(s); outbox.added(s); } else { removeLocal(s); outbox.removed(s); } }
  setSaved(false); scheduleSave(); updateButtons();
}

// ---------- text boxes ----------
// A text box is an annotation of kind "text": `rect` = [x0,y0,x1,y1] in PDF user space, `width` = font size in points, `text`.
// It is an HTML box over the page, sized in percentages and container-query units so it follows the zoom without any JS. A box is
// never changed in place: typing, moving or resizing replaces it by a new item (one undo step), like the strokes.
let editing = null;                    // {el, L, s|null, size, color} while a box has the cursor
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
function textGeom(s, L) {              // view-space (top-left origin, points) x, y, w of a saved box
  const [ax, ay] = L.base.convertToViewportPoint(s.rect[0], s.rect[1]), [bx, by] = L.base.convertToViewportPoint(s.rect[2], s.rect[3]);
  return { x: Math.min(ax, bx), y: Math.min(ay, by), w: Math.abs(bx - ax) };
}
function placeBox(d, L, x, y, w, size, col) {
  const W = L.base.width, H = L.base.height;
  d.style.left = (x / W * 100) + "%"; d.style.top = (y / H * 100) + "%"; d.style.width = (w / W * 100) + "%";
  d.style.fontSize = `calc(${size / W * 100}cqw)`; d.style.color = col;
}
function boxEl(L, x, y, w, size, col, text, id) {
  const d = document.createElement("div"); d.className = "tbx"; if (id) d.dataset.id = id;
  const tt = document.createElement("div"); tt.className = "tt"; tt.textContent = text;
  const tc = document.createElement("div"); tc.className = "tc";
  const mv = document.createElement("button"); mv.type = "button"; mv.className = "mv"; mv.textContent = "⋮⋮"; mv.title = "Drag to move";
  const rm = document.createElement("button"); rm.type = "button"; rm.textContent = "✕"; rm.title = "Delete this text box";
  tc.append(mv, rm);
  const rs = document.createElement("div"); rs.className = "rs"; rs.title = "Drag to change the width";
  d.append(tc, tt, rs); placeBox(d, L, x, y, w, size, col);
  return { d, tt, mv, rm, rs };
}
function textEl(s, L) {
  const g = textGeom(s, L), { d, tt, mv, rm, rs } = boxEl(L, g.x, g.y, g.w, s.width, s.color, s.text, s.id);
  d.addEventListener("click", e => { if (e.target.closest(".tc, .rs")) return; if (tool === "eraser") deleteText(s); else if (!editing) beginEdit(L, s); });
  rm.addEventListener("click", e => { e.stopPropagation(); deleteText(s); });
  mv.addEventListener("pointerdown", e => dragBox(e, L, s, d, "move"));
  rs.addEventListener("pointerdown", e => dragBox(e, L, s, d, "size"));
  return d;
}
function deleteText(s) { removeLocal(s); outbox.removed(s); history.push({ type: "del", items: [s] }); setSaved(false); scheduleSave(); updateButtons(); }
function rectOf(L, x, y, w, hPx) {      // view-space box -> PDF user space rect; hPx is its rendered height in CSS px
  const hPt = hPx / (L.tb.clientWidth / L.base.width);
  const [ax, ay] = L.base.convertToPdfPoint(x, y), [bx, by] = L.base.convertToPdfPoint(x + w, y + hPt);
  return [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)].map(v => +v.toFixed(2));
}
function makeText(L, x, y, w, size, col, text, d) {
  return { id: newId(), page: L.pv.id - 1, kind: "text", color: col, width: size, opacity: 1, paths: [], pressures: null, text,
           rect: rectOf(L, x, y, w, d.offsetHeight), rot: L.base.rotation || 0 };
}
function replaceText(from, to) {        // one undo step: `from` becomes `to`
  removeLocal(from); outbox.removed(from); addLocal(to); outbox.added(to);
  history.push({ type: "edit", from, to }); setSaved(false); scheduleSave(); updateButtons();
}
function beginEdit(L, s, at) {          // s = an existing box, or null with at = {x, y} (view points) for a new one
  if (editing) endEdit();
  const g = s ? textGeom(s, L) : { x: at.x, y: at.y, w: clamp(L.base.width - at.x - 8, 60, 190) };
  const size = s ? s.width : widths.text, col = s ? s.color : color;
  let d, tt;
  if (s) { d = L.tb.querySelector(`.tbx[data-id="${s.id}"]`); tt = d.querySelector(".tt"); }
  else { ({ d, tt } = boxEl(L, g.x, g.y, g.w, size, col, "", null)); L.tb.append(d); }
  d.classList.add("editing"); tt.contentEditable = "true"; tt.spellcheck = true;
  editing = { el: d, tt, L, s, size, color: col, g };
  tt.addEventListener("paste", e => { e.preventDefault(); document.execCommand("insertText", false, (e.clipboardData || window.clipboardData).getData("text/plain")); });
  tt.addEventListener("keydown", e => { e.stopPropagation(); if (e.key === "Escape") { e.preventDefault(); tt.blur(); } });
  tt.addEventListener("focusout", () => setTimeout(() => { if (editing && editing.tt === tt && !d.contains(document.activeElement)) endEdit(); }, 0));
  tt.focus();
  const r = document.createRange(); r.selectNodeContents(tt); r.collapse(false);
  const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
}
function endEdit() {
  const e = editing; if (!e) return; editing = null;
  const text = e.tt.innerText.replace(/\u00a0/g, " ").replace(/\s+$/, "");
  const { x, y, w } = e.g;
  if (e.s) {
    if (!text) { deleteText(e.s); return; }
    if (text === e.s.text && e.color === e.s.color && e.size === e.s.width) { e.el.classList.remove("editing"); e.tt.contentEditable = "false"; return; }
    const to = makeText(e.L, x, y, w, e.size, e.color, text, e.el);
    e.el.remove(); replaceText(e.s, to);
  } else {
    e.el.remove();
    if (!text) return;
    const s = makeText(e.L, x, y, w, e.size, e.color, text, e.el);
    addLocal(s); history.push({ type: "add", items: [s] }); outbox.added(s); setSaved(false); scheduleSave(); updateButtons();
  }
}
function dragBox(ev, L, s, d, mode) {   // move by the grip, or change the width by the right edge
  if (ev.button !== undefined && ev.button !== 0) return;
  ev.preventDefault(); ev.stopPropagation();
  const g = textGeom(s, L), k = L.tb.clientWidth / L.base.width, x0 = ev.clientX, y0 = ev.clientY;
  let nx = g.x, ny = g.y, nw = g.w; const h = d.offsetHeight;
  ev.target.setPointerCapture(ev.pointerId);
  const mv = e => {
    const dx = (e.clientX - x0) / k, dy = (e.clientY - y0) / k;
    if (mode === "move") { nx = clamp(g.x + dx, 0, L.base.width - 10); ny = clamp(g.y + dy, 0, L.base.height - 6); }
    else nw = clamp(g.w + dx, 20, L.base.width - g.x);
    placeBox(d, L, nx, ny, nw, s.width, s.color);
  };
  const up = () => {
    ev.target.removeEventListener("pointermove", mv); ev.target.removeEventListener("pointerup", up); ev.target.removeEventListener("pointercancel", up);
    if (nx === g.x && ny === g.y && nw === g.w) return;
    const to = { ...s, id: newId(), rect: rectOf(L, nx, ny, nw, d.offsetHeight || h) };
    replaceText(s, to);
  };
  ev.target.addEventListener("pointermove", mv); ev.target.addEventListener("pointerup", up); ev.target.addEventListener("pointercancel", up);
}
function textDown(ev, L) {              // a tap (not a drag or a two-finger gesture) with the text tool puts a new box there
  if (!ev.isPrimary || (ev.pointerType === "mouse" && ev.button !== 0)) return;
  const m = L.svg.getScreenCTM(); if (!m) return;
  const x0 = ev.clientX, y0 = ev.clientY, t0 = ev.timeStamp;
  const up = e => {
    L.wrap.removeEventListener("pointerup", up); L.wrap.removeEventListener("pointercancel", cancel);
    if (e.pointerId !== ev.pointerId || Math.hypot(e.clientX - x0, e.clientY - y0) > 8 || e.timeStamp - t0 > 700) return;
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
    e.preventDefault(); beginEdit(L, null, { x: clamp(p.x, 0, L.base.width - 20), y: clamp(p.y - 6, 0, L.base.height - 10) });
  };
  const cancel = () => { L.wrap.removeEventListener("pointerup", up); L.wrap.removeEventListener("pointercancel", cancel); };
  L.wrap.addEventListener("pointerup", up); L.wrap.addEventListener("pointercancel", cancel);
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

async function flush() {   // make sure everything drawn so far is on the server before something reads it there
  clearTimeout(saveTimer);
  for (let i = 0; i < 40 && (outbox.dirty || saving); i++) { await save(); if (saving) await new Promise(r => setTimeout(r, 150)); }
}
$("dlann").addEventListener("click", async ev => { ev.preventDefault(); const href = ev.currentTarget.href; $("dl").open = false; await flush(); location.href = href; });
addEventListener("click", ev => { const d = $("dl"); if (d.open && !d.contains(ev.target)) d.open = false; });

// ---------- finger gestures while drawing ----------
// A drawing tool sets touch-action:none so the stylus can draw, which also stops the browser scrolling for fingers; so a finger
// drags the page by hand here, and a quick two-finger tap undoes. Touches are ignored while a stylus stroke is in progress.
const gestures = new TouchGestures();
let flingId = null;
function stopFling() { if (flingId) { cancelAnimationFrame(flingId); flingId = null; } }
function fling(vx, vy) {
  let last = performance.now();
  const step = now => {
    const dt = now - last; last = now;
    container.scrollLeft -= vx * dt; container.scrollTop -= vy * dt;
    const decay = Math.exp(-dt / 325); vx *= decay; vy *= decay;
    flingId = Math.hypot(vx, vy) > 0.02 ? requestAnimationFrame(step) : null;
  };
  flingId = requestAnimationFrame(step);
}
const fingerMode = () => tool !== "hand" && !fingerOK;
container.addEventListener("pointerdown", ev => {
  if (ev.pointerType !== "touch" || !fingerMode() || drawing) return;
  stopFling(); gestures.down(ev.pointerId, ev.clientX, ev.clientY, ev.timeStamp);
});
container.addEventListener("pointermove", ev => {
  if (ev.pointerType !== "touch" || !gestures.active || drawing) return;
  const a = gestures.move(ev.pointerId, ev.clientX, ev.clientY, ev.timeStamp);
  if (a && a.type === "pan") { container.scrollLeft -= a.dx; container.scrollTop -= a.dy; }
});
container.addEventListener("pointerup", ev => {
  if (ev.pointerType !== "touch" || !gestures.active) return;
  const a = gestures.up(ev.pointerId, ev.timeStamp);
  if (!a) return;
  if (a.type === "undo") { const op = history.undo(); if (op) applyOp(op); }
  else if (a.type === "fling") fling(a.vx, a.vy);
});
container.addEventListener("pointercancel", ev => { if (ev.pointerType === "touch") gestures.reset(); });

// ---------- toolbar ----------
function setTool(t) {
  if (t !== "hand" && !$("showink").checked) { $("showink").checked = true; applyVisible(); }
  if (editing) endEdit();
  tool = t;
  const w = $("width");
  if (t === "text") { w.min = 6; w.max = 48; w.step = 1; width = widths.text; w.value = width; w.title = "Font size"; }
  else { w.min = 0.6; w.max = 30; w.step = 0.2; w.title = "Stroke width"; if (TOOLS[t]) { width = widths[t]; w.value = width; } }
  document.querySelectorAll("[data-tool]").forEach(b => b.classList.toggle("on", b.dataset.tool === t));
  viewerEl.classList.toggle("drawing", t !== "hand");
  container.classList.toggle("drawmode", t !== "hand");
  viewerEl.classList.toggle("textmode", t === "text");
  gestures.reset(); stopFling();
  $("hint").textContent = t === "text" ? "Text: click where the text goes, then type. Click a box to edit it, drag ⋮⋮ to move it or its right edge to resize; Esc or clicking elsewhere finishes." : t === "hand" ? "Hand: scroll, zoom and select text." :
    "Draw with a stylus or mouse. " + (fingerOK ? "A finger draws too." : "One finger scrolls; tap with two fingers to undo. Switch to Hand to zoom by pinching.");
}
function applyVisible() { const on = $("showink").checked; viewerEl.classList.toggle("ink-hidden", !on); store.set("lw-ink-visible:" + D.stem, on ? "1" : "0"); if (!on && tool !== "hand") setTool("hand"); }
function updateButtons() { $("undo").disabled = !history.canUndo; $("redo").disabled = !history.canRedo; $("count").textContent = strokes.size ? strokes.size + " annotation" + (strokes.size > 1 ? "s" : "") : ""; }
document.querySelectorAll("[data-tool]").forEach(b => b.addEventListener("click", () => setTool(b.dataset.tool)));
$("undo").addEventListener("click", () => { const op = history.undo(); if (op) applyOp(op); });
$("redo").addEventListener("click", () => { const op = history.redo(); if (op) applyOp(op); });
$("showink").addEventListener("change", applyVisible);
$("finger").addEventListener("change", () => { fingerOK = $("finger").checked; setTool(tool); });
$("width").addEventListener("input", () => { width = +$("width").value; widths[tool in widths ? tool : "pen"] = width; if (editing) { editing.size = width; editing.el.style.fontSize = `calc(${width / editing.L.base.width * 100}cqw)`; } });
document.querySelectorAll("[data-color]").forEach(b => { b.style.background = b.dataset.color; b.addEventListener("pointerdown", e => { if (editing) e.preventDefault(); });   // keep the cursor in the text box
  b.addEventListener("click", () => { color = b.dataset.color; if (editing) { editing.color = color; editing.el.style.color = color; } store.set("lw-ink-color", color); document.querySelectorAll("[data-color]").forEach(x => x.classList.toggle("on", x === b)); }); });
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
// The chat is a drawer behind the button at the top right of the viewer: closed until opened (remembered).
function showChat(on) { document.body.classList.toggle("nochat", !on); $("chatbtn").classList.toggle("on", on); $("chatbtn").setAttribute("aria-pressed", String(on)); store.set("lw-reader-drawer", on ? "1" : "0"); }
$("chatbtn").addEventListener("click", () => showChat(document.body.classList.contains("nochat")));
showChat(store.get("lw-reader-drawer", "0") === "1");

// The toolbars take a lot of a phone screen: a button tucks them away (remembered).
function showChrome(on) {
  document.body.classList.toggle("chromeoff", !on); $("chromebtn").setAttribute("aria-expanded", String(on));
  $("chromebtn").title = on ? "Hide the toolbars" : "Show the toolbars"; store.set("lw-reader-chrome", on ? "1" : "0");
}
$("chromebtn").addEventListener("click", () => showChrome(document.body.classList.contains("chromeoff")));
showChrome(store.get("lw-reader-chrome", "1") !== "0");

// Re-fit the page when the viewer's size changes (toolbars hidden, chat toggled, device turned).
let refitTimer = null;
new ResizeObserver(() => {
  clearTimeout(refitTimer);
  refitTimer = setTimeout(() => {
    if (!pdfViewer.pagesCount) return;   // no document yet
    const v = pdfViewer.currentScaleValue;
    if (isNaN(parseFloat(v))) pdfViewer.currentScaleValue = v; else pdfViewer.update();   // "page-width" and friends are re-applied
  }, 120);
}).observe(container);

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

// ---------- back ----------
// The arrow goes back to wherever you came from (the paper's page, a project, a chat answer...). Opened with no history to go back to
// (a new tab, a bookmark, another site), it falls back to the paper's wiki page, which is its href.
$("back").addEventListener("click", ev => {
  let from = false;
  // window.history, spelled out: the undo stack above is a module variable that is also called "history"
  try { from = window.history.length > 1 && !!document.referrer && new URL(document.referrer).origin === location.origin; } catch (e) { /* bad referrer */ }
  if (from) { ev.preventDefault(); window.history.back(); }
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
