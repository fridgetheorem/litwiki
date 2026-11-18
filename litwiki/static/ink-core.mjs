// Pure logic for the reader's annotation layer (no DOM), so it can be unit-tested under Node.
// Coordinates are PDF user space (points, origin bottom-left), the same as Zotero's ink annotations.

export function newId() {
  const b = new Uint8Array(8);
  (globalThis.crypto || {}).getRandomValues ? globalThis.crypto.getRandomValues(b) : b.forEach((_, i) => (b[i] = Math.random() * 256));
  return [...b].map(x => x.toString(16).padStart(2, "0")).join("");
}

// Smallest distance from point (x,y) to the polyline `flat` = [x1,y1,x2,y2,...].
export function distToPath(flat, x, y) {
  if (flat.length < 2) return Infinity;
  if (flat.length === 2) return Math.hypot(x - flat[0], y - flat[1]);
  let best = Infinity;
  for (let i = 0; i + 3 < flat.length; i += 2) {
    const ax = flat[i], ay = flat[i + 1], bx = flat[i + 2], by = flat[i + 3];
    const dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
    let t = len2 ? ((x - ax) * dx + (y - ay) * dy) / len2 : 0;
    t = Math.max(0, Math.min(1, t));
    best = Math.min(best, Math.hypot(x - (ax + t * dx), y - (ay + t * dy)));
  }
  return best;
}

// Does an eraser of radius r at (x,y) touch this stroke (its width counts)?
export function strokeHit(stroke, x, y, r) {
  const reach = r + stroke.width / 2;
  return stroke.paths.some(p => distToPath(p, x, y) <= reach);
}

// Outline polygon [[x,y],...] -> SVG path data with smoothed quadratic curves (the perfect-freehand recipe).
export function outlineToPath(pts) {
  const n = pts.length;
  if (n < 4) return "";
  const f = v => v.toFixed(2);
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  let d = `M${f(pts[0][0])},${f(pts[0][1])} Q`;
  for (let i = 0; i < n; i++) {
    const m = mid(pts[i], pts[(i + 1) % n]);
    d += `${f(pts[i][0])},${f(pts[i][1])} ${f(m[0])},${f(m[1])} `;
  }
  return d + "Z";
}

// Is the keyboard focus somewhere the user is typing text? (Ctrl+Z must undo the drawing everywhere else, including
// after dragging the width slider or ticking a checkbox, which also take focus.)
export function isTyping(el) {
  if (!el || !el.tagName) return false;
  const t = el.tagName.toLowerCase();
  if (t === "textarea" || t === "select" || el.isContentEditable) return true;
  if (t !== "input") return false;
  return !["range", "checkbox", "radio", "button", "submit", "reset", "color", "file", "image"].includes(String(el.type || "text").toLowerCase());
}

export function firstWords(text, n) {
  return String(text || "").split(/\s+/).filter(Boolean).slice(0, n).join(" ");
}

// Link that opens a paper in the reader at a page, optionally searching for (the start of) a quote.
export function readerUrl(stem, page, quote) {
  let u = `/app/paper/${encodeURIComponent(stem)}/read?page=${Math.max(1, page | 0)}`;
  const q = firstWords(quote, 14);
  return q ? `${u}&find=${encodeURIComponent(q)}` : u;
}

// Candidate searches for finding a quote on a page: the whole start first, then shorter, since PDF text
// extraction can differ from the model's copy (line breaks, ligatures).
export function findAttempts(quote) {
  const out = [];
  for (const n of [14, 7, 4]) {
    const q = firstWords(quote, n);
    if (q && !out.includes(q)) out.push(q);
  }
  return out;
}

// Undo/redo over operations {type:"add"|"del", items:[stroke...]}. undo() returns the operation to apply to undo it.
export class History {
  constructor(limit = 200) { this.undoStack = []; this.redoStack = []; this.limit = limit; }
  push(op) { this.undoStack.push(op); if (this.undoStack.length > this.limit) this.undoStack.shift(); this.redoStack = []; }
  undo() {
    const op = this.undoStack.pop();
    if (!op) return null;
    this.redoStack.push(op);
    return { type: op.type === "add" ? "del" : "add", items: op.items };
  }
  redo() {
    const op = this.redoStack.pop();
    if (!op) return null;
    this.undoStack.push(op);
    return { type: op.type, items: op.items };
  }
  get canUndo() { return this.undoStack.length > 0; }
  get canRedo() { return this.redoStack.length > 0; }
}

// Changes not yet sent to the server. Strokes added and removed before a save cancel out; a stroke that
// exists on the server and is removed is "delete"; one brought back by undo is "restore".
export class Outbox {
  constructor() { this.add = new Map(); this.del = new Set(); this.restore = new Set(); this.known = new Set(); }
  markKnown(ids) { ids.forEach(i => this.known.add(i)); }   // ids the server already has
  added(s) {
    if (this.del.has(s.id)) { this.del.delete(s.id); return; }          // deleted then re-added: back to what the server has
    if (this.known.has(s.id)) { this.restore.add(s.id); return; }
    this.add.set(s.id, s);
  }
  removed(s) {
    if (this.add.delete(s.id)) return;                                    // never reached the server
    this.restore.delete(s.id);
    this.del.add(s.id);
  }
  get dirty() { return this.add.size + this.del.size + this.restore.size > 0; }
  take() {
    const ops = { add: [...this.add.values()], delete: [...this.del], restore: [...this.restore] };
    this.add = new Map(); this.del = new Set(); this.restore = new Set();
    ops.add.forEach(s => this.known.add(s.id));
    return ops;
  }
  requeue(ops) {                                                          // a failed save: put it back, newer changes win
    for (const s of ops.add) if (!this.del.has(s.id)) this.add.set(s.id, s);
    for (const i of ops.delete) if (!this.add.has(i) && !this.restore.has(i)) this.del.add(i);
    for (const i of ops.restore) if (!this.del.has(i)) this.restore.add(i);
    ops.add.forEach(s => this.known.delete(s.id));
  }
}
