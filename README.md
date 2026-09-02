# rmsync

Sync between documents on this laptop and a rooted reMarkable 2, two ways:

| path | laptop source | on the tablet | what comes back |
|---|---|---|---|
| **md** | markdown (+ Typst fences, SVG images) | native, editable text; Typst and images as ink | typed edits, new pages, drawings as SVG images in the markdown |
| **pdf** | Typst (or a ready PDF) | a fixed-layout PDF document | per-page annotation ink as SVG overlays |

Use **md** when you want to keep writing on the tablet. Use **pdf** when the layout matters
and the tablet is for reading and annotating.

## Setup

```sh
uv sync                      # creates .venv with rmscene, rmc, svgelements, cairosvg, pypdf
uv run rmsync                # prints the two command groups
```

- Tablet plugged in over USB (it appears at `10.11.99.1`). Credentials live in `.env`;
  key auth is installed, so plain `ssh rm` works.
- Typst is the snap `typst`. Keep the tablet awake while syncing; auto-sleep drops the USB link.

## Path 1: markdown ⇄ native text

Every markdown file is one notebook. Register it once, then push and pull as you go.

```sh
uv run rmsync md init notes/post.md "My post" --folder "Blog"   # once
uv run rmsync md push notes/post.md                              # after editing on the laptop
uv run rmsync md pull notes/post.md                              # after writing on the tablet
```

`push` makes the tablet match the file. `pull` rewrites the file from the tablet.
If both sides changed since the last sync, whichever command you run second refuses and
names the one to run first. `--force` overrides that when you know which side wins.
Leave the page on the tablet before pulling so it saves.

Markdown that syncs:

| you write | on the tablet |
|---|---|
| `# Heading` | heading (one level; `##` comes back as `#`) |
| plain lines, blank lines | paragraphs |
| `- item`, `  - nested` | bullets |
| `- [ ] todo`, `- [x] done` | checkboxes |
| `**whole line**` | bold paragraph |
| ```` ```typst ```` fenced block | rendered to ink (math, tables, anything Typst does) |
| `![alt](figure.svg)` | drawn as ink, scaled to the text width |
| `![alt](photo.png)` | labelled placeholder box (rasters can't become ink) |
| `---` on its own line | page break |

Inside a Typst fence, a first line of `// rmsync: style=outline` (or `thick`, `hatch3`,
`ballpoint`) changes how it's drawn. Default is `hatch`, filled glyphs.

What comes back on pull:

- Text you typed on the tablet, including new paragraphs and style changes.
- Drawings, as `![ink: N strokes](post.ink/p1-1-1168.svg)` lines after the paragraph they were
  drawn beside or over. `post.ink/` is regenerated on every pull.
- Pages you added on the tablet, after a `---`.

Ink drawn over a wrapped paragraph is placed as if on its first line; drawings beside text or
in empty space export exactly. Tablet text that looks like markdown (a line starting with `- `)
is read as markdown on pull.

## Path 2: Typst → PDF, annotations back

```sh
uv run rmsync pdf init paper.typ "Paper" --folder "Reading"   # compile, upload
uv run rmsync pdf push paper.typ                               # recompile and replace the PDF
uv run rmsync pdf pull paper.typ                               # export ink to paper.ink/p<N>.svg
```

`push` keeps each page's annotations attached to its page number; pages beyond the new page
count are dropped along with their ink. A ready-made `.pdf` can be given instead of a `.typ`.

`pull` writes, into `paper.ink/`:

- `p<N>.svg`, one per annotated page, in the tablet's screen space (`viewBox="-702 0 1404 1872"`)
- `p<N>.png`, the page rendered with the ink on top (Typst sources only)
- `overlay.typ`, a show rule that draws each page's ink as its background

and adds two lines to the top of your `.typ` the first time:

```typst
#import "paper.ink/overlay.typ": ink
#show: ink
```

So `typst compile paper.typ` (or your editor's preview) shows the annotations. `push` compiles
with `--input rmsync-ink=no`, which turns the overlay off, so the tablet never gets the ink baked
into the PDF underneath its own live annotations.

For a 1:1 overlay, size the Typst page like the screen:

```typst
#set page(width: 157.8mm, height: 210.4mm)   // 1404 x 1872 px at 226 dpi
```

## Tablet notes

Each push restarts the tablet UI once, which closes whatever is open on screen. Never restart
it more than three times in ten minutes by hand: the firmware reboots the device on the fourth
start.

## Layout

- `rmsync/cli.py` entry point; `rmsync/sync.py` the md path; `rmsync/pdfsync.py` the pdf path
- `rmsync/textcrdt.py` id-stable editing of the tablet's text CRDT (tests: `test_textcrdt.py`)
- `rmsync/typst_ink.py` Typst and SVG to strokes; `rmsync/ink_svg.py` strokes to SVG
- `rmsync/mdmodel.py` markdown model; `rmsync/manifest.py` SQLite bookkeeping (`sync.db`)
- `stage/` working copy of device files; `backup/xochitl/` full device backup (never touched)
- `RESEARCH.md` device-verified findings and test log; `rmsync/DESIGN.md` architecture brief

## Recovery

`backup/xochitl/` is a full copy of the tablet's document store from before any sync.
To restore one document, rsync its uuid-named files back into
`~/.local/share/remarkable/xochitl/` on the tablet and restart xochitl.
