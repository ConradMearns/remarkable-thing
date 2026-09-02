"""Typst -> PDF -> reMarkable, annotations -> SVG overlays.

  rmsync pdf init <file.typ|file.pdf> "<name>" [--folder "A/B"]
  rmsync pdf push <file>            recompile (if .typ) and replace the PDF; page annotations are kept
  rmsync pdf pull <file>            export each annotated page's ink to <file>.ink/p<N>.svg

For a 1:1 overlay the Typst page should be the tablet's aspect ratio:
  #set page(width: 157.8mm, height: 210.4mm)     (1404 x 1872 px at 226 dpi)
Overlays use the tablet's screen space: viewBox "-702 0 1404 1872" over the page scaled to full width.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader
from rmscene import read_tree, scene_items as si

from . import ink_svg
from .manifest import Manifest
from .sync import ROOT, STAGE, Doc, now_ms, page_idx, push_stage, resolve_folder, rsync_from_device

SCREEN_W, SCREEN_H = 1404, 1872


def compile_pdf(src: Path, out: Path):
    if src.suffix == ".pdf":
        shutil.copy(src, out)
    else:
        r = subprocess.run(["typst", "compile", "--root", "/", "--input", "rmsync-ink=no",
                            str(src.resolve()), str(out.resolve())], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"typst failed:\n{r.stderr}")
    return len(PdfReader(str(out)).pages)


def new_pdf_doc(uuid: str, name: str, parent: str) -> Doc:
    doc = Doc.new(uuid, name, parent)
    doc.content.update({"fileType": "pdf", "orientation": "portrait", "margins": 125, "coverPageNumber": 0})
    return doc


def set_pages(doc: Doc, n: int) -> list[str]:
    """Keep existing page entries (and their annotation files) for indices < n, add/drop the rest."""
    pages, seen = [], set()
    for p in doc.content["cPages"]["pages"]:
        if "deleted" in p: continue
        r = p.get("redir", {}).get("value")
        if r in seen:                       # duplicate entry for the same PDF page (device-generated)
            if not doc.rm_path(p["id"]).exists(): continue
        seen.add(r); pages.append(p)
    dropped = pages[n:]
    pages = pages[:n]
    for i in range(len(pages), n):
        pages.append({"id": str(uuid4()), "idx": {"timestamp": "1:1", "value": page_idx(i)},
                      "redir": {"timestamp": "1:1", "value": i}, "template": {"timestamp": "1:1", "value": "Blank"}})
    doc.content["cPages"]["pages"] = pages
    doc.content["cPages"]["lastOpened"]["value"] = pages[0]["id"]
    # original = the PDF's own page count; without it xochitl generates a second set of page entries
    doc.content["cPages"]["original"] = {"timestamp": "1:1", "value": n}
    doc.content["pageCount"] = n
    for p in dropped:
        doc.rm_path(p["id"]).unlink(missing_ok=True)
    return [p["id"] for p in dropped]


def finish(doc: Doc, pdf: Path):
    doc.content["sizeInBytes"] = str(pdf.stat().st_size)
    (STAGE / f"{doc.uuid}.pagedata").write_text("Blank\n" * doc.content["pageCount"])
    doc.meta["version"] = doc.meta.get("version", 0) + 1
    doc.meta["lastModified"] = now_ms()
    (STAGE / f"{doc.uuid}.metadata").write_text(json.dumps(doc.meta, indent=4) + "\n")
    (STAGE / f"{doc.uuid}.content").write_text(json.dumps(doc.content, indent=4) + "\n")


def cmd_init(src: str, name: str, folder: str = ""):
    man = Manifest(str(ROOT / "sync.db"))
    src_path = str(Path(src).resolve())
    if man.get_pdf_doc(src_path=src_path):
        sys.exit(f"{src_path} already initialised")
    parent, new_folders = resolve_folder(folder) if folder else ("", [])
    uuid = str(uuid4())
    doc = new_pdf_doc(uuid, name, parent)
    n = compile_pdf(Path(src), STAGE / f"{uuid}.pdf")
    set_pages(doc, n)
    finish(doc, STAGE / f"{uuid}.pdf")
    push_stage(uuid, *new_folders)
    man.upsert_pdf_doc(uuid, name, src_path)
    print(f"init {name}: doc {uuid}, {n} pages" + (f", in folder {folder}" if folder else ""))


def cmd_push(src: str):
    man = Manifest(str(ROOT / "sync.db"))
    row = man.get_pdf_doc(src_path=str(Path(src).resolve())) or sys.exit("not initialised; run init")
    rsync_from_device(row["uuid"])
    doc = Doc(row["uuid"])
    n = compile_pdf(Path(src), STAGE / f"{row['uuid']}.pdf")
    dropped = set_pages(doc, n)
    finish(doc, STAGE / f"{row['uuid']}.pdf")
    push_stage(row["uuid"])
    print(f"push {row['name']}: {n} pages" + (f"; dropped {len(dropped)} trailing page(s) and their ink" if dropped else ""))


def cmd_pull(src: str):
    man = Manifest(str(ROOT / "sync.db"))
    row = man.get_pdf_doc(src_path=str(Path(src).resolve())) or sys.exit("not initialised; run init")
    rsync_from_device(row["uuid"])
    doc = Doc(row["uuid"])
    p = Path(row["src_path"])
    ink_dir = p.with_name(p.stem + ".ink")
    if ink_dir.exists():
        shutil.rmtree(ink_dir)
    exported = {}
    for i, pu in enumerate(doc.order, 1):
        rm = doc.rm_path(pu)
        if not rm.exists():
            continue
        with open(rm, "rb") as f:
            tree = read_tree(f)
        groups = [g for g in tree.root.children.values() if isinstance(g, si.Group)]
        ink_dir.mkdir(exist_ok=True)
        n = ink_svg.render_groups(groups, str(ink_dir / f"p{i}.svg"), viewbox=(-SCREEN_W / 2, 0, SCREEN_W, SCREEN_H))
        if n:
            exported[i] = n
        else:
            (ink_dir / f"p{i}.svg").unlink()
    if p.suffix == ".typ":
        write_overlay(p, ink_dir, exported)
        if exported:
            composite(p, ink_dir, exported)
    print(f"pull {row['name']}: {len(doc.order)} pages, ink on pages {exported or 'none'} -> {ink_dir}/")


IMPORT_LINES = '#import "{ink}/overlay.typ": ink\n#show: ink\n'


def write_overlay(src: Path, ink_dir: Path, pages: dict):
    """Write <ink_dir>/overlay.typ (a show rule drawing p<N>.svg as each page's background) and make
    sure the source imports it. `rmsync pdf push` compiles with --input rmsync-ink=no so the PDF sent to
    the tablet never contains the ink twice."""
    ink_dir.mkdir(exist_ok=True)
    nums = ", ".join(str(n) for n in sorted(pages)) + ("," if len(pages) == 1 else "")
    (ink_dir / "overlay.typ").write_text(
        "// generated by `rmsync pdf pull`; do not edit. Ink from the tablet, one SVG per annotated page.\n"
        '#let ink(body) = if sys.inputs.at("rmsync-ink", default: "yes") == "no" { body } else {\n'
        "  set page(background: context {\n"
        "    let n = counter(page).get().first()\n"
        f"    if n in ({nums}) {{ place(top + left, image(\"p\" + str(n) + \".svg\", width: 100%)) }}\n"
        "  })\n"
        "  body\n"
        "}\n")
    lines = IMPORT_LINES.format(ink=ink_dir.name)
    text = src.read_text()
    if lines not in text:
        src.write_text(lines + text)
        print(f"added overlay import to {src.name}")


def composite(src: Path, ink_dir: Path, pages: dict):
    """Render annotated pages to <ink_dir>/p<N>.png: the Typst page at 226 ppi with the ink on top."""
    import cairosvg
    from PIL import Image
    r = subprocess.run(["typst", "compile", "--root", "/", "--format", "png", "--ppi", "226",
                        str(src.resolve()), str(ink_dir / "page{n}.png")], capture_output=True, text=True)
    if r.returncode:
        print(f"composite skipped: {r.stderr.strip()[:200]}"); return
    for n in pages:
        base = Image.open(ink_dir / f"page{n}.png").convert("RGBA")
        cairosvg.svg2png(url=str(ink_dir / f"p{n}.svg"), write_to=str(ink_dir / f"overlay{n}.png"),
                         output_width=base.width, output_height=base.height)
        base.alpha_composite(Image.open(ink_dir / f"overlay{n}.png").convert("RGBA"))
        base.save(ink_dir / f"p{n}.png")
    for f in list(ink_dir.glob("page*.png")) + list(ink_dir.glob("overlay*.png")):
        f.unlink()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    folder = argv[argv.index("--folder") + 1] if "--folder" in argv else ""
    if "--folder" in argv:
        i = argv.index("--folder"); del argv[i:i + 2]
    try:
        if len(argv) == 3 and argv[0] == "init": cmd_init(argv[1], argv[2], folder)
        elif len(argv) == 2 and argv[0] == "push": cmd_push(argv[1])
        elif len(argv) == 2 and argv[0] == "pull": cmd_pull(argv[1])
        else: sys.exit(__doc__)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
