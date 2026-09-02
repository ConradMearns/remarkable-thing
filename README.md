# rmsync

Two-way sync between markdown files on this laptop and notebooks on a rooted reMarkable 2.
Text you write in markdown becomes native, editable text on the tablet. Typst blocks and SVG
images become ink. Anything you draw or type on the tablet comes back into the markdown, with
drawings exported as SVG files you can use in a blog.

## Setup

- Tablet plugged in over USB (it appears at `10.11.99.1`). Credentials live in `.env`;
  key auth is installed, so plain `ssh rm` works.
- Python env: `.venv/` (rmscene, rmc, svgelements, cairosvg). Typst is the snap `typst`.
- Keep the tablet awake while syncing. Auto-sleep drops the USB link.

## Daily use

Every markdown file is one notebook. Register it once, then push and pull as you go.

```sh
# 1. Register a file as a notebook (optionally inside a folder on the tablet)
.venv/bin/python -m rmsync.sync init notes/post.md "My post" --folder "Blog"

# 2. After editing on the laptop
.venv/bin/python -m rmsync.sync push notes/post.md

# 3. After writing or drawing on the tablet (leave the page first so it saves)
.venv/bin/python -m rmsync.sync pull notes/post.md
```

`push` makes the tablet match the file. `pull` rewrites the file from the tablet.
If both sides changed since the last sync, whichever command you run second refuses and
tells you to run the other one first. `--force` overrides that when you know which side wins.

Each push restarts the tablet UI once, which closes whatever is open on screen.
Never restart it more than three times in ten minutes by hand: the firmware reboots the
device on the fourth start.

## Markdown that syncs

| You write | On the tablet |
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

## What comes back on pull

- Text you typed on the tablet, including new paragraphs and style changes.
- Drawings, as `![ink: N strokes](post.ink/p1-1-1168.svg)` lines placed after the paragraph
  they were drawn beside or over. The `post.ink/` folder is regenerated on every pull, so
  edit the SVGs elsewhere if you want to keep changes.
- Pages you added on the tablet, after a `---`.

Ink drawn over a wrapped paragraph is placed as if on its first line; drawings beside text or
in empty space export exactly. Text typed on the tablet that looks like markdown, such as a
line starting with `- `, is interpreted as markdown on pull.

## Layout

- `rmsync/sync.py` CLI and the document/page sync logic
- `rmsync/textcrdt.py` id-stable editing of the tablet's text CRDT (tests in `test_textcrdt.py`)
- `rmsync/typst_ink.py` Typst and SVG to strokes; `rmsync/ink_svg.py` strokes to SVG
- `rmsync/mdmodel.py` markdown model; `rmsync/manifest.py` the SQLite bookkeeping (`sync.db`)
- `stage/` working copy of device files, `backup/xochitl/` full device backup (never touched)
- `RESEARCH.md` the device-verified findings and test log; `rmsync/DESIGN.md` the architecture brief

## Recovery

`backup/xochitl/` is a full copy of the tablet's document store from before any sync.
To restore one document, rsync its uuid-named files back into
`~/.local/share/remarkable/xochitl/` on the tablet and restart xochitl.
