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

# Net name keywords that identify the cathode (GND) side of a diode
_GND_KEYWORDS = ("GND", "VSS", "AGND", "PGND", "DGND", "0V", "EARTH",
                 "SGND", "CHASSIS", "PE", "SHIELD")
# Net name keywords that identify the anode (supply) side
_VCC_KEYWORDS = ("VCC", "VDD", "VIN", "VBUS", "VSUPPLY", "VMOT",
                 "+5V", "+3V3", "+3.3V", "+12V", "+24V", "+48V", "AVCC")


# ─────────────────────────────────────────────────────────────────────────────
# Internal data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _ODBPin:
    number: int     # 1-based pin number
    x: float        # absolute X in source units
    y: float        # absolute Y in source units
    net_id: int = 0 # net index (used for cathode detection via netlist)


@dataclass
class _ODBComponent:
    ref: str
    x: float            # centre X in source units
    y: float            # centre Y in source units
    rotation: float     # degrees
    mirrored: bool
    side: str = "top"   # "top" or "bot"
    pins: List[_ODBPin] = field(default_factory=list)
    package: str = ""   # PKG_TYPE from PRP line (IPC-7351 footprint name)
    description: str = ""  # PRP Description value

    # Set by resolve_polarity() — None means use heuristic
    _cathode_pin_num: Optional[int] = field(default=None, repr=False)

    def resolve_polarity(self, net_map: Dict[int, str]) -> None:
        """Use net names from the ODB++ netlist to find the cathode pin.

        Searches each pin's net name for GND-type keywords (→ cathode) or
        VCC-type keywords (→ anode, so the *other* pin is cathode).
        Only applied to diodes and LEDs.
        """
        if self.comp_type not in ("diode", "led"):
            return
        if len(self.pins) < 2:
            return

        # Pass 1: direct GND keyword → that pin is cathode
        for pin in self.pins:
            nm = net_map.get(pin.net_id, "").upper()
            if any(kw in nm for kw in _GND_KEYWORDS):
                self._cathode_pin_num = pin.number
                return

        # Pass 2: VCC keyword → the other pin is cathode
        for pin in self.pins:
            nm = net_map.get(pin.net_id, "").upper()
            if any(kw in nm for kw in _VCC_KEYWORDS):
                others = [p for p in self.pins if p.number != pin.number]
                if others:
                    self._cathode_pin_num = others[0].number
                    return

    @property
    def pin1(self) -> Optional[_ODBPin]:
        for p in self.pins:
            if p.number == 1:
                return p
        return self.pins[0] if self.pins else None

    @property
    def pin2(self) -> Optional[_ODBPin]:
        for p in self.pins:
            if p.number == 2:
                return p
        return None

    @property
    def polarity_pin(self) -> Optional[_ODBPin]:
        """Pin to mark for polarity.

        - Diodes / LEDs: the cathode pin determined by:
            1. Net-name lookup (most reliable, via resolve_polarity())
            2. Fallback: pin 2 (standard SMD diode convention: pin1=anode, pin2=cathode)
        - All other components: pin 1.
        """
        if self.comp_type in ("diode", "led"):
            if self._cathode_pin_num is not None:
                for p in self.pins:
                    if p.number == self._cathode_pin_num:
                        return p
            # Fallback heuristic: pin 2 = cathode for 2-pin SMD diodes
            return self.pin2 or self.pin1
        return self.pin1

    @property
    def pin_span_mm(self) -> float:
        """Max span between pin positions in source units."""
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
        ctype = self.comp_type
        if ctype not in POLAR_TYPES:
            return False
        if ctype == "capacitor" and self._is_ceramic_cap():
            return False
        return True

    def _is_ceramic_cap(self) -> bool:
        pkg = self.package.upper()
        if pkg:
            if pkg.startswith("CAPC") or pkg.startswith("CAPMP"):
                return True
            if pkg.startswith("CAPE") or pkg.startswith("CAPPR") or pkg.startswith("CAPT"):
                return False
        if len(self.pins) == 2 and self.pin_span_mm < 5.0:
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
    r"([\d.eE+-]+)\s+([\d.eE+-]+)\s+"  # x, y
    r"[\d.eE+-]+\s+"                    # rotation (skip)
    r"[NY]\s+"                          # mirrored (skip)
    r"(\d+)\s+\d+\s+"                  # net_id (capture), subnet (skip)
    r"(\d+)",                           # pin_number
    re.IGNORECASE,
)

# PRP PKG_TYPE 'value'
_RE_PRP_PKG = re.compile(r"^PRP\s+PKG_TYPE\s+'([^']*)'", re.IGNORECASE)
# PRP Description 'value'
_RE_PRP_DESC = re.compile(r"^PRP\s+Description\s+'([^']*)'", re.IGNORECASE)


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

            # Build net map from cadnet netlist
            net_map = {}
            for netlist_path in ("netlists/cadnet/netlist",):
                nl_cands = [n for n in names if n.lower().endswith(netlist_path)]
                if nl_cands:
                    net_map = self._parse_netlist(
                        zf.read(nl_cands[0]).decode("utf-8", errors="replace")
                    )
                    break

            for side in ("top", "bot"):
                pattern = f"comp_+_{side}/components"
                candidates = [n for n in names if n.endswith(pattern)]
                if not candidates:
                    continue
                content = zf.read(candidates[0]).decode("utf-8", errors="replace")
                scale = self._detect_scale(content)
                batch = self._parse_components(content, side)
                for c in batch:
                    c.resolve_polarity(net_map)
                comps.extend(batch)
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

            # Build net map from cadnet netlist
            net_map = {}
            for netlist_suffix in ("netlists/cadnet/netlist",):
                nl_cands = [n for n in names
                            if n.lower().replace("\\", "/").endswith(netlist_suffix)]
                if nl_cands:
                    f = tf.extractfile(tf.getmember(nl_cands[0]))
                    if f:
                        net_map = self._parse_netlist(
                            f.read().decode("utf-8", errors="replace")
                        )
                    break

            for side in ("top", "bot"):
                pattern = f"comp_+_{side}/components"
                candidates = [n for n in names if n.endswith(pattern)]
                if not candidates:
                    continue
                f = tf.extractfile(tf.getmember(candidates[0]))
                content = f.read().decode("utf-8", errors="replace")
                scale = self._detect_scale(content)
                batch = self._parse_components(content, side)
                for c in batch:
                    c.resolve_polarity(net_map)
                comps.extend(batch)
            if not comps:
                raise FileNotFoundError(
                    f"No comp_+_top/components in TGZ.\nContents: {names[:30]}"
                )
        return comps, scale

    # ── Directory ─────────────────────────────────────────────────────────

    def _from_dir(self, dir_path: str) -> Tuple[List[_ODBComponent], float]:
        comps: List[_ODBComponent] = []
        scale = MM_TO_PT
        net_map = {}

        # Try to find cadnet netlist
        for root, _dirs, files in os.walk(dir_path):
            if "netlist" in files and "cadnet" in root.lower():
                fpath = os.path.join(root, "netlist")
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as fh:
                        net_map = self._parse_netlist(fh.read())
                except Exception:
                    pass
                break

        for side in ("top", "bot"):
            for root, _dirs, files in os.walk(dir_path):
                dirname = os.path.basename(root).lower()
                if dirname == f"comp_+_{side}" and "components" in files:
                    fpath = os.path.join(root, "components")
                    with open(fpath, encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    scale = self._detect_scale(content)
                    batch = self._parse_components(content, side)
                    for c in batch:
                        c.resolve_polarity(net_map)
                    comps.extend(batch)
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

    # ── Netlist parser (cadnet format: "$0 NET_NAME") ─────────────────────

    @staticmethod
    def _parse_netlist(content: str) -> Dict[int, str]:
        """Parse ODB++ cadnet netlist → {net_id: net_name}."""
        net_map: Dict[int, str] = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("H "):
                continue
            if line.startswith("$"):
                parts = line.split(None, 1)
                if len(parts) == 2:
                    try:
                        net_map[int(parts[0][1:])] = parts[1].strip()
                    except ValueError:
                        pass
        return net_map

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

            # Parse PKG_TYPE property
            mp = _RE_PRP_PKG.match(line)
            if mp:
                current.package = mp.group(1)
                continue

            # Parse Description property
            md = _RE_PRP_DESC.match(line)
            if md:
                current.description = md.group(1)
                continue

            # Parse TOP line (pad / pin)
            m = _RE_TOP.match(line)
            if m:
                current.pins.append(_ODBPin(
                    x=float(m.group(1)),
                    y=float(m.group(2)),
                    net_id=int(m.group(3)),
                    number=int(m.group(4)),
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
            p_pin = oc.polarity_pin
            if p_pin is not None and oc.is_polar:
                p1x = p_pin.x * unit_scale
                p1y = p_pin.y * unit_scale
                mtype = "cathode_odb" if oc.comp_type in ("diode", "led") else "pin1_odb"
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
