"""
core/odb_silk_cathode.py
────────────────────────
Detect diode / LED cathode pins by analysing the ODB++ silkscreen layer.

For each 2-pin diode/LED component the algorithm:
  1. Reads silk-layer line features near the component footprint.
  2. Projects each feature's midpoint onto the pin-to-pin axis.
  3. Scores each pin half by total feature "mass" (length × width),
     boosted 3.5× for features running perpendicular to the axis
     (= cathode band candidates).
  4. Declares the heavier half the cathode when its ratio > _MIN_RATIO.

Returns a dict {ref: pin_index} where pin_index is 0 or 1
(index into _ODBComponent.pins[]).  Returns {} on any error so
callers can fall back to other detection methods without crashing.
"""
from __future__ import annotations

import math
import os
import re
import tarfile
import zipfile
from typing import Dict, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Tuning constants
# ─────────────────────────────────────────────────────────────────────────────

_MIN_RATIO = 0.55   # cathode side must hold this fraction of total silk mass
_MIN_MASS  = 0.02   # minimum total silk mass (mm²) required for a decision


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def detect_cathodes(odb_path: str, odb_comps: list) -> Dict[str, int]:
    """Return {ref: pin_index} for diodes/LEDs whose cathode can be inferred
    from silkscreen geometry.  *pin_index* is 0 or 1 (into comp.pins[]).

    Silently returns {} if the archive cannot be read, no silk layer exists,
    or scores are inconclusive.
    """
    try:
        return _detect_impl(odb_path, odb_comps)
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Archive I/O
# ─────────────────────────────────────────────────────────────────────────────

def _is_relevant(key: str) -> bool:
    """Return True for paths we need to read (matrix + layer features)."""
    return (
        (key.endswith("/features") and "/layers/" in key)
        or key.endswith("matrix/matrix")
    )


def _detect_step(names: list) -> str:
    """Detect the ODB++ step directory name from a list of archive paths."""
    for n in names:
        nn = n.replace("\\", "/").lower()
        for prefix in ("/steps/", "steps/"):
            idx = nn.find(prefix)
            if idx >= 0:
                rest = nn[idx + len(prefix):]
                if rest and "/" in rest:
                    candidate = rest.split("/")[0]
                    if candidate:
                        return candidate
    return "pcb"


def _detect_step_dir(dir_path: str) -> str:
    """Detect step name from an unzipped directory layout."""
    for root, dirs, _ in os.walk(dir_path):
        for d in dirs:
            if d.lower() == "steps":
                steps_path = os.path.join(root, d)
                for entry in os.listdir(steps_path):
                    if os.path.isdir(os.path.join(steps_path, entry)):
                        return entry.lower()
    return "pcb"


def _read_archive(odb_path: str) -> Tuple[Dict[str, str], str]:
    """Open the archive (zip / tgz / dir) and cache matrix + features files.

    Returns:
        cache      : {normalised_lower_path: text_content}
        step_name  : detected step directory name (e.g. 'pcb')
    """
    cache: Dict[str, str] = {}
    step_name = "pcb"
    low = odb_path.lower()

    if os.path.isfile(odb_path) and low.endswith(".zip"):
        with zipfile.ZipFile(odb_path, "r") as zf:
            names = zf.namelist()
            step_name = _detect_step(names)
            for name in names:
                key = name.replace("\\", "/").lower()
                if _is_relevant(key):
                    try:
                        cache[key] = zf.read(name).decode("utf-8", errors="replace")
                    except Exception:
                        pass

    elif os.path.isfile(odb_path) and (
        low.endswith(".tgz") or low.endswith(".tar.gz") or low.endswith(".tar")
    ):
        mode = "r:gz" if low.endswith((".gz", ".tgz")) else "r"
        with tarfile.open(odb_path, mode) as tf:
            names = tf.getnames()
            step_name = _detect_step(names)
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                key = member.name.replace("\\", "/").lower()
                if _is_relevant(key) and member.size < 30_000_000:
                    try:
                        f = tf.extractfile(member)
                        if f:
                            cache[key] = f.read().decode("utf-8", errors="replace")
                    except Exception:
                        pass

    elif os.path.isdir(odb_path):
        step_name = _detect_step_dir(odb_path)
        for root, _, files in os.walk(odb_path):
            for fname in files:
                if fname in ("features", "matrix"):
                    fpath = os.path.join(root, fname)
                    key = fpath.replace("\\", "/").lower()
                    if _is_relevant(key):
                        try:
                            with open(fpath, encoding="utf-8", errors="replace") as fh:
                                cache[key] = fh.read()
                        except Exception:
                            pass

    return cache, step_name


# ─────────────────────────────────────────────────────────────────────────────
# Layer discovery
# ─────────────────────────────────────────────────────────────────────────────

def _parse_silk_from_matrix(content: str) -> Dict[str, str]:
    """Parse ODB++ matrix file, return {side: layer_name} for silk layers."""
    in_layer = False
    name = ltype = ""
    silk_top = silk_bot = None

    for line in content.splitlines():
        line = line.strip()
        if line == "LAYER {":
            in_layer = True; name = ""; ltype = ""
        elif line == "}" and in_layer:
            if name and (
                ltype == "SILK_SCREEN"
                or "silk" in name.lower()
                or "overlay" in name.lower()
            ):
                n = name.lower()
                is_top = any(k in n for k in ("top", "f.", "top_overlay",
                                               "silkscreen_top", "silk_top"))
                is_bot = any(k in n for k in ("bot", "b.", "bottom",
                                               "silkscreen_bot", "silk_bot"))
                if is_top and not silk_top:
                    silk_top = name
                elif is_bot and not silk_bot:
                    silk_bot = name
                elif not is_top and not is_bot:
                    if not silk_top:
                        silk_top = name
                    elif not silk_bot:
                        silk_bot = name
            in_layer = False
        elif in_layer and "=" in line:
            k, _, v = line.partition("=")
            k = k.strip().upper(); v = v.strip()
            if k == "NAME":
                name = v
            elif k == "TYPE":
                ltype = v.upper()

    result: Dict[str, str] = {}
    if silk_top:
        result["top"] = silk_top
    if silk_bot:
        result["bot"] = silk_bot
    return result


def _find_silk_by_folder(cache: Dict[str, str]) -> Dict[str, str]:
    """Fallback: discover silk layers by scanning layer folder names."""
    silk_top = silk_bot = None
    for key in cache:
        if "/layers/" not in key:
            continue
        parts = key.split("/layers/")
        if len(parts) < 2:
            continue
        layer = parts[1].split("/")[0]
        if "silk" in layer or "overlay" in layer:
            n = layer.lower()
            is_bot = any(k in n for k in ("bot", "b.", "bottom"))
            is_top = any(k in n for k in ("top", "f.")) or not is_bot
            if is_top and not silk_top:
                silk_top = layer
            elif is_bot and not silk_bot:
                silk_bot = layer

    result: Dict[str, str] = {}
    if silk_top:
        result["top"] = silk_top
    if silk_bot:
        result["bot"] = silk_bot
    return result


def _get_layer_features(
    cache: Dict[str, str],
    step_name: str,
    layer_name: str,
) -> Optional[str]:
    """Return the text content of a layer's features file, or None."""
    needle = f"/layers/{layer_name.lower()}/features"
    for key, content in cache.items():
        if needle in key:
            return content
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Feature parser
# ─────────────────────────────────────────────────────────────────────────────

_RE_SYM  = re.compile(r"^\$(\d+)\s+(\S+)")
_RE_RND  = re.compile(r"^r([\d.]+)$")
_RE_SQ   = re.compile(r"^s([\d.]+)$")
_RE_RECT = re.compile(r"^rect([\d.]+)x([\d.]+)")


def _parse_features_simple(content: str) -> Tuple[dict, list, list]:
    """Parse an ODB++ features file.

    Returns:
        syms  : {idx: (w_mm, h_mm)} – symbol dimensions in mm
        lines : [(x1, y1, x2, y2, sym_idx)] – coordinates in mm
        pads  : [(x, y, sym_idx)] – coordinates in mm
    """
    # Pass 1 — detect board units from file header (first 20 lines)
    inch_mode = False
    for raw_line in content.splitlines()[:20]:
        s = raw_line.strip().upper()
        if s.startswith("UNITS") and "INCH" in s:
            inch_mode = True
            break
        if re.match(r'^U\s+INCH', s):
            inch_mode = True
            break

    c2mm = 25.4 if inch_mode else 1.0   # raw coordinate → mm

    syms:  dict = {}
    lines: list = []
    pads:  list = []
    in_features = False

    for raw in content.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if "#Layer features" in raw:
            in_features = True

        # Symbol table entries — appear before AND after #Layer features
        if ln.startswith("$"):
            m = _RE_SYM.match(ln)
            if m:
                idx = int(m.group(1))
                sn = m.group(2).lower()
                w = h = 0.1  # default 0.1 mm
                mr = _RE_RND.match(sn)
                ms = _RE_SQ.match(sn)
                mc = _RE_RECT.match(sn)
                if mr:
                    d = float(mr.group(1))
                    v = d * 0.0254 if inch_mode else d / 1000.0
                    w = h = max(0.01, v)
                elif ms:
                    d = float(ms.group(1))
                    v = d * 0.0254 if inch_mode else d / 1000.0
                    w = h = max(0.01, v)
                elif mc:
                    wv = float(mc.group(1)) * (0.0254 if inch_mode else 0.001)
                    hv = float(mc.group(2)) * (0.0254 if inch_mode else 0.001)
                    w, h = max(0.01, wv), max(0.01, hv)
                syms[idx] = (w, h)
            continue

        if not in_features:
            continue

        if ln.startswith("L "):
            parts = ln.split(";")[0].split()
            if len(parts) >= 6:
                try:
                    lines.append((
                        float(parts[1]) * c2mm,
                        float(parts[2]) * c2mm,
                        float(parts[3]) * c2mm,
                        float(parts[4]) * c2mm,
                        int(parts[5]),
                    ))
                except (ValueError, IndexError):
                    pass

        elif ln.startswith("P "):
            parts = ln.split(";")[0].split()
            if len(parts) >= 4:
                try:
                    pads.append((
                        float(parts[1]) * c2mm,
                        float(parts[2]) * c2mm,
                        int(parts[3]),
                    ))
                except (ValueError, IndexError):
                    pass

    return syms, lines, pads


# ─────────────────────────────────────────────────────────────────────────────
# Cathode scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score_pins(comp, lines: list, pads: list, syms: dict) -> Optional[int]:
    """Score which of comp.pins[0] vs comp.pins[1] is the cathode.

    Returns pin index (0 or 1), or None if the result is inconclusive.

    Algorithm
    ---------
    For each silk line feature within the search radius around the component:
      • Compute its midpoint and project onto the pin0→pin1 axis.
      • Weight by (line_length × line_width) boosted for perpendicular lines
        (a line perpendicular to the pad axis = the cathode band).
      • Accumulate weighted scores for pin[0] side and pin[1] side.

    The side with score_ratio >= _MIN_RATIO (default 55 %) wins.
    """
    p0, p1 = comp.pins[0], comp.pins[1]
    x0, y0 = p0.x, p0.y
    x1, y1 = p1.x, p1.y

    mx = (x0 + x1) / 2
    my = (y0 + y1) / 2
    dx = x1 - x0
    dy = y1 - y0
    span = math.hypot(dx, dy)
    if span < 0.3:
        return None  # pads too close — skip

    ux, uy = dx / span, dy / span     # unit vector from pin[0] to pin[1]
    search_r = span * 1.4 + 1.5       # search radius in mm
    deadzone  = span * 0.08            # 8 % of span — midpoint dead zone

    score = [0.0, 0.0]

    # ── Line features ─────────────────────────────────────────────────────
    for (lx1, ly1, lx2, ly2, sym_idx) in lines:
        lmx = (lx1 + lx2) / 2
        lmy = (ly1 + ly2) / 2
        if math.hypot(lmx - mx, lmy - my) > search_r:
            continue

        sym = syms.get(sym_idx, (0.1, 0.1))
        lw = sym[0]                        # line width in mm

        ldx = lx2 - lx1
        ldy = ly2 - ly1
        ll = math.hypot(ldx, ldy)
        if ll < 1e-6:
            ll = lw

        # Perpendicularity to pad axis (0 = parallel, 1 = perpendicular)
        if ll > 1e-6:
            along_frac = abs((ldx * ux + ldy * uy) / ll)
            perp_frac = math.sqrt(max(0.0, 1.0 - along_frac * along_frac))
        else:
            perp_frac = 0.5

        # Lines perpendicular to the pad axis get a 3.5× mass boost because
        # they are likely the cathode band rather than the body outline.
        perp_boost = 1.0 + 2.5 * perp_frac          # 1.0 … 3.5
        weight = max(ll, lw) * max(lw, 0.02) * perp_boost

        proj = (lmx - mx) * ux + (lmy - my) * uy
        if proj > deadzone:
            score[1] += weight
        elif proj < -deadzone:
            score[0] += weight

    # ── Pad-type features (filled symbols on silk) ─────────────────────────
    for (px, py, sym_idx) in pads:
        if math.hypot(px - mx, py - my) > search_r:
            continue
        sym = syms.get(sym_idx, (0.1, 0.1))
        weight = sym[0] * sym[1]
        proj = (px - mx) * ux + (py - my) * uy
        if proj > deadzone:
            score[1] += weight
        elif proj < -deadzone:
            score[0] += weight

    total = score[0] + score[1]
    if total < _MIN_MASS:
        return None  # not enough silk features

    ratio0 = score[0] / total
    ratio1 = score[1] / total

    if ratio0 >= _MIN_RATIO:
        return 0
    elif ratio1 >= _MIN_RATIO:
        return 1
    return None  # inconclusive


# ─────────────────────────────────────────────────────────────────────────────
# Main implementation
# ─────────────────────────────────────────────────────────────────────────────

def _detect_impl(odb_path: str, odb_comps: list) -> Dict[str, int]:
    cache, step_name = _read_archive(odb_path)
    if not cache:
        return {}

    # Find silk layer names
    matrix_content: Optional[str] = None
    for key, content in cache.items():
        if key.endswith("matrix/matrix"):
            matrix_content = content
            break

    silk_layers = _parse_silk_from_matrix(matrix_content) if matrix_content else {}
    if not silk_layers:
        silk_layers = _find_silk_by_folder(cache)
    if not silk_layers:
        return {}

    # Parse silk features per side
    silk_parsed: Dict[str, tuple] = {}  # side → (syms, lines, pads)
    for side, layer_name in silk_layers.items():
        content = _get_layer_features(cache, step_name, layer_name)
        if content:
            syms, lines, pads = _parse_features_simple(content)
            silk_parsed[side] = (syms, lines, pads)

    if not silk_parsed:
        return {}

    # Score each 2-pin diode/LED
    results: Dict[str, int] = {}
    for comp in odb_comps:
        if comp.comp_type not in ("diode", "led"):
            continue
        if len(comp.pins) != 2:
            continue

        side = comp.side  # "top" or "bot"
        data = (
            silk_parsed.get(side)
            or silk_parsed.get("top")
            or next(iter(silk_parsed.values()), None)
        )
        if not data:
            continue

        syms, lines, pads = data
        pin_idx = _score_pins(comp, lines, pads, syms)
        if pin_idx is not None:
            results[comp.ref] = pin_idx

    return results


