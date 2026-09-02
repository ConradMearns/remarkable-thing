# Viability research: bidirectional laptop ⇄ reMarkable 2 document sync

*Verified hands-on 2026-08-31 with `rmscene 0.6.1` and `rmc 0.3.0` (see `.venv/`, `test_out.rm`).*

## Verdict

**Viable.** Every load-bearing technical claim checks out against the real libraries.
The hard problems are product-scoping problems (what round-trips vs. what renders
one-way), not format problems.

## Verified facts

1. **Programmatic authoring of v6 `.rm` files works today.**
   `rmscene.simple_text_document("...")` + `write_blocks()` produces a valid
   `reMarkable .lines file, version=6` from a few lines of Python, and
   `read_tree()` round-trips it exactly.

2. **Native typed text and ink strokes coexist in one authored file.**
   Built a file containing a `RootTextBlock` (native, reflowable, on-device
   editable text) plus a hand-constructed `si.Line` stroke (40 points) appended
   as a `SceneLineItemBlock`; both read back intact. This confirms the
   substitution feature: delete stroke items, insert native text — the tablet
   renders and re-edits it as real text.

3. **Paragraph styles cover a markdown subset natively.**
   `ParagraphStyle`: BASIC, PLAIN, HEADING, BOLD, BULLET, BULLET2, CHECKBOX,
   CHECKBOX_CHECKED. Inline bold/italic exists too (firmware 3.3.2+ inline
   formatting, supported in `rmscene.text`).

4. **Anchoring machinery exists and is exposed.**
   `si.Group` carries `anchor_id`, `anchor_type`, `anchor_threshold`,
   `anchor_origin_x` — ink groups can anchor to text character `CrdtId`s so
   annotations travel when text reflows. This is the native implementation of
   "ink stays with its block."

5. **Everything has a stable CRDT identity.**
   Text characters and scene items are `CrdtSequence` entries with `CrdtId`s.
   The canonical store can key on these directly — diffing and merging anchor to
   stable IDs, not positions.

6. **`rmc` converts both directions.** `markdown → .rm` preserves heading and
   bullet styles through a full `md → rm → md` round trip (its markdown *emitter*
   drops some newlines — cosmetic, fixable, or bypass it and emit ourselves).
   `.rm → svg` rendering works, giving us laptop-side preview for free.

## Architecture (as discussed)

- **Canonical store:** SQLite. Document = ordered list of **vertical blocks**
  (the infinite scroll is 1-D, so a block is a y-band). Each block: source
  (Typst/markdown text, or ink), rendered height, anchor offset.
- **Provenance via layers:** all generated content in one group/layer, human ink
  in another. Generated strokes are never diffed — they're regenerated from
  source. Everything in the ink layer is user input, bucketed by y-band overlap.
- **Three block render classes:**
  1. *Markdown-subset text* → native v6 text (cheap, reflows, editable
     on-device, reads back trivially).
  2. *Rich Typst content* (math, tables, diagrams) → SVG → flattened polyline
     strokes (drawj2d-style), opaque and regenerate-only. Watch the stroke
     budget — dense sections can produce thousands of strokes and slow xochitl.
  3. *Ink* → stays ink, coordinates stored relative to owning block's anchor so
     reflow is a pure offset.
- **Sync transport:** rsync/SSH to `~/.local/share/remarkable/xochitl/` +
  per-document metadata/content JSON, then restart/HUP xochitl. Solved problem
  (rmapi, rmfakecloud, RCU as prior art).
- **Handwriting → text:** MyScript (built-in) is cloud/proprietary and not
  invokable. Plan: render the ink region (or serialize strokes) → vision LLM on
  the laptop → review-and-approve substitution.

## Known risks / open questions

- **Full Typst fidelity will not round-trip** — by design. The scope decision
  that makes or breaks this: markdown-ish subset round-trips losslessly; richer
  constructs render one-way.
- **Ink straddling a block boundary** — mitigate with a visible gutter/separator
  rule between blocks on the tablet.
- `rmc`'s text-box handling has known bugs; we may write our own emitter on
  rmscene primitives (they're small and clean).
- Firmware drift: v6 is current, but reMarkable can change the format;
  rmscene/Kaitai spec/remarkable-lines (Rust) are the reference implementations
  to track.

## Suggested v1 milestones

1. Sync harness: rsync a single notebook off the device, parse with rmscene,
   render to SVG. (Read path end-to-end.)
2. Emit path: markdown → native-text `.rm`, push to device, confirm on-device
   editability.
3. Block manifest in SQLite + generated/ink layer split.
4. Y-band ink extraction + reflow offsetting.
5. Vision-LLM transcription + approve/substitute flow.

## Device test log — 2026-09-01 (firmware 3.11.2.5)

Setup: USB at 10.11.99.1, creds in `.env`, ssh alias `rm`, full backup in `backup/xochitl/`
(2.8G). Helpers: `rmsync/emit.py` (markdown subset → native-text `.rm`), `rmsync/push.sh`,
`rmsync/pull.sh`. Test doc "SYNC TEST 01" (uuid in `stage/CURRENT`), pulled states in `pulled/N/`.

Results, all confirmed on-device:

1. **Read path works** on real notebooks — every v6 page of "Quick sheets" parses and renders.
   **But 204 of 301 pages are v5** (pre-3.0 firmware); rmscene rejects them. Old notebooks need
   a v5 reader or a re-save on device before they can sync.
2. **Laptop-authored native text renders and is editable.** rmc's markdown importer drops
   paragraph styles, so we emit directly on rmscene primitives; HEADING, BULLET, BULLET2,
   CHECKBOX, CHECKBOX_CHECKED, BOLD all survive the trip.
3. **xochitl anchors ink for us.** Handwriting beside "bullet two" was saved by the device as an
   `si.Group` with `anchor_id` = the CrdtId of that paragraph's start character,
   `anchor_type=2`, `anchor_threshold≈35.7`, `anchor_origin_x` set. Ink below all text anchors to
   the sentinel `CrdtId(0, 0xffffffffffff)`. No anchored groups existed anywhere on the device
   before this test, so this was the first real observation.
4. **Reflow follows anchors.** Inserting three paragraphs *above* the anchored one as a CRDT
   insert (new item with `left_id`/`right_id` pointing at existing chars, existing ids untouched)
   moved "bullet two" down and the ink moved with it; bottom scribbles stayed at the bottom.
5. The device writes its edits under a second author id; our author-1 items are left as-is.

**Design consequence:** the text block must never be regenerated. Laptop-side edits are applied
as CRDT inserts/deletes against the existing sequence so character ids stay stable. Provenance
still works: ink lives in device-authored groups, text in ours.

Not yet tested: on-device typed edits flowing back (the pulled text was unchanged, so the user
didn't type), deletions from the laptop side, rich Typst → strokes budget, ink straddling
paragraphs. `rmc`'s SVG renderer ignores anchors, so laptop previews of anchored ink are wrong.

## Milestone 3+4 — 2026-09-01 (late): sync CLI, manifest, Typst→ink

Built `rmsync/` (see `rmsync/DESIGN.md`): `mdmodel.py` (md ⇄ blocks), `textcrdt.py` (+ `test_textcrdt.py`,
lossless, id-stable edits), `typst_ink.py` (Typst → SVG → RDP-simplified polylines, text size 27.33pt gives
the device's 35.748 line pitch), `manifest.py` (SQLite `sync.db`), `sync.py` (`init|push|pull`).
Verified on device with "SYNC TEST 02" (`test02.md`): init → pull round-trips; edit text + equation + add
bullet → push → pull round-trips with existing char ids reused; remove a Typst block → tombstoned group.
Generated ink = a `gen:<hash>` group anchored to the first of ceil(h/35.748) reserved empty paragraphs.
Review pass found and fixed: manifest written only after a successful push (single transaction, self-healing
reconcile on load), broken reserved runs re-rendered once, dangling anchors re-anchored, conflict guard via
stored `synced_md` (pull refuses if local md changed; `--force`). Known: glyphs are hollow outlines; a doc
can't start with a Typst block; device-typed text that looks like markdown is re-parsed; LWW timestamp
comparison across authors unverified on device.

**Ink anchoring, refined (2026-09-02, SYNC TEST 02 pull):** xochitl uses two anchor modes. `anchor_type=2`
(threshold 35.748) anchors to a paragraph's "\n" id — used for ink beside/between lines. `anchor_type=1`
(threshold 0) anchors to a *single character id inside the paragraph* — used when writing over text;
`anchor_origin_x` then equals that character's x. So pull must map any anchor char to its containing
paragraph (fixed in `sync.py`). Ink beside "bullet two" was attached to bullet three's "\n" (nearest line
to the pen-down point), so paragraph attribution is approximate by design.

**Full loop verified 2026-09-02:** laptop insert above a Typst block while device ink existed → all 10
anchors intact on device (user confirmed ink-over-text stayed on its words, equation moved below the new
line). Then device-typed text + Enter inside a paragraph → pull brought the text and the new paragraph into
the md; the generated group's reserved line survived. Bidirectional sync of text, Typst blocks and ink
provenance works end to end.

**2026-09-02 later:** Typst glyphs now hatch-filled (`typst_ink.STYLES`, default `hatch`, per-block override
via `// rmsync: style=<name>` comment; user judged hatch best on "SYNC TEST 03", ~10× the stroke count of
outlines). Documents may now start with a Typst block or be empty: sync inserts an empty first paragraph
(the first paragraph has no "\n" id to anchor to). Verified on device with "SYNC TEST 04".

## Multi-page documents, folders, long pages — 2026-09-02

- Markdown `---` on its own line = page break. `sync.py` now has a `Doc` layer (content JSON `cPages`,
  fractional page idx `ba, bb, …`) over the per-page `Page`; pages map by position; extra device pages
  with ink/text are never deleted (they reappear in the pulled md after a `---`). Manifest has a `pages` table.
- `init --folder "A/B"` resolves/creates `CollectionType` folders by visibleName path (folder = `.metadata`
  + `.content` of `{}`), staged and pushed together with the document.
- "SYNC TEST 05": 3 pages, page 1 = 40 sections (~260 paragraphs) + 5 Typst blocks, in `rmsync tests/nested`.
  init → pull matches (only `##` → `#`: the device has one heading style). Removing page 3 and adding a
  new page → push → pull matches; the removed page's `.rm` is gone on the device.
- **Reboot hazard:** `xochitl.service` has `StartLimitBurst=4` per 10 min and `OnFailure=remarkable-fail.service`,
  which *reboots the tablet* ("goodroot has been set by xochitl. Rebooting"). Three restarts in ~10 s did it.
  `push.sh` now restarts once per push and runs `systemctl reset-failed xochitl` first (clears the counter).

**LWW semantics settled (2026-09-02, user-verified on device):** a laptop style entry with timestamp
(1, 9000) overrode the device's (2, 442) → xochitl compares Lww timestamps by sequence number (part2),
not author-first. Consequence: our allocator now seeds above the max part2 of *all* authors on the page
(`max_id(blocks)`), so laptop restyles always win when they are the newer edit. Also confirmed: a 260-paragraph
page with 4 hatch-rendered equations scrolls smoothly on the rM2.

## Images — 2026-09-02

- **Ink → markdown images.** `pull` exports every paragraph's anchored ink groups to `<md>.ink/p<page>-<start id>.svg`
  (`rmsync/ink_svg.py`, pen widths/colours/opacity from rmc's pen model, cropped viewBox) and emits
  `![ink: N strokes](<md>.ink/…)`. Groups anchored to characters inside a wrapped paragraph are placed at
  the paragraph's first line (their true line offset needs text layout we don't have), so multi-line
  overwrites may be vertically compressed. The `.ink/` dir is regenerated on every pull.
- **Laptop images → tablet.** `![alt](path)` lines are generated blocks whose manifest source is the markdown
  line itself (`// rmsync: md ![alt](path)`), so pull re-emits the line verbatim. SVG files are flattened
  to strokes (scaled to fit 936 units); other formats become a labelled placeholder box on the tablet
  (Typst's SVG output embeds rasters as base64 <image>, which cannot become strokes).
- Verified on device: TEST 02 pull → two ink SVGs; TEST 04 push with an SVG drawing + PNG placeholder → pull matches.
