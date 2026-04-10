"""
core/polarity_detector.py
─────────────────────────
Heuristic-based detection of SMT polarity markers from PDF vector data.

Six independent rules are applied; each yields zero or more PolarityMarker
objects. Rules are intentionally conservative to keep false-positive rates low.

Rule overview
─────────────
1. plus_text      – literal "+" text character
2. polarity_text  – "−", "A" / "K" (anode/cathode) labels
3. filled_dot     – small filled circle (dot marker near capacitor/diode)
4. cathode_band   – narrow filled rectangle = cathode band on diode
5. thick_line     – short thick stroke = cathode bar on diode
6. triangle       – closed 3-vertex polygon
7. cross_vector   – two perpendicular lines sharing a midpoint (vector "+")
8. corner_rect    – tiny filled rectangle = IC pin-1 corner mark

Layer-color filtering
─────────────────────
KiCad (and other EDA tools) export PCB layers in distinct colours.  Shapes on
copper, courtyard, via-drill and fab layers should NEVER be treated as polarity
indicators — only silkscreen-layer shapes qualify.  The helper
``_is_pcb_layer_color`` detects and rejects such colours:

  * Copper / solder (red-dominant):  R > G×2  and  R > B×2  and  R > 0.15
  * Via / drill (neutral mid-gray):  |R−G|<0.12, |G−B|<0.12, 0.20 < R < 0.85
  * Courtyard / back-copper (cyan):  G > 0.30 and B > 0.30 and R < 0.20
  * Fab layer (blue):                B > 0.30 and R < 0.20 and G < 0.30

Silkscreen (black 0,0,0 / white 1,1,1 / magenta 0.52,0,0.52 …) passes the
filter and is accepted as a valid polarity-marker layer.
"""
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from utils.geometry import (
    BoundingBox, Point,
    polygon_area, polyline_perimeter, circularity,
    deduplicate_points, is_approximately_square,
)
from utils.config import Config, DEFAULT_CONFIG
from core.pdf_parser import TextElement, VectorShape


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

# Human-readable descriptions for each marker type
MARKER_DESCRIPTIONS = {
    "plus_text":      "Plus sign (+) text character",
    "minus_text":     "Minus sign (−) text character",
    "anode_text":     "Anode label (A)",
    "cathode_text":   "Cathode label (K)",
    "filled_dot":     "Small filled circle (dot marker)",
    "cathode_band":   "Narrow filled band (cathode band)",
    "thick_line":     "Short thick stroke (cathode bar)",
    "triangle":       "Triangle shape",
    "cross_vector":   "Vector-drawn cross / plus sign",
    "corner_rect":    "Small corner rectangle (IC pin-1 mark)",
    "pad_asymmetry":  "Rounded copper pad (polarity/pin-1 via pad shape)",
    "fab_pin1_notch": "F.Fab layer pin-1 notch/dot on IC body",
}


@dataclass
class PolarityMarker:
    """A single detected polarity indicator."""
    marker_type: str       # key from MARKER_DESCRIPTIONS
    bbox: BoundingBox
    center: Point
    page: int
    confidence: float = 1.0   # 0.0 – 1.0
    source: str = "shape"     # "text" or "shape"
    detection_method: str = ""  # "silk"|"net_gnd"|"net_vcc"|"pin_name"|"pin1"|"fallback"|""


# ─────────────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────────────

class PolarityDetector:
    """
    Applies all heuristic rules to text elements and vector shapes.

    Usage::
        detector = PolarityDetector(config=Config())
        markers = detector.detect(text_elements, shapes)
    """

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config

    # ─────────────────────────────────────────────────────────────────────
    # Layer-colour filter
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_pcb_layer_color(color: Optional[Tuple]) -> bool:
        """Return True if *color* looks like a non-silkscreen PCB layer.

        Shapes on copper, via/drill, courtyard or fab layers are NOT polarity
        markers and must be rejected regardless of their geometry.

        Accepted (silkscreen-like) colours pass the filter:
          black  (0,0,0), white (1,1,1), magenta (0.52,0,0.52), etc.
        Rejected (PCB-layer) colours:
          copper / solder  – R-dominant:  R > G×2 and R > B×2 and R > 0.15
          via / drill gray – neutral mid: all channels equal ±0.12, 0.20<R<0.85
          courtyard cyan   – G and B high, R low: G>0.30, B>0.30, R<0.20
          fab blue         – B dominant: B>0.30, R<0.20, G<0.30
        """
        if not color:
            return False
        r, g, b = float(color[0]), float(color[1]), float(color[2])

        # Copper / solder mask (red-dominant)
        if r > 0.15 and r > g * 2.0 and r > b * 2.0:
            return True

        # Via / drill gray (all channels similar, mid-range)
        if (abs(r - g) < 0.12 and abs(g - b) < 0.12 and
                abs(r - b) < 0.12 and 0.20 < r < 0.85):
            return True

        # Courtyard / back-copper (cyan / teal)
        if g > 0.30 and b > 0.30 and r < 0.20:
            return True

        # Fab layer (blue-dominant, not magenta)
        if b > 0.30 and r < 0.20 and g < 0.30:
            return True

        return False

    def detect(
        self,
        text_elements: List[TextElement],
        shapes: List[VectorShape],
    ) -> List[PolarityMarker]:
        """Run all rules and return the combined list of markers."""
        markers: List[PolarityMarker] = []
        markers += self._rule_text_plus(text_elements)
        markers += self._rule_text_polarity_labels(text_elements)
        markers += self._rule_filled_dot(shapes)
        markers += self._rule_cathode_band(shapes)
        markers += self._rule_thick_line(shapes)
        markers += self._rule_triangle(shapes)
        markers += self._rule_cross_vector(shapes)
        markers += self._rule_corner_rect(shapes)
        return markers

    # ─────────────────────────────────────────────────────────────────────
    # Rule 1 – Literal "+" text
    # ─────────────────────────────────────────────────────────────────────

    def _rule_text_plus(self, texts: List[TextElement]) -> List[PolarityMarker]:
        """Detect a standalone '+' text character.

        Font-size guard: PCB silkscreen "+" labels are tiny (≤5 pt).
        Larger "+" characters are likely part of title/version strings.
        """
        result = []
        for elem in texts:
            if elem.text.strip() in ("+", "＋", "(+)"):
                if elem.font_size > 5.0:
                    continue   # skip title-bar "KiCad + KiBot" style text
                result.append(PolarityMarker(
                    marker_type="plus_text",
                    bbox=elem.bbox,
                    center=elem.bbox.center,
                    page=elem.page,
                    confidence=0.95,
                    source="text",
                ))
        return result

    # ─────────────────────────────────────────────────────────────────────
    # Rule 2 – Polarity text labels (−, A, K)
    # ─────────────────────────────────────────────────────────────────────

    def _rule_text_polarity_labels(self, texts: List[TextElement]) -> List[PolarityMarker]:
        """Detect polarity label text: '−', 'A' (anode), 'K' (cathode).

        Font-size guards prevent false-positives from:
          • Drawing-border grid letters  (A/B/C/D at size ~4.9 pt)
          • Title/table header text
        Genuine PCB silkscreen polarity labels are tiny: ≤3.5 pt.
        """
        MINUS_CHARS = {"-", "−", "–", "(-)"}
        # Maximum font-size accepted as a polarity label (pt).
        # KiCad silkscreen reference text is typically 1.0–2.5 pt.
        MAX_POLARITY_FONT_SIZE = 3.5
        result = []
        for elem in texts:
            t = elem.text.strip()
            if t in MINUS_CHARS:
                if elem.font_size <= MAX_POLARITY_FONT_SIZE:
                    result.append(PolarityMarker(
                        marker_type="minus_text",
                        bbox=elem.bbox,
                        center=elem.bbox.center,
                        page=elem.page,
                        confidence=0.75,
                        source="text",
                    ))
            elif t == "A":
                if elem.font_size <= MAX_POLARITY_FONT_SIZE:
                    result.append(PolarityMarker(
                        marker_type="anode_text",
                        bbox=elem.bbox,
                        center=elem.bbox.center,
                        page=elem.page,
                        confidence=0.65,
                        source="text",
                    ))
            elif t == "K":
                if elem.font_size <= MAX_POLARITY_FONT_SIZE:
                    result.append(PolarityMarker(
                        marker_type="cathode_text",
                        bbox=elem.bbox,
                        center=elem.bbox.center,
                        page=elem.page,
                        confidence=0.65,
                        source="text",
                    ))
        return result

    # ─────────────────────────────────────────────────────────────────────
    # Rule 3 – Filled dot OR outline circle (small dot / via marker)
    # ─────────────────────────────────────────────────────────────────────

    def _rule_filled_dot(self, shapes: List[VectorShape]) -> List[PolarityMarker]:
        """
        A polarity dot is a small circle — filled or unfilled.

        KiCad exports:
          • Filled polarity dots   → filled_circle (fill is set)
          • Outline polarity marks → circle        (fill=None, closePath=False)

        Color guard: circles on copper, via/drill, courtyard, or fab layers are
        SMD pads / annular rings, NOT polarity markers.  We reject those using
        the layer-colour filter for BOTH filled and unfilled circles.
        """
        result = []
        for shape in shapes:
            if shape.shape_type not in ("filled_circle", "circle"):
                continue
            w, h = shape.bbox.width, shape.bbox.height
            if w <= 0 or h <= 0:
                continue

            # ── Color guard (filled circles) ──────────────────────────────
            # Reject circles whose fill colour looks like a PCB layer
            # (copper pad, via hole, courtyard ring, fab outline).
            if shape.is_filled and self._is_pcb_layer_color(shape.fill_color):
                continue

            # ── Color guard (outline circles) ────────────────────────────
            # Non-filled circles that are clearly not in silkscreen colour
            # (black/white) are component outlines / courtyard rings.
            if not shape.is_filled and shape.stroke_color is not None:
                if self._is_pcb_layer_color(shape.stroke_color):
                    continue
                sr = shape.stroke_color[0]
                sg = shape.stroke_color[1]
                sb = shape.stroke_color[2]
                near_black = sr < 0.20 and sg < 0.20 and sb < 0.20
                near_white = sr > 0.80 and sg > 0.80 and sb > 0.80
                if not (near_black or near_white):
                    continue   # still reject if not clearly silkscreen-like

            # Approx radius from average of half-dimensions
            r = (w + h) / 4.0
            if not (self.cfg.min_dot_radius <= r <= self.cfg.max_dot_radius):
                continue
            if not is_approximately_square(shape.bbox, tolerance=0.35):
                continue
            pts = deduplicate_points(shape.points)
            if len(pts) >= 3:
                area = polygon_area(pts)
                perim = polyline_perimeter(pts)
                circ = circularity(area, perim)
            else:
                area = math.pi * (w / 2) * (h / 2)
                perim = math.pi * (3 * (w + h) / 2 - math.sqrt(3 * w * h))
                circ = circularity(area, perim)

            if circ < self.cfg.circularity_threshold:
                continue

            conf = 0.90 if shape.is_filled else 0.70
            result.append(PolarityMarker(
                marker_type="filled_dot",
                bbox=shape.bbox,
                center=shape.bbox.center,
                page=shape.page,
                confidence=conf,
                source="shape",
            ))
        return result

    # ─────────────────────────────────────────────────────────────────────
    # Rule 4 – Cathode band (narrow filled rectangle)
    # ─────────────────────────────────────────────────────────────────────

    def _rule_cathode_band(self, shapes: List[VectorShape]) -> List[PolarityMarker]:
        """
        A cathode band is a narrow filled rectangle:
          - filled_rect shape type
          - one dimension ≤ band_max_width pt
          - other dimension ≥ band_min_length pt
        Common on diode silkscreen outlines.
        """
        result = []
        for shape in shapes:
            if shape.shape_type != "filled_rect":
                continue
            if not shape.is_filled:
                continue
            # Reject copper/via/courtyard fills — genuine cathode bands are on
            # the silkscreen layer, never on the copper or courtyard layers.
            if self._is_pcb_layer_color(shape.fill_color):
                continue
            w, h = shape.bbox.width, shape.bbox.height
            if w <= 0 or h <= 0:
                continue
            short = min(w, h)
            long_ = max(w, h)
            if short <= self.cfg.band_max_width and long_ >= self.cfg.band_min_length:
                result.append(PolarityMarker(
                    marker_type="cathode_band",
                    bbox=shape.bbox,
                    center=shape.bbox.center,
                    page=shape.page,
                    confidence=0.85,
                    source="shape",
                ))
        return result

    # ─────────────────────────────────────────────────────────────────────
    # Rule 5 – Thick line (cathode bar)
    # ─────────────────────────────────────────────────────────────────────

    def _rule_thick_line(self, shapes: List[VectorShape]) -> List[PolarityMarker]:
        """
        A thick line segment whose stroke_width ≥ thick_line_min_stroke.
        Orientation does not matter; the thickness is the key indicator.
        Excludes very long lines that are likely component outline borders.
        Also excludes lines on copper / courtyard / fab layers — genuine
        cathode bars live on the silkscreen layer.
        """
        result = []
        MAX_LINE_LENGTH = 40.0
        for shape in shapes:
            if shape.shape_type != "line":
                continue
            if shape.stroke_width < self.cfg.thick_line_min_stroke:
                continue
            # Reject copper / courtyard / via layer strokes
            if self._is_pcb_layer_color(shape.stroke_color):
                continue
            length = max(shape.bbox.width, shape.bbox.height)
            if not (2.0 <= length <= MAX_LINE_LENGTH):
                continue
            result.append(PolarityMarker(
                marker_type="thick_line",
                bbox=shape.bbox,
                center=shape.bbox.center,
                page=shape.page,
                confidence=0.75,
                source="shape",
            ))
        return result

    # ─────────────────────────────────────────────────────────────────────
    # Rule 6 – Triangle
    # ─────────────────────────────────────────────────────────────────────

    def _rule_triangle(self, shapes: List[VectorShape]) -> List[PolarityMarker]:
        """
        Detect triangular shapes (exactly 3 unique vertices after deduplication).
        Used for diode polarity triangles and LED emission direction arrows.
        """
        result = []
        for shape in shapes:
            if shape.shape_type not in ("polyline", "path"):
                continue
            pts = deduplicate_points(shape.points)
            if len(pts) > 1 and pts[-1] == pts[0]:
                pts = pts[:-1]
            if len(pts) != 3:
                continue
            # Reject triangles on copper / courtyard / fab layers
            if self._is_pcb_layer_color(shape.fill_color):
                continue
            if not shape.is_filled and self._is_pcb_layer_color(shape.stroke_color):
                continue
            area = polygon_area(pts)
            if area < self.cfg.triangle_min_area:
                continue
            w, h = shape.bbox.width, shape.bbox.height
            if w > 0 and h > 0:
                aspect = max(w, h) / min(w, h)
                if aspect > self.cfg.triangle_max_aspect:
                    continue
            result.append(PolarityMarker(
                marker_type="triangle",
                bbox=shape.bbox,
                center=shape.bbox.center,
                page=shape.page,
                confidence=0.80,
                source="shape",
            ))
        return result

    # ─────────────────────────────────────────────────────────────────────
    # Rule 7 – Vector-drawn cross / plus sign
    # ─────────────────────────────────────────────────────────────────────

    def _rule_cross_vector(self, shapes: List[VectorShape]) -> List[PolarityMarker]:
        """
        Some EDA tools render the '+' polarity mark as two perpendicular line
        segments instead of a text character.  Detect by finding pairs of
        'line' shapes on the same page where:
          - one is roughly horizontal, the other roughly vertical
          - their midpoints are within cross_midpoint_tol pt of each other
          - each arm is ≤ cross_arm_max_len pt long
        Only silkscreen-layer lines qualify (copper/courtyard filtered out).
        """
        result = []
        line_shapes = [
            s for s in shapes
            if s.shape_type == "line" and len(s.points) >= 2
            and not self._is_pcb_layer_color(s.stroke_color)
        ]

        by_page: dict = {}
        for s in line_shapes:
            by_page.setdefault(s.page, []).append(s)

        for page_shapes in by_page.values():
            for i, s1 in enumerate(page_shapes):
                for s2 in page_shapes[i + 1:]:
                    if not self._is_cross_pair(s1, s2):
                        continue
                    union_bbox = s1.bbox.union(s2.bbox)
                    result.append(PolarityMarker(
                        marker_type="cross_vector",
                        bbox=union_bbox,
                        center=union_bbox.center,
                        page=s1.page,
                        confidence=0.88,
                        source="shape",
                    ))
        return result

    def _is_cross_pair(self, s1: VectorShape, s2: VectorShape) -> bool:
        """Return True if s1 and s2 form a '+' cross shape."""
        cfg = self.cfg
        l1 = max(s1.bbox.width, s1.bbox.height)
        l2 = max(s2.bbox.width, s2.bbox.height)
        if l1 > cfg.cross_arm_max_len or l2 > cfg.cross_arm_max_len:
            return False
        if l1 < cfg.cross_arm_min_len or l2 < cfg.cross_arm_min_len:
            return False

        m1 = s1.bbox.center
        m2 = s2.bbox.center
        if m1.distance_to(m2) > cfg.cross_midpoint_tol:
            return False

        def _is_horizontal(s: VectorShape) -> bool:
            return s.bbox.height > 0 and s.bbox.width >= s.bbox.height * 2.0

        def _is_vertical(s: VectorShape) -> bool:
            return s.bbox.width > 0 and s.bbox.height >= s.bbox.width * 2.0

        return ((_is_horizontal(s1) and _is_vertical(s2)) or
                (_is_vertical(s1) and _is_horizontal(s2)))

    # ─────────────────────────────────────────────────────────────────────
    # Rule 8 – Corner rectangle (IC pin-1 mark)
    # ─────────────────────────────────────────────────────────────────────

    def _rule_corner_rect(self, shapes: List[VectorShape]) -> List[PolarityMarker]:
        """
        Tiny filled squares/rectangles at the corner of an IC outline indicate
        pin-1.  Detection: filled rect/circle with area ≤ corner_rect_max_area.

        Color guard: copper SMD pads, via drill dots, and courtyard marks are
        all small filled shapes but are NOT pin-1 indicators.  We reject any
        shape whose fill colour matches a known PCB layer colour (copper /
        via-gray / courtyard-cyan / fab-blue).  Only silkscreen-layer shapes
        (black, white, magenta, etc.) are accepted.
        """
        result = []
        for shape in shapes:
            if shape.shape_type not in ("filled_rect", "filled_circle"):
                continue
            if not shape.is_filled:
                continue
            # ── Layer-colour guard ────────────────────────────────────────
            # Reject copper pads (0.52,0,0), via holes (0.52,0.52,0.52),
            # courtyard circles (0,0.76,0.76), etc.
            if self._is_pcb_layer_color(shape.fill_color):
                continue
            # ─────────────────────────────────────────────────────────────
            a = shape.bbox.area
            if a <= 0 or a > self.cfg.corner_rect_max_area:
                continue
            if shape.bbox.width > 8.0 or shape.bbox.height > 8.0:
                continue
            result.append(PolarityMarker(
                marker_type="corner_rect",
                bbox=shape.bbox,
                center=shape.bbox.center,
                page=shape.page,
                confidence=0.82,
                source="shape",
            ))
        return result

