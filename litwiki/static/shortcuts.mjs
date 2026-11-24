// Reader keyboard shortcuts: the one place to change them. Edit a key here, redeploy (`litwiki restart web`) and
// reload the reader; the toolbar tooltips are generated from this table, so they never go out of date.
// Tool keys are single letters, pressed on their own (no Ctrl/Alt/⌘). Undo/redo keys are pressed with Ctrl (⌘ on a Mac).
export const TOOL_KEYS = {
  hand: "a",
  pen: "z",
  highlighter: "q",
  text: "w",
  eraser: "e",
};
export const UNDO_KEY = "z";
export const REDO_KEYS = ["y"];   // Ctrl+Shift+Z always redoes as well

// What a key press means: {tool: name} | {undo: true} | {redo: true} | null. Pure, so it can be tested without a browser.
export function actionFor(ev) {
  const k = (ev.key || "").toLowerCase(), mod = ev.ctrlKey || ev.metaKey;
  if (mod && !ev.altKey) {
    if (k === UNDO_KEY) return ev.shiftKey ? { redo: true } : { undo: true };
    if (REDO_KEYS.includes(k) && !ev.shiftKey) return { redo: true };
    return null;
  }
  if (ev.altKey || ev.shiftKey || ev.repeat) return null;
  for (const [tool, key] of Object.entries(TOOL_KEYS)) if (k === key) return { tool };
  return null;
}

export const label = k => (k.length === 1 ? k.toUpperCase() : k);
