"""Adversarial tests for textcrdt.TextState.  Run: .venv/bin/python rmsync/test_textcrdt.py

Every scenario runs against (a) the real device file pulled/3/<doc>/<page>.rm and
(b) a fresh emit.py page.  After each edit the blocks are written to disk, read back
with rmscene, and checked: paragraph list, id stability, fresh-id discipline, chain.
"""
import io, os, sys, dataclasses
sys.path.insert(0, os.path.dirname(__file__))
from rmscene import read_blocks, write_blocks, RootTextBlock, CrdtId, CrdtSequence, CrdtSequenceItem
from rmscene import scene_items as si
from rmscene.text import TextDocument, expand_text_items
from textcrdt import TextState, IdAlloc, END
import emit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.environ.get("SCRATCH", "/tmp/claude-1000/-home-conrad-remarkable-thing/9309daf1-8f8c-417c-8285-0b4d51249556/scratchpad")
os.makedirs(SCRATCH, exist_ok=True)
PS = si.ParagraphStyle


# ---------------------------------------------------------------- helpers
def all_ids(obj, out):
    """Collect every CrdtId reachable inside a block (dataclass walk)."""
    if isinstance(obj, CrdtId): out.add(obj)
    elif isinstance(obj, CrdtSequence): all_ids(obj.sequence_items(), out)
    elif dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj): all_ids(getattr(obj, f.name), out)
    elif isinstance(obj, dict):
        for k, v in obj.items(): all_ids(k, out); all_ids(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj: all_ids(v, out)
    return out


def max_id(blocks, author):
    ids = all_ids(blocks, set())
    return max((i.part2 for i in ids if i.part1 == author), default=0)


def text_block(blocks):
    return next(b for b in blocks if isinstance(b, RootTextBlock))


def write_read(blocks, state, name):
    """Replace the text block with state.to_text(), write, read back. Returns (bytes, blocks)."""
    out = [RootTextBlock(block_id=b.block_id, value=state.to_text()) if isinstance(b, RootTextBlock) else b
           for b in blocks]
    path = os.path.join(SCRATCH, name + ".rm")
    with open(path, "wb") as f: write_blocks(f, out)
    data = open(path, "rb").read()
    return data, list(read_blocks(io.BytesIO(data)))


def paras_of(text):
    return [(p.style.value, "".join(c.s for c in p.contents)) for p in TextDocument.from_scene_item(text).contents]


def expanded(text):
    return list(expand_text_items(text.items.sequence_items()))


class Checker:
    """Tracks ids across a sequence of edits on one TextState with one IdAlloc."""
    def __init__(self, blocks, state, alloc):
        self.blocks, self.st, self.alloc = blocks, state, alloc
        self.prev_max = max_id(blocks, alloc.author)
        assert alloc.next > self.prev_max, (alloc.next, self.prev_max)
        self.snapshot()

    def snapshot(self):
        self.live_before = {c.id: c.ch for c in self.st.chars if not c.deleted and c.fmt is None}
        self.ids_before = {c.id for c in self.st.chars}

    def check(self, expected, name):
        data, blocks2 = write_read(self.blocks, self.st, name)
        text2 = text_block(blocks2).value
        got = paras_of(text2)
        assert got == expected, f"{name}: paragraphs\n got: {got}\n exp: {expected}"
        ex = expanded(text2)
        ids = [it.item_id for it in ex]
        idset = set(ids)
        assert len(ids) == len(idset), f"{name}: duplicate ids after expansion"
        # nothing ever leaves the sequence (tombstones keep their ids)
        assert self.ids_before <= idset, f"{name}: ids vanished: {self.ids_before - idset}"
        # every pre-existing live char that is still live keeps its id and char
        live_after = {it.item_id: it.value for it in ex if it.deleted_length == 0 and isinstance(it.value, str)}
        for cid, ch in self.live_before.items():
            if cid in live_after: assert live_after[cid] == ch, f"{name}: char {cid} changed"
        # new ids: from our author, unique, strictly greater than any prior id of that author
        new = idset - self.ids_before
        for cid in new:
            assert cid.part1 == self.alloc.author, f"{name}: foreign new id {cid}"
            assert cid.part2 > self.prev_max, f"{name}: id {cid} not > {self.prev_max}"
        # style timestamps we minted must also be fresh
        for k, lww in text2.styles.items():
            if lww.timestamp.part1 == self.alloc.author and lww.timestamp.part2 > self.prev_max:
                assert lww.timestamp not in idset, f"{name}: style timestamp {lww.timestamp} collides with a char id"
        # chain: left/right are END or exist
        for it in ex:
            assert it.left_id == END or it.left_id in idset, f"{name}: dangling left {it}"
            assert it.right_id == END or it.right_id in idset, f"{name}: dangling right {it}"
        # toposorted string equals what the state believes
        seq = CrdtSequence(ex)
        topo = "".join(v for v in seq.values() if isinstance(v, str))
        assert topo == self.st.string(), f"{name}: toposort order != state order"
        # to_items / expand is a fixed point (re-reading gives identical items)
        st2 = TextState(text2)
        assert st2.to_items() == text2.items.sequence_items(), f"{name}: to_items not idempotent"
        assert st2.string() == self.st.string()
        self.prev_max = max(self.prev_max, max_id(blocks2, self.alloc.author))
        self.snapshot()
        return text2


def fresh(blocks, author=1):
    text = text_block(blocks).value
    st = TextState(text)
    alloc = IdAlloc(author, max_id(blocks, author) + 1)
    return st, alloc, Checker(blocks, st, alloc)


def body(st, p):
    return [i for i in p.idx if st.chars[i].ch != "\n"]


# ---------------------------------------------------------------- scenarios
def t_roundtrip(blocks, label):
    st = TextState(text_block(blocks).value)
    orig = io.BytesIO(); write_blocks(orig, blocks)
    data, _ = write_read(blocks, st, label + "_rt")
    assert data == orig.getvalue(), f"{label}: round trip not byte identical"
    return st


def t_insert_para_between(blocks, base):
    st, alloc, ck = fresh(blocks)
    k = 2
    p = st.paragraphs()[k]
    new = st.insert_after(p.idx[-1], "\nbrand new paragraph", alloc)
    st.set_style(st.chars[new[0]].id, PS.BULLET, alloc)
    exp = base[:k + 1] + [(PS.BULLET, "brand new paragraph")] + base[k + 1:]
    ck.check(exp, "insert_between")


def t_delete_para(blocks, base):
    for k in (0, 3, len(base) - 1):
        st, alloc, ck = fresh(blocks)
        p = st.paragraphs()[k]
        st.delete(p.idx)
        exp = list(base)
        if k == 0: exp[0] = (base[0][0], "")     # first paragraph has no "\n" to delete
        else: del exp[k]
        ck.check(exp, f"delete_para_{k}")


def t_replace_text(blocks, base):
    k = 2
    for target in ("short", "a much longer replacement string than before", "", "Some body text typed on the LAPTOP."):
        st, alloc, ck = fresh(blocks)
        p = st.paragraphs()[k]
        st.replace_para(p, target, alloc)
        exp = list(base); exp[k] = (base[k][0], target)
        ck.check(exp, "replace_" + str(len(target)))
    # replace text of the first paragraph (no leading "\n") incl. from empty
    st, alloc, ck = fresh(blocks)
    st.replace_para(st.paragraphs()[0], "", alloc)
    exp = list(base); exp[0] = (base[0][0], "")
    ck.check(exp, "replace_first_empty")
    st.replace_para(st.paragraphs()[0], "Refilled", alloc)
    exp[0] = (base[0][0], "Refilled")
    ck.check(exp, "replace_first_refill")
    # fill an empty paragraph (pulled file: index 1 is empty)
    st, alloc, ck = fresh(blocks)
    k = next(i for i, (_, t) in enumerate(base) if t == "")
    st.replace_para(st.paragraphs()[k], "was empty", alloc)
    exp = list(base); exp[k] = (base[k][0], "was empty")
    ck.check(exp, "fill_empty")


def t_style(blocks, base):
    st, alloc, ck = fresh(blocks)
    ps = st.paragraphs()
    st.set_style(ps[0].start, PS.PLAIN, alloc)
    st.set_style(ps[2].start, PS.CHECKBOX_CHECKED, alloc)
    exp = list(base); exp[0] = (PS.PLAIN, base[0][1]); exp[2] = (PS.CHECKBOX_CHECKED, base[2][1])
    ck.check(exp, "style")
    # setting the same style again mints no id
    n = alloc.next
    st.set_style(ps[2].start, PS.CHECKBOX_CHECKED, alloc)
    assert alloc.next == n


def t_start_end(blocks, base):
    st, alloc, ck = fresh(blocks)
    st.insert_after(-1, ">>", alloc)
    exp = list(base); exp[0] = (base[0][0], ">>" + base[0][1])
    ck.check(exp, "prefix_start")
    last = st.paragraphs()[-1]
    st.insert_after(last.idx[-1], "<<", alloc)
    exp[-1] = (exp[-1][0], exp[-1][1] + "<<")
    ck.check(exp, "suffix_end")
    # new paragraph at the very end and at the very start
    st.insert_after(len(st.chars) - 1, "\nthe end", alloc)
    exp.append((PS.PLAIN, "the end"))
    ck.check(exp, "para_end")
    new = st.insert_after(-1, "the start\n", alloc)
    # the first paragraph is keyed on END: its old style now applies to "the start";
    # the old first paragraph is keyed on the new "\n"
    st.set_style(st.chars[new[-1]].id, exp[0][0], alloc)
    st.set_style(END, PS.HEADING, alloc)
    exp = [(PS.HEADING, "the start")] + exp
    ck.check(exp, "para_start")


def t_delete_across(blocks, base):
    st, alloc, ck = fresh(blocks)
    k = 2
    ps = st.paragraphs()
    st.delete(ps[k].idx[-3:] + ps[k + 1].idx[:4])
    exp = list(base)
    exp[k] = (base[k][0], base[k][1][:-3] + base[k + 1][1][3:]); del exp[k + 1]
    ck.check(exp, "delete_across")
    # and delete everything from the middle of one paragraph to the middle of one three further on
    ps = st.paragraphs()
    rng = ps[k].idx[-2:] + ps[k + 1].idx + ps[k + 2].idx + ps[k + 3].idx[:3]
    st.delete(rng)
    exp[k] = (exp[k][0], exp[k][1][:-2] + exp[k + 3][1][2:]); del exp[k + 1:k + 4]
    ck.check(exp, "delete_across_3")


def t_sequence(blocks, base):
    """Many edits on one state with one allocator, checked after each."""
    st, alloc, ck = fresh(blocks)
    exp = list(base)
    # 1. append a paragraph
    p = st.paragraphs()[1]
    new = st.insert_after(p.idx[-1], "\nfirst insert", alloc)
    st.set_style(st.chars[new[0]].id, PS.BULLET2, alloc)
    exp.insert(2, (PS.BULLET2, "first insert")); ck.check(exp, "seq1")
    # 2. edit inside the paragraph we just inserted (fresh ids adjacent to fresh ids)
    p = st.paragraphs()[2]
    st.replace_para(p, "first insert, edited", alloc)
    exp[2] = (PS.BULLET2, "first insert, edited"); ck.check(exp, "seq2")
    # 3. insert a char in the middle of the fresh run, then delete the char before it
    p = st.paragraphs()[2]; b = body(st, p)
    st.insert_after(b[4], "X", alloc)
    st.delete([b[4]])
    exp[2] = (PS.BULLET2, "firsX insert, edited"); ck.check(exp, "seq3")
    # 4. replace the original paragraph after it, then restyle it
    p = st.paragraphs()[3]
    st.replace_para(p, "totally different", alloc); st.set_style(p.start, PS.HEADING, alloc)
    exp[3] = (PS.HEADING, "totally different"); ck.check(exp, "seq4")
    # 5. delete the inserted paragraph again
    st.delete(st.paragraphs()[2].idx)
    del exp[2]; ck.check(exp, "seq5")
    # 6. split a paragraph in two by replacing with text containing "\n"
    p = st.paragraphs()[2]
    st.replace_para(p, "totally\ndifferent", alloc)
    exp[2:3] = [(PS.HEADING, "totally"), (PS.PLAIN, "different")]; ck.check(exp, "seq6")
    # 7. re-join them by deleting the new "\n"
    st.delete([st.paragraphs()[3].idx[0]])
    exp[2:4] = [(PS.HEADING, "totallydifferent")]; ck.check(exp, "seq7")
    # 8. insert between two previously inserted chars (fresh id neighbours both sides)
    p = st.paragraphs()[2]; b = body(st, p)
    st.insert_after(b[6], " ", alloc)
    exp[2] = (PS.HEADING, "totally different"); ck.check(exp, "seq8")
    # 9. many small inserts at the same position, in reverse (each new one lands before the last)
    p = st.paragraphs()[2]; b = body(st, p)
    for ch in "cba": st.insert_after(b[6], ch, alloc)
    exp[2] = (PS.HEADING, "totallyabc different"); ck.check(exp, "seq9")
    # 10. delete everything except the first paragraph, then add new content
    ps = st.paragraphs()
    st.delete([i for p in ps[1:] for i in p.idx])
    exp = exp[:1]; ck.check(exp, "seq10")
    st.insert_after(len(st.chars) - 1, "\nrebuilt\nagain", alloc)
    exp += [(PS.PLAIN, "rebuilt"), (PS.PLAIN, "again")]; ck.check(exp, "seq11")


def t_device_authored(blocks, base):
    """Edits touching the device-authored 'E' + tombstone + 'ditting inlone 123' paragraph."""
    k = next(i for i, (_, t) in enumerate(base) if t == "Editting inlone 123")
    st, alloc, ck = fresh(blocks)
    p = st.paragraphs()[k]
    assert st.chars[p.idx[1]].id == CrdtId(2, 443) and st.chars[p.idx[1] + 1].id == CrdtId(2, 444)
    assert st.chars[p.idx[1] + 1].deleted
    exp = list(base)
    # fix the typos: char-diff keeps the surviving device ids
    st.replace_para(p, "Editing inline 123", alloc)
    exp[k] = (PS.PLAIN, "Editing inline 123")
    t2 = ck.check(exp, "dev_fix_typos")
    live = {it.item_id for it in expanded(t2) if it.deleted_length == 0}
    assert CrdtId(2, 443) in live and CrdtId(2, 445) in live and CrdtId(2, 462) in live
    # insert directly between 'E' and the device tombstone; delete the 'E'
    p = st.paragraphs()[k]
    st.insert_after(p.idx[1], "ZZ", alloc); st.delete([p.idx[1]])
    exp[k] = (PS.PLAIN, "ZZditing inline 123"); ck.check(exp, "dev_around_tombstone")
    # insert right after the device tombstone
    i444 = next(i for i, c in enumerate(st.chars) if c.id == CrdtId(2, 444))
    st.insert_after(i444, "Q", alloc)
    exp[k] = (PS.PLAIN, "ZZQditing inline 123"); ck.check(exp, "dev_after_tombstone")
    # insert after the device's trailing "\n" (2,493) which has right_id (1,142) far away
    i493 = next(i for i, c in enumerate(st.chars) if c.id == CrdtId(2, 493))
    st.insert_after(i493, "in the empty one", alloc)
    exp[k + 1] = (PS.PLAIN, "in the empty one"); ck.check(exp, "dev_after_493")
    # delete the device paragraph's "\n" (1,140) so it merges with 'a checked box'
    p = st.paragraphs()[k]
    st.delete([p.idx[0]])
    exp[k - 1] = (exp[k - 1][0], exp[k - 1][1] + exp[k][1]); del exp[k]; ck.check(exp, "dev_merge_up")
    # edit the device's last paragraph 'some text from the rm dev' (author-2 run, ends at END)
    p = st.paragraphs()[-1]
    st.replace_para(p, "some text from the rm dev, edited by laptop", alloc)
    exp[-1] = (exp[-1][0], "some text from the rm dev, edited by laptop"); ck.check(exp, "dev_last")
    # and the device's run of four "\n" (2,389..392): delete two of the empty paragraphs
    ps = st.paragraphs()
    empties = [q for q in ps if q.start in (CrdtId(2, 390), CrdtId(2, 391))]
    assert len(empties) == 2
    st.delete([i for q in empties for i in q.idx])
    exp = [e for j, e in enumerate(exp) if ps[j].start not in (CrdtId(2, 390), CrdtId(2, 391))]
    ck.check(exp, "dev_del_empties")


def t_synthetic():
    """Inline formatting items, an empty document, pathological diffs, mergeable adjacent runs."""
    def I(a, l, r, dl, v): return CrdtSequenceItem(CrdtId(*a), CrdtId(*l), CrdtId(*r), dl, v)
    def T(items): return si.Text(items=CrdtSequence(items), styles={}, pos_x=0, pos_y=0, width=1)
    # "ab" <bold on> "cd" <bold off>: kept verbatim; inserts on either side of the code item
    items = [I((1, 1), (0, 0), (1, 3), 0, "ab"), I((1, 3), (1, 2), (1, 4), 0, 1),
             I((1, 4), (1, 3), (1, 6), 0, "cd"), I((1, 6), (1, 5), (0, 0), 0, 2)]
    st = TextState(T(items)); assert st.to_items() == items
    al = IdAlloc(1, 100); st.insert_after(1, "X", al); st.insert_after(3, "Y", al)
    d = TextDocument.from_scene_item(st.to_text())
    assert [(c.s, c.properties["font-weight"]) for p in d.contents for c in p.contents] == [("abX", "normal"), ("Ycd", "bold")]
    # empty document
    st = TextState(T([])); assert st.paragraphs()[0].idx == []
    al = IdAlloc(1, 1); st.replace_para(st.paragraphs()[0], "hello\nworld", al); st.set_style(END, PS.HEADING, al)
    assert paras_of(st.to_text()) == [(PS.HEADING, "hello"), (PS.PLAIN, "world")]
    assert st.to_items() == [I((1, 1), (0, 0), (0, 0), 0, "hello\nworld")]
    # pathological diffs
    for cur, tgt in [("aaaa", "aa"), ("abab", "baba"), ("abc", "cba"), ("x", "xxxx"), ("hello world", "world hello"), ("aXbXc", "abc"), ("", "z"), ("z", "")]:
        st = TextState(T([I((1, 1), (0, 0), (0, 0), 0, cur)] if cur else []))
        st.replace_para(st.paragraphs()[0], tgt, IdAlloc(1, 50))
        t2 = st.to_text(); ids = {e.item_id for e in expanded(t2)}
        assert paras_of(t2) == [(PS.PLAIN, tgt)], (cur, tgt)
        assert all(e.left_id in ids | {END} and e.right_id in ids | {END} for e in expanded(t2))
    # adjacent items with contiguous chained ids coalesce into one (equivalent, not byte-identical)
    st = TextState(T([I((1, 1), (0, 0), (1, 3), 0, "ab"), I((1, 3), (1, 2), (0, 0), 0, "c")]))
    assert st.to_items() == [I((1, 1), (0, 0), (0, 0), 0, "abc")]
    print("synthetic: ok")


def run_all(blocks, label, device=False):
    base = paras_of(text_block(blocks).value)
    t_roundtrip(blocks, label)
    t_insert_para_between(blocks, base)
    t_delete_para(blocks, base)
    t_replace_text(blocks, base)
    t_style(blocks, base)
    t_start_end(blocks, base)
    t_delete_across(blocks, base)
    t_sequence(blocks, base)
    if device: t_device_authored(blocks, base)
    print(f"{label}: ok ({len(base)} paragraphs)")


def main():
    doc, page = open(os.path.join(ROOT, "stage/CURRENT")).read().split()
    path = os.path.join(ROOT, "pulled/3", doc, page + ".rm")
    blocks = list(read_blocks(open(path, "rb")))
    run_all(blocks, "pulled", device=True)

    md = "# Title\n\nSome body text typed on the laptop.\n- bullet one\n  - nested\n- [ ] box\n- [x] done\n**bold para**\nlast line"
    buf = io.BytesIO(); write_blocks(buf, list(emit.text_blocks(emit.parse_md(md))))
    run_all(list(read_blocks(io.BytesIO(buf.getvalue()))), "emit")
    t_synthetic()
    print("all ok")


if __name__ == "__main__":
    main()
