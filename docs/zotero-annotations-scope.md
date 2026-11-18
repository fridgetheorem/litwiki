# Scope: Zotero annotations in the reader (phase 1)

Status: **scoped, nothing built yet.** Read-only, one way (Zotero to litwiki). The PDF reader as it is today stays untouched
unless a paper actually has Zotero annotations.

## What the data says (checked against the live library, 2026-11-18)

| Fact | Number |
|---|---|
| Annotations already synced (matches Zotero's own count, so the sync is complete) | 546 on 45 attachments |
| Types | 287 ink, 250 highlight, 8 note, 1 underline (no image or free-text yet) |
| Annotations on a PDF that is on disk and byte-identical to the annotated attachment (md5 match against the Zotero metadata) | **529 of 546, on 43 papers, 40 of 40 files matched** |
| Annotations on a paper with no local PDF | 6 (one paper whose Zotero file never downloaded) |
| Annotations on a duplicate Zotero item that was merged into another page (key is in `also_zotero_keys`) | 11 (shown once matching also uses that list) |

Two consequences:

1. **On-demand PDF download is not needed for this library, so I recommend dropping it from this phase.** Earlier I said most
   annotated PDFs were not stored under the annotation's key. That was wrong: the vault names a PDF after the *paper's*
   key, and it holds the annotated attachment's file. The one paper without a local PDF has an attachment Zotero's cloud
   never served; downloading it again would most likely fail the same way. If it ever matters, "fetch from Zotero" is a small
   follow-up (`Zotero.download_file` already exists).
2. **The md5 gives a cheap safety check.** Annotation coordinates only mean something on the exact file they were made on.
   The reader will show Zotero annotations only for an attachment whose md5 equals the local file's, and say so when some are
   hidden ("3 annotations belong to a different copy of this PDF").

**Coordinates are verified.** Zotero stores positions in PDF user space (points, origin bottom-left, 0-based page), the same
space as the reader's own strokes. I mapped all 251 highlight/underline rectangles onto the real PDFs and read the words under
them: 164 match Zotero's saved text exactly and 232 cover at least 90% of it. The rest differ because Zotero's own saved text
has dropped ligature characters ("sequ tial"), not because of position. So the geometry is right; what is not verified here is
how it looks in a browser (there is none on this host), the same caveat as the reader.

## What ships

**Stage 1: backend (no visible change).** `litwiki/zotero_annotations.py`: for a paper, find its Zotero keys (`zotero_key` and
`also_zotero_keys`), collect annotations of its PDF attachments (or of the item itself for a standalone PDF), keep those whose
attachment md5 equals the local file's md5 (cached by size and mtime), and return normalised records: id, type, colour,
comment, text, page index, page label, rects or ink paths and width, tags, dates. `GET /app/paper/<stem>/zotero` returns them
plus how many were hidden and when Zotero was last synced. Tests use the real library.

**Stage 2: the layer in the reader.**
- Highlights: translucent colour over each rectangle (multiply blend). Underlines: a line under each rectangle. Ink: the stroke
  in Zotero's colour and width. Notes: a small note icon. Comments: a small marker on highlights that have one, with the
  comment on hover or tap. Image and free-text annotations: an outline or a text label (none in this library yet).
- A new toggle in the top row next to "My notes", only present when the paper has Zotero annotations, with the count in its
  tooltip, remembered per paper. Zotero and your own layer are separate toggles, so you can show either, both or neither.
- Drawn under your own strokes and above the page; it never captures input except the small comment markers, so text selection
  and drawing behave exactly as now.
- Same coordinate transform as your own strokes, so rotated and cropped pages are handled the same way.

**Stage 3: annotation list in the drawer.** The drawer gets two tabs, Chat and Annotations. The list groups by page, shows the
highlighted text, your Zotero comment and a colour dot, and clicking an entry jumps to it and flashes it. Shows both the
page label Zotero uses and the PDF page ("p. 644 (PDF page 2)") because they differ.

**Stage 4: download.** The download menu gains "with Zotero annotations" and "with both", written as standard PDF highlight,
underline, ink and note annotations through PyMuPDF (same code path as the export of your own strokes, tested on rotated and
cropped pages).

**Stage 5: paper wiki page.** A "Highlights (from Zotero)" section generated at site-build time, like the citations section:
highlighted text, comment, and a link to open the reader at that spot. Nothing is written into the vault.

Each stage is its own branch, merged and deployed separately, so you can try the layer before the rest exists.

## Protecting the reader you like

- New code lives in new files (`zotero_annotations.py`, `static/zlayer.mjs`). `reader.mjs` gets a few lines that load the layer
  inside a `try/catch`: if the endpoint or the layer fails, the reader behaves exactly as today.
- A paper without usable Zotero annotations gets no new controls and no extra requests beyond one small JSON call.
- Your own annotation code, the toolbar, the drawer and the touch handling are not edited.
- `?zotero=0` on the reader URL switches the layer off, for debugging.
- Rollback is reverting a merge commit.

## Not in this phase

Writing anything back to Zotero (see `docs/zotero-two-way-sync.md`, spike first), editing Zotero annotations in litwiki,
downloading PDFs from Zotero, using your highlights as context for the chat (a natural idea for later), importing Zotero
annotations into your own layer.

## Decisions for you

1. **Default visibility.** Recommended: the Zotero layer is on by default for papers that have it, since you made those
   annotations to be seen.
2. **Download menu.** Recommended: four entries (original, with mine, with Zotero's, with both).
3. **Wiki highlights section (stage 5).** In or out?
4. **Freshness.** Annotations arrive with the hourly Zotero sync. Do you want a "sync now" control in the reader, or is
   "as of last sync, time shown in the toggle's tooltip" enough?
