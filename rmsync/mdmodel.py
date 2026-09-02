"""Markdown document model: text paragraphs (native rM styles), ```typst blocks, ink notes."""
import hashlib
import re
from dataclasses import dataclass
from rmscene import scene_items as si

PS = si.ParagraphStyle

@dataclass(frozen=True)
class TextPara:
    style: si.ParagraphStyle
    text: str

@dataclass(frozen=True)
class TypstBlock:
    source: str
    hash: str

@dataclass(frozen=True)
class InkNote:
    n_strokes: int

Block = TextPara | TypstBlock | InkNote

_INK_RE = re.compile(r"^\s*<!--\s*ink:\s*(\d+)\s+strokes?\s*-->\s*$")
_FENCE_OPEN = re.compile(r"^```typst\s*$")
_FENCE_CLOSE = re.compile(r"^```\s*$")

def normalize_typst(source: str) -> str:
    lines = [l.rstrip() for l in source.splitlines()]
    while lines and not lines[0]: lines.pop(0)
    while lines and not lines[-1]: lines.pop()
    return "\n".join(lines)

def typst_hash(source: str) -> str:
    return hashlib.sha1(normalize_typst(source).encode()).hexdigest()

def parse_line(line: str) -> TextPara:
    """Same regexes as emit.parse_md."""
    if m := re.match(r"^#+\s+(.*)", line): return TextPara(PS.HEADING, m[1])
    if m := re.match(r"^\s*[-*]\s+\[x\]\s+(.*)", line, re.I): return TextPara(PS.CHECKBOX_CHECKED, m[1])
    if m := re.match(r"^\s*[-*]\s+\[ \]\s+(.*)", line): return TextPara(PS.CHECKBOX, m[1])
    if m := re.match(r"^(\s{2,}|\t)[-*]\s+(.*)", line): return TextPara(PS.BULLET2, m[2])
    if m := re.match(r"^[-*]\s+(.*)", line): return TextPara(PS.BULLET, m[1])
    if m := re.match(r"^\*\*(.*)\*\*$", line): return TextPara(PS.BOLD, m[1])
    return TextPara(PS.PLAIN, line)

@dataclass(frozen=True)
class PageBreak:
    """A line containing only `---`: the following blocks go on the next device page."""


def split_pages(blocks: list) -> list[list]:
    pages, cur = [], []
    for b in blocks:
        if isinstance(b, PageBreak): pages.append(cur); cur = []
        else: cur.append(b)
    pages.append(cur)
    return pages


def parse_doc(md: str) -> list[Block]:
    """Parse markdown into blocks. `<!-- ink: N strokes -->` comments are dropped."""
    out: list[Block] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE_OPEN.match(line):
            j = i + 1
            while j < len(lines) and not _FENCE_CLOSE.match(lines[j]): j += 1
            src = normalize_typst("\n".join(lines[i + 1:j]))
            out.append(TypstBlock(src, typst_hash(src)))
            i = j + 1
            continue
        if _INK_RE.match(line):
            i += 1
            continue
        if line.strip() == "---":
            out.append(PageBreak()); i += 1
            continue
        out.append(parse_line(line))
        i += 1
    return out

_PREFIX = {PS.HEADING: "# ", PS.BULLET: "- ", PS.BULLET2: "  - ",
           PS.CHECKBOX: "- [ ] ", PS.CHECKBOX_CHECKED: "- [x] "}

def render_block(b: Block) -> str:
    if isinstance(b, TextPara):
        if b.style == PS.BOLD: return f"**{b.text}**"
        return _PREFIX.get(b.style, "") + b.text
    if isinstance(b, TypstBlock):
        return f"```typst\n{b.source}\n```"
    if isinstance(b, InkNote):
        return f"<!-- ink: {b.n_strokes} strokes -->"
    if isinstance(b, PageBreak):
        return "---"
    raise TypeError(b)

def render_doc(blocks: list[Block]) -> str:
    return "\n".join(render_block(b) for b in blocks) + ("\n" if blocks else "")
