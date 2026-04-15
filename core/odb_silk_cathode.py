"""
core/odb_silk_cathode.py
────────────────────────
Detect diode / LED cathode pins by analysing ODB++ silkscreen and assembly
layers.

Three-tier scoring approach (stops at first conclusive result):
  Tier 1 – Triangle convergence: diagonal lines converge at the triangle
            apex → cathode side.
  Tier 2 – Perpendicular line count: cathode bar = extra perp line on one side.
  Tier 3 – Mass scoring fallback: weighted feature mass comparison.

Layer discovery excludes mask/paste/solder layers by NAME (not TYPE) because
some boards have swapped TYPE fields in the matrix.
"""
from __future__ import annotations

import math
import os
import re
import tarfile
import zipfile
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Tuning constants
# ─────────────────────────────────────────────────────────────────────────────

_MIN_RATIO    = 0.55   # Tier 3: cathode side must hold this fraction
_MIN_MASS     = 0.02   # Tier 3: minimum total silk mass (mm²)

_SEARCH_FACTOR = 0.9   # search_r = span * _SEARCH_FACTOR + _SEARCH_OFFSET
_SEARCH_OFFSET = 0.8
_DEADZONE_FRAC = 0.05  # deadzone = span * _DEADZONE_FRAC

_DIAG_SPREAD   = 0.50  # Tier 1: max ratio min_spread/max_spread for decision

# Debug: set to a ref designator (e.g. "D12") to print detailed scoring info
_DEBUG_REF = ""  # set to ref like "D101" for debug

# Layer names containing any of these substrings are excluded regardless of TYPE
_EXCLUDE_NAMES = ("mask", "paste", "solder", "drill", "rout")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def detect_cathodes(odb_path: str, odb_comps: list) -> Dict[str, int]:
    """Return {ref: pin_index} for diodes/LEDs whose cathode can be inferred
    from silkscreen / assembly geometry.  *pin_index* is 0 or 1.

    Silently returns {} on any error.
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
# Layer discovery  (NEW — name-based, excludes mask/paste/solder)
# ─────────────────────────────────────────────────────────────────────────────

def _discover_cathode_layers(content: str) -> Dict[str, List[str]]:
    """Parse the ODB++ matrix and return candidate layers for cathode detection.

    Returns {side: [layer_name, ...]} where side is "top" or "bot".
    Layer lists are ordered by preference:
      1. Silk layers identified by NAME (silk/overlay in name)
      2. Assembly layers (assembly/assy in name)
      3. Silk layers identified by TYPE=SILK_SCREEN only
    Layers whose name contains mask/paste/solder/drill/rout are excluded.
    """
    # Parse all layers from matrix
    layers_info: List[Tuple[str, str]] = []   # [(name, type), ...]
    in_layer = False
    name = ltype = ""

    for line in content.splitlines():
        line = line.strip()
        if line == "LAYER {":
            in_layer = True; name = ""; ltype = ""
        elif line == "}" and in_layer:
            if name:
                layers_info.append((name, ltype))
            in_layer = False
        elif in_layer and "=" in line:
            k, _, v = line.partition("=")
            k = k.strip().upper(); v = v.strip()
            if k == "NAME":
                name = v
            elif k == "TYPE":
                ltype = v.upper()

    # Categorize
    silk_name_top:  List[str] = []
    silk_name_bot:  List[str] = []
    silk_type_top:  List[str] = []
    silk_type_bot:  List[str] = []
    assy_top:       List[str] = []
    assy_bot:       List[str] = []

    for lname, ltyp in layers_info:
        nl = lname.lower()

        # Exclude mask/paste/solder/drill/rout by name
        if any(ex in nl for ex in _EXCLUDE_NAMES):
            continue

        # Detect side
        is_top = any(k in nl for k in ("top", "f.", "_t.", "_t_"))
        is_bot = any(k in nl for k in ("bot", "b.", "_b.", "_b_", "bottom"))
        # Short assembly layer names often end with 't' or 'b' (e.g. assemt, assemb)
        if not is_top and not is_bot:
            if nl.endswith("t"):
                is_top = True
            elif nl.endswith("b"):
                is_bot = True
            else:
                is_top = True  # default

        # Categorize by name patterns
        is_silk_by_name = ("silk" in nl or "overlay" in nl)
        is_assy_by_name = ("assem" in nl or "assy" in nl)
        is_silk_by_type = (ltyp == "SILK_SCREEN")

        if is_silk_by_name:
            if is_top:
                silk_name_top.append(lname)
            if is_bot:
                silk_name_bot.append(lname)
        elif is_assy_by_name:
            if is_top:
                assy_top.append(lname)
            if is_bot:
                assy_bot.append(lname)
        elif is_silk_by_type:
            if is_top:
                silk_type_top.append(lname)
            if is_bot:
                silk_type_bot.append(lname)

    # Build ordered preference lists per side
    result: Dict[str, List[str]] = {}
    top_list = silk_name_top + assy_top + silk_type_top
    bot_list = silk_name_bot + assy_bot + silk_type_bot
    if top_list:
        result["top"] = top_list
    if bot_list:
        result["bot"] = bot_list
    return result


def _find_layers_by_folder(cache: Dict[str, str]) -> Dict[str, List[str]]:
    """Fallback: discover silk/assembly layers by scanning folder names."""
    silk_top:  List[str] = []
    silk_bot:  List[str] = []
    assy_top:  List[str] = []
    assy_bot:  List[str] = []

    for key in cache:
        if "/layers/" not in key:
            continue
        parts = key.split("/layers/")
        if len(parts) < 2:
            continue
        layer = parts[1].split("/")[0]
        nl = layer.lower()

        # Exclude
        if any(ex in nl for ex in _EXCLUDE_NAMES):
            continue

        is_bot = any(k in nl for k in ("bot", "b.", "bottom"))
        is_top = any(k in nl for k in ("top", "f.")) or not is_bot
        # Suffix abbreviation fallback (assemt → top, assemb → bot)
        if not is_top and not is_bot:
            if nl.endswith("t"):
                is_top = True
            elif nl.endswith("b"):
                is_bot = True
            else:
                is_top = True

        if "silk" in nl or "overlay" in nl:
            if is_top and layer not in silk_top:
                silk_top.append(layer)
            elif is_bot and layer not in silk_bot:
                silk_bot.append(layer)
        elif "assem" in nl or "assy" in nl:
            if is_top and layer not in assy_top:
                assy_top.append(layer)
            elif is_bot and layer not in assy_bot:
                assy_bot.append(layer)

    result: Dict[str, List[str]] = {}
    top_list = silk_top + assy_top
    bot_list = silk_bot + assy_bot
    if top_list:
        result["top"] = top_list
    if bot_list:
        result["bot"] = bot_list
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
# Cathode scoring — 3-tier approach
# ─────────────────────────────────────────────────────────────────────────────

def _nearby_lines(comp, lines, syms, search_r, mx, my):
    """Yield (lx1, ly1, lx2, ly2, sym_w, length, along_frac, perp_frac, proj)
    for lines within search_r of the component midpoint."""
    p0, p1 = comp.pins[0], comp.pins[1]
    dx = p1.x - p0.x
    dy = p1.y - p0.y
    span = math.hypot(dx, dy)
    if span < 0.01:
        return
    ux, uy = dx / span, dy / span

    for (lx1, ly1, lx2, ly2, sym_idx) in lines:
        lmx = (lx1 + lx2) / 2
        lmy = (ly1 + ly2) / 2
        if math.hypot(lmx - mx, lmy - my) > search_r:
            continue
        sym = syms.get(sym_idx, (0.1, 0.1))
        lw = sym[0]
        ldx = lx2 - lx1
        ldy = ly2 - ly1
        ll = math.hypot(ldx, ldy)
        if ll < 1e-6:
            continue
        along = abs((ldx * ux + ldy * uy) / ll)
        perp = math.sqrt(max(0.0, 1.0 - along * along))
        proj = (lmx - mx) * ux + (lmy - my) * uy
        yield lx1, ly1, lx2, ly2, lw, ll, along, perp, proj


def _score_triangle(comp, lines, syms, search_r, mx, my, span, ux, uy, deadzone) -> Optional[int]:
    """Tier 1: Diagonal line convergence → triangle apex = cathode.

    Diagonal lines (along_frac between 0.15 and 0.92) have their endpoints
    collected per side.  The side where endpoints cluster tightly (small spread)
    = triangle apex = cathode.
    """
    diag_eps_0: List[Tuple[float, float]] = []   # endpoints on pin0 side
    diag_eps_1: List[Tuple[float, float]] = []   # endpoints on pin1 side
    n_diag = 0

    debug = _DEBUG_REF and comp.ref == _DEBUG_REF

    # Perpendicular distance limit: reject diagonal lines whose midpoint is
    # far from the component axis — these are usually ref designator text
    # strokes from nearby components.
    max_perp_dist = span * 0.5

    for lx1, ly1, lx2, ly2, lw, ll, along, perp, proj in _nearby_lines(comp, lines, syms, search_r, mx, my):
        # Diagonal: along_frac between 0.15 and 0.92, length >= span*0.20
        # Upper bound 0.92 accommodates elongated triangles on assembly layers
        if 0.15 <= along <= 0.92 and ll >= span * 0.20:
            # Check perpendicular distance of line midpoint from component axis
            lmx = (lx1 + lx2) / 2
            lmy = (ly1 + ly2) / 2
            perp_dist = abs((lmx - mx) * (-uy) + (lmy - my) * ux)
            if perp_dist > max_perp_dist:
                if debug:
                    print("  [TRI] skip diag (%.1f,%.1f)-(%.1f,%.1f) perp_dist=%.3f > %.3f"
                          % (lx1, ly1, lx2, ly2, perp_dist, max_perp_dist))
                continue
            n_diag += 1
            # Collect both endpoints
            for ex, ey in [(lx1, ly1), (lx2, ly2)]:
                ep = (ex - mx) * ux + (ey - my) * uy
                if ep < -deadzone:
                    diag_eps_0.append((ex, ey))
                elif ep > deadzone:
                    diag_eps_1.append((ex, ey))
            if debug:
                print("  [TRI] diag line (%.3f,%.3f)-(%.3f,%.3f) along=%.2f len=%.3f perp=%.3f"
                      % (lx1, ly1, lx2, ly2, along, ll, perp_dist))

    if n_diag < 2:
        if debug:
            print("  [TRI] only %d diagonal lines -- skip" % n_diag)
        return None

    # Both sides must have endpoints — if all endpoints are on one side,
    # it's not a triangle (likely noise or text strokes)
    if len(diag_eps_0) < 1 or len(diag_eps_1) < 1:
        if debug:
            print("  [TRI] one side empty (eps0=%d eps1=%d) -- skip"
                  % (len(diag_eps_0), len(diag_eps_1)))
        return None

    def spread(pts):
        if len(pts) < 2:
            return 0.0
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return math.hypot(max(xs) - min(xs), max(ys) - min(ys))

    s0 = spread(diag_eps_0)
    s1 = spread(diag_eps_1)

    if debug:
        print("  [TRI] n_diag=%d eps0=%d eps1=%d spread0=%.4f spread1=%.4f"
              % (n_diag, len(diag_eps_0), len(diag_eps_1), s0, s1))

    if max(s0, s1) < 1e-6:
        return None

    ratio = min(s0, s1) / max(s0, s1)
    if ratio > _DIAG_SPREAD:
        if debug:
            print("  [TRI] ratio=%.3f > %.2f -- inconclusive" % (ratio, _DIAG_SPREAD))
        return None

    result = 0 if s0 < s1 else 1
    if debug:
        print("  [TRI] -> cathode on pin%d (ratio=%.3f)" % (result, ratio))
    return result


def _score_perp_count(comp, lines, syms, search_r, mx, my, span, ux, uy, deadzone) -> Optional[int]:
    """Tier 2: Count perpendicular lines per side.

    A perpendicular line (along_frac < 0.3) with length > max(span*0.30, 1.0)
    is counted.  The higher threshold filters out ref designator text strokes
    (~0.83mm) that would otherwise pollute the count.
    Cathode bar adds an extra perp line. Need diff >= 2 for a decision.

    The 0.99 factor compensates for floating-point rounding in coordinate
    subtraction (e.g. 8.2 − 7.2 ≈ 0.9999… in IEEE 754).
    """
    debug = _DEBUG_REF and comp.ref == _DEBUG_REF
    count = [0, 0]
    min_perp_len = max(span * 0.30, 1.0) * 0.99

    for lx1, ly1, lx2, ly2, lw, ll, along, perp, proj in _nearby_lines(comp, lines, syms, search_r, mx, my):
        if along < 0.30 and ll >= min_perp_len:
            if proj < -deadzone:
                count[0] += 1
            elif proj > deadzone:
                count[1] += 1
            if debug:
                side = 0 if proj < -deadzone else (1 if proj > deadzone else -1)
                print("  [PERP] line len=%.3f along=%.2f proj=%.3f -> side%d" % (ll, along, proj, side))

    diff = abs(count[0] - count[1])
    if debug:
        print("  [PERP] count0=%d count1=%d diff=%d" % (count[0], count[1], diff))

    if diff >= 2:
        result = 0 if count[0] > count[1] else 1
        if debug:
            print("  [PERP] -> cathode on pin%d" % result)
        return result
    return None


def _score_mass(comp, lines, pads, syms, search_r, mx, my, span, ux, uy, deadzone) -> Optional[int]:
    """Tier 3: Original mass-based scoring (fallback).

    Weight features by (length × width) with 3.5× perpendicular boost.

    A perpendicular-distance filter suppresses features from nearby components:
    estimate the body half-height from the longest parallel lines near the
    component axis, and reject lines whose midpoint is too far from the axis.
    """
    debug = _DEBUG_REF and comp.ref == _DEBUG_REF

    # ── Estimate body half-height from parallel lines near centre ──
    body_half_h = 0.0
    for lx1, ly1, lx2, ly2, lw, ll, along, perp, proj in _nearby_lines(comp, lines, syms, search_r, mx, my):
        # Long parallel line close to the centre → body edge
        if along > 0.70 and abs(proj) <= span * 0.15 and ll >= span * 0.30:
            lmx = (lx1 + lx2) / 2
            lmy = (ly1 + ly2) / 2
            pd = abs((lmx - mx) * (-uy) + (lmy - my) * ux)
            if pd > body_half_h:
                body_half_h = pd

    # Allow 30 % margin beyond the body edge; fall back to search_r
    max_perp_d = body_half_h * 1.3 if body_half_h > 0.1 else search_r
    if debug:
        print("  [MASS] body_half_h=%.3f max_perp_d=%.3f" % (body_half_h, max_perp_d))

    score = [0.0, 0.0]

    for lx1, ly1, lx2, ly2, lw, ll, along, perp, proj in _nearby_lines(comp, lines, syms, search_r, mx, my):
        # Filter features too far from the component axis (nearby components)
        lmx = (lx1 + lx2) / 2
        lmy = (ly1 + ly2) / 2
        pd = abs((lmx - mx) * (-uy) + (lmy - my) * ux)
        if pd > max_perp_d:
            continue
        perp_boost = 1.0 + 2.5 * perp
        weight = max(ll, lw) * max(lw, 0.02) * perp_boost
        if proj > deadzone:
            score[1] += weight
        elif proj < -deadzone:
            score[0] += weight

    # Pad features
    p0, p1 = comp.pins[0], comp.pins[1]
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
    if debug:
        print("  [MASS] score0=%.4f score1=%.4f total=%.4f" % (score[0], score[1], total))

    if total < _MIN_MASS:
        return None

    ratio0 = score[0] / total
    ratio1 = score[1] / total

    if ratio0 >= _MIN_RATIO:
        if debug:
            print("  [MASS] -> cathode on pin0 (ratio=%.3f)" % ratio0)
        return 0
    elif ratio1 >= _MIN_RATIO:
        if debug:
            print("  [MASS] -> cathode on pin1 (ratio=%.3f)" % ratio1)
        return 1

    if debug:
        print("  [MASS] inconclusive (ratio0=%.3f ratio1=%.3f)" % (ratio0, ratio1))
    return None


def _score_component(comp, lines, pads, syms) -> Optional[int]:
    """Run the 3-tier scoring cascade for a single component.

    Returns pin index (0 or 1) or None.
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
        return None

    ux, uy = dx / span, dy / span
    search_r = span * _SEARCH_FACTOR + _SEARCH_OFFSET
    deadzone = span * _DEADZONE_FRAC

    debug = _DEBUG_REF and comp.ref == _DEBUG_REF
    if debug:
        print("\n=== Scoring %s ===" % comp.ref)
        print("  pin0=(%.3f,%.3f) pin1=(%.3f,%.3f) span=%.3f" % (x0, y0, x1, y1, span))
        print("  search_r=%.3f deadzone=%.3f" % (search_r, deadzone))

    # Tier 1 — triangle convergence
    result = _score_triangle(comp, lines, syms, search_r, mx, my, span, ux, uy, deadzone)
    if result is not None:
        return result

    # Tier 2 — perpendicular line count
    result = _score_perp_count(comp, lines, syms, search_r, mx, my, span, ux, uy, deadzone)
    if result is not None:
        return result

    # Tier 3 — mass scoring
    result = _score_mass(comp, lines, pads, syms, search_r, mx, my, span, ux, uy, deadzone)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main implementation
# ─────────────────────────────────────────────────────────────────────────────

def _detect_impl(odb_path: str, odb_comps: list) -> Dict[str, int]:
    cache, step_name = _read_archive(odb_path)
    if not cache:
        return {}

    # Find matrix content
    matrix_content: Optional[str] = None
    for key, content in cache.items():
        if key.endswith("matrix/matrix"):
            matrix_content = content
            break

    # Discover layers (new multi-layer approach)
    layer_map: Dict[str, List[str]] = {}
    if matrix_content:
        layer_map = _discover_cathode_layers(matrix_content)
    if not layer_map:
        layer_map = _find_layers_by_folder(cache)
    if not layer_map:
        return {}

    # Parse features for all candidate layers per side
    parsed_per_side: Dict[str, List[Tuple[dict, list, list]]] = {}
    for side, layer_names in layer_map.items():
        side_data: List[Tuple[dict, list, list]] = []
        for lname in layer_names:
            fcontent = _get_layer_features(cache, step_name, lname)
            if fcontent:
                syms, lines, pads = _parse_features_simple(fcontent)
                if lines or pads:
                    side_data.append((syms, lines, pads))
        if side_data:
            parsed_per_side[side] = side_data

    if not parsed_per_side:
        return {}

    # Score each 2-pin diode/LED
    results: Dict[str, int] = {}
    for comp in odb_comps:
        if comp.comp_type not in ("diode", "led", "zener", "tvs", "schottky"):
            continue
        if len(comp.pins) != 2:
            continue

        side = comp.side  # "top" or "bot"
        side_layers = (
            parsed_per_side.get(side)
            or parsed_per_side.get("top")
            or next(iter(parsed_per_side.values()), None)
        )
        if not side_layers:
            continue

        # Try each layer in preference order; stop at first conclusive result
        for syms, lines, pads in side_layers:
            pin_idx = _score_component(comp, lines, pads, syms)
            if pin_idx is not None:
                results[comp.ref] = pin_idx
                break

    # ── Board-level consistency ──────────────────────────────────────────
    # If at least 2 detected diodes ALL agree on the same pin index,
    # propagate that consensus to any undetected 2-pin diodes.
    # This handles cases where the silk marking is ambiguous or absent
    # for some components but the board uses a uniform cathode convention.
    _DIODE_TYPES = ("diode", "led", "zener", "tvs", "schottky")
    if len(results) >= 2:
        idx_set = set(results.values())
        if len(idx_set) == 1:
            consensus_idx = next(iter(idx_set))
            for comp in odb_comps:
                if comp.ref in results:
                    continue
                if comp.comp_type not in _DIODE_TYPES:
                    continue
                if len(comp.pins) != 2:
                    continue
                results[comp.ref] = consensus_idx

    return results


