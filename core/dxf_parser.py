"""
core/dxf_parser.py
──────────────────
Parses DXF (and limited DWG) assembly drawings into the same
``ParsedPage`` / ``VectorShape`` / ``TextElement`` objects produced by
``pdf_parser.py``, so the entire polarity detection pipeline works
unchanged.

Supported input
───────────────
• DXF R12 – R2018 (all ASCII/binary variants via ``ezdxf``)
• DWG files are NOT directly supported (ezdxf cannot read DWG).
  Convert DWG → DXF first (e.g. with LibreCAD, ODA File Converter,
  or ``dwg2dxf`` CLI tool) then open the resulting DXF.

Layer → colour mapping
──────────────────────
KiCad DXF exports use layer **names** (``F.SilkS``, ``F.Cu`` …) and set
all entity colours to the layer colour, which matches the KiCad PDF
palette.  This parser synthesises the same (r, g, b) tuples used by the
PDF parser so that ``PolarityDetector``, ``PadAsymmetryDetector`` and
``ImagePolarityDetector`` all work without modification.

For Altium and other tools, a best-effort name-match is performed.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from utils.geometry import BoundingBox, Point
from core.pdf_parser import ParsedPage, TextElement, VectorShape

# ─────────────────────────────────────────────────────────────────────────────
# Layer name → synthetic PDF colour
# ─────────────────────────────────────────────────────────────────────────────

# These colours must match exactly what the rest of the pipeline expects
# (see pad_asymmetry_detector.py colour helpers and image_polarity_detector.py).
_LAYER_COLOR: Dict[str, Tuple[float, float, float]] = {
    # ── KiCad layer names ────────────────────────────────────────────────
    "F.Cu":        (0.52, 0.00, 0.00),   # front copper – red
    "B.Cu":        (0.00, 0.52, 0.00),   # back copper  – green (rare)
    "F.SilkS":     (0.52, 0.00, 0.52),   # front silk   – magenta
    "B.SilkS":     (0.52, 0.00, 0.52),
    "F.Fab":       (0.00, 0.00, 0.52),   # front fab    – blue
    "B.Fab":       (0.00, 0.00, 0.52),
    "F.CrtYd":     (0.00, 0.52, 0.52),   # front courtyard – cyan
    "B.CrtYd":     (0.00, 0.52, 0.52),
    "F.Paste":     (0.52, 0.52, 0.52),
    "B.Paste":     (0.52, 0.52, 0.52),
    "F.Mask":      (0.80, 0.00, 0.00),
    "B.Mask":      (0.80, 0.00, 0.00),
    "Edge.Cuts":   (0.80, 0.80, 0.00),
    "Eco1.User":   (0.00, 0.80, 0.00),
    "Eco2.User":   (0.00, 0.80, 0.00),
    "Dwgs.User":   (0.00, 0.00, 0.00),
    "Cmts.User":   (0.00, 0.00, 0.00),
    # ── Altium / common generic names ────────────────────────────────────
    "Top Layer":        (0.52, 0.00, 0.00),
    "Bottom Layer":     (0.00, 0.52, 0.00),
    "Top Silk Screen":  (0.52, 0.00, 0.52),
    "Top Silkscreen":   (0.52, 0.00, 0.52),
    "Bottom Silk Screen": (0.52, 0.00, 0.52),
    "Top Assembly":     (0.52, 0.00, 0.52),
    "Top Overlay":      (0.52, 0.00, 0.52),
    "Top Courtyard":    (0.00, 0.52, 0.52),
    "Top Room":         (0.00, 0.52, 0.52),
    "Mechanical 1":     (0.00, 0.00, 0.00),
    "Mechanical 15":    (0.00, 0.52, 0.52),
    # ── Fallback generic names ────────────────────────────────────────────
    "silk":          (0.52, 0.00, 0.52),
    "silkscreen":    (0.52, 0.00, 0.52),
    "assembly":      (0.52, 0.00, 0.52),
    "copper":        (0.52, 0.00, 0.00),
    "fab":           (0.00, 0.00, 0.52),
    "courtyard":     (0.00, 0.52, 0.52),
    "paste":         (0.52, 0.52, 0.52),
}

# DXF ACI colour index (1–7) → approximate (r, g, b) 0-1
_ACI_COLOR: Dict[int, Tuple[float, float, float]] = {
    1:  (1.0,  0.0,  0.0),   # red
    2:  (1.0,  1.0,  0.0),   # yellow
    3:  (0.0,  1.0,  0.0),   # green
    4:  (0.0,  1.0,  1.0),   # cyan
    5:  (0.0,  0.0,  1.0),   # blue
    6:  (1.0,  0.0,  1.0),   # magenta
    7:  (0.52, 0.52, 0.52),  # white/black → gray
    256: None,                # ByLayer
}

_DEFAULT_COLOR = (0.52, 0.52, 0.52)


def _layer_color(layer_name: str) -> Tuple[float, float, float]:
    """Return the synthetic colour for *layer_name*, or a grey fallback."""
    # Exact match
    c = _LAYER_COLOR.get(layer_name)
    if c:
        return c
    # Case-insensitive match
    ln = layer_name.lower().replace(" ", "").replace(".", "").replace("_", "")
    for k, v in _LAYER_COLOR.items():
        if k.lower().replace(" ", "").replace(".", "").replace("_", "") == ln:
            return v
    return _DEFAULT_COLOR


def _aci_color(aci: int) -> Optional[Tuple[float, float, float]]:
    return _ACI_COLOR.get(aci, _DEFAULT_COLOR)


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pt(val: float, scale: float) -> float:
    """Convert DXF drawing unit to PDF points."""
    return val * scale


# ─────────────────────────────────────────────────────────────────────────────
# Entity → VectorShape / TextElement converters
# ─────────────────────────────────────────────────────────────────────────────

def _entity_color(
    entity,
    layer_colors: Dict[str, Tuple],
) -> Optional[Tuple[float, float, float]]:
    """Resolve entity colour: entity RGB → ACI → layer → fallback."""
    try:
        # True-color override
        if hasattr(entity.dxf, "true_color") and entity.dxf.true_color is not None:
            tc = entity.dxf.true_color
            r = ((tc >> 16) & 0xFF) / 255.0
            g = ((tc >>  8) & 0xFF) / 255.0
            b = ( tc        & 0xFF) / 255.0
            return (r, g, b)
    except Exception:
        pass
    try:
        aci = entity.dxf.color
        if aci not in (0, 256):          # 0=ByBlock, 256=ByLayer
            return _aci_color(aci)
    except Exception:
        pass
    # Fall back to layer colour
    try:
        return layer_colors.get(entity.dxf.layer, _DEFAULT_COLOR)
    except Exception:
        return _DEFAULT_COLOR


def _line_to_shape(entity, page: int, s: float, color) -> Optional[VectorShape]:
    try:
        p1 = entity.dxf.start
        p2 = entity.dxf.end
        x0 = min(p1.x, p2.x) * s; x1 = max(p1.x, p2.x) * s
        y0 = min(p1.y, p2.y) * s; y1 = max(p1.y, p2.y) * s
        if x0 == x1: x1 += 0.01
        if y0 == y1: y1 += 0.01
        return VectorShape(
            shape_type="line",
            bbox=BoundingBox(x0, y0, x1, y1),
            page=page,
            points=[Point(p1.x * s, p1.y * s), Point(p2.x * s, p2.y * s)],
            stroke_width=max(0.5, entity.dxf.lineweight / 100.0 if hasattr(entity.dxf, "lineweight") and entity.dxf.lineweight > 0 else 0.5),
            is_filled=False,
            stroke_color=color,
        )
    except Exception:
        return None


def _circle_to_shape(entity, page: int, s: float, color) -> Optional[VectorShape]:
    try:
        c = entity.dxf.center
        r = entity.dxf.radius * s
        cx, cy = c.x * s, c.y * s
        return VectorShape(
            shape_type="circle",
            bbox=BoundingBox(cx - r, cy - r, cx + r, cy + r),
            page=page,
            points=[Point(cx, cy)],
            stroke_width=0.5,
            is_filled=False,
            stroke_color=color,
        )
    except Exception:
        return None


def _arc_to_shape(entity, page: int, s: float, color) -> Optional[VectorShape]:
    try:
        c = entity.dxf.center
        r = entity.dxf.radius * s
        cx, cy = c.x * s, c.y * s
        # Approximate arc bounding box using start/end angles
        start_a = math.radians(entity.dxf.start_angle)
        end_a   = math.radians(entity.dxf.end_angle)
        pts = [Point(cx + r * math.cos(a), cy + r * math.sin(a))
               for a in [start_a, end_a,
                          0, math.pi/2, math.pi, 3*math.pi/2]]
        xs = [p.x for p in pts]; ys = [p.y for p in pts]
        return VectorShape(
            shape_type="path",
            bbox=BoundingBox(min(xs), min(ys), max(xs), max(ys)),
            page=page,
            points=pts,
            stroke_width=0.5,
            is_filled=False,
            stroke_color=color,
        )
    except Exception:
        return None


def _lwpolyline_to_shapes(entity, page: int, s: float, color) -> List[VectorShape]:
    """Split an LWPOLYLINE into individual line/arc segments."""
    shapes = []
    try:
        pts_raw = list(entity.get_points())   # (x, y, start_width, end_width, bulge)
        if not pts_raw:
            return []
        is_closed = entity.is_closed
        n = len(pts_raw)
        for i in range(n):
            if i == n - 1 and not is_closed:
                break
            p1r = pts_raw[i]
            p2r = pts_raw[(i + 1) % n]
            x0 = min(p1r[0], p2r[0]) * s; x1 = max(p1r[0], p2r[0]) * s
            y0 = min(p1r[1], p2r[1]) * s; y1 = max(p1r[1], p2r[1]) * s
            if x0 == x1: x1 += 0.01
            if y0 == y1: y1 += 0.01
            shapes.append(VectorShape(
                shape_type="line",
                bbox=BoundingBox(x0, y0, x1, y1),
                page=page,
                points=[Point(p1r[0]*s, p1r[1]*s), Point(p2r[0]*s, p2r[1]*s)],
                stroke_width=0.5,
                is_filled=False,
                stroke_color=color,
            ))
    except Exception:
        pass
    return shapes


def _hatch_to_shape(entity, page: int, s: float, color) -> Optional[VectorShape]:
    """Convert a HATCH (filled polygon) to a filled VectorShape."""
    try:
        pts = []
        for path in entity.paths:
            for edge in path.edges if hasattr(path, "edges") else []:
                if hasattr(edge, "start"):
                    pts.append(Point(edge.start.x * s, edge.start.y * s))
            if hasattr(path, "vertices"):
                for v in path.vertices:
                    pts.append(Point(v[0] * s, v[1] * s))
        if not pts:
            return None
        xs = [p.x for p in pts]; ys = [p.y for p in pts]
        return VectorShape(
            shape_type="polyline",
            bbox=BoundingBox(min(xs), min(ys), max(xs), max(ys)),
            page=page,
            points=pts,
            stroke_width=0.0,
            is_filled=True,
            fill_color=color,
            stroke_color=None,
        )
    except Exception:
        return None


def _text_to_element(entity, page: int, s: float) -> Optional[TextElement]:
    """Convert TEXT or MTEXT to TextElement."""
    try:
        if entity.dxftype() == "MTEXT":
            text = entity.plain_mtext().strip()
            ip = entity.dxf.insert
            h  = entity.dxf.char_height * s
            x, y = ip.x * s, ip.y * s
        else:
            text = entity.dxf.text.strip()
            ip = entity.dxf.insert
            h  = entity.dxf.height * s
            x, y = ip.x * s, ip.y * s
        if not text:
            return None
        return TextElement(
            text=text,
            bbox=BoundingBox(x, y - h, x + len(text) * h * 0.6, y + h * 0.2),
            page=page,
            font_size=h,
            color=(0.0, 0.0, 0.0),
        )
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public parser class
# ─────────────────────────────────────────────────────────────────────────────

class DXFParser:
    """
    Parse a DXF file and return a list of ``ParsedPage`` objects.

    The coordinate system is flipped (DXF uses Y-up, PDF uses Y-down)
    by negating all Y coordinates and offsetting so all values are ≥ 0.

    Usage::

        parser = DXFParser("board.dxf")
        pages  = parser.parse()
    """

    # Units: DXF INSUNITS code → mm (then converted to pt)
    _INSUNITS_TO_MM: Dict[int, float] = {
        0:  1.0,     # Unitless – assume mm
        1:  25.4,    # Inches
        2:  304.8,   # Feet
        4:  1.0,     # Millimeters
        5:  10.0,    # Centimeters
        6:  1000.0,  # Meters
        13: 25.4,    # US Survey Feet
        14: 25.4,    # US Survey Inches
    }

    def __init__(self, dxf_path: str):
        self.dxf_path = dxf_path

    def parse(self) -> List[ParsedPage]:
        import ezdxf
        try:
            doc = ezdxf.readfile(self.dxf_path)
        except Exception as exc:
            raise ValueError(f"Cannot read DXF file: {self.dxf_path}\n{exc}")

        # Determine scale: DXF drawing units → PDF points
        try:
            insunits = doc.header.get("$INSUNITS", 4)  # default=mm
        except Exception:
            insunits = 4
        mm_per_unit = self._INSUNITS_TO_MM.get(int(insunits), 1.0)
        scale = mm_per_unit * MM_TO_PT  # drawing_unit → pt

        # Resolve layer colours
        layer_colors: Dict[str, Tuple] = {}
        try:
            for layer in doc.layers:
                layer_colors[layer.dxf.name] = _layer_color(layer.dxf.name)
        except Exception:
            pass

        # Process modelspace (DXF has a single flat model space)
        msp = doc.modelspace()
        shapes: List[VectorShape] = []
        texts: List[TextElement] = []

        self._process_entities(msp, 0, scale, layer_colors, shapes, texts)

        # Also process any inserted blocks (INSERT entities)
        for entity in msp.query("INSERT"):
            try:
                block = doc.blocks[entity.dxf.block_name]
                self._process_entities(block, 0, scale, layer_colors, shapes, texts)
            except Exception:
                pass

        # Flip Y axis (DXF Y-up → PDF Y-down) so polarity detection works
        if shapes or texts:
            all_y = (
                [s.bbox.y0 for s in shapes] +
                [s.bbox.y1 for s in shapes] +
                [t.bbox.y0 for t in texts]  +
                [t.bbox.y1 for t in texts]
            )
            max_y = max(all_y) if all_y else 0.0
            shapes = [self._flip_shape(s, max_y) for s in shapes]
            texts  = [self._flip_text(t, max_y)  for t in texts]

        # Estimate page bounds
        if shapes:
            xs = [s.bbox.x0 for s in shapes] + [s.bbox.x1 for s in shapes]
            ys = [s.bbox.y0 for s in shapes] + [s.bbox.y1 for s in shapes]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
        else:
            w, h = 595.0, 842.0   # A4 fallback

        page = ParsedPage(page_index=0, width_pt=w, height_pt=h)
        page.shapes = shapes
        page.texts  = texts
        return [page]

    # ── Entity processing ─────────────────────────────────────────────────

    def _process_entities(
        self,
        space,
        page: int,
        s: float,
        layer_colors: Dict[str, Tuple],
        shapes: List[VectorShape],
        texts: List[TextElement],
    ) -> None:
        for entity in space:
            color = _entity_color(entity, layer_colors)
            etype = entity.dxftype()

            if etype == "LINE":
                sh = _line_to_shape(entity, page, s, color)
                if sh:
                    shapes.append(sh)

            elif etype == "CIRCLE":
                sh = _circle_to_shape(entity, page, s, color)
                if sh:
                    shapes.append(sh)

            elif etype == "ARC":
                sh = _arc_to_shape(entity, page, s, color)
                if sh:
                    shapes.append(sh)

            elif etype == "LWPOLYLINE":
                for sh in _lwpolyline_to_shapes(entity, page, s, color):
                    shapes.append(sh)

            elif etype == "POLYLINE":
                try:
                    pts_raw = [v.dxf.location for v in entity.vertices]
                    for i in range(len(pts_raw) - 1):
                        p1, p2 = pts_raw[i], pts_raw[i+1]
                        x0 = min(p1.x, p2.x)*s; x1 = max(p1.x, p2.x)*s
                        y0 = min(p1.y, p2.y)*s; y1 = max(p1.y, p2.y)*s
                        if x0 == x1: x1 += 0.01
                        if y0 == y1: y1 += 0.01
                        shapes.append(VectorShape(
                            shape_type="line",
                            bbox=BoundingBox(x0, y0, x1, y1),
                            page=page,
                            points=[Point(p1.x*s, p1.y*s), Point(p2.x*s, p2.y*s)],
                            stroke_width=0.5, is_filled=False, stroke_color=color,
                        ))
                except Exception:
                    pass

            elif etype == "HATCH":
                sh = _hatch_to_shape(entity, page, s, color)
                if sh:
                    shapes.append(sh)

            elif etype == "SOLID":
                try:
                    pts = [Point(entity.dxf.vtx0.x*s, entity.dxf.vtx0.y*s),
                           Point(entity.dxf.vtx1.x*s, entity.dxf.vtx1.y*s),
                           Point(entity.dxf.vtx2.x*s, entity.dxf.vtx2.y*s),
                           Point(entity.dxf.vtx3.x*s, entity.dxf.vtx3.y*s)]
                    xs = [p.x for p in pts]; ys = [p.y for p in pts]
                    shapes.append(VectorShape(
                        shape_type="polyline",
                        bbox=BoundingBox(min(xs), min(ys), max(xs), max(ys)),
                        page=page, points=pts,
                        stroke_width=0.0, is_filled=True,
                        fill_color=color, stroke_color=None,
                    ))
                except Exception:
                    pass

            elif etype in ("TEXT", "MTEXT"):
                te = _text_to_element(entity, page, s)
                if te:
                    texts.append(te)

    # ── Y-flip helpers ────────────────────────────────────────────────────

    @staticmethod
    def _flip_shape(s: VectorShape, max_y: float) -> VectorShape:
        new_y0 = max_y - s.bbox.y1
        new_y1 = max_y - s.bbox.y0
        new_pts = [Point(p.x, max_y - p.y) for p in s.points]
        return VectorShape(
            shape_type=s.shape_type,
            bbox=BoundingBox(s.bbox.x0, new_y0, s.bbox.x1, new_y1),
            page=s.page,
            points=new_pts,
            stroke_width=s.stroke_width,
            is_filled=s.is_filled,
            fill_color=s.fill_color,
            stroke_color=s.stroke_color,
        )

    @staticmethod
    def _flip_text(t: TextElement, max_y: float) -> TextElement:
        new_y0 = max_y - t.bbox.y1
        new_y1 = max_y - t.bbox.y0
        return TextElement(
            text=t.text,
            bbox=BoundingBox(t.bbox.x0, new_y0, t.bbox.x1, new_y1),
            page=t.page,
            font_size=t.font_size,
            color=t.color,
        )


# Convenience
def parse_dxf(path: str) -> List[ParsedPage]:
    """Shorthand: ``parse_dxf(path)`` → List[ParsedPage]."""
    return DXFParser(path).parse()


# ─────────────────────────────────────────────────────────────────────────────
# mm → pt (reused from odb_parser, but defined here too for standalone use)
# ─────────────────────────────────────────────────────────────────────────────
MM_TO_PT: float = 72.0 / 25.4

