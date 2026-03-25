"""
core/pdf_parser.py
──────────────────
Extracts text elements and vector shapes from a PDF using PyMuPDF (fitz).

All coordinates are kept in PDF points (72 pt/inch), top-left origin,
matching PyMuPDF's own convention so no coordinate flip is needed.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

from utils.geometry import BoundingBox, Point, bbox_from_fitz_rect


# ─────────────────────────────────────────────────────────────────────────────
# Data transfer objects
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TextElement:
    """A single text span extracted from a PDF page."""
    text: str
    bbox: BoundingBox
    page: int
    font_size: float = 0.0
    color: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # normalised RGB 0–1


@dataclass
class VectorShape:
    """
    A single drawing path extracted from a PDF page via page.get_drawings().

    shape_type values:
        "line"          – single straight segment
        "rect"          – single rectangle (not filled)
        "filled_rect"   – single filled rectangle
        "circle"        – closed Bezier approximation of a circle (not filled)
        "filled_circle" – same, but filled
        "polyline"      – closed polygon made of straight segments
        "path"          – general mixed/open path
    """
    shape_type: str
    bbox: BoundingBox
    page: int
    points: List[Point] = field(default_factory=list)
    stroke_width: float = 1.0
    is_filled: bool = False
    fill_color: Optional[Tuple] = None
    stroke_color: Optional[Tuple] = None


@dataclass
class ParsedPage:
    """All extracted data for one PDF page."""
    page_index: int          # 0-based
    width_pt: float
    height_pt: float
    texts: List[TextElement] = field(default_factory=list)
    shapes: List[VectorShape] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

class PDFParser:
    """
    Opens a PDF and extracts structured text + vector shape data.

    Usage::
        parser = PDFParser("board.pdf")
        pages = parser.parse()
        parser.close()
    """

    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self._doc: Optional[fitz.Document] = None

    # ── Public API ────────────────────────────────────────────────────────

    def parse(self) -> List[ParsedPage]:
        """Parse all pages and return a list of ParsedPage objects."""
        self._doc = fitz.open(self.pdf_path)
        pages: List[ParsedPage] = []
        for idx in range(len(self._doc)):
            page = self._doc[idx]
            pages.append(self._parse_page(page, idx))
        return pages

    def close(self) -> None:
        if self._doc:
            self._doc.close()
            self._doc = None

    # ── Per-page extraction ───────────────────────────────────────────────

    def _parse_page(self, page: fitz.Page, idx: int) -> ParsedPage:
        parsed = ParsedPage(
            page_index=idx,
            width_pt=page.rect.width,
            height_pt=page.rect.height,
        )
        parsed.texts = self._extract_texts(page, idx)
        parsed.shapes = self._extract_shapes(page, idx)
        return parsed

    # ── Text extraction ───────────────────────────────────────────────────

    def _extract_texts(self, page: fitz.Page, page_idx: int) -> List[TextElement]:
        """
        Extract every non-empty text span with its bounding box.
        PyMuPDF's "dict" mode gives us block → line → span granularity.
        """
        elements: List[TextElement] = []
        data = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in data.get("blocks", []):
            if block.get("type") != 0:  # 0 = text, 1 = image
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue
                    raw_bbox = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    bbox = BoundingBox(*raw_bbox)
                    # Span color is a packed 24-bit integer (0xRRGGBB)
                    color_int = span.get("color", 0)
                    color = self._unpack_color(color_int)
                    elements.append(TextElement(
                        text=text,
                        bbox=bbox,
                        page=page_idx,
                        font_size=span.get("size", 0.0),
                        color=color,
                    ))
        return elements

    @staticmethod
    def _unpack_color(packed: int) -> Tuple[float, float, float]:
        """Convert a packed 0xRRGGBB integer to a normalised (r, g, b) tuple."""
        r = ((packed >> 16) & 0xFF) / 255.0
        g = ((packed >> 8) & 0xFF) / 255.0
        b = (packed & 0xFF) / 255.0
        return (r, g, b)

    # ── Shape extraction ──────────────────────────────────────────────────

    def _extract_shapes(self, page: fitz.Page, page_idx: int) -> List[VectorShape]:
        """
        Extract vector paths via page.get_drawings().

        get_drawings() decomposes the page content stream into individual
        drawing commands. Each returned dict represents one complete path.
        """
        shapes: List[VectorShape] = []
        seen: set = set()  # deduplicate near-identical paths

        for drawing in page.get_drawings():
            shape = self._parse_drawing(drawing, page_idx)
            if shape is None:
                continue
            # Deduplication key: type + rounded bbox corners
            key = (
                shape.shape_type,
                round(shape.bbox.x0, 1), round(shape.bbox.y0, 1),
                round(shape.bbox.x1, 1), round(shape.bbox.y1, 1),
                shape.is_filled,
            )
            if key in seen:
                continue
            seen.add(key)
            shapes.append(shape)

        return shapes

    def _parse_drawing(self, d: Dict, page_idx: int) -> Optional[VectorShape]:
        """Convert a raw PyMuPDF drawing dict into a VectorShape."""
        rect = d.get("rect")
        if rect is None:
            return None

        bbox = bbox_from_fitz_rect(rect)
        if not bbox.is_valid:
            return None

        items = d.get("items", [])
        if not items:
            return None

        # Collect all explicit points from items
        points = self._collect_points(items)

        fill = d.get("fill")        # None if not filled
        color = d.get("color")      # stroke color; None = no stroke
        stroke_width = d.get("width") or 1.0
        is_filled = fill is not None

        shape_type = self._classify(d, bbox, items, is_filled)

        return VectorShape(
            shape_type=shape_type,
            bbox=bbox,
            page=page_idx,
            points=points,
            stroke_width=float(stroke_width),
            is_filled=is_filled,
            fill_color=fill,
            stroke_color=color,
        )

    @staticmethod
    def _collect_points(items: List) -> List[Point]:
        """Flatten drawing items into a list of Point objects."""
        pts: List[Point] = []
        for item in items:
            kind = item[0]
            if kind == "l":               # ('l', p1, p2) – line
                pts.append(Point(item[1].x, item[1].y))
                pts.append(Point(item[2].x, item[2].y))
            elif kind == "re":            # ('re', rect) – rectangle
                r = item[1]
                pts += [
                    Point(r.x0, r.y0), Point(r.x1, r.y0),
                    Point(r.x1, r.y1), Point(r.x0, r.y1),
                ]
            elif kind == "c":             # ('c', p1, cp1, cp2, p2) – cubic Bézier
                # Only add endpoints for polygon approximation
                pts.append(Point(item[1].x, item[1].y))
                pts.append(Point(item[4].x, item[4].y))
            elif kind == "qu":            # ('qu', quad) – quadrilateral
                q = item[1]
                for corner in (q.ul, q.ur, q.lr, q.ll):
                    pts.append(Point(corner.x, corner.y))
        return pts

    @staticmethod
    def _classify(d: Dict, bbox: BoundingBox, items: List, is_filled: bool) -> str:
        """
        Heuristically determine the shape type from the drawing dictionary.
        """
        kinds = [item[0] for item in items]
        all_lines = all(k == "l" for k in kinds)
        all_curves = all(k == "c" for k in kinds)
        has_curves = "c" in kinds
        only_rect = kinds == ["re"]
        close_path = d.get("closePath", False)

        # ── Single rectangle ──────────────────────────────────────────────
        if only_rect:
            return "filled_rect" if is_filled else "rect"

        # ── Single straight line ──────────────────────────────────────────
        if kinds == ["l"]:
            return "line"

        # ── Circle / ellipse approximation ───────────────────────────────
        # EDA tools (KiCad, Altium) draw circles as 4 cubic Bézier segments.
        # IMPORTANT: KiCad does NOT set closePath=True on its bezier circles,
        # so we intentionally skip the closePath check here.
        # Require at least 4 bezier items to avoid false-matching short arcs.
        if all_curves and len(items) >= 4:
            w, h = bbox.width, bbox.height
            if w > 0 and h > 0 and abs(w / h - 1.0) < 0.25:
                return "filled_circle" if is_filled else "circle"

        # ── Closed polygon (e.g. triangle, diamond) ───────────────────────
        if all_lines and close_path:
            return "polyline"

        # ── Mixed or open path ────────────────────────────────────────────
        return "path"



