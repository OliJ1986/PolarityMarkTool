"""
core/pad_asymmetry_detector.py
──────────────────────────────
Detects polarity indicators on KiCad PCB PDFs by finding **copper pad
asymmetry** within each component's footprint area.

KiCad draws the polarity/pin-1 pad with a distinctive "D-pad" (rounded)
shape — a ``path`` composed of Bézier curves — while all other pads in the
same footprint are plain ``filled_rect`` rectangles.  The single outlier
pad IS the polarity indicator.

Detection also looks at the F.Fab (blue) layer for IC pin-1 notch/dot marks.

This module introduces **component-aware** rules that need both the shapes
AND the already-detected components to work.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from utils.geometry import BoundingBox, Point
from utils.config import Config, DEFAULT_CONFIG
from core.pdf_parser import VectorShape
from core.component_detector import Component
from core.polarity_detector import PolarityMarker


# ─────────────────────────────────────────────────────────────────────────────
# Colour helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_copper_color(color: Optional[Tuple]) -> bool:
    """True if *color* is a copper-layer red (R-dominant)."""
    if not color:
        return False
    r, g, b = float(color[0]), float(color[1]), float(color[2])
    return r > 0.15 and r > g * 2.0 and r > b * 2.0


def _is_fab_blue(color: Optional[Tuple]) -> bool:
    """True if *color* is the F.Fab blue layer (≈0, 0, 0.52)."""
    if not color:
        return False
    r, g, b = float(color[0]), float(color[1]), float(color[2])
    return b > 0.30 and r < 0.20 and g < 0.20


def _is_courtyard_color(color: Optional[Tuple]) -> bool:
    """True if *color* is the F.CrtYd cyan layer (≈0, 0.52, 0.52)."""
    if not color:
        return False
    r, g, b = float(color[0]), float(color[1]), float(color[2])
    return g > 0.30 and b > 0.30 and r < 0.20


# ─────────────────────────────────────────────────────────────────────────────
# Pad classification
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Pad:
    """Internal representation of one SMD pad."""
    shape: VectorShape
    center: Point
    is_rounded: bool       # True = Bézier "D-pad", False = plain rect pad


def _classify_pad(s: VectorShape) -> Optional[_Pad]:
    """Return a _Pad if *s* looks like a copper SMD pad, else None."""
    if not s.is_filled:
        return None
    if not _is_copper_color(s.fill_color):
        return None
    # Must be small enough to be a pad (not a ground plane / copper fill)
    if s.bbox.width > 15.0 or s.bbox.height > 15.0:
        return None
    if s.bbox.area < 0.5:
        return None

    # Classify shape geometry
    if s.shape_type == "filled_rect":
        return _Pad(shape=s, center=s.bbox.center, is_rounded=False)
    elif s.shape_type in ("path", "filled_circle"):
        # "path" with copper fill = rounded/D-pad
        return _Pad(shape=s, center=s.bbox.center, is_rounded=True)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Footprint area estimation
# ─────────────────────────────────────────────────────────────────────────────

def _build_footprint_areas(
    components: List[Component],
    shapes: List[VectorShape],
    fallback_radius: float = 15.0,
) -> Dict[str, BoundingBox]:
    """
    Return a search-area BoundingBox for each component, keyed by
    ``(ref, page)`` string.

    Strategy:
      1. Find courtyard rectangles (cyan outlines) and assign each component
         to the courtyard whose centre is nearest.
      2. Fall back to a circle of *fallback_radius* around the ref-des centre
         for components that don't match any courtyard.
    """
    # Collect courtyard shapes (unfilled rectangles, or anything with cyan
    # stroke that forms a reasonably sized bounding box)
    courtyard_bboxes: Dict[int, List[BoundingBox]] = {}  # page → list
    for s in shapes:
        color = s.stroke_color or s.fill_color
        if not _is_courtyard_color(color):
            continue
        # Only consider shapes large enough to be a courtyard (~3 pt min)
        if s.bbox.width < 2.0 or s.bbox.height < 2.0:
            continue
        courtyard_bboxes.setdefault(s.page, []).append(s.bbox)

    # Merge overlapping courtyard boxes into component footprints
    # (KiCad draws courtyard as 4 lines → we union nearby boxes)
    merged_courtyards: Dict[int, List[BoundingBox]] = {}
    for page, boxes in courtyard_bboxes.items():
        merged = _merge_nearby_bboxes(boxes, gap=2.0)
        merged_courtyards[page] = merged

    result: Dict[str, BoundingBox] = {}
    for comp in components:
        key = f"{comp.ref}_{comp.page}"
        page_courts = merged_courtyards.get(comp.page, [])

        best_court = None
        best_dist = float("inf")
        for cb in page_courts:
            if cb.contains_point(comp.center):
                best_court = cb
                break
            d = comp.center.distance_to(cb.center)
            if d < best_dist and d < fallback_radius * 2:
                best_dist = d
                best_court = cb

        if best_court is not None:
            result[key] = best_court
        else:
            # Fallback: square area around ref-des center
            result[key] = BoundingBox(
                comp.center.x - fallback_radius,
                comp.center.y - fallback_radius,
                comp.center.x + fallback_radius,
                comp.center.y + fallback_radius,
            )

    return result


def _merge_nearby_bboxes(boxes: List[BoundingBox], gap: float) -> List[BoundingBox]:
    """Iteratively union bounding boxes that overlap or are within *gap* pt."""
    if not boxes:
        return []
    merged: List[BoundingBox] = list(boxes)
    changed = True
    while changed:
        changed = False
        new_merged: List[BoundingBox] = []
        used: Set[int] = set()
        for i, b1 in enumerate(merged):
            if i in used:
                continue
            current = b1
            for j in range(i + 1, len(merged)):
                if j in used:
                    continue
                b2 = merged[j]
                if _bboxes_close(current, b2, gap):
                    current = current.union(b2)
                    used.add(j)
                    changed = True
            new_merged.append(current)
            used.add(i)
        merged = new_merged
    return merged


def _bboxes_close(a: BoundingBox, b: BoundingBox, gap: float) -> bool:
    """True if *a* and *b* overlap or are within *gap* pt of each other."""
    return not (
        a.x1 + gap < b.x0 or b.x1 + gap < a.x0 or
        a.y1 + gap < b.y0 or b.y1 + gap < a.y0
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────────────

class PadAsymmetryDetector:
    """
    Component-aware polarity detection through copper pad shape asymmetry
    and F.Fab layer pin-1 notch marks.

    Usage::

        detector = PadAsymmetryDetector()
        markers  = detector.detect(shapes, components)
    """

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config

    def detect(
        self,
        shapes: List[VectorShape],
        components: List[Component],
    ) -> List[PolarityMarker]:
        """
        Detect polarity markers by analysing pad asymmetry + fab-layer
        shapes near each polar component.
        """
        if not components:
            return []

        # 1. Build footprint search areas (still used for fab-pin1 only)
        footprint_map = _build_footprint_areas(
            components, shapes, fallback_radius=15.0
        )

        # 2. Classify all copper pads
        all_pads = [p for p in (_classify_pad(s) for s in shapes) if p is not None]

        # 3. Collect F.Fab shapes (for IC pin-1 notch detection)
        fab_shapes = [
            s for s in shapes
            if _is_fab_blue(s.stroke_color) or _is_fab_blue(s.fill_color)
        ]

        markers: List[PolarityMarker] = []

        for comp in components:
            if not comp.is_polar:
                continue

            key = f"{comp.ref}_{comp.page}"

            # --- Pad asymmetry (radius-based search) ---
            m = self._check_pad_asymmetry(comp, all_pads)
            if m is not None:
                markers.append(m)
                continue   # found via pads, no need for fab check

            # --- F.Fab pin-1 notch (fallback for ICs) ---
            if comp.comp_type in ("ic",):
                area = footprint_map.get(key)
                if area is not None:
                    m2 = self._check_fab_pin1(comp, area, fab_shapes)
                    if m2 is not None:
                        markers.append(m2)

        return markers

    # ── Pad asymmetry rule ────────────────────────────────────────────────

    # Radius (pt) around component center to search for copper pads
    PAD_SEARCH_RADIUS: float = 20.0

    def _check_pad_asymmetry(
        self,
        comp: Component,
        all_pads: List[_Pad],
    ) -> Optional[PolarityMarker]:
        """
        Within a radius around the component center, find all copper pads.
        If there is a clear minority of rounded pads among rectangular pads
        (or vice versa), the outlier marks the polarity / pin-1 position.

        Uses a simple radius search (robust) instead of courtyard areas.
        """
        # Collect pads within search radius of the component center
        pads_in: List[_Pad] = [
            p for p in all_pads
            if p.shape.page == comp.page
            and comp.center.distance_to(p.center) <= self.PAD_SEARCH_RADIUS
        ]

        if len(pads_in) < 2:
            return None

        n_rounded = sum(1 for p in pads_in if p.is_rounded)
        n_rect    = len(pads_in) - n_rounded

        # We want a clear minority: exactly 1 outlier (or a small minority)
        outlier_pads: List[_Pad]
        if 0 < n_rounded <= max(1, len(pads_in) // 3) and n_rect > 0:
            # Rounded pads are the minority → they mark polarity
            outlier_pads = [p for p in pads_in if p.is_rounded]
        elif 0 < n_rect <= max(1, len(pads_in) // 3) and n_rounded > 0:
            # Rect pads are the minority (unusual but possible)
            outlier_pads = [p for p in pads_in if not p.is_rounded]
        else:
            return None   # no clear asymmetry

        # Pick the outlier pad closest to the component center
        best = min(outlier_pads, key=lambda p: comp.center.distance_to(p.center))

        # Confidence scales with how clear the asymmetry is
        ratio = len(outlier_pads) / len(pads_in)
        conf = 0.92 - ratio * 0.15   # e.g. 1/20 → 0.91, 1/2 → 0.84

        return PolarityMarker(
            marker_type="pad_asymmetry",
            bbox=best.shape.bbox,
            center=best.center,
            page=comp.page,
            confidence=round(conf, 2),
            source="shape",
        )

    # ── F.Fab pin-1 notch rule ────────────────────────────────────────────

    def _check_fab_pin1(
        self,
        comp: Component,
        area: BoundingBox,
        fab_shapes: List[VectorShape],
    ) -> Optional[PolarityMarker]:
        """
        On the F.Fab (blue) layer, ICs often have a small arc or circle
        near pin-1 that distinguishes it from the rectangular body outline.

        We look for small blue circles or arcs (bbox < 4 pt) within the
        footprint area that are NOT part of the main rectangular outline.
        """
        candidates: List[VectorShape] = []
        for s in fab_shapes:
            if s.page != comp.page:
                continue
            if not area.contains_point(s.bbox.center):
                continue
            # Must be small (notch/dot, not a body outline side)
            if s.bbox.width > 4.0 or s.bbox.height > 4.0:
                continue
            # Must have some non-zero dimension
            if s.bbox.width < 0.2 and s.bbox.height < 0.2:
                continue
            # Circles or arcs (Bézier paths)
            if s.shape_type in ("circle", "filled_circle", "path"):
                candidates.append(s)

        if not candidates:
            return None

        # Pick the one closest to a corner of the footprint area (pin-1 is
        # always at a corner of the IC body)
        corners = [
            Point(area.x0, area.y0), Point(area.x1, area.y0),
            Point(area.x0, area.y1), Point(area.x1, area.y1),
        ]

        def _min_corner_dist(s: VectorShape) -> float:
            return min(s.bbox.center.distance_to(c) for c in corners)

        best = min(candidates, key=_min_corner_dist)

        return PolarityMarker(
            marker_type="fab_pin1_notch",
            bbox=best.bbox,
            center=best.bbox.center,
            page=comp.page,
            confidence=0.78,
            source="shape",
        )

