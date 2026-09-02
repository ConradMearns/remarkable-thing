"""Markdown-subset -> reMarkable v6 .rm with native text (own emitter on rmscene primitives)."""
import re
from uuid import uuid4
from rmscene import (write_blocks, AuthorIdsBlock, MigrationInfoBlock, PageInfoBlock,
                     SceneTreeBlock, RootTextBlock, TreeNodeBlock, SceneGroupItemBlock,
                     CrdtId, CrdtSequence, CrdtSequenceItem, LwwValue)
from rmscene import scene_items as si

PS = si.ParagraphStyle

def parse_md(md: str):
    """Return list of (style, text) paragraphs from a markdown subset."""
    out = []
    for line in md.splitlines():
        if m := re.match(r"^#+\s+(.*)", line): out.append((PS.HEADING, m[1]))
        elif m := re.match(r"^\s*[-*]\s+\[x\]\s+(.*)", line, re.I): out.append((PS.CHECKBOX_CHECKED, m[1]))
        elif m := re.match(r"^\s*[-*]\s+\[ \]\s+(.*)", line): out.append((PS.CHECKBOX, m[1]))
        elif m := re.match(r"^(\s{2,}|\t)[-*]\s+(.*)", line): out.append((PS.BULLET2, m[2]))
        elif m := re.match(r"^[-*]\s+(.*)", line): out.append((PS.BULLET, m[1]))
        elif m := re.match(r"^\*\*(.*)\*\*$", line): out.append((PS.BOLD, m[1]))
        else: out.append((PS.PLAIN, line))
    return out

def text_blocks(paragraphs, author=1):
    """Build the block list for a page whose only content is native text."""
    items, styles = [], {}
    nid = 20  # running CrdtId counter
    prev = CrdtId(0, 0)
    for i, (style, text) in enumerate(paragraphs):
        if i == 0:
            key = si.END_MARKER
            chunk = text
        else:
            key = CrdtId(author, nid)
            chunk = "\n" + text
        if chunk:
            item = CrdtSequenceItem(item_id=CrdtId(author, nid), left_id=prev, right_id=CrdtId(0, 0),
                                    deleted_length=0, value=chunk)
            items.append(item)
            prev = CrdtId(author, nid + len(chunk) - 1)
            nid += len(chunk)
        styles[key] = LwwValue(timestamp=CrdtId(author, nid), value=style); nid += 1
    total = sum(len(t) for _, t in paragraphs) + len(paragraphs)
    yield AuthorIdsBlock(author_uuids={author: uuid4()})
    yield MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True)
    yield PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=total, text_lines_count=len(paragraphs))
    yield SceneTreeBlock(tree_id=CrdtId(0, 11), node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1))
    yield RootTextBlock(block_id=CrdtId(0, 0), value=si.Text(items=CrdtSequence(items), styles=styles,
                                                             pos_x=-468.0, pos_y=234.0, width=936.0))
    yield TreeNodeBlock(si.Group(node_id=CrdtId(0, 1)))
    yield TreeNodeBlock(si.Group(node_id=CrdtId(0, 11), label=LwwValue(timestamp=CrdtId(0, 12), value="Layer 1")))
    yield SceneGroupItemBlock(parent_id=CrdtId(0, 1), item=CrdtSequenceItem(
        item_id=CrdtId(0, 13), left_id=CrdtId(0, 0), right_id=CrdtId(0, 0), deleted_length=0, value=CrdtId(0, 11)))

def md_to_rm(md: str, path: str):
    with open(path, "wb") as f:
        write_blocks(f, list(text_blocks(parse_md(md))))

if __name__ == "__main__":
    import sys; md_to_rm(open(sys.argv[1]).read(), sys.argv[2])
