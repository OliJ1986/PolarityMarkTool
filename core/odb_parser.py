"""
core/odb_parser.py
──────────────────
Parses ODB++ fabrication data to extract component placement with
**exact pin-1 positions** — giving 99%-confidence polarity markers.

The actual data lives in:
    steps/pcb/layers/comp_+_top/components   (top-side components)
    steps/pcb/layers/comp_+_bot/components   (bottom-side components)

Format (KiCad ODB++ v7+)::

    CMP <pkg_idx> <x> <y> <rotation> <N/Y> <refdes> <package> ;0=<mount>
    PRP <key> '<value>'
    TOP <pad_idx> <abs_x> <abs_y> <rot> <N/Y> <net_id> <subnet> <pin_number>

The TOP lines give **absolute** pad coordinates in mm — no rotation
needed.  pin_number (last field) is the 1-based pin number.
"""
from __future__ import annotations

import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from utils.geometry import BoundingBox, Point
from core.component_detector import Component, _PATTERNS, POLAR_TYPES
from core.polarity_detector import PolarityMarker

MM_TO_PT: float = 72.0 / 25.4
INCH_TO_PT: float = 72.0


# ─────────────────────────────────────────────────────────────────────────────
# Internal data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _ODBPin:
    number: int     # 1-based pin number
    x: float        # absolute X in source units (mm)
    y: float        # absolute Y in source units (mm)


@dataclass
class _ODBComponent:
    ref: str
    x: float            # centre X in mm
    y: float            # centre Y in mm
    rotation: float     # degrees
    mirrored: bool
    side: str = "top"   # "top" or "bot"
    pins: List[_ODBPin] = field(default_factory=list)
    package: str = ""   # PKG_TYPE from PRP line (IPC-7351 footprint name)

    @property
    def pin1(self) -> Optional[_ODBPin]:
        """Return pin number 1, or the first pin if available."""
        for p in self.pins:
            if p.number == 1:
                return p
        return self.pins[0] if self.pins else None

    @property
    def pin2(self) -> Optional[_ODBPin]:
        """Return pin number 2 (useful for diode cathode)."""
        for p in self.pins:
            if p.number == 2:
                return p
        return None

    @property
    def polarity_pin(self) -> Optional[_ODBPin]:
        """Return the pin to mark for polarity.

        - Diodes / LEDs: mark pin **2** (cathode) because the physical
          PCB marking (band) indicates the cathode side, while ODB++
          pin 1 is the anode.
        - All other components: mark pin 1.
        """
        if self.comp_type in ("diode", "led"):
            return self.pin2 or self.pin1
        return self.pin1

    @property
    def pin_span_mm(self) -> float:
        """Max span of pin positions in mm (rough component body size)."""
        if len(self.pins) < 2:
            return 0.0
        xs = [p.x for p in self.pins]
        ys = [p.y for p in self.pins]
        return max(max(xs) - min(xs), max(ys) - min(ys))

    @property
    def comp_type(self) -> str:
        for ctype, pat in _PATTERNS.items():
            if pat.match(self.ref):
                return ctype
        return "unknown"

    @property
    def is_polar(self) -> bool:
        """True if this component typically carries a polarity marker.

        Ceramic capacitors (IPC-7351 prefix CAPC, or small 2-pin caps
        without electrolytic/tantalum package indicator) are non-polar
        and are excluded.
        """
        ctype = self.comp_type
        if ctype not in POLAR_TYPES:
            return False
        # Ceramic capacitors are non-polar
        if ctype == "capacitor" and self._is_ceramic_cap():
            return False
        return True

    def _is_ceramic_cap(self) -> bool:
        """Heuristic: detect non-polar ceramic / film capacitors.

        Returns True if the package/properties indicate a non-polar cap.
        Electrolytic (CAPE*, CAPPR*), tantalum (CAPT*) remain polar.
        """
        pkg = self.package.upper()
        if pkg:
            # IPC-7351 naming: CAPC = ceramic chip, CAPMP = metallized polyester
            # Polar caps: CAPE (electrolytic), CAPPR (radial polar), CAPT (tantalum)
            if pkg.startswith("CAPC") or pkg.startswith("CAPMP"):
                return True
            if pkg.startswith("CAPE") or pkg.startswith("CAPPR") or pkg.startswith("CAPT"):
                return False
        # Heuristic: 2-pin small caps (≤1210) are very likely ceramic
        if len(self.pins) == 2 and self.pin_span_mm < 5.0:
            # Without explicit polar package info, assume ceramic
            if not pkg or not any(kw in pkg for kw in ("EL", "POL", "TANT", "ELKO")):
                return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns for comp_+_top/components format
# ─────────────────────────────────────────────────────────────────────────────

# CMP <pkg_idx> <x> <y> <rotation> <N/Y> <refdes> <package> ;...
_RE_CMP = re.compile(
    r"^CMP\s+\d+\s+"
    r"([\d.eE+-]+)\s+([\d.eE+-]+)\s+"
    r"([\d.eE+-]+)\s+"
    r"([NY])\s+"
    r"(\S+)\s+"
    r"(\S+)",
    re.IGNORECASE,
)

# TOP <pad_idx> <abs_x> <abs_y> <rot> <N/Y> <net_id> <subnet> <pin_number>
_RE_TOP = re.compile(
    r"^TOP\s+\d+\s+"
    r"([\d.eE+-]+)\s+([\d.eE+-]+)\s+"
    r"[\d.eE+-]+\s+"
    r"[NY]\s+"
    r"\d+\s+\d+\s+"
    r"(\d+)",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

class ODBParser:
    """
    Parses ODB++ and returns ``MatchResult`` per component.
    Accepts .zip, .tgz/.tar.gz, or unzipped directory.
    """

    def parse(self, path: str) -> "List[MatchResult]":
        from core.matcher import MatchResult
        odb_comps, scale = self._load(path)
        return self._build_results(odb_comps, scale)

    def _load(self, path: str) -> Tuple[List[_ODBComponent], float]:
        low = path.lower()
        if os.path.isfile(path) and low.endswith(".zip"):
            return self._from_zip(path)
        elif os.path.isfile(path) and (
            low.endswith(".tgz") or low.endswith(".tar.gz") or low.endswith(".tar")
        ):
            return self._from_tgz(path)
        elif os.path.isdir(path):
            return self._from_dir(path)
        else:
            raise ValueError(f"Unsupported ODB++ path: {path}")

    # ── ZIP ───────────────────────────────────────────────────────────────

    def _from_zip(self, zip_path: str) -> Tuple[List[_ODBComponent], float]:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            comps: List[_ODBComponent] = []
            scale = MM_TO_PT
            for side in ("top", "bot"):
                pattern = f"comp_+_{side}/components"
                candidates = [n for n in names if n.endswith(pattern)]
                if not candidates:
                    continue
                content = zf.read(candidates[0]).decode("utf-8", errors="replace")
                scale = self._detect_scale(content)
                comps.extend(self._parse_components(content, side))
            if not comps:
                raise FileNotFoundError(
                    f"No comp_+_top/components found in ZIP.\n"
                    f"Contents: {names[:30]}"
                )
        return comps, scale

    # ── TGZ ───────────────────────────────────────────────────────────────

    def _from_tgz(self, tgz_path: str) -> Tuple[List[_ODBComponent], float]:
        mode = "r:gz" if (tgz_path.lower().endswith(".gz") or
                          tgz_path.lower().endswith(".tgz")) else "r"
        with tarfile.open(tgz_path, mode) as tf:
            names = tf.getnames()
            comps: List[_ODBComponent] = []
            scale = MM_TO_PT
            for side in ("top", "bot"):
                pattern = f"comp_+_{side}/components"
                candidates = [n for n in names if n.endswith(pattern)]
                if not candidates:
                    continue
                f = tf.extractfile(tf.getmember(candidates[0]))
                content = f.read().decode("utf-8", errors="replace")
                scale = self._detect_scale(content)
                comps.extend(self._parse_components(content, side))
            if not comps:
                raise FileNotFoundError(
                    f"No comp_+_top/components in TGZ.\nContents: {names[:30]}"
                )
        return comps, scale

    # ── Directory ─────────────────────────────────────────────────────────

    def _from_dir(self, dir_path: str) -> Tuple[List[_ODBComponent], float]:
        comps: List[_ODBComponent] = []
        scale = MM_TO_PT
        for side in ("top", "bot"):
            for root, _dirs, files in os.walk(dir_path):
                dirname = os.path.basename(root).lower()
                if dirname == f"comp_+_{side}" and "components" in files:
                    fpath = os.path.join(root, "components")
                    with open(fpath, encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    scale = self._detect_scale(content)
                    comps.extend(self._parse_components(content, side))
        if not comps:
            raise FileNotFoundError(f"No comp_+_top/components under: {dir_path}")
        return comps, scale

    # ── Unit detection ────────────────────────────────────────────────────

    @staticmethod
    def _detect_scale(content: str) -> float:
        for line in content.splitlines()[:15]:
            stripped = line.strip().upper()
            if stripped.startswith("UNITS"):
                if "INCH" in stripped:
                    return INCH_TO_PT
                return MM_TO_PT
        return MM_TO_PT

    # ── Component file parser ─────────────────────────────────────────────

    @staticmethod
    def _parse_components(content: str, side: str) -> List[_ODBComponent]:
        components: List[_ODBComponent] = []
        current: Optional[_ODBComponent] = None

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            m = _RE_CMP.match(line)
            if m:
                current = _ODBComponent(
                    ref=m.group(5),
                    x=float(m.group(1)),
                    y=float(m.group(2)),
                    rotation=float(m.group(3)),
                    mirrored=(m.group(4).upper() == "Y"),
                    side=side,
                )
                components.append(current)
                continue

            if current is None:
                continue

            # Parse PKG_TYPE property for package identification
            if line.startswith("PRP ") and "PKG_TYPE" in line.upper():
                # PRP PKG_TYPE 'CAPC1709X90N'
                m_prp = re.match(
                    r"^PRP\s+PKG_TYPE\s+'([^']*)'",
                    line, re.IGNORECASE,
                )
                if m_prp:
                    current.package = m_prp.group(1)
                continue

            m = _RE_TOP.match(line)
            if m:
                current.pins.append(_ODBPin(
                    number=int(m.group(3)),
                    x=float(m.group(1)),
                    y=float(m.group(2)),
                ))
        return components

    # ── Build MatchResult ─────────────────────────────────────────────────

    @staticmethod
    def _build_results(
        odb_comps: List[_ODBComponent],
        unit_scale: float = MM_TO_PT,
    ) -> "List[MatchResult]":
        from core.matcher import MatchResult

        results = []
        for oc in odb_comps:
            cx_pt = oc.x * unit_scale
            cy_pt = oc.y * unit_scale
            comp = Component(
                ref=oc.ref,
                comp_type=oc.comp_type,
                bbox=BoundingBox(cx_pt - 3, cy_pt - 3, cx_pt + 3, cy_pt + 3),
                center=Point(cx_pt, cy_pt),
                page=0,
            )
            # Use polarity_pin: for diodes → cathode (pin 2), else → pin 1
            p_pin = oc.polarity_pin
            if p_pin is not None and oc.is_polar:
                p1x = p_pin.x * unit_scale
                p1y = p_pin.y * unit_scale
                # Determine marker type label
                if oc.comp_type in ("diode", "led"):
                    mtype = "cathode_odb"
                else:
                    mtype = "pin1_odb"
                marker = PolarityMarker(
                    marker_type=mtype,
                    bbox=BoundingBox(p1x - 2, p1y - 2, p1x + 2, p1y + 2),
                    center=Point(p1x, p1y),
                    page=0,
                    confidence=0.99,
                    source="odb",
                )
                results.append(MatchResult(
                    component=comp,
                    markers=[marker],
                    polarity_status="marked",
                    overall_confidence=0.99,
                ))
            else:
                results.append(MatchResult(
                    component=comp,
                    polarity_status="unmarked",
                ))
        return results


# ─────────────────────────────────────────────────────────────────────────────
# Convenience functions
# ─────────────────────────────────────────────────────────────────────────────

def parse_odb(path: str) -> "List[MatchResult]":
    return ODBParser().parse(path)


def parse_odb_raw(path: str):
    """Return (odb_components, unit_scale) for coordinate registration."""
    return ODBParser()._load(path)



