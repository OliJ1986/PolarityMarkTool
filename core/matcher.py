"""
core/matcher.py
───────────────
Spatially matches detected polarity markers to the nearest component.

Strategy
────────
For every polar component:
  1. Expand its text-span bbox by *search_expand_margin* pt on all sides.
  2. Collect all markers whose centre lies inside the expanded bbox, OR
     whose centre-to-centre distance to the component is ≤ max_match_distance.
  3. Accept the closest marker of each distinct type (avoid duplicates from
     the same physical indicator hit by multiple rules).
  4. Determine polarity_status:
       "marked"    – ≥ 1 marker assigned
       "unmarked"  – no markers found within range
       "ambiguous" – markers found but of conflicting polarity hint
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from utils.config import Config, DEFAULT_CONFIG
from core.component_detector import Component
from core.polarity_detector import PolarityMarker


# ─────────────────────────────────────────────────────────────────────────────
# Result data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    """All polarity information associated with one component."""
    component: Component
    markers: List[PolarityMarker] = field(default_factory=list)
    polarity_status: str = "unmarked"   # "marked" | "unmarked" | "ambiguous"
    overall_confidence: float = 0.0

    @property
    def has_polarity(self) -> bool:
        return bool(self.markers)

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        return {
            "reference":        self.component.ref,
            "type":             self.component.comp_type,
            "page":             self.component.page + 1,   # 1-based
            "polarity_status":  self.polarity_status,
            "confidence":       round(self.overall_confidence, 3),
            "position": {
                "x": round(self.component.center.x, 2),
                "y": round(self.component.center.y, 2),
            },
            "bbox": self.component.bbox.to_dict(),
            "markers": [
                {
                    "type":             m.marker_type,
                    "confidence":       round(m.confidence, 3),
                    "source":           m.source,
                    "detection_method": m.detection_method,
                    "position": {
                        "x": round(m.center.x, 2),
                        "y": round(m.center.y, 2),
                    },
                    "bbox": m.bbox.to_dict(),
                }
                for m in self.markers
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Matcher
# ─────────────────────────────────────────────────────────────────────────────

class Matcher:
    """
    Assigns polarity markers to components.

    Only components in POLAR_TYPES are matched; non-polar components
    (resistors, connectors …) still appear in the output with
    polarity_status = "unmarked" and an empty markers list.
    """

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config

    def match(
        self,
        components: List[Component],
        markers: List[PolarityMarker],
    ) -> List[MatchResult]:
        """
        Returns one MatchResult per component, sorted by reference designator.
        """
        # Pre-group markers by page for fast iteration
        markers_by_page: Dict[int, List[PolarityMarker]] = {}
        for m in markers:
            markers_by_page.setdefault(m.page, []).append(m)

        results: List[MatchResult] = []
        for comp in components:
            result = self._match_one(comp, markers_by_page.get(comp.page, []))
            results.append(result)

        # Sort by reference designator (natural sort: D1 < D2 < D10)
        results.sort(key=lambda r: self._natural_sort_key(r.component.ref))
        return results

    # ── Core matching logic ───────────────────────────────────────────────

    def _match_one(
        self,
        comp: Component,
        page_markers: List[PolarityMarker],
    ) -> MatchResult:
        """Match a single component against all markers on the same page."""
        if not comp.is_polar:
            return MatchResult(component=comp, polarity_status="unmarked")

        expanded_bbox = comp.bbox.expand(self.cfg.search_expand_margin)
        candidates: List[Tuple[float, PolarityMarker]] = []

        for marker in page_markers:
            dist = comp.center.distance_to(marker.center)
            in_expanded = expanded_bbox.overlaps(marker.bbox)
            in_range = dist <= self.cfg.max_match_distance

            if in_expanded or in_range:
                candidates.append((dist, marker))

        if not candidates:
            return MatchResult(component=comp, polarity_status="unmarked")

        # Deduplicate: keep only the closest marker of each type
        # (handles the case where the same physical mark triggers multiple rules)
        best_by_type: Dict[str, Tuple[float, PolarityMarker]] = {}
        for dist, marker in candidates:
            key = marker.marker_type
            if key not in best_by_type or dist < best_by_type[key][0]:
                best_by_type[key] = (dist, marker)

        assigned = [m for _, m in best_by_type.values()]

        # Determine overall confidence (weighted average by inverse distance)
        total_w = 0.0
        weighted_conf = 0.0
        for dist, marker in best_by_type.values():
            w = 1.0 / (1.0 + dist)
            weighted_conf += marker.confidence * w
            total_w += w
        overall_conf = weighted_conf / total_w if total_w > 0 else 0.0

        # Determine status
        status = self._determine_status(assigned)

        return MatchResult(
            component=comp,
            markers=assigned,
            polarity_status=status,
            overall_confidence=overall_conf,
        )

    @staticmethod
    def _determine_status(markers: List[PolarityMarker]) -> str:
        """
        "marked"    – at least one high-confidence marker
        "ambiguous" – only low-confidence markers, or mixed conflicting types
        """
        if not markers:
            return "unmarked"
        max_conf = max(m.confidence for m in markers)
        if max_conf >= 0.75:
            return "marked"
        return "ambiguous"

    # ── Utility ───────────────────────────────────────────────────────────

    @staticmethod
    def _natural_sort_key(ref: str) -> Tuple:
        """
        Natural sort key so "D2" < "D10".
        Returns a tuple (alpha_prefix, numeric_suffix).
        """
        import re
        match = re.match(r"([A-Za-z]+)(\d+)(.*)", ref)
        if match:
            return (match.group(1).upper(), int(match.group(2)), match.group(3))
        return (ref.upper(), 0, "")



