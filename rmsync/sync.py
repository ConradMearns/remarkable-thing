"""rmsync CLI: init/push/pull a markdown(+typst) file to a reMarkable notebook page.

  .venv/bin/python -m rmsync.sync init  <md> "<name>" [--folder "A/B"]
  .venv/bin/python -m rmsync.sync push  <md> [--force]
  .venv/bin/python -m rmsync.sync pull  <md> [--force]

push refuses when the device text changed since the last sync (pull first); pull refuses
when the local md changed since the last sync (push first). --force overrides.
Text lives in the page's RootTextBlock and is edited as a CRDT (ids stay stable).
Each ```typst block reserves ceil(h/35.748) empty paragraphs and is drawn as a
generated ink group anchored to the first reserved "\n" char.
"""
from __future__ import annotations
import difflib
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from rmscene import (read_blocks, write_blocks, AuthorIdsBlock, MigrationInfoBlock, PageInfoBlock,
                     SceneInfo, SceneTreeBlock, RootTextBlock, TreeNodeBlock, SceneGroupItemBlock,
                     SceneLineItemBlock, SceneTombstoneItemBlock, SceneItemBlock,
                     CrdtId, CrdtSequence, CrdtSequenceItem, LwwValue)
from rmscene import scene_items as si

from . import mdmodel, typst_ink
from .manifest import Manifest
from .mdmodel import TextPara, TypstBlock, InkNote
from .textcrdt import TextState, IdAlloc, END

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "stage"
DEVICE_STORE = "rm:~/.local/share/remarkable/xochitl/"
AUTHOR = 1
FIRST_ID = 1000
LAYER = CrdtId(0, 11)
LINE_H = typst_ink.LINE_HEIGHT
SENTINEL_TOP = CrdtId(0, 0xfffffffffffe)
PS = si.ParagraphStyle


# ---------------------------------------------------------------- page I/O
def max_id(blocks, author: int | None = None) -> int:
    """Largest part2 used by `author` (any real author if None) anywhere in the blocks: ids, Lww
    timestamps, expanded chars. xochitl resolves LWW conflicts by part2 alone (verified on device:
    our (1,9000) beat the device's (2,442)), so new timestamps must exceed every author's counter."""
    best = 0
    def mine(cid): return cid.part1 != 0 and (author is None or cid.part1 == author)
    def visit(o):
        nonlocal best
        if isinstance(o, CrdtId):
            if mine(o) and o.part2 < 0xfffffffffff0: best = max(best, o.part2)
        elif isinstance(o, CrdtSequenceItem):
            visit(o.item_id); visit(o.left_id); visit(o.right_id)
            if isinstance(o.value, str) and mine(o.item_id):
                best = max(best, o.item_id.part2 + max(len(o.value), o.deleted_length) - 1)
            else: visit(o.value)
        elif isinstance(o, CrdtSequence): [visit(i) for i in o.sequence_items()]
        elif isinstance(o, LwwValue): visit(o.timestamp); visit(o.value)
        elif isinstance(o, dict): [visit(x) for kv in o.items() for x in kv]
        elif isinstance(o, (list, tuple)): [visit(x) for x in o]
        elif hasattr(o, "__dataclass_fields__"):
            [visit(getattr(o, f)) for f in o.__dataclass_fields__]
    visit(blocks)
    return best


def empty_page_blocks():
    return [AuthorIdsBlock(author_uuids={AUTHOR: uuid4()}),
            MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
            PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=1, text_lines_count=1),
            SceneInfo(current_layer=LwwValue(CrdtId(0, 0), CrdtId(0, 0)), background_visible=LwwValue(CrdtId(0, 0), True),
                      root_document_visible=LwwValue(CrdtId(0, 0), True)),
            SceneTreeBlock(tree_id=LAYER, node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1)),
            RootTextBlock(block_id=CrdtId(0, 0), value=si.Text(items=CrdtSequence([]), styles={},
                                                                 pos_x=-468.0, pos_y=234.0, width=936.0)),
            TreeNodeBlock(si.Group(node_id=CrdtId(0, 1))),
            TreeNodeBlock(si.Group(node_id=LAYER, label=LwwValue(timestamp=CrdtId(0, 12), value="Layer 1"))),
            SceneGroupItemBlock(parent_id=CrdtId(0, 1), item=CrdtSequenceItem(
                item_id=CrdtId(0, 13), left_id=END, right_id=END, deleted_length=0, value=LAYER))]


_ORDER = [(SceneTreeBlock, 1), (RootTextBlock, 2), (TreeNodeBlock, 3), (SceneGroupItemBlock, 4),
          (SceneTombstoneItemBlock, 5), (SceneLineItemBlock, 7), (SceneItemBlock, 6)]   # glyph/text items before lines

def canonical(blocks):
    """Stable-sort blocks into the device's file order (header blocks, trees, text, nodes, items, lines)."""
    def rank(b):
        return next((r for t, r in _ORDER if isinstance(b, t)), 0)
    return sorted(blocks, key=rank)


def gen_label(b) -> bool:
    return isinstance(b, TreeNodeBlock) and b.group.label is not None and str(b.group.label.value).startswith("gen:")


# ---------------------------------------------------------------- tokens
def current_tokens(ts: TextState, gen_blocks: list[dict]):
    """Group the page's paragraphs into diff tokens. gen_blocks must be intact (see Page.intact).
    Returns (tokens, spans): tokens[i] is (style, text) or ('typst', hash);
    spans[i] is (first_para_index, last_para_index_exclusive, gen_block_or_None)."""
    by_reserved = {CrdtId(AUTHOR, r): g for g in gen_blocks for r in g["reserved_ids"]}
    tokens, spans = [], []
    for i, p in enumerate(ts.paragraphs()):
        g = by_reserved.get(p.start)
        if g is not None and spans and spans[-1][2] is g:
            spans[-1] = (spans[-1][0], i + 1, g)
        elif g is not None:
            tokens.append(("typst", g["source_hash"])); spans.append((i, i + 1, g))
        else:
            tokens.append((p.style, p.text)); spans.append((i, i + 1, None))
    return tokens, spans


def target_tokens(blocks):
    return [("typst", b.hash) if isinstance(b, TypstBlock) else (b.style, b.text) for b in blocks]


# ---------------------------------------------------------------- page edits
class Page:
    """One .rm page: blocks + TextState + generated-group bookkeeping."""

    def __init__(self, blocks, man: Manifest, page_uuid: str):
        self.blocks, self.man, self.page_uuid = list(blocks), man, page_uuid
        self.text_block = next(b for b in self.blocks if isinstance(b, RootTextBlock))
        self.ts = TextState(self.text_block.value)
        self.alloc = IdAlloc(AUTHOR, max(man.next_id(page_uuid, AUTHOR), FIRST_ID, max_id(self.blocks) + 1))
        # Reconcile manifest with the file (a failed push can leave them apart): drop rows whose group is
        # not in the file, tombstone gen groups the manifest does not know. Nothing is committed until commit().
        nodes = {b.group.node_id for b in self.blocks if isinstance(b, TreeNodeBlock)}
        self.gen = {g["group_node_part2"]: g for g in man.list_gen_blocks(page_uuid)
                    if CrdtId(AUTHOR, g["group_node_part2"]) in nodes}
        for b in [b for b in self.blocks if gen_label(b)]:
            if b.group.node_id.part2 not in self.gen or b.group.node_id.part1 != AUTHOR:
                self.remove_group(b.group.node_id)

    def commit(self):
        """Write the id counter and gen_blocks rows to the manifest (call after the page reached the device)."""
        self.man.commit_page(self.page_uuid, AUTHOR, self.alloc.next, list(self.gen.values()))

    # --- reserved paragraphs
    def reserved_paras(self, g: dict, paras=None) -> list[int]:
        """Indices of the live paragraphs that start with one of g's reserved ids."""
        res = {CrdtId(AUTHOR, r) for r in g["reserved_ids"]}
        return [i for i, p in enumerate(paras or self.ts.paragraphs()) if p.start in res]

    def intact(self, g: dict, paras=None) -> bool:
        """All reserved paragraphs live, consecutive and empty (nothing typed/deleted on the device)."""
        paras = paras or self.ts.paragraphs()
        idx = self.reserved_paras(g, paras)
        return (len(idx) == len(g["reserved_ids"]) and idx == list(range(idx[0], idx[0] + len(idx)))
                and all(paras[i].text == "" for i in idx))

    # --- layer children
    def layer_items(self):
        return [b for b in self.blocks if isinstance(b, SceneItemBlock) and b.parent_id == LAYER]

    def last_layer_item(self) -> CrdtId:
        seq = CrdtSequence([b.item for b in self.layer_items()])
        keys = seq.keys()
        return keys[-1] if keys else END

    # --- generated groups
    def add_gen_group(self, src: TypstBlock, lines, n_lines: int, reserved: list[CrdtId]):
        node = self.alloc.take()
        def lww(v): return LwwValue(timestamp=self.alloc.take(), value=v)
        group = si.Group(node_id=node, label=lww("gen:" + src.hash), anchor_id=lww(reserved[0]),
                         anchor_type=lww(2), anchor_threshold=lww(LINE_H), anchor_origin_x=lww(-468.0))
        item_id = self.alloc.take()
        new = [TreeNodeBlock(group),
               SceneTreeBlock(tree_id=node, node_id=CrdtId(0, 0), is_update=True, parent_id=LAYER),
               SceneGroupItemBlock(parent_id=LAYER, item=CrdtSequenceItem(
                   item_id=item_id, left_id=self.last_layer_item(), right_id=END, deleted_length=0, value=node))]
        prev = END
        for ln in lines:
            sid = self.alloc.take()
            new.append(SceneLineItemBlock(parent_id=node, item=CrdtSequenceItem(
                item_id=sid, left_id=prev, right_id=END, deleted_length=0, value=ln)))
            prev = sid
        self.blocks += new
        self.gen[node.part2] = dict(group_node_part2=node.part2, source_hash=src.hash, source=src.source,
                                    anchor_start_part1=reserved[0].part1, anchor_start_part2=reserved[0].part2,
                                    reserved_ids=[r.part2 for r in reserved], n_lines=n_lines, group_item_part2=item_id.part2)

    def remove_group(self, node: CrdtId):
        """Drop a group's node/tree/children and tombstone its item in the layer."""
        out = []
        for b in self.blocks:
            if isinstance(b, TreeNodeBlock) and b.group.node_id == node: continue
            if isinstance(b, SceneTreeBlock) and b.tree_id == node: continue
            if isinstance(b, SceneItemBlock) and b.parent_id == node: continue
            if isinstance(b, SceneGroupItemBlock) and b.item.value == node:
                b = SceneTombstoneItemBlock(parent_id=b.parent_id, item=CrdtSequenceItem(
                    item_id=b.item.item_id, left_id=b.item.left_id, right_id=b.item.right_id, deleted_length=1, value=None))
            out.append(b)
        self.blocks = out

    def remove_gen_group(self, g: dict):
        self.remove_group(CrdtId(AUTHOR, g["group_node_part2"]))
        self.gen.pop(g["group_node_part2"], None)

    # --- the diff
    def apply(self, target: list):
        """Edit the page so its content matches `target` (list of TextPara/TypstBlock)."""
        target = [b for b in target if not isinstance(b, InkNote)]
        if not target or isinstance(target[0], TypstBlock):
            # The first paragraph has no "\n" id to anchor to, so a leading Typst block (or an empty
            # document) gets an empty text paragraph in front of it.
            target = [TextPara(PS.PLAIN, "")] + target
        rendered = {}   # hash -> (lines, n_lines)
        for b in target:
            if isinstance(b, TypstBlock) and b.hash not in rendered:
                lines, h, _ = typst_ink.render(b.source)
                rendered[b.hash] = (lines, max(1, math.ceil(h / LINE_H)))

        paras = self.ts.paragraphs()
        for g in list(self.gen.values()):           # device touched a reserved run: re-render the block
            if not self.intact(g, paras): self.remove_gen_group(g)
        cur, spans = current_tokens(self.ts, list(self.gen.values()))
        tgt = target_tokens(target)
        n = len(paras)
        old_end_style = paras[0].style
        sm = difflib.SequenceMatcher(None, cur, tgt, autojunk=False)
        for tag, a0, a1, b0, b1 in reversed(sm.get_opcodes()):
            if tag == "equal": continue
            p0 = spans[a0][0] if a0 < len(cur) else n
            p1 = spans[a1 - 1][1] if a1 > a0 else p0
            tblocks = target[b0:b1]
            for sp in spans[a0:a1]:
                if sp[2] is not None: self.remove_gen_group(sp[2])
            # paragraph bodies of the target run
            bodies = []
            for b in tblocks:
                bodies += [b.text] if isinstance(b, TextPara) else [""] * rendered[b.hash][1]
            run = [i for k in range(p0, p1) for i in paras[k].idx]
            if p0 == 0:
                tstr = "\n".join(bodies)
                if bodies and p1 == 0: tstr += "\n"                    # inserting above the first paragraph
                if not bodies and p1 < n:                               # deleting the first paragraph(s)
                    if paras[p1].start in {CrdtId(AUTHOR, r) for g in self.gen.values() for r in g["reserved_ids"]}:
                        run = [i for i in run if self.ts.chars[i].ch != "\n" or i != paras[p1].idx[0]]
                        bodies, tblocks = [""], [TextPara(PS.PLAIN, "")]     # keep an empty first paragraph
                    else:
                        run.append(paras[p1].idx[0])
            else:
                tstr = "".join("\n" + b for b in bodies)
            if run: pos = run[0] - 1
            elif p0 < n and paras[p0].idx: pos = paras[p0].idx[0] - 1
            else: pos = len(self.ts.chars) - 1 if p0 >= n else -1
            text_only = all(t[0] != "typst" for t in cur[a0:a1]) and all(isinstance(b, TextPara) for b in tblocks)
            if text_only:
                self.ts.replace_range(run, tstr, self.alloc, pos)
            else:
                self.ts.delete(run); self.ts.insert_after(pos, tstr, self.alloc)
            # styles / reserved paragraphs of the new run
            new = self.ts.paragraphs()
            q = p0
            for b in tblocks:
                if isinstance(b, TextPara):
                    self.ts.set_style(new[q].start, b.style, self.alloc); q += 1
                else:
                    lines, nl = rendered[b.hash]
                    reserved = [new[q + j].start for j in range(nl)]
                    for r in reserved: self.ts.set_style(r, PS.PLAIN, self.alloc)
                    self.add_gen_group(b, lines, nl, reserved); q += nl
            if p0 == 0 and bodies and p1 == 0:
                self.ts.set_style(new[q].start, old_end_style, self.alloc)
            if p0 == 0 and not bodies and p1 < n:
                self.ts.set_style(END, paras[p1].style, self.alloc)
        self.finish()

    def reanchor(self):
        """Groups anchored to a tombstoned char move to the paragraph now occupying that spot."""
        paras = self.ts.paragraphs()
        start_of = {i: p.start for p in paras for i in p.idx}
        index = {c.id: i for i, c in enumerate(self.ts.chars)}
        for b in self.blocks:
            if not (isinstance(b, TreeNodeBlock) and b.group.anchor_id): continue
            i = index.get(b.group.anchor_id.value)
            if i is None or not self.ts.chars[i].deleted: continue
            j = next((k for k in range(i + 1, len(self.ts.chars)) if k in start_of), max(start_of, default=None))
            new = start_of[j] if j is not None else END
            b.group.anchor_id = LwwValue(self.alloc.take(), SENTINEL_TOP if new == END else new)

    def finish(self):
        self.text_block.value = self.ts.to_text()
        self.reanchor()
        paras = self.ts.paragraphs()
        for b in self.blocks:
            if isinstance(b, PageInfoBlock):
                b.text_chars_count = sum(len(p.text) for p in paras) + len(paras)
                b.text_lines_count = len(paras)
        self.blocks = canonical(self.blocks)

    # --- reading back
    def to_blocks(self) -> list:
        """Page -> markdown model (text paras, typst blocks from manifest, ink notes)."""
        strokes = {}
        for b in self.blocks:
            if isinstance(b, SceneLineItemBlock) and b.item.deleted_length == 0:
                strokes[b.parent_id] = strokes.get(b.parent_id, 0) + 1
        gen_nodes = {CrdtId(AUTHOR, k) for k in self.gen}
        paras = self.ts.paragraphs()
        # The device anchors ink either to a paragraph's "\n" (type 2) or to a single character inside a
        # paragraph (type 1, written over text); resolve both to the containing paragraph's start id.
        para_of = {self.ts.chars[i].id: p.start for p in paras for i in p.idx}
        ink = {}   # paragraph start id -> total strokes
        for b in self.blocks:
            if isinstance(b, TreeNodeBlock) and b.group.anchor_id and b.group.node_id not in gen_nodes \
                    and strokes.get(b.group.node_id):
                a = b.group.anchor_id.value
                key = para_of.get(a, a)
                ink[key] = ink.get(key, 0) + strokes[b.group.node_id]
        # Broken blocks (device typed into / deleted reserved lines): the typst block goes where its first
        # surviving reserved paragraph is, typed text stays as text, leftover empty reserved lines are dropped.
        broken = [g for g in self.gen.values() if not self.intact(g, paras)]
        place, skip, tail = {}, set(), []
        for g in broken:
            idx = self.reserved_paras(g, paras)
            if idx: place[idx[0]] = g
            else: tail.append(g)
            skip |= {i for i in idx if paras[i].text == ""}
        out = []
        cur, spans = current_tokens(self.ts, [g for g in self.gen.values() if g not in broken])
        for tok, (p0, p1, g) in zip(cur, spans):
            if p0 in place: out.append(TypstBlock(place[p0]["source"], place[p0]["source_hash"]))
            if g is not None:
                out.append(TypstBlock(g["source"], g["source_hash"]))
            elif p0 not in skip:
                out.append(TextPara(*tok))
            keys = [paras[k].start for k in range(p0, p1)] + ([SENTINEL_TOP] if p0 == 0 else [])
            n = sum(ink.pop(a, 0) for a in keys)
            if n: out.append(InkNote(n))
        out += [TypstBlock(g["source"], g["source_hash"]) for g in tail]
        if ink: out.append(InkNote(sum(ink.values())))   # sentinel-anchored / unresolved ink at the end
        return out


# ---------------------------------------------------------------- files / device
def rsync_from_device(uuid: str):
    STAGE.mkdir(exist_ok=True)
    subprocess.run(["rsync", "-a", f"{DEVICE_STORE}{uuid}*", str(STAGE) + "/"], check=True)


def push_stage(*uuids: str):
    """rsync the staged items to the device and restart xochitl once."""
    subprocess.run([str(ROOT / "rmsync" / "push.sh"), *uuids], check=True)


def now_ms() -> str:
    return str(int(time.time() * 1000))


def page_idx(i: int) -> str:
    """Fractional-index page keys the way xochitl generates them: ba, bb, ..., bz, ca, ..."""
    return chr(ord("b") + i // 26) + chr(ord("a") + i % 26)


class Doc:
    """A staged notebook: metadata/content JSON plus one .rm per page (all under stage/)."""

    def __init__(self, uuid: str):
        self.uuid = uuid
        self.meta = json.loads((STAGE / f"{uuid}.metadata").read_text())
        self.content = json.loads((STAGE / f"{uuid}.content").read_text())

    @classmethod
    def new(cls, uuid: str, name: str, parent: str = "") -> "Doc":
        now = now_ms()
        meta = {"createdTime": now, "deleted": False, "lastModified": now, "lastOpened": now, "lastOpenedPage": 0,
                "metadatamodified": False, "modified": False, "parent": parent, "pinned": False, "synced": False,
                "type": "DocumentType", "version": 0, "visibleName": name}
        content = {
            "cPages": {"lastOpened": {"timestamp": "1:1", "value": ""}, "original": {"timestamp": "0:0", "value": -1},
                       "pages": [], "uuids": [{"first": str(uuid4()), "second": 1}]},
            "coverPageNumber": -1, "customZoomCenterX": 0, "customZoomCenterY": 936, "customZoomOrientation": "portrait",
            "customZoomPageHeight": 1872, "customZoomPageWidth": 1404, "customZoomScale": 1, "documentMetadata": {},
            "dummyDocument": False, "extraMetadata": {}, "fileType": "notebook", "fontName": "", "formatVersion": 2,
            "lineHeight": -1, "margins": 125, "orientation": "portrait", "pageCount": 0, "pageTags": [],
            "sizeInBytes": "0", "tags": [], "textAlignment": "justify", "textScale": 1, "zoomMode": "bestFit"}
        STAGE.mkdir(exist_ok=True)
        (STAGE / f"{uuid}.metadata").write_text(json.dumps(meta, indent=4) + "\n")
        (STAGE / f"{uuid}.content").write_text(json.dumps(content, indent=4) + "\n")
        (STAGE / f"{uuid}.pagedata").write_text("")
        return cls(uuid)

    @property
    def order(self) -> list[str]:
        return [p["id"] for p in self.content["cPages"]["pages"] if "deleted" not in p]

    def rm_path(self, page_uuid: str) -> Path:
        return STAGE / self.uuid / f"{page_uuid}.rm"

    def page(self, page_uuid: str, man: Manifest) -> Page:
        p = self.rm_path(page_uuid)
        if p.exists():
            with open(p, "rb") as f:
                return Page(read_blocks(f), man, page_uuid)
        return Page(empty_page_blocks(), man, page_uuid)    # device page without an .rm yet

    def add_page(self) -> str:
        pu = str(uuid4())
        pages = self.content["cPages"]["pages"]
        pages.append({"id": pu, "idx": {"timestamp": "1:2", "value": page_idx(len(pages))},
                      "template": {"timestamp": "1:1", "value": "Blank"}})
        if not self.content["cPages"]["lastOpened"]["value"]:
            self.content["cPages"]["lastOpened"]["value"] = pu
        return pu

    def remove_page(self, page_uuid: str):
        self.content["cPages"]["pages"] = [p for p in self.content["cPages"]["pages"] if p["id"] != page_uuid]
        self.rm_path(page_uuid).unlink(missing_ok=True)

    def write(self, pages: dict):
        """Write the given Page objects (page_uuid -> Page) and refresh content/metadata."""
        for pu, page in pages.items():
            p = self.rm_path(pu); p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as f:
                write_blocks(f, page.blocks)
        self.content["pageCount"] = len(self.order)
        self.content["sizeInBytes"] = str(sum(self.rm_path(pu).stat().st_size for pu in self.order
                                              if self.rm_path(pu).exists()))
        (STAGE / f"{self.uuid}.pagedata").write_text("Blank\n" * len(self.order))
        self.meta["version"] = self.meta.get("version", 0) + 1
        self.meta["lastModified"] = now_ms()
        (STAGE / f"{self.uuid}.metadata").write_text(json.dumps(self.meta, indent=4) + "\n")
        (STAGE / f"{self.uuid}.content").write_text(json.dumps(self.content, indent=4) + "\n")


def page_has_content(page: Page) -> bool:
    return page.ts.string().strip() != "" or any(
        isinstance(b, SceneLineItemBlock) and b.item.deleted_length == 0 for b in page.blocks)


# ---------------------------------------------------------------- folders
def device_metadata() -> dict:
    """uuid -> metadata for every (non-deleted) item on the device."""
    d = STAGE / "meta"; d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", "--include=*.metadata", "--exclude=*", DEVICE_STORE, str(d) + "/"], check=True)
    out = {}
    for f in d.glob("*.metadata"):
        m = json.loads(f.read_text())
        if not m.get("deleted") and m.get("parent") != "trash":
            out[f.stem] = m
    return out


def resolve_folder(path: str) -> tuple[str, list[str]]:
    """(uuid, newly staged folder uuids) for the folder at 'A/B/C' (visibleName path). New folders are
    staged, not pushed: the caller pushes them together with the document so xochitl restarts once."""
    meta = device_metadata()
    parent, created = "", []
    for name in [p for p in path.split("/") if p]:
        found = next((u for u, m in meta.items() if m["type"] == "CollectionType"
                      and m["parent"] == parent and m["visibleName"] == name), None)
        if found is None:
            found, now = str(uuid4()), now_ms()
            (STAGE / f"{found}.metadata").write_text(json.dumps(
                {"createdTime": now, "deleted": False, "lastModified": now, "lastOpened": "0", "lastOpenedPage": 0,
                 "metadatamodified": False, "modified": False, "parent": parent, "pinned": False, "synced": False,
                 "type": "CollectionType", "version": 0, "visibleName": name}, indent=4) + "\n")
            (STAGE / f"{found}.content").write_text("{}\n")
            meta[found] = {"type": "CollectionType", "parent": parent, "visibleName": name}
            created.append(found)
        parent = found
    return parent, created


# ---------------------------------------------------------------- commands
def content(md: str) -> list:
    """Text + typst blocks (ink notes ignored, pages normalised) of a markdown string, for change detection."""
    out = []
    for i, page in enumerate(mdmodel.split_pages(mdmodel.parse_doc(md))):
        blocks = [b for b in page if not isinstance(b, InkNote)]
        if not blocks or isinstance(blocks[0], TypstBlock):   # same normalisation Page.apply performs
            blocks = [TextPara(PS.PLAIN, "")] + blocks
        out += ([mdmodel.PageBreak()] if i else []) + blocks
    return out


def render_device(doc: Doc, man: Manifest) -> tuple[str, dict, list]:
    """Markdown of the whole device document (pages joined by ---), the Page objects and block list."""
    pages, blocks = {}, []
    for i, pu in enumerate(doc.order):
        pages[pu] = doc.page(pu, man)
        blocks += ([mdmodel.PageBreak()] if i else []) + pages[pu].to_blocks()
    return mdmodel.render_doc(blocks), pages, blocks


def finish_sync(man: Manifest, doc: Doc, pages: dict, name: str, md_path: str, extra: list[str] = ()):
    """Push the staged document (plus any new folders), then record the new state in one go."""
    push_stage(doc.uuid, *extra)
    for page in pages.values():
        page.commit()
    man.set_pages(doc.uuid, doc.order)
    synced, _, _ = render_device(doc, man)
    man.upsert_document(doc.uuid, name, md_path, doc.order[0], synced)


def apply_document(doc: Doc, man: Manifest, md: str) -> tuple[dict, list[str]]:
    """Edit every page so the document matches the markdown. Returns (pages written, kept extra pages)."""
    targets = mdmodel.split_pages(mdmodel.parse_doc(md))
    order = doc.order
    pages, kept = {}, []
    for k, tblocks in enumerate(targets):
        pu = order[k] if k < len(order) else doc.add_page()
        page = doc.page(pu, man)
        page.apply(tblocks)
        pages[pu] = page
    for pu in order[len(targets):]:                 # device has more pages than the markdown
        page = doc.page(pu, man)
        if page_has_content(page):
            kept.append(pu); pages[pu] = page        # never delete a page with ink or text
        else:
            doc.remove_page(pu)
    doc.write(pages)
    return pages, kept


def cmd_init(md: str, name: str, folder: str = ""):
    man = Manifest(str(ROOT / "sync.db"))
    md_path = str(Path(md).resolve())
    if man.get_document(md_path=md_path):
        sys.exit(f"{md_path} already initialised")
    parent, new_folders = resolve_folder(folder) if folder else ("", [])
    doc = Doc.new(str(uuid4()), name, parent)
    pages, _ = apply_document(doc, man, Path(md).read_text())
    finish_sync(man, doc, pages, name, md_path, new_folders)
    print(f"init {name}: doc {doc.uuid}, {len(pages)} pages, "
          f"{sum(len(p.gen) for p in pages.values())} typst blocks" + (f", in folder {folder}" if folder else ""))


def cmd_push(md: str, force: bool = False):
    man = Manifest(str(ROOT / "sync.db"))
    docrow = man.get_document(md_path=str(Path(md).resolve())) or sys.exit("not initialised; run init")
    rsync_from_device(docrow["uuid"])
    doc = Doc(docrow["uuid"])
    synced = docrow.get("synced_md")
    if synced is not None and not force and content(render_device(doc, man)[0]) != content(synced):
        sys.exit("device text changed since the last sync; pull first (or push --force to overwrite it)")
    pages, kept = apply_document(doc, man, Path(md).read_text())
    finish_sync(man, doc, pages, docrow["name"], docrow["md_path"])
    print(f"push {docrow['name']}: {len(doc.order)} pages, "
          f"{sum(len(p.ts.paragraphs()) for p in pages.values())} paragraphs, "
          f"{sum(len(p.gen) for p in pages.values())} typst blocks"
          + (f"; kept {len(kept)} device page(s) with content beyond the markdown" if kept else ""))


def cmd_pull(md: str, force: bool = False):
    man = Manifest(str(ROOT / "sync.db"))
    docrow = man.get_document(md_path=str(Path(md).resolve())) or sys.exit("not initialised; run init")
    synced, local = docrow.get("synced_md"), Path(docrow["md_path"])
    if synced is not None and not force and local.exists() and content(local.read_text()) != content(synced):
        sys.exit(f"{local} changed since the last sync; push first (or pull --force to overwrite it)")
    rsync_from_device(docrow["uuid"])
    doc = Doc(docrow["uuid"])
    rendered, pages, blocks = render_device(doc, man)
    local.write_text(rendered)
    for page in pages.values():
        page.commit()   # reconciliation of orphan/broken groups happens on the next push; only ids/rows here
    man.set_pages(doc.uuid, doc.order)
    man.upsert_document(doc.uuid, docrow["name"], docrow["md_path"], doc.order[0], rendered)
    kinds = {k.__name__: sum(isinstance(b, k) for b in blocks) for k in (TextPara, TypstBlock, InkNote)}
    print(f"pull {docrow['name']} -> {docrow['md_path']}: {len(doc.order)} pages, {kinds}")


def main(argv=None):
    import logging
    logging.getLogger("rmscene.text").setLevel(logging.ERROR)   # rmscene 0.6.1 mislabels bold codes as unknown
    argv = sys.argv[1:] if argv is None else argv
    force = "--force" in argv
    folder = argv[argv.index("--folder") + 1] if "--folder" in argv else ""
    argv = [a for a in argv if a != "--force"]
    if "--folder" in argv:
        i = argv.index("--folder"); del argv[i:i + 2]
    try:
        if len(argv) == 3 and argv[0] == "init": cmd_init(argv[1], argv[2], folder)
        elif len(argv) == 2 and argv[0] == "push": cmd_push(argv[1], force)
        elif len(argv) == 2 and argv[0] == "pull": cmd_pull(argv[1], force)
        else: sys.exit(__doc__)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
