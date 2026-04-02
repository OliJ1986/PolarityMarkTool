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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from core.matcher import MatchResult


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
        # Skip truly degenerate zero-size shapes only (not individual line segments:
        # KiCad exports courtyard as many tiny sub-pixel path segments, so we must
        # accept even very small individual bboxes and rely on the merge step below)
        if s.bbox.width < 0.001 and s.bbox.height < 0.001:
            continue
        courtyard_bboxes.setdefault(s.page, []).append(s.bbox)

    # Merge overlapping courtyard boxes into component footprints
    # (KiCad draws courtyard as many tiny segments → union them all)
    merged_courtyards: Dict[int, List[BoundingBox]] = {}
    for page, boxes in courtyard_bboxes.items():
        merged = _merge_nearby_bboxes(boxes, gap=1.5)
        # Keep only bboxes large enough to be a real component footprint
        merged_courtyards[page] = [b for b in merged if b.width >= 3.0 and b.height >= 3.0]

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

def _cluster_pads_into_footprints(
    all_pads: List[_Pad],
    cluster_radius: float = 6.0,
) -> List[List[_Pad]]:
    """
    Group pads into footprint clusters by spatial proximity.

    Two pads are in the same footprint if they are within *cluster_radius* pt
    of at least one other pad in the cluster (connected-components style).
    This replaces the old search-radius approach that accidentally mixed pads
    from neighbouring components.
    """
    clusters: List[List[_Pad]] = []
    used: Set[int] = set()

    for i, seed in enumerate(all_pads):
        if i in used:
            continue
        cluster: List[_Pad] = [seed]
        used.add(i)
        changed = True
        while changed:
            changed = False
            for j, other in enumerate(all_pads):
                if j in used:
                    continue
                if other.shape.page != cluster[0].shape.page:
                    continue
                if any(p.center.distance_to(other.center) <= cluster_radius
                       for p in cluster):
                    cluster.append(other)
                    used.add(j)
                    changed = True
        clusters.append(cluster)

    return clusters


class PadAsymmetryDetector:
    """
    Component-aware polarity detection through copper pad shape asymmetry
    and F.Fab layer pin-1 notch marks.

    Returns ``List[MatchResult]`` directly so the Matcher is bypassed —
    the physical pad position already tells us unambiguously which component
    the marker belongs to.
    """

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config

    # Maximum distance (pt) between pads of the same footprint
    PAD_CLUSTER_RADIUS: float = 6.0

    def detect(
        self,
        shapes: List[VectorShape],
        components: List[Component],
    ) -> List["MatchResult"]:
        """
        Detect polarity markers using proper footprint clustering:

          1. Classify all copper pads (rounded vs rectangular).
          2. Cluster pads into footprints by spatial proximity — this avoids
             the "search radius spills into neighbours" problem.
          3. For each footprint cluster with BOTH rounded AND rect pads
             (mixed shape = asymmetry = polarity indicator), take the single
             rounded pad as the polarity mark.
          4. Assign each polar-cluster to the nearest polar component.
          5. Also detect F.Fab pin-1 circles for ICs.
          6. Return one MatchResult per component.
        """
        from core.matcher import MatchResult  # local import avoids circularity

        if not components:
            return []

        # ── 1. Classify all copper pads ──────────────────────────────────
        all_pads = [p for p in (_classify_pad(s) for s in shapes) if p is not None]

        # ── 2. Build footprint areas (for F.Fab fallback only) ────────────
        footprint_map = _build_footprint_areas(components, shapes, fallback_radius=15.0)
        fab_shapes = [s for s in shapes
                      if _is_fab_blue(s.stroke_color) or _is_fab_blue(s.fill_color)]

        polar_comps = [c for c in components if c.is_polar]

        # ── 3. Cluster pads into footprints ───────────────────────────────
        clusters = _cluster_pads_into_footprints(all_pads, self.PAD_CLUSTER_RADIUS)

        # ── 4. Find clusters with mixed pad shapes (= polarity indicator) ─
        candidates: List[tuple] = []   # (dist_to_comp, rounded_pad, comp, conf)

        for cluster in clusters:
            n_rounded = sum(1 for p in cluster if p.is_rounded)
            n_rect    = len(cluster) - n_rounded

            # Mixed: has BOTH rounded AND rect pads → exactly the rounded one
            # is the polarity pad.  Strict: rounded must be minority (1 pad).
            if not (n_rounded == 1 and n_rect >= 1):
                continue

            rpad = next(p for p in cluster if p.is_rounded)
            ratio = n_rounded / len(cluster)
            conf  = round(0.90 - ratio * 0.10, 2)

            # Cluster centre = average of all pad centres
            cx = sum(p.center.x for p in cluster) / len(cluster)
            cy = sum(p.center.y for p in cluster) / len(cluster)
            cluster_center = Point(cx, cy)
            page = rpad.shape.page

            # Find closest polar component on the same page
            best_comp, best_dist = None, float("inf")
            for comp in polar_comps:
                if comp.page != page:
                    continue
                d = comp.center.distance_to(cluster_center)
                if d < best_dist:
                    best_dist, best_comp = d, comp

            # Max distance: footprint area should contain or be very close to comp text
            max_assign_dist = max(25.0, footprint_map.get(
                f"{best_comp.ref}_{best_comp.page}", BoundingBox(0,0,0,0)
            ).width * 1.5 if best_comp else 25.0)

            if best_comp is None or best_dist > max_assign_dist:
                continue

            candidates.append((best_dist, rpad, best_comp, conf))

        # Sort closest-first
        candidates.sort(key=lambda x: x[0])

        # ── 5. Greedy one-to-one assignment ──────────────────────────────
        assigned_comps: Dict[str, PolarityMarker] = {}   # comp_key → marker
        used_pad_keys:  Set[tuple] = set()

        for dist, rpad, comp, conf in candidates:
            comp_key = f"{comp.ref}_{comp.page}"
            pad_key  = (round(rpad.center.x, 1), round(rpad.center.y, 1))
            if comp_key in assigned_comps or pad_key in used_pad_keys:
                continue
            used_pad_keys.add(pad_key)
            assigned_comps[comp_key] = PolarityMarker(
                marker_type="pad_asymmetry",
                bbox=rpad.shape.bbox,
                center=rpad.center,
                page=comp.page,
                confidence=conf,
                source="shape",
            )

        # ── 5. F.Fab pin-1 notch for ICs that got no pad marker ─────────────
        # Use the footprint BoundingBox (not a loose radius) so we only pick up
        # F.Fab circles that are INSIDE the component's actual courtyard area.
        # Expand the footprint by 4pt on each side to tolerate slight offsets.
        FAB_EXPAND: float = 4.0

        fab_candidates: List[tuple] = []
        for comp in polar_comps:
            comp_key = f"{comp.ref}_{comp.page}"
            if comp_key in assigned_comps:
                continue
            if comp.comp_type not in ("ic",):
                continue

            fp = footprint_map.get(comp_key)
            if fp is None:
                continue
            search_area = BoundingBox(
                fp.x0 - FAB_EXPAND, fp.y0 - FAB_EXPAND,
                fp.x1 + FAB_EXPAND, fp.y1 + FAB_EXPAND,
            )

            for s in fab_shapes:
                if s.shape_type not in ("circle", "filled_circle"):
                    continue
                if s.page != comp.page:
                    continue
                r = (s.bbox.width + s.bbox.height) / 4.0
                if not (0.5 <= r <= 6.0):
                    continue
                if search_area.contains_point(s.bbox.center):
                    d = comp.center.distance_to(s.bbox.center)
                    fab_candidates.append((d, s, comp))

        fab_candidates.sort(key=lambda x: x[0])

        used_fab_centers: Set[tuple] = set()
        for dist, s, comp in fab_candidates:
            comp_key = f"{comp.ref}_{comp.page}"
            fab_key  = (round(s.bbox.center.x, 1), round(s.bbox.center.y, 1))
            if comp_key in assigned_comps or fab_key in used_fab_centers:
                continue
            assigned_comps[comp_key] = PolarityMarker(
                marker_type="fab_pin1_notch",
                bbox=s.bbox,
                center=s.bbox.center,
                page=comp.page,
                confidence=0.78,
                source="shape",
            )
            used_fab_centers.add(fab_key)

        # ── 6. Build MatchResult list for ALL components ──────────────────
        results: List[MatchResult] = []
        for comp in components:
            comp_key = f"{comp.ref}_{comp.page}"
            marker   = assigned_comps.get(comp_key)
            if marker is not None:
                results.append(MatchResult(
                    component=comp,
                    markers=[marker],
                    polarity_status="marked",
                    overall_confidence=marker.confidence,
                ))
            else:
                results.append(MatchResult(
                    component=comp,
                    polarity_status="unmarked" if comp.is_polar else "unmarked",
                ))
        return results

    # ── F.Fab pin-1 notch rule ────────────────────────────────────────────

    def _check_fab_pin1(
        self,
        comp: Component,
        area: BoundingBox,
        fab_shapes: List[VectorShape],
    ) -> Optional[PolarityMarker]:
        """Small F.Fab arc/circle near a corner of the IC body = pin-1."""
        candidates: List[VectorShape] = []
        for s in fab_shapes:
            if s.page != comp.page:
                continue
            if not area.contains_point(s.bbox.center):
                continue
            if s.bbox.width > 4.0 or s.bbox.height > 4.0:
                continue
            if s.bbox.width < 0.2 and s.bbox.height < 0.2:
                continue
            if s.shape_type in ("circle", "filled_circle", "path"):
                candidates.append(s)
        if not candidates:
            return None
        corners = [
            Point(area.x0, area.y0), Point(area.x1, area.y0),
            Point(area.x0, area.y1), Point(area.x1, area.y1),
        ]
        best = min(candidates,
                   key=lambda s: min(s.bbox.center.distance_to(c) for c in corners))
        return PolarityMarker(
            marker_type="fab_pin1_notch",
            bbox=best.bbox,
            center=best.bbox.center,
            page=comp.page,
            confidence=0.78,
            source="shape",
        )

