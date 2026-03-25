"""
core/image_polarity_detector.py
────────────────────────────────
Raster-based polarity detection.

Instead of parsing raw PDF vector paths (which can be incomplete or
misleading), this module:
  1. Renders each PDF page to a high-resolution RGB image via PyMuPDF.
  2. For each component footprint, crops out the relevant region.
  3. Applies colour-segmented image analysis to find polarity marks:
       • A small circle / filled dot (pin-1 marker on ICs)
       • A thick bar or asymmetric line at one end (cathode stripe on diodes)
       • A copper- or silk-layer "+" symbol near one pad
       • Visual pad-shape asymmetry (one pad looks different from the others)

The OpenCV library is used for image processing.  If it is not installed
the detector gracefully returns an empty list so the rest of the pipeline
continues unaffected.

Coordinate system
─────────────────
PDF points (72 pt/inch) ↔ pixel = pt × (dpi / 72).
All returned PolarityMarker objects use PDF-point coordinates so they can
be overlaid on the original PDF directly.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF – always available

from utils.config import Config, DEFAULT_CONFIG
from utils.geometry import BoundingBox, Point
from core.pdf_parser import VectorShape
from core.component_detector import Component
from core.polarity_detector import PolarityMarker
from core.pad_asymmetry_detector import _build_footprint_areas

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Colour ranges (BGR for OpenCV, 0-255)
# ─────────────────────────────────────────────────────────────────────────────

# KiCad PDF default layer colours (approximate)
_LAYER_BGR: Dict[str, Tuple[Tuple[int,int,int], Tuple[int,int,int]]] = {
    # layer_name: (lower_BGR, upper_BGR)
    "copper":    ((0,   0,  80), (80,  80, 175)),  # R-dominant red  0.52,0,0
    "silk":      ((80,  0,  80), (210, 80, 210)),  # magenta  0.52,0,0.52
    "fab":       ((80,  0,   0), (210, 80,  80)),  # blue  0,0,0.52
    "courtyard": ((0,  80,   0), (80, 210,  80)),  # cyan  0,0.52,0.52
}

# ─────────────────────────────────────────────────────────────────────────────
# Page renderer (cached per page)
# ─────────────────────────────────────────────────────────────────────────────

class _PageRenderer:
    """Renders a single PDF page to a NumPy array (BGR, uint8) at *dpi* DPI."""

    def __init__(self, doc: fitz.Document, page_index: int, dpi: int = 200):
        self._page_index = page_index
        self._dpi = dpi
        self._scale = dpi / 72.0        # pt → pixel conversion factor
        mat = fitz.Matrix(self._scale, self._scale)
        page = doc[page_index]
        pix = page.get_pixmap(matrix=mat, alpha=False)
        # fitz returns RGB bytes; convert to numpy BGR for OpenCV
        import numpy as np
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        self.image_bgr = rgb[:, :, ::-1].copy()   # RGB → BGR
        self.width_px = pix.width
        self.height_px = pix.height

    def pt_to_px(self, pt: float) -> int:
        return max(1, int(round(pt * self._scale)))

    def bbox_to_slice(
        self,
        bbox: BoundingBox,
        margin_pt: float = 4.0,
    ) -> Tuple[slice, slice, float, float]:
        """
        Return (row_slice, col_slice, x0_pt, y0_pt) for *bbox* expanded by
        *margin_pt* on each side, clamped to the image dimensions.
        """
        x0 = max(0.0, bbox.x0 - margin_pt) * self._scale
        y0 = max(0.0, bbox.y0 - margin_pt) * self._scale
        x1 = min(self.width_px  / self._scale, bbox.x1 + margin_pt) * self._scale
        y1 = min(self.height_px / self._scale, bbox.y1 + margin_pt) * self._scale
        return (
            slice(int(y0), int(y1)),
            slice(int(x0), int(x1)),
            (bbox.x0 - margin_pt),   # origin in pt (for back-projection)
            (bbox.y0 - margin_pt),
        )

    def scale(self) -> float:
        return self._scale


# ─────────────────────────────────────────────────────────────────────────────
# Detection helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mask_layer(bgr_crop, layer: str):
    """Return a binary mask for the given layer colour in *bgr_crop*."""
    import cv2
    import numpy as np
    lo, hi = _LAYER_BGR[layer]
    lo_arr = np.array(lo, dtype=np.uint8)
    hi_arr = np.array(hi, dtype=np.uint8)
    return cv2.inRange(bgr_crop, lo_arr, hi_arr)


def _find_circles(mask, min_r_px: int, max_r_px: int, min_area_frac: float = 0.5):
    """
    Find filled circular blobs in *mask*.
    Returns list of (cx_px, cy_px, r_px) tuples.
    Only returns blobs that are roughly circular (area / π r² ≥ min_area_frac).
    """
    import cv2
    import numpy as np
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    circles = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 2:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        r = (w + h) / 4.0
        if not (min_r_px <= r <= max_r_px):
            continue
        # Circularity check
        perimeter = cv2.arcLength(cnt, True)
        if perimeter <= 0:
            continue
        circularity = 4 * 3.14159 * area / (perimeter ** 2)
        if circularity < 0.5:
            continue
        cx = x + w / 2
        cy = y + h / 2
        circles.append((cx, cy, r))
    return circles


def _find_stripe(mask, comp_bbox_px, orientation: str = "any"):
    """
    Detect a bar/stripe at one end of the component body outline.
    Returns (cx_px, cy_px) of the stripe midpoint if found, else None.

    *orientation* = "h" | "v" | "any"
    """
    import cv2
    import numpy as np

    H, W = mask.shape
    if H < 4 or W < 4:
        return None

    # Look for a horizontal or vertical line cluster
    row_sums = np.sum(mask > 0, axis=1)   # horizontal spread per row
    col_sums = np.sum(mask > 0, axis=0)   # vertical spread per column

    min_extent = max(3, int(min(W, H) * 0.3))

    best = None

    if orientation in ("h", "any"):
        # Find rows with high horizontal extent (a horizontal stripe)
        strong_rows = np.where(row_sums >= min_extent)[0]
        if len(strong_rows) > 0:
            # Group into bands
            bands = []
            start = strong_rows[0]
            prev = strong_rows[0]
            for r in strong_rows[1:]:
                if r > prev + 2:
                    bands.append((start, prev))
                    start = r
                prev = r
            bands.append((start, prev))
            # Prefer a band that touches the top or bottom edge
            edge_bands = [b for b in bands if b[0] <= 1 or b[1] >= H - 2]
            chosen = edge_bands[0] if edge_bands else bands[0]
            cy = (chosen[0] + chosen[1]) / 2.0
            # Find horizontal extent of these rows
            sub = mask[chosen[0]:chosen[1]+1, :]
            cols = np.where(np.any(sub > 0, axis=0))[0]
            if len(cols) >= min_extent:
                cx = (cols[0] + cols[-1]) / 2.0
                best = (cx, cy)

    if orientation in ("v", "any") and best is None:
        strong_cols = np.where(col_sums >= min_extent)[0]
        if len(strong_cols) > 0:
            bands = []
            start = strong_cols[0]
            prev = strong_cols[0]
            for c in strong_cols[1:]:
                if c > prev + 2:
                    bands.append((start, prev))
                    start = c
                prev = c
            bands.append((start, prev))
            edge_bands = [b for b in bands if b[0] <= 1 or b[1] >= W - 2]
            chosen = edge_bands[0] if edge_bands else bands[0]
            cx = (chosen[0] + chosen[1]) / 2.0
            sub = mask[:, chosen[0]:chosen[1]+1]
            rows = np.where(np.any(sub > 0, axis=1))[0]
            if len(rows) >= min_extent:
                cy = (rows[0] + rows[-1]) / 2.0
                best = (cx, cy)

    return best


def _pad_blobs(copper_mask, min_area_px: int = 4):
    """
    Return a list of (cx, cy, area, bbox) for each copper pad blob found
    in *copper_mask*.  Filters tiny noise blobs.
    """
    import cv2
    contours, _ = cv2.findContours(copper_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    blobs = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area_px:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        blobs.append((x + w/2, y + h/2, area, (x, y, w, h)))
    return blobs


def _blob_circularity(cnt) -> float:
    import cv2
    area = cv2.contourArea(cnt)
    perimeter = cv2.arcLength(cnt, True)
    if perimeter <= 0:
        return 0.0
    return 4 * 3.14159 * area / (perimeter ** 2)


# ─────────────────────────────────────────────────────────────────────────────
# Main detector class
# ─────────────────────────────────────────────────────────────────────────────

class ImagePolarityDetector:
    """
    Detects polarity marks by rendering the PDF page to pixels and running
    OpenCV-based image analysis on each component's footprint area.

    Falls back gracefully (returns []) if OpenCV or NumPy are not available.
    """

    RENDER_DPI: int = 200           # Rendering resolution
    MARGIN_PT: float = 5.0          # Extra margin around footprint bbox (pt)
    MIN_CIRCLE_R_PT: float = 1.0    # Smallest detectable pin-1 circle
    MAX_CIRCLE_R_PT: float = 6.0    # Largest pin-1 circle to consider
    STRIPE_MIN_LENGTH_PT: float = 3.0  # Min length for cathode-bar detection

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config
        self._renderers: Dict[Tuple[str, int], _PageRenderer] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def detect(
        self,
        pdf_path: str,
        shapes: List[VectorShape],
        components: List[Component],
    ) -> "List[MatchResult]":
        """
        Analyse the rendered PDF and return a MatchResult for every component.
        """
        try:
            import cv2  # noqa: F401 – just validate availability
            import numpy as np  # noqa: F401
        except ImportError:
            log.warning("OpenCV / NumPy not available – ImagePolarityDetector disabled")
            return []

        from core.matcher import MatchResult

        if not components:
            return []

        doc = fitz.open(pdf_path)
        try:
            footprint_map = _build_footprint_areas(
                components, shapes, fallback_radius=15.0
            )

            # Build a renderer per page (cached)
            renderers: Dict[int, _PageRenderer] = {}
            for comp in components:
                if comp.page not in renderers:
                    renderers[comp.page] = _PageRenderer(
                        doc, comp.page, dpi=self.RENDER_DPI
                    )

            results: List[MatchResult] = []
            for comp in components:
                renderer = renderers.get(comp.page)
                if renderer is None:
                    results.append(MatchResult(component=comp))
                    continue

                key = f"{comp.ref}_{comp.page}"
                fp_bbox = footprint_map.get(key)
                if fp_bbox is None:
                    results.append(MatchResult(component=comp))
                    continue

                marker = self._detect_in_area(comp, fp_bbox, renderer)
                if marker:
                    results.append(MatchResult(
                        component=comp,
                        markers=[marker],
                        polarity_status="marked",
                        overall_confidence=marker.confidence,
                    ))
                else:
                    results.append(MatchResult(
                        component=comp,
                        polarity_status="unmarked",
                    ))

            return results
        finally:
            doc.close()

    # ── Per-component analysis ────────────────────────────────────────────

    def _detect_in_area(
        self,
        comp: Component,
        fp_bbox: BoundingBox,
        renderer: _PageRenderer,
    ) -> Optional[PolarityMarker]:
        """Run all detection rules on the component's footprint image crop."""
        if not comp.is_polar:
            return None

        row_sl, col_sl, x0_pt, y0_pt = renderer.bbox_to_slice(
            fp_bbox, margin_pt=self.MARGIN_PT
        )
        crop = renderer.image_bgr[row_sl, col_sl]
        if crop.size == 0:
            return None

        scale = renderer.scale()
        h_px, w_px = crop.shape[:2]

        # ── Rule 1: small circle on silk or F.Fab layer ───────────────────
        marker = self._rule_circle(crop, scale, comp, x0_pt, y0_pt)
        if marker:
            return marker

        # ── Rule 2: cathode stripe (thick bar at one end) on silk layer ───
        marker = self._rule_cathode_stripe(crop, scale, comp, x0_pt, y0_pt, fp_bbox)
        if marker:
            return marker

        # ── Rule 3: copper pad asymmetry (one pad visually different) ─────
        marker = self._rule_pad_asymmetry(crop, scale, comp, x0_pt, y0_pt)
        if marker:
            return marker

        return None

    def _rule_circle(
        self,
        crop, scale: float,
        comp: Component,
        x0_pt: float, y0_pt: float,
    ) -> Optional[PolarityMarker]:
        """Find a small filled circle on the silk or F.Fab layer."""
        min_r_px = max(2, int(self.MIN_CIRCLE_R_PT * scale))
        max_r_px = max(3, int(self.MAX_CIRCLE_R_PT * scale))

        for layer in ("fab", "silk"):
            mask = _mask_layer(crop, layer)
            circles = _find_circles(mask, min_r_px, max_r_px)
            if not circles:
                continue
            # Pick the circle closest to a corner of the footprint crop
            cx_px, cy_px, r_px = min(
                circles,
                key=lambda c: min(
                    (c[0]**2 + c[1]**2)**0.5,
                    (c[0]**2 + (crop.shape[0]-c[1])**2)**0.5,
                    ((crop.shape[1]-c[0])**2 + c[1]**2)**0.5,
                    ((crop.shape[1]-c[0])**2 + (crop.shape[0]-c[1])**2)**0.5,
                )
            )
            cx_pt = x0_pt + cx_px / scale
            cy_pt = y0_pt + cy_px / scale
            return PolarityMarker(
                marker_type="pin1_circle",
                bbox=BoundingBox(
                    cx_pt - r_px/scale, cy_pt - r_px/scale,
                    cx_pt + r_px/scale, cy_pt + r_px/scale,
                ),
                center=Point(cx_pt, cy_pt),
                page=comp.page,
                confidence=0.82,
                source="image",
            )
        return None

    def _rule_cathode_stripe(
        self,
        crop, scale: float,
        comp: Component,
        x0_pt: float, y0_pt: float,
        fp_bbox: BoundingBox,
    ) -> Optional[PolarityMarker]:
        """
        Detect a cathode bar: a line/filled stripe on the silk layer at
        exactly one end of the component body.
        """
        if comp.comp_type not in ("diode", "led"):
            return None

        silk_mask = _mask_layer(crop, "silk")
        h_px, w_px = silk_mask.shape

        # Total silk pixel count
        import numpy as np
        total = int(np.sum(silk_mask > 0))
        if total < 4:
            return None

        # Try to find an edge stripe (top, bottom, left, right)
        stripe_w_px = max(2, int(2.0 * scale))   # stripe is ~2pt wide

        regions = {
            "top":    silk_mask[:stripe_w_px, :],
            "bottom": silk_mask[-stripe_w_px:, :],
            "left":   silk_mask[:, :stripe_w_px],
            "right":  silk_mask[:, -stripe_w_px:],
        }

        scores = {edge: int(np.sum(r > 0)) for edge, r in regions.items()}
        best_edge = max(scores, key=lambda e: scores[e])
        best_score = scores[best_edge]

        # Must be significantly more than the opposite edge
        opposite = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}
        opposite_score = scores[opposite[best_edge]]

        # Require at least 3× more pixels on one edge vs the other
        if best_score < 3 or best_score < opposite_score * 2.5:
            return None

        # Position the marker at the edge centre
        if best_edge == "top":
            cx_px = w_px / 2; cy_px = 0
        elif best_edge == "bottom":
            cx_px = w_px / 2; cy_px = h_px
        elif best_edge == "left":
            cx_px = 0; cy_px = h_px / 2
        else:
            cx_px = w_px; cy_px = h_px / 2

        cx_pt = x0_pt + cx_px / scale
        cy_pt = y0_pt + cy_px / scale
        stripe_r_pt = 2.0

        return PolarityMarker(
            marker_type="cathode_stripe",
            bbox=BoundingBox(
                cx_pt - stripe_r_pt, cy_pt - stripe_r_pt,
                cx_pt + stripe_r_pt, cy_pt + stripe_r_pt,
            ),
            center=Point(cx_pt, cy_pt),
            page=comp.page,
            confidence=0.75,
            source="image",
        )

    def _rule_pad_asymmetry(
        self,
        crop, scale: float,
        comp: Component,
        x0_pt: float, y0_pt: float,
    ) -> Optional[PolarityMarker]:
        """
        For 2-pad components: if one pad is visually more circular than the
        other, mark it as the polarity pad (pin-1 / positive).

        This catches KiCad rounded-corner pads where pin-1 has a distinct
        shape from pad-2.
        """
        if comp.comp_type not in ("capacitor", "diode", "led", "transistor"):
            return None

        import cv2
        import numpy as np

        copper_mask = _mask_layer(crop, "copper")
        blobs = _pad_blobs(copper_mask, min_area_px=max(4, int(1.0 * scale)))

        if len(blobs) < 2:
            return None

        # Sort by area descending, keep the two largest (= the pads)
        blobs.sort(key=lambda b: b[2], reverse=True)
        pads = blobs[:2]

        # Compute circularity for each pad
        circularities = []
        for blob in pads:
            bx, by, bw, bh = blob[3]
            pad_roi = copper_mask[by:by+bh, bx:bx+bw]
            cnts, _ = cv2.findContours(pad_roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not cnts:
                circularities.append(0.0)
                continue
            cnt = max(cnts, key=cv2.contourArea)
            circularities.append(_blob_circularity(cnt))

        # Asymmetry threshold: one pad must be noticeably more circular
        diff = abs(circularities[0] - circularities[1])
        if diff < 0.15:
            return None   # both pads look the same → no detectable asymmetry

        # The more circular pad = polarity pad
        polar_idx = 0 if circularities[0] > circularities[1] else 1
        cx_px, cy_px = pads[polar_idx][0], pads[polar_idx][1]
        cx_pt = x0_pt + cx_px / scale
        cy_pt = y0_pt + cy_px / scale
        r_pt = (pads[polar_idx][3][2] + pads[polar_idx][3][3]) / 4.0 / scale

        conf = min(0.90, 0.60 + diff * 0.8)   # 60–90% based on asymmetry magnitude

        return PolarityMarker(
            marker_type="pad_asymmetry_visual",
            bbox=BoundingBox(
                cx_pt - r_pt, cy_pt - r_pt,
                cx_pt + r_pt, cy_pt + r_pt,
            ),
            center=Point(cx_pt, cy_pt),
            page=comp.page,
            confidence=round(conf, 2),
            source="image",
        )


