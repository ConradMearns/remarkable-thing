"""Typst source -> reMarkable strokes (si.Line) via `typst compile --format svg` + svgelements.

Coordinates are group-local: x in 0..936 (typst pt == device unit at page width 936pt),
y from 0 at the top of the rendered block. Glyphs are emitted as outline strokes.
"""
import io
import re
import subprocess

from rmscene import scene_items as si
from rmscene.scene_items import Point, PenColor, Pen
from svgelements import SVG, Path, Shape, Rect, Line as SvgLine, Polyline, Polygon, Color

WIDTH = 936.0
LINE_HEIGHT = 35.74803        # device paragraph pitch (anchor_threshold, float32 35.74803161621094)
TEXT_SIZE_PT = 27.33          # measured: wrapped-line pitch = 35.748 at this size (Libertinus Serif)
TOLERANCE = 0.4               # RDP simplification tolerance, device units
PREAMBLE = (f"#set page(width: {WIDTH}pt, height: auto, margin: 0pt)\n"
            f"#set text(size: {TEXT_SIZE_PT}pt)\n")


def compile_svg(source: str) -> str:
    """Run typst on stdin; return SVG text. Raises RuntimeError with typst's stderr on failure."""
    r = subprocess.run(["typst", "compile", "--format", "svg", "-", "-"],
                       input=(PREAMBLE + source).encode(), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"typst failed:\n{r.stderr.decode()}")
    # Drop the pt-unit width/height on the root so svgelements keeps viewBox units 1:1 (no pt->px scaling).
    return re.sub(r'<svg([^>]*?) width="[^"]*" height="[^"]*"', r'<svg\1', r.stdout.decode(), count=1)


def _rdp(pts, eps):
    """Ramer-Douglas-Peucker on a list of (x, y)."""
    if len(pts) < 3:
        return pts
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = (dx * dx + dy * dy) ** 0.5
    best, idx = 0.0, 0
    for i in range(1, len(pts) - 1):
        x, y = pts[i]
        d = abs(dx * (y0 - y) - dy * (x0 - x)) / norm if norm else ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
        if d > best:
            best, idx = d, i
    if best <= eps:
        return [pts[0], pts[-1]]
    return _rdp(pts[:idx + 1], eps)[:-1] + _rdp(pts[idx:], eps)


def _is_visible(shape: Shape) -> bool:
    fill, stroke = shape.fill, shape.stroke
    filled = fill is not None and fill.value is not None and fill != Color("white")
    stroked = stroke is not None and stroke.value is not None and stroke != Color("white")
    return filled or stroked


def _shapes(svg_text: str):
    """Yield (path, filled) for every visible shape, <use>/<symbol> resolved by svgelements."""
    svg = SVG.parse(io.StringIO(svg_text), reify=True, ppi=72)
    for el in svg.elements():
        if not isinstance(el, Shape) or not _is_visible(el):
            continue
        path = el if isinstance(el, Path) else Path(el)
        path.reify()
        filled = el.fill is not None and el.fill.value is not None and el.fill != Color("white")
        yield path, filled


def _sample(sub: Path):
    length = sub.length(error=1e-2)
    n = max(2, min(400, int(length / 0.5)))       # ~2 samples per unit
    return [(p.x, p.y) for p in (sub.point(i / n) for i in range(n + 1)) if p is not None]


def svg_polylines(svg_text: str, tol: float = TOLERANCE, hatch: float | None = None):
    """Flatten an SVG into simplified polylines [[(x, y), ...], ...] in viewBox units.

    With `hatch`, filled shapes are additionally filled with horizontal scanlines `hatch` units apart
    (even-odd rule across all subpaths of the shape), so glyphs read as solid instead of hollow.
    """
    out = []
    for path, filled in _shapes(svg_text):
        polys = []
        for sub in path.as_subpaths():
            sub = Path(sub)
            if len(sub) < 2:
                continue
            pts = _sample(sub)
            polys.append(pts)
            simp = _rdp(pts, tol)
            if len(simp) >= 2:
                out.append(simp)
        if hatch and filled and polys:
            out += _scanlines(polys, hatch)
    return out


def _scanlines(polys, spacing):
    """Horizontal even-odd fill segments through a set of closed polygons."""
    ys = [y for poly in polys for _, y in poly]
    y0, y1 = min(ys), max(ys)
    segs = []
    y = y0 + spacing / 2
    while y < y1:
        xs = []
        for poly in polys:
            for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
                if (ay <= y) != (by <= y):
                    xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
        xs.sort()
        for a, b in zip(xs[::2], xs[1::2]):
            if b - a > 0.3:
                segs.append([(a, y), (b, y)])
        y += spacing
    return segs


# Rendering styles, selectable per block with a leading `// rmsync: style=<name>` comment in the source.
STYLES = {
    "outline":  dict(tool=Pen.FINELINER_2, thickness=1.0, width=8,  hatch=None),
    "thick":    dict(tool=Pen.FINELINER_2, thickness=2.0, width=12, hatch=None),
    "hatch":    dict(tool=Pen.FINELINER_2, thickness=1.0, width=8,  hatch=2.0),
    "hatch3":   dict(tool=Pen.FINELINER_2, thickness=2.0, width=12, hatch=3.0),
    "ballpoint": dict(tool=Pen.BALLPOINT_2, thickness=2.0, width=12, hatch=2.5),
}
DEFAULT_STYLE = "hatch"
_STYLE_RE = re.compile(r"^\s*//\s*rmsync:\s*style=(\w+)", re.M)


def _line(pts, st) -> si.Line:
    return si.Line(color=PenColor.BLACK, tool=st["tool"], thickness_scale=st["thickness"], starting_length=0.0,
                   points=[Point(x=float(x), y=float(y), speed=0, direction=0, width=st["width"], pressure=0)
                           for x, y in pts])


def render(source: str, style: str | None = None):
    """Typst source -> (lines, height, width). Lines are si.Line in group-local units."""
    m = _STYLE_RE.search(source)
    st = STYLES[style or (m.group(1) if m else DEFAULT_STYLE)]
    svg_text = compile_svg(source)
    svg = SVG.parse(io.StringIO(svg_text), ppi=72)
    width, height = float(svg.viewbox.width), float(svg.viewbox.height)
    lines = [_line(p, st) for p in svg_polylines(svg_text, hatch=st["hatch"])]
    return lines, height, width


def to_svg_preview(lines, path: str, width: float = WIDTH, height: float | None = None):
    """Write a plain SVG of the strokes for eyeballing."""
    if height is None:
        height = max((p.y for l in lines for p in l.points), default=0) + 1
    with open(path, "w") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
                f'width="{width}" height="{height}"><rect width="100%" height="100%" fill="white"/>\n')
        for l in lines:
            d = " ".join(f"{p.x:.2f},{p.y:.2f}" for p in l.points)
            f.write(f'<polyline points="{d}" fill="none" stroke="black" stroke-width="1" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>\n')
        f.write("</svg>\n")
