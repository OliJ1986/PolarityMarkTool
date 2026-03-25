"""
core/component_detector.py
──────────────────────────
Detects SMT component reference designators from text elements extracted
by the PDF parser using regular expressions.

Supported prefixes (case-insensitive):
    D / LED / LD  – diodes, LEDs
    C             – capacitors
    IC / U        – integrated circuits
    Q / T         – transistors
    R             – resistors
    L             – inductors
    J / P / CN    – connectors
    F             – fuses
"""
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Pattern

from utils.geometry import BoundingBox, Point
from core.pdf_parser import TextElement


# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns  (anchored so "C" in "CONNECTOR" doesn't match "C1")
# ─────────────────────────────────────────────────────────────────────────────

_PATTERNS: Dict[str, Pattern] = {
    "diode":      re.compile(r"^D\d+[A-Z]?$",              re.IGNORECASE),
    "led":        re.compile(r"^(?:LED|LD)\d+[A-Z]?$",     re.IGNORECASE),
    "capacitor":  re.compile(r"^C\d+[A-Z]?$",              re.IGNORECASE),
    "ic":         re.compile(r"^(?:IC|U)\d+[A-Z]?$",       re.IGNORECASE),
    "transistor": re.compile(r"^(?:Q|T)\d+[A-Z]?$",        re.IGNORECASE),
    "resistor":   re.compile(r"^R\d+[A-Z]?$",              re.IGNORECASE),
    "inductor":   re.compile(r"^L\d+[A-Z]?$",              re.IGNORECASE),
    "connector":  re.compile(r"^(?:J|P|CN|XP)\d+[A-Z]?$",  re.IGNORECASE),
    "fuse":       re.compile(r"^F\d+[A-Z]?$",              re.IGNORECASE),
    # Additional component types commonly seen in KiCad PCB PDFs
    "test_point": re.compile(r"^TP\d+[A-Z]?$",             re.IGNORECASE),
    "fiducial":   re.compile(r"^FID\d+[A-Z]?$",            re.IGNORECASE),
    "antenna":    re.compile(r"^AE\d+[A-Z]?$",             re.IGNORECASE),
}

# Component types that commonly carry polarity markers
POLAR_TYPES = {"diode", "led", "capacitor", "ic", "transistor"}


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Component:
    """A detected component reference designator."""
    ref: str              # e.g. "D1", "C45", "LED3"
    comp_type: str        # key from _PATTERNS
    bbox: BoundingBox     # bounding box of the reference text span
    center: Point         # centre of bbox
    page: int             # 0-based page index

    @property
    def is_polar(self) -> bool:
        """True if this component type typically carries a polarity marker."""
        return self.comp_type in POLAR_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────────────

class ComponentDetector:
    """
    Scans a list of TextElement objects and returns detected Component instances.

    PCB PDFs (especially KiCad exports) often contain DUPLICATE reference
    designator text: once for the silkscreen layer and once for the fab/
    courtyard layer.  The detector deduplicates by (ref, page), keeping the
    instance with the largest bounding box (= most visible text).

    It also handles the rare case where a ref-des is split across two adjacent
    text spans (e.g. "D" and "1").  The merge is very conservative: it only
    fires when the current span does NOT already match a pattern AND the merged
    result DOES match one.
    """

    # Maximum horizontal gap (pt) between spans to attempt a merge.
    # Kept very small (1.5 pt) to avoid merging distinct but nearby labels.
    MERGE_GAP_PX: float = 1.5

    def detect(self, text_elements: List[TextElement]) -> List[Component]:
        """Return a deduplicated list of Component objects."""
        merged   = self._merge_split_spans(text_elements)
        raw      = [c for c in (self._try_match(e) for e in merged) if c]
        return self._deduplicate(raw)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _merge_split_spans(self, elements: List[TextElement]) -> List[TextElement]:
        """
        Attempt to merge split ref-des spans (e.g. "D" + "1" → "D1").

        CRITICAL RULE: if a span already matches a component pattern it is
        passed through unchanged.  This prevents duplicate silkscreen/fab
        layer entries (e.g. two "R6" spans 2 pt apart) from being merged
        into garbage strings like "R6R6".
        """
        if not elements:
            return []

        # Sort by (page, y-centre bucket, x0) so left-to-right reading order
        sorted_elems = sorted(
            elements,
            key=lambda e: (e.page, round(e.bbox.center.y, 1), e.bbox.x0),
        )

        result: List[TextElement] = []
        skip: set = set()           # indices already consumed by a merge

        for i, current in enumerate(sorted_elems):
            if i in skip:
                continue

            # ── Fast path: already a complete ref-des ─────────────────────
            # Do NOT attempt to merge – avoids "R6" + "R6" → "R6R6" bug
            if self._try_match(current) is not None:
                result.append(current)
                continue

            # ── Slow path: try merging with the next span ─────────────────
            # Only attempt a single merge (handles "D"+"1" style splits).
            found = False
            if i + 1 < len(sorted_elems) and (i + 1) not in skip:
                nxt = sorted_elems[i + 1]
                same_page = current.page == nxt.page
                same_line = abs(current.bbox.center.y - nxt.bbox.center.y) < 3.0
                # Gap must be non-negative (nxt is to the right) and tiny
                gap = nxt.bbox.x0 - current.bbox.x1
                close_x = 0.0 <= gap < self.MERGE_GAP_PX
                if same_page and same_line and close_x:
                    candidate = TextElement(
                        text=current.text + nxt.text,
                        bbox=current.bbox.union(nxt.bbox),
                        page=current.page,
                        font_size=current.font_size,
                        color=current.color,
                    )
                    if self._try_match(candidate) is not None:
                        result.append(candidate)
                        skip.add(i + 1)
                        found = True

            if not found:
                result.append(current)   # keep as-is (may not match – filtered later)

        return result

    @staticmethod
    def _try_match(elem: TextElement) -> Optional[Component]:
        """Return a Component if *elem.text* matches any ref-des pattern."""
        text = elem.text.strip()
        for comp_type, pattern in _PATTERNS.items():
            if pattern.match(text):
                return Component(
                    ref=text,
                    comp_type=comp_type,
                    bbox=elem.bbox,
                    center=elem.bbox.center,
                    page=elem.page,
                )
        return None

    @staticmethod
    def _deduplicate(components: List[Component]) -> List[Component]:
        """
        Remove duplicate detections of the same ref on the same page.
        Keeps the instance with the largest bounding-box area (most visible).
        """
        best: Dict[tuple, Component] = {}
        for comp in components:
            key = (comp.ref.upper(), comp.page)
            existing = best.get(key)
            if existing is None or comp.bbox.area > existing.bbox.area:
                best[key] = comp
        return list(best.values())




