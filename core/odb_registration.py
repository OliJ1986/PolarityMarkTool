"""
core/odb_registration.py
─────────────────────────
Registers ODB++ component positions (millimetres, Y-up) to the
PDF coordinate system (points, Y-down) using matched reference
designator labels as tie-points.

Two-pass algorithm
──────────────────
1. Match components by ref designator (ODB++ ref ↔ PDF text ref).
2. Compute a rough transform using ALL pairs (median offsets).
3. Evaluate residuals — keep only pairs with residual < 2 pt.
   These are fiducials, test-points, and small components whose
   text label happens to sit at the body centre.
4. Recompute the transform using ONLY the inlier pairs.
   This eliminates the bias caused by text labels placed at
   arbitrary offsets from the component body.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from utils.geometry import BoundingBox, Point
from core.component_detector import Component
from core.polarity_detector import PolarityMarker

MM_TO_PT: float = 72.0 / 25.4


class RegistrationError(ValueError):
    """Raised when coordinate registration cannot be performed."""


@dataclass
class _Transform:
    scale: float
    tx: float
    ty: float
    y_sign: float     # +1 = Y-normal,  -1 = Y-flipped

    def apply(self, x_mm: float, y_mm: float) -> Tuple[float, float]:
        x_pt = self.scale * x_mm + self.tx
        y_pt = self.y_sign * self.scale * y_mm + self.ty
        return x_pt, y_pt

    def residual(self, x_mm: float, y_mm: float, x_pdf: float, y_pdf: float) -> float:
        xp, yp = self.apply(x_mm, y_mm)
        return ((xp - x_pdf) ** 2 + (yp - y_pdf) ** 2) ** 0.5


def _mad(values: List[float]) -> float:
    if not values:
        return 0.0
    med = statistics.median(values)
    return statistics.median(abs(v - med) for v in values)


def _fit_transform(
    pairs: List[Tuple[float, float, float, float]],
    unit_scale: float,
) -> Tuple[_Transform, float]:
    """Fit translation + y-flip from pairs, return (transform, y_sign)."""
    best: Optional[_Transform] = None
    best_spread = float("inf")
    for y_sign in (+1.0, -1.0):
        txs = [x_pt - unit_scale * x_mm for x_mm, _, x_pt, _ in pairs]
        tys = [y_pt - y_sign * unit_scale * y_mm for _, y_mm, _, y_pt in pairs]
        spread = _mad(txs) + _mad(tys)
        if spread < best_spread:
            best_spread = spread
            best = _Transform(
                scale=unit_scale,
                tx=statistics.median(txs),
                ty=statistics.median(tys),
                y_sign=y_sign,
            )
    return best, best_spread


# Inlier threshold: only pairs closer than this (pt) are used for refinement
_INLIER_THRESHOLD_PT: float = 2.0


def _find_dominant_cluster(
    values: List[float],
    radius: float = 1.0,
) -> float:
    """
    Find the value around which the most other values cluster (within
    *radius*), and return the mean of that cluster.

    This is robust against asymmetric outlier distributions where median
    fails (e.g. 5 fiducials at 0.0, 20 caps at -5, 10 caps at +6).
    """
    if not values:
        return 0.0
    if len(values) <= 2:
        return statistics.median(values)

    best_center = values[0]
    best_count = 0

    for v in values:
        count = sum(1 for u in values if abs(u - v) <= radius)
        if count > best_count:
            best_count = count
            best_center = v

    # Take mean of the winning cluster
    cluster = [u for u in values if abs(u - best_center) <= radius]
    return statistics.mean(cluster)


def register(
    odb_comps,
    pdf_comps: List[Component],
    unit_scale: float = MM_TO_PT,
) -> _Transform:
    """
    Compute the ODB++ → PDF coordinate transform.

    Uses cluster analysis on per-pair translation offsets to find the
    tightest group (typically fiducials and test points with text
    exactly at the body centre).  This is immune to the scattered
    text-placement offsets that plague median-based approaches.
    """
    pdf_by_ref: Dict[str, Component] = {c.ref: c for c in pdf_comps}

    # Build matched pairs.
    # Exclude test points (TP*) — their text has a fixed Y-offset from
    # the pad centre that biases the registration.
    _SKIP_PREFIXES = ("TP",)
    pairs: List[Tuple[float, float, float, float]] = []
    for oc in odb_comps:
        if any(oc.ref.startswith(pfx) for pfx in _SKIP_PREFIXES):
            continue
        pc = pdf_by_ref.get(oc.ref)
        if pc is None:
            continue
        pairs.append((oc.x, oc.y, pc.center.x, pc.center.y))

    if len(pairs) < 2:
        raise RegistrationError(
            f"Only {len(pairs)} matching component(s) between ODB++ and PDF (need ≥ 2)."
        )

    # Try both Y orientations, pick the one with a tighter cluster
    best_transform: Optional[_Transform] = None
    best_cluster_size = -1

    for y_sign in (+1.0, -1.0):
        txs = [x_pt - unit_scale * x_mm for x_mm, _, x_pt, _ in pairs]
        tys = [y_pt - y_sign * unit_scale * y_mm for _, y_mm, _, y_pt in pairs]

        # Find the dominant cluster for tx and ty independently
        tx = _find_dominant_cluster(txs, radius=1.0)
        ty = _find_dominant_cluster(tys, radius=1.0)

        # Count how many pairs fit this transform within 1.5 pt
        t = _Transform(scale=unit_scale, tx=tx, ty=ty, y_sign=y_sign)
        n_fit = sum(
            1 for p in pairs
            if t.residual(p[0], p[1], p[2], p[3]) < 1.5
        )

        if n_fit > best_cluster_size:
            best_cluster_size = n_fit
            best_transform = t

    assert best_transform is not None
    return best_transform


def odb_to_pdf_markers(
    odb_comps,
    pdf_comps: List[Component],
    unit_scale: float = MM_TO_PT,
) -> Tuple[List[PolarityMarker], _Transform, Dict[str, PolarityMarker]]:
    """
    Register ODB++ component positions to the PDF coordinate system,
    then return one ``PolarityMarker`` per ODB++ polar component whose
    pin-1 can be located, expressed in PDF-point coordinates.

    Returns
    -------
    markers : list of PolarityMarker
        One marker per polar component with a detectable pin-1.
    transform : _Transform
        The solved coordinate transform (for diagnostics / debug).
    by_ref : dict[str, PolarityMarker]
        ``{ ref: marker }`` lookup for merging into MatchResult list.
    """
    transform = register(odb_comps, pdf_comps, unit_scale)

    by_ref: Dict[str, PolarityMarker] = {}
    markers: List[PolarityMarker] = []

    for oc in odb_comps:
        if not oc.is_polar:
            continue
        p1 = oc.pin1
        if p1 is None:
            continue

        x_pt, y_pt = transform.apply(p1.x, p1.y)

        marker = PolarityMarker(
            marker_type="pin1_odb",
            bbox=BoundingBox(x_pt - 2.5, y_pt - 2.5, x_pt + 2.5, y_pt + 2.5),
            center=Point(x_pt, y_pt),
            page=0,
            confidence=0.99,
            source="odb",
        )
        by_ref[oc.ref] = marker
        markers.append(marker)

    return markers, transform, by_ref




