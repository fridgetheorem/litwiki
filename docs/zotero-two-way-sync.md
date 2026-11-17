# Spec: two-way Zotero sync (annotations first)

Status: **draft for review, nothing here is built.** Today litwiki is one-way: it reads Zotero with a read-only key and
already stores every annotation (546 in the owner's library, none yet shown). This document specifies how litwiki-made
work could flow back to Zotero, and what must be proven before anyone builds it.

## 1. Goals and non-goals

Goals
1. Annotations made in litwiki's reader (highlights, underlines, notes, pen strokes) can be sent to Zotero and appear
   in Zotero's own PDF reader, attached to the right PDF.
2. Papers added to litwiki (uploaded PDFs, DOI/arXiv adds) can be created in Zotero as items, with the PDF attached.
3. Nothing is ever sent automatically until the owner turns it on; nothing in Zotero is deleted unless litwiki created it.

Non-goals (for now): editing Zotero-origin annotations from litwiki; syncing collections or tags back; group libraries;
Zotero notes (standalone) and "image" (area) annotations.

## 2. What is verified and what is not

Verified against Zotero's documentation (write_requests, file_upload and basics pages of the Web API v3):
- Items are created with `POST /users/<id>/items`, a JSON array of **at most 50** objects (more returns 413).
- Every write needs `Zotero-Write-Token` (client-generated 32-character string, cached 12 h; replaying it returns 412, which makes
  retries idempotent) **or** `If-Unmodified-Since-Version` (library version, or a per-object `version`).
- The response is 200 with `successful`, `unchanged` and `failed` maps keyed by array index; a failure carries `key`, `code`, `message`.
  `Last-Modified-Version` is the new library version.
- Updates are `PATCH /items/<key>` with `If-Unmodified-Since-Version: <item version>`, changed fields only, 204 on success.
  Deletes are `DELETE /items/<key>` with the same header (428 without it).
- 409 = library locked (retry later); 412 = version changed since you read it (refetch, then decide); 429 may carry `Retry-After`;
  any response may carry `Backoff: <seconds>`. At most 4 concurrent requests; prefer batches to many single writes.
- File upload is three steps: authorise (`POST .../file` with `md5`, `filename`, `filesize`, `mtime` in **milliseconds**, plus
  `If-None-Match: *` for a new file), upload prefix+bytes+suffix to the returned URL (201), then register with `upload=<uploadKey>`
  (204). `{"exists":1}` means Zotero already has that file. 413 = storage quota exceeded.

Observed in the owner's real data (`state.db`): annotation items have `parentItem` = the **PDF attachment** key (not the paper),
and fields `annotationType` (highlight 250, ink 287, note 8, underline 1), `annotationColor`, `annotationComment`,
`annotationPageLabel`, `annotationSortIndex` (e.g. `00004|003784|00569`), `annotationPosition` (a JSON string) and, for
highlight/underline, `annotationText`. Position shapes: highlight/underline/note `{"pageIndex":4,"rects":[[x1,y1,x2,y2],...]}`;
ink `{"pageIndex":12,"width":1.116,"paths":[[x,y,x,y,...],...]}`. Coordinates are PDF points, origin bottom-left, `pageIndex` 0-based.

**NOT verified: whether the Web API accepts creating `annotation` items.** The documentation does not cover annotation fields and
the public schema lists none for the type. This is the single biggest risk, so it is a gate (section 7, Spike 0). If the API refuses,
annotations stay one-way and goal 1 is dropped; goal 2 is unaffected.

## 3. Prerequisites and safety

- A **second, write-scoped API key** the owner creates in Zotero (Settings > Feeds/API). Read-only remains the default key; the
  write key is `ZOTERO_WRITE_API_KEY` in `~/.env`, used only by the write-back module. `config.yaml: zotero.write_back: false`
  by default. On startup, `GET /keys/<key>` must show library write access, else write-back disables itself loudly.
- **Test first on a throwaway library** (a new Zotero account or a private group with a copy of one paper), never the real library.
- **Per-item opt-in at first**: a "Send to Zotero" button on a paper or annotation set, with a preview of what will be created. An
  "auto-send" mode is a later, separate switch.
- Everything litwiki writes is tagged `litwiki` (Zotero tag on the item) and recorded in a local table; litwiki only ever updates or
  deletes items it created itself.
- An `audit` table logs every write (time, endpoint, item key, request hash, outcome). A kill switch (`write_back: false`) takes
  effect immediately without a restart.

## 4. Data model

New table `zotero_link` in `state.db`:

| column | meaning |
|---|---|
| `local_kind`, `local_id` | `annotation` / `paper`, and litwiki's own id |
| `zotero_key`, `zotero_version` | key and item version returned by the last successful write |
| `local_hash` | hash of the exported Zotero payload at last push (detects local edits) |
| `state` | `pending`, `synced`, `conflict`, `failed`, `local_only` |
| `last_error`, `updated` | for the UI |

`local_only` is how un-representable annotations are kept honest (section 5). Pull (the existing hourly sync) is unchanged, except
that items tagged `litwiki` are matched to their `zotero_link` row instead of being treated as new Zotero-origin content, which
prevents echo loops.

## 5. Annotation mapping (litwiki reader to Zotero)

The reader (planned phase 3) stores annotations in the same coordinate system Zotero uses (PDF points, bottom-left origin,
0-based page index), so mapping is a re-shaping, not a transform.

| litwiki | Zotero `annotationType` | Round-trips? |
|---|---|---|
| text highlight | `highlight` (`annotationText` = selected text, `rects`) | yes |
| underline | `underline` | yes |
| sticky note | `note` (`rects` of the icon, `annotationComment`) | yes |
| pen stroke | `ink` (`paths`, `width`) | yes, if the point format matches (verify in Spike 0) |
| free-form shape, arrow, text box, eraser marks | none (Zotero 7 may support a text type; check) | **no: `local_only`** |

Fields to derive: `annotationPageLabel` (the PDF's own page label, falling back to `pageIndex+1`), `annotationColor`
(Zotero only offers a fixed palette; map to the nearest), and **`annotationSortIndex`** (`pageIndex` 5 digits | character offset
6 digits | vertical position 5 digits; derive the formula empirically by comparing against annotations Zotero itself created, and
verify whether Zotero tolerates an approximate value).

The attachment: an annotation can only be written to a PDF attachment that already exists in Zotero **and is the same file**
litwiki rendered. litwiki always downloads the annotated attachment from Zotero for the reader, so the keys match; for an uploaded
paper the attachment must be created first (section 6).

## 6. Papers added to litwiki

Create a Zotero item from resolved metadata (title, authors, DOI, URL, year, abstract), tagged `litwiki`, in a configurable
collection ("litwiki uploads"), then upload the PDF as a child attachment with the three-step flow above. Before creating, search
Zotero by DOI and title and **link instead of duplicating** when a match exists (existing `dedupe.attach_zotero_item` already handles the
pull side). Store the returned key as the page's `zotero_key`. Failures keep the page and retry later; the page never depends on Zotero.

## 7. Plan

| step | what | gate |
|---|---|---|
| **Spike 0** | In a throwaway library: POST one `highlight`, one `ink`, one `note` annotation onto a test PDF attachment. Open the library in the Zotero desktop app and confirm they render in the right place, with the right colour and sort order. Record exact accepted payloads and errors. | Go/no-go for annotation write-back |
| 1 | `zotero_link`, write client (batching, write tokens, version headers, Backoff/Retry-After, 4-concurrency cap), audit log, key check | unit tests against a recorded fake server |
| 2 | Export + push for highlight/underline/note; "Send to Zotero" UI with preview | Spike 0 passed |
| 3 | Ink export; update (PATCH) and delete for litwiki-made annotations | |
| 4 | Papers to Zotero with PDF attachment | independent of annotations |
| 5 | Optional auto-send | owner decision after a trial period |

Rough effort after the spike: steps 1-3 about 3-4 days, step 4 about 2 days.

## 8. Conflicts and failure handling

- **Ownership rule (recommended):** Zotero-origin annotations are read-only in litwiki; litwiki-origin ones (tagged `litwiki`) are
  edited only in litwiki. This avoids most conflicts by design.
- **412 on an update:** someone edited the item in Zotero since the last push. Default: keep Zotero's version, mark the link
  `conflict`, show both, let the owner choose. Never overwrite silently.
- **Deleted in Zotero:** the next pull reports it; mark the link `local_only` rather than recreating it.
- **429 / Backoff / 409:** sleep as instructed, retry the same batch with the same write token. **5xx:** exponential backoff, then `failed`.
- **Partial batch failure:** apply the per-index `successful`/`failed` result; only failed rows retry.
- **Quota (413) on upload:** surface "Zotero storage full", skip the file, keep the metadata item.

## 9. Test plan

Recorded-response fake of the Zotero API for CI (idempotent replay with the same write token, 412, 429 + Retry-After, partial
failure, `exists:1` upload); a manual end-to-end checklist on the throwaway library (create, edit, delete, view in desktop app,
re-sync back down without duplicates); and a "no echo" test that a pushed annotation is not re-imported as new on the next pull.

## 10. Decisions needed from the owner

1. Is a throwaway Zotero library/group available for Spike 0? (Required.)
2. Per-item send first, then maybe auto: agreed?
3. If Zotero rejects API-created annotations: accept one-way annotations and do only goal 2, or export a standalone annotated PDF instead?
4. Which collection should litwiki-created items go into?

## Sources
Zotero Web API v3: write requests, file upload and basics pages (zotero.org/support/dev/web_api/v3/). Field shapes observed in this
install's `state/state.db`.
