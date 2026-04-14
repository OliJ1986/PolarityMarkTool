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
    number: int     # 1-based pin number (0 if non-numeric name)
    x: float        # absolute X in source units
    y: float        # absolute Y in source units
    net_id: int = 0 # net index (used for cathode detection via netlist)
    name: str = ""  # pin identifier suffix: "1", "A", "K", "G", "S", "D", "P1", ...


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

    # Set by resolve_polarity() or set_cathode_from_silk() — None means use heuristic
    _cathode_pin_num:  Optional[int] = field(default=None, repr=False)
    _cathode_pin_name: Optional[str] = field(default=None, repr=False)
    # Tracks which method was used to determine the cathode
    # "silk" | "net_gnd" | "net_vcc" | "pin_name" | "pin1" | "fallback" | ""
    _detection_method: str = field(default="", repr=False)

    def set_cathode_from_silk(self, pin_idx: int) -> None:
        """Set cathode from silk / assembly layer geometric analysis.

        Silk detection is skipped only when functional pin names (K / A)
        are present — those are set by the component designer and are
        never wrong.

        Silk DOES override net_gnd because the GND heuristic is unreliable
        for protection / TVS / Schottky diodes where the anode (not cathode)
        often connects to GND.  The physical board geometry (diode triangle
        symbol) is the ground truth.
        """
        # Do not override K/A functional pin names — always correct
        has_k = any(p.name.upper() in _CATHODE_NAMES for p in self.pins)
        has_a = any(p.name.upper() in _ANODE_NAMES   for p in self.pins)
        if has_k or has_a:
            return  # pin names are unambiguous — leave them alone


        if 0 <= pin_idx < len(self.pins):
            pin = self.pins[pin_idx]
            self._cathode_pin_num  = pin.number
            self._cathode_pin_name = pin.name
            self._detection_method = "silk"

    def _is_led_package(self) -> bool:
        """Return True only for indicator LED / optocoupler packages.

        The VCC→other-pin heuristic is reliable only for these: an LED is
        forward-biased so cathode → VCC makes no sense.  For protection /
        Schottky / TVS diodes the cathode is often deliberately connected to
        a VCC rail (reverse-biased clamping), so the VCC heuristic would
        give the wrong answer there.
        """
        pkg  = self.package.upper()
        desc = self.description.upper()
        _LED_KEYWORDS = ("LED", "CHIPLED", "INDICATOR", "OPTO",
                         "OPTOCOUPLER", "PHOTO", "OPTOISO")
        return any(kw in pkg or kw in desc for kw in _LED_KEYWORDS)

    def resolve_polarity(self, net_map: Dict[int, str]) -> None:
        """Use net names from the ODB++ netlist to find the cathode pin.

        Pass 1 — direct GND keyword → that pin is cathode (reliable for all).
        Pass 2 — VCC keyword → other pin is cathode, but ONLY for indicator
                 LED / optocoupler packages.  Protection, Schottky and TVS
                 diodes are often reverse-biased with cathode at VCC, so
                 applying this heuristic generically gives wrong results.
        """
        if self.comp_type not in ("diode", "led"):
            return
        if len(self.pins) < 2:
            return

        # Pass 1: direct GND keyword → that pin is cathode
        for pin in self.pins:
            nm = net_map.get(pin.net_id, "").upper()
            if any(kw in nm for kw in _GND_KEYWORDS):
                self._cathode_pin_num  = pin.number
                self._cathode_pin_name = pin.name
                self._detection_method = "net_gnd"
                return

        # Pass 2: VCC keyword → the OTHER pin is cathode
        # Restricted to LED/indicator packages to avoid false positives on
        # protection diodes whose cathode intentionally connects to VCC.
        if self._is_led_package():
            for pin in self.pins:
                nm = net_map.get(pin.net_id, "").upper()
                if any(kw in nm for kw in _VCC_KEYWORDS):
                    others = [p for p in self.pins if p.number != pin.number]
                    if others:
                        self._cathode_pin_num  = others[0].number
                        self._cathode_pin_name = others[0].name
                        self._detection_method = "net_vcc"
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
            2. Functional pin name: 'K', 'KA', 'CAT', 'CATH', 'CATHODE', 'NEG'
            3. Anode identified → other pin is cathode: 'A', 'AN', 'ANODE', 'POS'
            4. Fallback: pin 2 (standard SMD diode convention: pin1=anode, pin2=cathode)
        - Transistors: gate/base pin ('G', 'GATE', 'B', 'BASE') or pin 1
        - All other components: pin 1.
        """
        if self.comp_type in ("diode", "led"):
            # 1. Net-name lookup — match by stored name first, then by number
            if self._cathode_pin_name is not None:
                for p in self.pins:
                    if p.name == self._cathode_pin_name:
                        return p
            if self._cathode_pin_num is not None:
                for p in self.pins:
                    if p.number == self._cathode_pin_num:
                        return p
            # 2. Functional cathode names
            for p in self.pins:
                if p.name.upper() in _CATHODE_NAMES:
                    return p
            # 3. Functional anode names → other pin is cathode
            for p in self.pins:
                if p.name.upper() in _ANODE_NAMES:
                    others = [x for x in self.pins if x is not p]
                    if others:
                        return others[0]
            # 4. Fallback: pin 2 = cathode
            return self.pin2 or self.pin1

        if self.comp_type == "transistor":
            # Gate / base = control pin for polarity marking
            for p in self.pins:
                if p.name.upper() in _GATE_NAMES:
                    return p
            return self.pin1

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
        desc = self.description.upper()

        # Determine if description suggests an electrolytic/polar capacitor:
        # "POLARIZED" is only electrolytic if NOT preceded by "UN"
        _ELEC_DESC_KW = ("ELKO", "ALU-ELKO", "ALUM", "TANT", "ELECTROLYTIC",
                         "EL.CAP", "ALUMINIUM", "ALUMINUM", "POLYMER")
        is_elec_desc = any(kw in desc for kw in _ELEC_DESC_KW)
        if "POLARIZED" in desc and "UNPOLAR" not in desc:
            is_elec_desc = True
        # Standalone ",EL," token in comma-separated descriptions (Xpedition style)
        if not is_elec_desc and re.search(r'(^|,|\s)EL($|,|\s)', desc):
            is_elec_desc = True

        # If description strongly suggests electrolytic → NOT ceramic
        if is_elec_desc:
            return False

        if pkg:
            # Standard IPC-7351 chip capacitor prefixes
            if pkg.startswith("CAPC") or pkg.startswith("CAPMP"):
                return True
            # Electrolytic / polar body types (package name contains these)
            _ELEC_PKG_KW = ("EL", "POL", "TANT", "ELKO", "ECAP", "ALUM",
                            "CAP_EL", "CAP_AL")
            if any(kw in pkg for kw in _ELEC_PKG_KW):
                return False
            # Electrolytic package shape prefixes (IPC-7351 radial/axial)
            if pkg.startswith(("CAPPR", "CAPE", "CAPT", "CP_ELEC", "CP_TANT")):
                return False
            # Standard SMD chip sizes (Altium/other CAD tool Body format)
            # These are all non-polar ceramic capacitor package designators
            _CHIP_SIZES = {"0201", "0402", "0603", "0805", "1206", "1210",
                           "1812", "2512", "1805", "0303", "0606", "1616"}
            if pkg in _CHIP_SIZES or any(pkg.startswith(s) for s in _CHIP_SIZES):
                return True
            # MLCC / ceramic keyword in package name
            if "MLCC" in pkg or pkg.startswith("CHIPLED"):
                return True

        # Fallback heuristic: 2 pins + small pitch → assume ceramic
        if len(self.pins) == 2 and self.pin_span_mm < 5.0:
            _elec_pkg_kw = ("EL", "POL", "TANT", "ELKO", "ECAP", "ALUM")
            if not any(kw in pkg for kw in _elec_pkg_kw) and not is_elec_desc:
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

# TOP <pad_idx> <abs_x> <abs_y> <rot> <N/Y> <net_id> <subnet> <pin_name>
# pin_name may be a plain integer ("1"), a functional name ("A","K","G","S","D"),
# or a REFDES-PINNAME string ("IC700-1", "D804-A", "D804-K").
_RE_TOP = re.compile(
    r"^TOP\s+\d+\s+"
    r"([\d.eE+-]+)\s+([\d.eE+-]+)\s+"  # x, y
    r"[\d.eE+-]+\s+"                    # rotation (skip)
    r"[NY]\s+"                          # mirrored (skip)
    r"(\d+)\s+\d+\s+"                  # net_id (capture), subnet (skip)
    r"(\S+)",                           # pin_name — may be "1","A","K","IC700-1", etc.
    re.IGNORECASE,
)

# PRP PKG_TYPE 'value'  ← KiCad / Altium "PKG_TYPE"
# PRP Body 'value'      ← Altium "Body" field
_RE_PRP_PKG = re.compile(r"^PRP\s+(?:PKG_TYPE|Body)\s+'([^']*)'", re.IGNORECASE)

# PRP Description 'value'           ← KiCad
# PRP ShortDesription 'value'       ← Altium (note typo in spec)
# PRP ShortDescription 'value'      ← Altium (correctly spelled)
# PRP Bezeichnung 'value'           ← German CAD tools
_RE_PRP_DESC = re.compile(
    r"^PRP\s+(?:Description|ShortDesription|ShortDescription|Bezeichnung)\s+'([^']*)'",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Functional pin name sets (used for polarity detection)
# ─────────────────────────────────────────────────────────────────────────────

# Pin names that indicate the CATHODE side of a diode/LED
_CATHODE_NAMES = frozenset({"K", "KA", "CAT", "CATH", "CATHODE", "NEG"})
# Pin names that indicate the ANODE side (the OTHER pin is cathode)
_ANODE_NAMES   = frozenset({"A", "AN", "ANODE", "POS"})
# Pin names that indicate the gate/base of a transistor
_GATE_NAMES    = frozenset({"G", "GATE", "B", "BASE"})


# ─────────────────────────────────────────────────────────────────────────────
# Pin name helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pin_suffix(raw_name: str) -> str:
    """Extract the pin identifier from an ODB++ pin name.

    Two common formats:
      - Plain:        '1', '2', 'A', 'K', 'G', 'P1'
      - RefDes-Pin:   'D804-A', 'IC700-1', 'IC700-P1'  → strip refdes prefix
    """
    if '-' in raw_name:
        return raw_name.rsplit('-', 1)[-1]
    return raw_name


def _pin_num(suffix: str) -> int:
    """Convert a pin suffix string to an integer pin number.

    'A', 'K', 'G', 'S', 'D' → 0  (functional names, not numbered)
    '1', '2', '3'            → 1, 2, 3
    'P1', 'P2'               → 1, 2  (thermal pad)
    '1A', '1B'               → 1     (multi-section)
    """
    if suffix.isdigit():
        return int(suffix)
    # P<n> thermal pad
    if len(suffix) > 1 and suffix[0].upper() == 'P' and suffix[1:].isdigit():
        return int(suffix[1:])
    # Leading digits (e.g. "1A", "1B")
    m = re.match(r'^(\d+)', suffix)
    if m:
        return int(m.group(1))
    return 0  # functional name — not a numbered pin


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
            comps, scale = self._from_zip(path)
        elif os.path.isfile(path) and (
            low.endswith(".tgz") or low.endswith(".tar.gz") or low.endswith(".tar")
        ):
            comps, scale = self._from_tgz(path)
        elif os.path.isdir(path):
            comps, scale = self._from_dir(path)
        else:
            raise ValueError(f"Unsupported ODB++ path: {path}")

        # ── Silk-layer cathode detection (priority 0 — overrides net heuristics) ──
        # The silkscreen physically shows the cathode band, making it the most
        # reliable source.  Runs after resolve_polarity() so it can override.
        try:
            from core.odb_silk_cathode import detect_cathodes
            silk = detect_cathodes(path, comps)
            if silk:
                comps_by_ref = {c.ref: c for c in comps}
                for ref, pin_idx in silk.items():
                    c = comps_by_ref.get(ref)
                    if c:
                        c.set_cathode_from_silk(pin_idx)
        except Exception:
            pass  # silk detection failure must never block analysis

        return comps, scale

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

            # Read board profile for fallback unit detection
            profile_content: Optional[str] = None
            prof_cands = [n for n in names
                          if re.search(r'/steps/[^/]+/profile$', n.lower())]
            if not prof_cands:
                prof_cands = [n for n in names if n.lower().endswith("/profile")]
            if prof_cands:
                try:
                    profile_content = zf.read(prof_cands[0]).decode(
                        "utf-8", errors="replace")
                except Exception:
                    pass

            for side in ("top", "bot"):
                pattern = f"comp_+_{side}/components"
                candidates = [n for n in names if n.endswith(pattern)]
                if not candidates:
                    continue
                content = zf.read(candidates[0]).decode("utf-8", errors="replace")
                scale = self._detect_scale(content)
                if scale == MM_TO_PT and profile_content is not None:
                    scale = self._resolve_scale_from_profile(content,
                                                             profile_content)
                batch = self._parse_components(content, side)
                if scale == INCH_TO_PT:
                    self._normalize_to_mm(batch)
                for c in batch:
                    c.resolve_polarity(net_map)
                comps.extend(batch)
            if not comps:
                raise FileNotFoundError(
                    f"No comp_+_top/components found in ZIP.\n"
                    f"Contents: {names[:30]}"
                )
        return comps, MM_TO_PT

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

            # Read board profile for fallback unit detection
            profile_content: Optional[str] = None
            prof_cands = [n for n in names
                          if re.search(r'/steps/[^/]+/profile$', n.lower())]
            if not prof_cands:
                prof_cands = [n for n in names if n.lower().endswith("/profile")]
            if prof_cands:
                f = tf.extractfile(tf.getmember(prof_cands[0]))
                if f:
                    profile_content = f.read().decode("utf-8", errors="replace")

            for side in ("top", "bot"):
                pattern = f"comp_+_{side}/components"
                candidates = [n for n in names if n.endswith(pattern)]
                if not candidates:
                    continue
                f = tf.extractfile(tf.getmember(candidates[0]))
                content = f.read().decode("utf-8", errors="replace")
                scale = self._detect_scale(content)
                # Xpedition Layout omits UNITS in components file → use profile
                if scale == MM_TO_PT and profile_content is not None:
                    scale = self._resolve_scale_from_profile(content,
                                                             profile_content)
                batch = self._parse_components(content, side)
                if scale == INCH_TO_PT:
                    self._normalize_to_mm(batch)
                for c in batch:
                    c.resolve_polarity(net_map)
                comps.extend(batch)
            if not comps:
                raise FileNotFoundError(
                    f"No comp_+_top/components in TGZ.\nContents: {names[:30]}"
                )
        # Coordinates are always in mm after optional normalization
        return comps, MM_TO_PT

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

        # Read board profile for fallback unit detection
        profile_content: Optional[str] = None
        for root, _dirs, files in os.walk(dir_path):
            if "profile" in files:
                fpath = os.path.join(root, "profile")
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as fh:
                        profile_content = fh.read()
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
                    if scale == MM_TO_PT and profile_content is not None:
                        scale = self._resolve_scale_from_profile(
                            content, profile_content)
                    batch = self._parse_components(content, side)
                    if scale == INCH_TO_PT:
                        self._normalize_to_mm(batch)
                    for c in batch:
                        c.resolve_polarity(net_map)
                    comps.extend(batch)
        if not comps:
            raise FileNotFoundError(f"No comp_+_top/components under: {dir_path}")
        return comps, MM_TO_PT

    # ── Unit detection ────────────────────────────────────────────────────

    @staticmethod
    def _detect_scale(content: str) -> float:
        """Detect coordinate scale from file header.

        Handles both formats:
          • ``UNITS=INCH`` / ``UNITS=MM``  (KiCad / old-style)
          • ``U INCH`` / ``U MM``           (ODB++ features / Xpedition profile)
        Returns MM_TO_PT (default) when no declaration is found.
        """
        for line in content.splitlines()[:15]:
            stripped = line.strip().upper()
            if stripped.startswith("UNITS"):
                if "INCH" in stripped:
                    return INCH_TO_PT
                return MM_TO_PT
            # ODB++ features/profile format: "U MM" or "U INCH"
            if re.match(r'^U\s+(MM|INCH)', stripped):
                if "INCH" in stripped:
                    return INCH_TO_PT
                return MM_TO_PT
        return MM_TO_PT

    @staticmethod
    def _resolve_scale_from_profile(comp_content: str,
                                    profile_content: str) -> float:
        """Fallback unit detection for *components* files that carry no explicit
        ``UNITS`` declaration (Xpedition Layout exports components in inches even
        when the rest of the board is in millimetres).

        Strategy: compare the component coordinate extent against the board
        bounding-box derived from the *profile* file.  The unit that produces a
        ratio closest to 1.0 wins.
        """
        # --- Profile bounding box (in mm) ---
        prof_scale = MM_TO_PT
        for line in profile_content.splitlines()[:15]:
            s = line.strip().upper()
            if s.startswith("UNITS"):
                prof_scale = INCH_TO_PT if "INCH" in s else MM_TO_PT
                break
            if re.match(r'^U\s+(MM|INCH)', s):
                prof_scale = INCH_TO_PT if "INCH" in s else MM_TO_PT
                break

        prof_c2mm = 25.4 if prof_scale == INCH_TO_PT else 1.0
        pxs: List[float] = []
        pys: List[float] = []
        for ln in profile_content.splitlines():
            m = re.match(r'^O[BS]\s+([\d.eE+-]+)\s+([\d.eE+-]+)',
                         ln.strip(), re.IGNORECASE)
            if m:
                pxs.append(float(m.group(1)) * prof_c2mm)
                pys.append(float(m.group(2)) * prof_c2mm)

        if not pxs:
            return MM_TO_PT

        prof_ext_x_mm = max(pxs) - min(pxs)
        prof_ext_y_mm = max(pys) - min(pys)
        if prof_ext_x_mm < 1.0:            # profile too small to be reliable
            return MM_TO_PT

        # --- Component coordinate extent ---
        cxs: List[float] = []
        cys: List[float] = []
        for ln in comp_content.splitlines():
            m = _RE_CMP.match(ln.strip())
            if m:
                cxs.append(float(m.group(1)))
                cys.append(float(m.group(2)))

        if not cxs:
            return MM_TO_PT

        comp_ext_x = max(cxs) - min(cxs)
        comp_ext_y = max(cys) - min(cys)
        if comp_ext_x < 1e-6:
            return MM_TO_PT

        # Compare ratios: ideal = 1.0 when units match
        ratio_as_mm   = comp_ext_x / prof_ext_x_mm
        ratio_as_inch = (comp_ext_x * 25.4) / prof_ext_x_mm

        if abs(ratio_as_inch - 1.0) < abs(ratio_as_mm - 1.0):
            return INCH_TO_PT
        return MM_TO_PT

    @staticmethod
    def _normalize_to_mm(comps: "List[_ODBComponent]") -> None:
        """Convert all coordinates in *comps* from inches to mm in-place."""
        for c in comps:
            c.x *= 25.4
            c.y *= 25.4
            for p in c.pins:
                p.x *= 25.4
                p.y *= 25.4

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
                    package=m.group(6),  # initial: footprint/partnum from CMP line
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
                raw_pin_name = m.group(4)           # e.g. "D804-A", "IC700-1", "1"
                suffix = _pin_suffix(raw_pin_name)  # e.g. "A",      "1",       "1"
                num    = _pin_num(suffix)            # e.g.  0,        1,         1
                current.pins.append(_ODBPin(
                    x=float(m.group(1)),
                    y=float(m.group(2)),
                    net_id=int(m.group(3)),
                    number=num,
                    name=suffix,
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
            page_idx = 0 if oc.side == "top" else 1
            comp = Component(
                ref=oc.ref,
                comp_type=oc.comp_type,
                bbox=BoundingBox(cx_pt - 3, cy_pt - 3, cx_pt + 3, cy_pt + 3),
                center=Point(cx_pt, cy_pt),
                page=page_idx,
            )
            p_pin = oc.polarity_pin
            if p_pin is not None and oc.is_polar:
                p1x = p_pin.x * unit_scale
                p1y = p_pin.y * unit_scale
                mtype = "cathode_odb" if oc.comp_type in ("diode", "led") else "pin1_odb"

                # ── Determine detection method and confidence ──────────────
                method = oc._detection_method  # "silk"|"net_gnd"|"net_vcc"|""
                if not method:
                    if oc.comp_type in ("diode", "led"):
                        # polarity_pin fell through to functional-name or fallback path
                        has_k = any(p.name.upper() in _CATHODE_NAMES for p in oc.pins)
                        has_a = any(p.name.upper() in _ANODE_NAMES   for p in oc.pins)
                        method = "pin_name" if (has_k or has_a) else "fallback"
                    else:
                        method = "pin1"  # non-diode: pin1 is always reliable

                # "fallback" = only convention-based; surface for user review
                if method == "fallback":
                    confidence     = 0.60
                    polarity_status = "needs_review"
                else:
                    confidence     = 0.99
                    polarity_status = "marked"

                marker = PolarityMarker(
                    marker_type=mtype,
                    bbox=BoundingBox(p1x - 2, p1y - 2, p1x + 2, p1y + 2),
                    center=Point(p1x, p1y),
                    page=page_idx,
                    confidence=confidence,
                    source="odb",
                    detection_method=method,
                )
                results.append(MatchResult(
                    component=comp,
                    markers=[marker],
                    polarity_status=polarity_status,
                    overall_confidence=confidence,
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
