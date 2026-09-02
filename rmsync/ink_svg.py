"""Render device ink groups (si.Group with strokes) to a cropped SVG file for use in markdown."""
from rmc.exporters.writing_tools import Pen
from rmscene import scene_items as si

PAD = 12.0


def _abs_points(group: si.Group, anchor_y: float):
    """Strokes of one anchored group in page coordinates: x += anchor_origin_x, y += anchor_y."""
    ox = group.anchor_origin_x.value if group.anchor_origin_x else 0.0
    for child in group.children.values():
        if isinstance(child, si.Line) and child.points:
            yield child, [(p.x + ox, p.y + anchor_y, p) for p in child.points]
        elif isinstance(child, si.Group):
            yield from _abs_points(child, anchor_y)


def render_groups(groups, path: str, line_y=None, viewbox=None) -> int:
    """Write the strokes of `groups` (list of si.Group anchored to the same paragraph) to `path`.
    `line_y` optionally maps a group's anchor id to a y offset (wrapped-line position); default 0.
    Returns the number of strokes written."""
    strokes = []
    for g in groups:
        ay = (line_y or {}).get(g.anchor_id.value, 0.0) if g.anchor_id else 0.0
        strokes += list(_abs_points(g, ay))
    if not strokes:
        return 0
    xs = [x for _, pts in strokes for x, _, _ in pts]
    ys = [y for _, pts in strokes for _, y, _ in pts]
    if viewbox:                                   # fixed page space (PDF overlays)
        x0, y0, w, h = viewbox
    else:                                         # crop to the ink
        x0, y0 = min(xs) - PAD, min(ys) - PAD
        w, h = max(xs) - min(xs) + 2 * PAD, max(ys) - min(ys) + 2 * PAD
    with open(path, "w") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.1f} {y0:.1f} {w:.1f} {h:.1f}" '
                f'width="{w:.0f}" height="{h:.0f}">\n')
        for line, pts in strokes:
            pen = Pen.create(line.tool.value, line.color.value, line.thickness_scale)
            last_w = 0
            seg = []
            for i, (x, y, p) in enumerate(pts):
                if i % pen.segment_length == 0:
                    if seg:
                        f.write(_polyline(seg, color, width, opacity, pen))
                        seg = [seg[-1]]
                    # rmc shades ballpoint segments gray from pressure, but the rM2 reports pressure 0 and
                    # draws solid ink; keep the pen's base colour and let width carry the variation.
                    color = "rgb" + str(tuple(pen.base_color))
                    width = pen.get_segment_width(p.speed, p.direction, p.width, p.pressure, last_w)
                    opacity = pen.get_segment_opacity(p.speed, p.direction, p.width, p.pressure, last_w)
                    last_w = width
                seg.append((x, y))
            if seg:
                f.write(_polyline(seg, color, width, opacity, pen))
        f.write("</svg>\n")
    return len(strokes)


def _polyline(seg, color, width, opacity, pen) -> str:
    d = " ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
    return (f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="{width:.2f}" '
            f'opacity="{opacity}" stroke-linecap="{pen.stroke_linecap}" stroke-linejoin="round"/>\n')
