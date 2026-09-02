# rmsync — design brief (state as of 2026-09-01 evening)

Goal: bidirectional sync laptop markdown(+Typst) ⇄ rooted reMarkable 2 (fw 3.11.2.5), v6 `.rm` via rmscene 0.6.1.
Read RESEARCH.md first (verified device facts). Python venv: `.venv/bin/python`. Never use system pip.

## Device facts (all verified on the real tablet today)
- Device at `ssh rm` (alias, key auth). Store: `~/.local/share/remarkable/xochitl/`. Full backup in `backup/xochitl/` (do not touch).
- Push = rsync `stage/` into the store, then `systemctl restart xochitl` (see `rmsync/push.sh`). Pull = `rmsync/pull.sh` → `pulled/N/`.
- Test doc uuid + page uuid in `stage/CURRENT`. Latest pulled state: `pulled/3/`.
- Text: one `RootTextBlock` per page; `si.Text.items` is a CrdtSequence of string chunks; each chunk's item_id is its first char, later chars have implicit sequential ids. `styles` maps paragraph-start char id → LwwValue(ParagraphStyle). Paragraph start id = id of the "\n" char that begins that paragraph; the first paragraph's key is END_MARKER CrdtId(0,0).
- Deletions are tombstones: item with `deleted_length=N`, `value=""` (text) or `SceneTombstoneItemBlock` with value None (scene items). Ids of deleted chars must stay in the sequence because neighbours reference them via left_id/right_id.
- Insert semantics (device-observed): new item left_id = id of the char before, right_id = id of the char after (both "original" neighbours). E.g. typed 'E' → item(2,443) left (1,140) right (1,142).
- The device writes with its own author id (2 on our test doc; we are author 1). Our items are never rewritten by the device.
- **xochitl auto-anchors handwritten ink**: each pen-down group becomes `si.Group(anchor_id=<paragraph start char id>, anchor_type=2, anchor_threshold=35.748, anchor_origin_x=<float>)`. Ink below all text anchors to `CrdtId(0, 0xffffffffffff)`. Stroke coordinates inside an anchored group are LOCAL: screen_x = anchor_origin_x + x; y is relative to the anchored paragraph's line (observed y range for a scribble on that line ≈ -13..67). Text block: pos_x=-468, pos_y=234, width=936. Screen x ∈ [-702,702] (1404 px wide), y grows downward.
- 35.748 (anchor_threshold) is presumably the line height for PLAIN at textScale 1. Unverified for HEADING.
- Reflow verified: inserting paragraphs above an anchored paragraph as a CRDT insert moved the ink with the paragraph.
- A device stroke: `si.Line(color=PenColor.BLACK, tool=Pen.BALLPOINT_2, thickness_scale=2.0, starting_length=0.0, points=[Point(x,y,speed=0,direction=0,width=12,pressure=0)...])`; each stroke is a `SceneLineItemBlock(parent_id=<group node id>, item=CrdtSequenceItem(item_id, left_id, right_id, deleted_length=0, value=Line))`. Groups: `TreeNodeBlock(si.Group(node_id, label, anchor_*))` + `SceneTreeBlock(tree_id=node_id, node_id=CrdtId(0,0), is_update=True, parent_id=<layer node id>)` + `SceneGroupItemBlock(parent_id=<layer node id>, item=CrdtSequenceItem(item_id, left, right, 0, value=node_id))`. Layer 1 node id is CrdtId(0,11); its group item is CrdtId(0,13). See `rmscene.scene_stream.simple_text_document` and `rmc -t blocks <file>` for block dumps.
- rmc's SVG renderer ignores anchors (previews of anchored ink are mispositioned). rmc's markdown importer drops styles; we have our own emitter.

## Modules (rmsync/)
- `emit.py` — markdown subset → fresh native-text page (done, works on device).
- `textcrdt.py` — `TextState` edit layer: expand chars, delete/insert_after/replace_range/replace_para/set_style, coalesce back to items. Lossless round trip verified. `test_textcrdt.py` (run directly) exercises every edit kind against pulled/3 and a fresh emit page: paragraphs, id stability, fresh ids, left/right chain. Edits NOT yet exercised on device.
- `push.sh`, `pull.sh`.
- `sync.py` — CLI `init|push|pull` (see module docstring). Verified end to end on device 2026-09-01: doc "SYNC TEST 02" (uuid 3ab0dfdd-…), init + two push/pull cycles (text edit, new bullet, changed equation, removed table); device renders the generated groups at their anchors and leaves our blocks untouched. Manifest in `sync.db`.

## Planned (this round)
- `typst_ink.py`: Typst source → `typst compile --format svg` (page width 936pt, height auto, margin 0, text size tuned so a line ≈ 35.7 units) → flatten SVG (svgelements is installed; typst emits glyphs as <use>/<symbol> paths) → list of `si.Line` in group-local coords (x 0..936, y from 0 = top) + total height. Tool FINELINER_2, thin width. Keep stroke count low (simplify polylines).
- `manifest.py`: SQLite `sync.db`: documents(uuid, name, md_path), pages(page_uuid, doc_uuid, idx), authors(page_uuid, author, next_id), gen_blocks(page_uuid, group_node_id, source_hash, source, anchor_start_id, reserved_start_ids json, n_lines), plus whatever else is needed. Markdown model: doc = sequence of blocks: text paragraphs (styles from emit.parse_md) and ```typst fenced blocks.
- `sync.py` CLI: `init <md> "<name>"` (new doc, fresh uuid, push), `push <md>` (pull current device file, paragraph-level diff with typst blocks as opaque tokens — matched by source hash — char-level diff inside changed text runs via TextState, reserve ceil(h/35.748) empty paragraphs per typst block and anchor a generated group to the first reserved "\n" id with anchor_origin_x=-468, tombstone groups of removed/changed typst blocks, update PageInfoBlock counts, bump metadata version + lastModified, push), `pull` (device → regenerate the md: paragraph styles → md syntax, typst blocks re-emitted from manifest source, device ink groups noted as `<!-- ink: N strokes -->` comments after the paragraph they anchor to).
