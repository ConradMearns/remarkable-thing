"""Edit a reMarkable v6 text CRDT in place, keeping existing character ids stable.

Model: the text is a flat list of Ch (one per character, including tombstones).
Edits mark chars deleted or insert new Ch with fresh ids. `to_items()` coalesces
runs back into CrdtSequenceItems (exact inverse of rmscene's expand_text_item).
"""
from __future__ import annotations
from dataclasses import dataclass, field
import difflib
from rmscene import CrdtId, CrdtSequence, CrdtSequenceItem, LwwValue
from rmscene import scene_items as si
from rmscene.text import expand_text_items

END = si.END_MARKER


class IdAlloc:
    """Allocates sequential CrdtIds for one author."""
    def __init__(self, author: int, next_id: int):
        self.author, self.next = author, next_id

    def take(self, n: int = 1) -> CrdtId:
        cid = CrdtId(self.author, self.next); self.next += n; return cid


@dataclass
class Ch:
    id: CrdtId
    ch: str            # "" for tombstone
    left: CrdtId
    right: CrdtId
    deleted: bool = False
    fmt: int | None = None   # inline formatting code items (kept verbatim)


@dataclass
class Para:
    start: CrdtId          # id of the "\n" that begins it, or END for the first paragraph
    text: str
    style: si.ParagraphStyle
    idx: list[int]         # indices into TextState.chars of live chars (incl. the leading "\n")


class TextState:
    def __init__(self, text: si.Text):
        self.text = text
        self.chars: list[Ch] = []
        expanded = list(expand_text_items(text.items.sequence_items()))
        order = CrdtSequence(expanded).keys()          # CRDT order; xochitl stores items in this order already
        if [it.item_id for it in expanded] != order:
            by_id = {it.item_id: it for it in expanded}
            expanded = [by_id[k] for k in order]
        for it in expanded:
            if isinstance(it.value, int):
                self.chars.append(Ch(it.item_id, "", it.left_id, it.right_id, False, it.value))
            else:
                self.chars.append(Ch(it.item_id, it.value, it.left_id, it.right_id, it.deleted_length > 0))
        self.styles: dict[CrdtId, LwwValue] = dict(text.styles)

    # ---- reading -------------------------------------------------------
    def live(self) -> list[int]:
        return [i for i, c in enumerate(self.chars) if not c.deleted and c.fmt is None]

    def string(self) -> str:
        return "".join(self.chars[i].ch for i in self.live())

    def paragraphs(self) -> list[Para]:
        paras, cur, start = [], [], END
        for i in self.live():
            c = self.chars[i]
            if c.ch == "\n":
                paras.append(self._para(start, cur)); start, cur = c.id, [i]
            else:
                cur.append(i)
        paras.append(self._para(start, cur))
        return paras

    def _para(self, start, idx):
        txt = "".join(self.chars[i].ch for i in idx if self.chars[i].ch != "\n")
        st = self.styles.get(start)
        return Para(start, txt, st.value if st else si.ParagraphStyle.PLAIN, idx)

    # ---- editing -------------------------------------------------------
    def delete(self, indices: list[int]):
        for i in indices:
            self.chars[i].deleted = True; self.chars[i].ch = ""

    def insert_after(self, pos: int, s: str, alloc: IdAlloc) -> list[int]:
        """Insert string s after chars[pos] (pos=-1 for start). Returns new indices."""
        if not s: return []
        left = self.chars[pos].id if pos >= 0 else END
        right = self.chars[pos + 1].id if pos + 1 < len(self.chars) else END
        base = alloc.take(len(s))
        new = []
        for k, ch in enumerate(s):
            cid = CrdtId(base.part1, base.part2 + k)
            l = left if k == 0 else CrdtId(base.part1, base.part2 + k - 1)
            r = right if k == len(s) - 1 else CrdtId(base.part1, base.part2 + k + 1)
            new.append(Ch(cid, ch, l, r))
        self.chars[pos + 1:pos + 1] = new
        return list(range(pos + 1, pos + 1 + len(new)))

    def replace_range(self, live_idx: list[int], target: str, alloc: IdAlloc, anchor_pos: int):
        """Char-diff the live chars at live_idx against target; anchor_pos = index to insert after if live_idx empty."""
        cur = "".join(self.chars[i].ch for i in live_idx)
        sm = difflib.SequenceMatcher(None, cur, target, autojunk=False)
        # apply from the end so earlier indices stay valid
        for tag, a0, a1, b0, b1 in reversed(sm.get_opcodes()):
            if tag == "equal": continue
            if tag in ("delete", "replace"):
                self.delete(live_idx[a0:a1])
            if tag in ("insert", "replace"):
                pos = live_idx[a0 - 1] if a0 > 0 else (anchor_pos if not live_idx else live_idx[0] - 1)
                if tag == "replace": pos = live_idx[a1 - 1]   # after the (now deleted) run keeps ordering stable
                self.insert_after(pos, target[b0:b1], alloc)

    def replace_para(self, p: Para, target: str, alloc: IdAlloc):
        """Set paragraph p's body text to target (target may contain "\n" to split it)."""
        body = [i for i in p.idx if self.chars[i].ch != "\n"]
        anchor = p.idx[0] if p.idx and self.chars[p.idx[0]].ch == "\n" else -1
        self.replace_range(body, target, alloc, anchor)

    def set_style(self, start: CrdtId, style: si.ParagraphStyle, alloc: IdAlloc):
        cur = self.styles.get(start)
        if cur is None or cur.value != style:
            self.styles[start] = LwwValue(timestamp=alloc.take(), value=style)

    # ---- writing -------------------------------------------------------
    def to_items(self) -> list[CrdtSequenceItem]:
        items, run = [], None
        def flush():
            nonlocal run
            if run: items.append(run); run = None
        for c in self.chars:
            if c.fmt is not None:
                flush(); items.append(CrdtSequenceItem(c.id, c.left, c.right, 0, c.fmt)); continue
            if run is not None:
                last_id = CrdtId(run.item_id.part1, run.item_id.part2 + run_len[0] - 1)
                same_kind = (run.deleted_length > 0) == c.deleted
                if same_kind and c.left == last_id and c.id == CrdtId(last_id.part1, last_id.part2 + 1) and run_right[0] == c.id:
                    run_len[0] += 1; run_right[0] = c.right
                    if c.deleted: run.deleted_length = run_len[0]
                    else: run.value += c.ch
                    run.right_id = c.right
                    continue
                flush()
            run = CrdtSequenceItem(c.id, c.left, c.right, 1 if c.deleted else 0, "" if c.deleted else c.ch)
            run_len, run_right = [1], [c.right]
        flush()
        return items

    def to_text(self) -> si.Text:
        return si.Text(items=CrdtSequence(self.to_items()), styles=self.styles,
                       pos_x=self.text.pos_x, pos_y=self.text.pos_y, width=self.text.width)
