"""
core/odb_renderer.py
────────────────────
Renders an ODB++ archive directly to a PDF with named PDF layers (OCGs).

PDF layers created (all toggleable in any PDF viewer):
  • Board outline | Copper (top) | Fab/Assembly | Silkscreen
  • Courtyard | Reference labels | Polarity markers

Polarity markers:
  - 2-pin SMD  →  D-shaped (semicircle) at polarity pin, flat side toward
                  component centre → marker stays inside the component body.
  - Multi-pin  →  small filled circle at polarity pin.
  - All polar types  → bright green highlighter stroke (unified colour)

Supports UNITS=MM and UNITS=INCH.
"""
from __future__ import annotations

import math
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF >= 1.23

MM_TO_PT   = 72.0 / 25.4
INCH_TO_PT = 72.0
MIL_TO_MM  = 0.0254

_ROLE_COLORS = {
    "copper_top":    (0.70, 0.16, 0.16),
    "silk_top":      (0.55, 0.0,  0.55),
    "fab_top":       (0.20, 0.20, 0.72),
    "courtyard_top": (0.0,  0.55, 0.55),
    "notes_top":     (0.35, 0.35, 0.35),
    "copper_bot":    (0.80, 0.35, 0.10),
    "silk_bot":      (0.65, 0.10, 0.10),
    "fab_bot":       (0.10, 0.45, 0.20),
    "courtyard_bot": (0.15, 0.50, 0.30),
    "profile":       (0.0,  0.0,  0.0),
}
_HIGHLIGHT_GREEN  = (0.20, 0.95, 0.08)   # bright green highlighter
_HIGHLIGHT_ORANGE = (1.00, 0.62, 0.00)   # orange highlighter (DNP)
_HIGHLIGHT_YELLOW = (1.00, 0.88, 0.00)   # yellow highlighter (needs_review)


# ─────────────────────────────────────────────────────────────────────────────
# Symbol / feature parsers  (unchanged logic, condensed)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Symbol:
    name: str; width_raw: float = 0.0; height_raw: float = 0.0
    is_round: bool = False; is_rect: bool = False

    def w_mm(self, inch: bool = False) -> float:
        return self.width_raw * MIL_TO_MM if inch else self.width_raw / 1000.0
    def h_mm(self, inch: bool = False) -> float:
        return self.height_raw * MIL_TO_MM if inch else self.height_raw / 1000.0

_RE_SR = re.compile(r"^r([\d.]+)$");  _RE_SS = re.compile(r"^s([\d.]+)$")
_RE_RC = re.compile(r"^rect([\d.]+)x([\d.]+)"); _RE_DO = re.compile(r"^donut_r([\d.]+)x")

def _parse_symbol(name: str) -> _Symbol:
    m = _RE_SR.match(name)
    if m: d=float(m.group(1)); return _Symbol(name,d,d,is_round=True)
    m = _RE_SS.match(name)
    if m: d=float(m.group(1)); return _Symbol(name,d,d,is_rect=True)
    m = _RE_RC.match(name)
    if m: return _Symbol(name,float(m.group(1)),float(m.group(2)),is_rect=True)
    m = _RE_DO.match(name)
    if m: d=float(m.group(1)); return _Symbol(name,d,d,is_round=True)
    return _Symbol(name,150,150,is_round=True)

@dataclass
class _Line:
    x1:float;y1:float;x2:float;y2:float;sym_idx:int
@dataclass
class _Pad:
    x:float;y:float;sym_idx:int;orient:float=0.0
@dataclass
class _Arc:
    points:List[Tuple[float,float]]

def _parse_features(content: str):
    syms:Dict[int,_Symbol]={}; lines:List[_Line]=[]; pads:List[_Pad]=[]; arcs:List[_Arc]=[]
    in_f=False; cur_arc:Optional[_Arc]=None
    for raw in content.splitlines():
        ln=raw.strip()
        if not ln or ln.startswith("#"):
            if "#Layer features" in raw: in_f=True
            continue
        if ln.startswith("$"):
            p=ln.split(None,1)
            if len(p)==2:
                try: syms[int(p[0][1:])]=_parse_symbol(p[1])
                except: pass
            continue
        if not in_f: continue
        if ln.startswith("L "):
            p=ln.split(";")[0].split()
            if len(p)>=6:
                try: lines.append(_Line(float(p[1]),float(p[2]),float(p[3]),float(p[4]),int(p[5])))
                except: pass
        elif ln.startswith("P "):
            p=ln.split(";")[0].split()
            if len(p)>=4:
                try: pads.append(_Pad(float(p[1]),float(p[2]),int(p[3]),float(p[7]) if len(p)>7 else 0.0))
                except: pass
        elif ln.startswith("OB "):
            p=ln.split()
            try: cur_arc=_Arc([(float(p[1]),float(p[2]))])
            except: cur_arc=None
        elif ln.startswith("OS ") and cur_arc is not None:
            p=ln.split()
            try: cur_arc.points.append((float(p[1]),float(p[2])))
            except: pass
        elif ln.startswith("OE") and cur_arc is not None:
            arcs.append(cur_arc); cur_arc=None
    return syms,lines,pads,arcs


# ─────────────────────────────────────────────────────────────────────────────
# Matrix / layer discovery  (unchanged logic)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _MatrixLayer:
    row:int; name:str; layer_type:str; context:str; polarity:str="POSITIVE"

def _parse_matrix(content: str) -> List[_MatrixLayer]:
    layers=[]; in_l=False; row=0; name=""; ltype=""; ctx=""; pol="POSITIVE"
    for raw in content.splitlines():
        ln=raw.strip()
        if ln=="LAYER {": in_l=True; row=0; name=""; ltype=""; ctx=""; pol="POSITIVE"
        elif ln=="}" and in_l:
            if name: layers.append(_MatrixLayer(row,name.lower(),ltype.upper(),ctx.upper(),pol.upper()))
            in_l=False
        elif in_l and "=" in ln:
            k,_,v=ln.partition("="); k=k.strip().upper(); v=v.strip()
            if k=="ROW":
                try: row=int(v)
                except: pass
            elif k=="NAME":    name=v
            elif k=="TYPE":    ltype=v
            elif k=="CONTEXT": ctx=v
            elif k=="POLARITY": pol=v
    return layers

def _discover_layers(mls: List[_MatrixLayer]) -> Dict[str, str]:
    def _find(*kws) -> Optional[str]:
        for ml in mls:
            if all(k in ml.name for k in kws): return ml.name
        return None

    # Keywords that mark a DOCUMENT layer as non-assembly (keepout, milling, etc.)
    # These should never be used as a fab/assembly layer fallback.
    # Note: "3d-body" is handled explicitly below, so "3d" is NOT in this list.
    _NON_FAB = ("keep", "mill", "drill", "paste", "mask", "solder",
                "courtyard", "wiring", "outline", "board")

    r: Dict[str, str] = {}

    # ── Top layers ────────────────────────────────────────────────────────────
    r["copper_top"] = (
        _find("f.cu") or _find("layer_1", "top") or _find("top_layer")
        or next((ml.name for ml in mls
                 if ml.layer_type in ("SIGNAL", "POWER")
                 and "top" in ml.name and "bot" not in ml.name), None)
        or next((ml.name for ml in mls
                 if "layer" in ml.name and "top" in ml.name
                 and "assembly" not in ml.name and "silk" not in ml.name
                 and "overlay" not in ml.name), None)
    )
    silk = next((ml.name for ml in mls if "silk" in ml.name and "top" in ml.name
                 and "bot" not in ml.name), None)
    r["silk_top"] = (
        silk or _find("f.silk") or _find("overlay", "top") or _find("top_overlay")
        or next((ml.name for ml in mls if ml.layer_type == "SILK_SCREEN"
                 and "mask" not in ml.name and "bot" not in ml.name), None)
    )
    r["fab_top"] = (
        _find("f.fab") or _find("assembly", "top") or _find("assemt")
        or _find("top_assembly") or _find("fab", "top")
        or _find("3d-body")          # Altium ODB++: single layer with all component body outlines
        or next((ml.name for ml in mls
                 if ml.layer_type == "DOCUMENT"
                 and "bot" not in ml.name and not ml.name.endswith("b")
                 and not any(k in ml.name for k in _NON_FAB)), None)
    )
    court = _find("f.courtyard") or next(
        (ml.name for ml in mls if "courtyard" in ml.name
         and "bot" not in ml.name and "b." not in ml.name), None)
    r["courtyard_top"] = court
    notes = (
        _find("f.user") or _find("user", "drawing") or _find("dwgs", "user")
        or _find("cmts", "user") or _find("comment") or _find("notes")
        or next((ml.name for ml in mls
                 if any(k in ml.name for k in ("user", "dwgs", "cmts", "comment", "notes", "drawing"))
                 and "bot" not in ml.name and "b." not in ml.name), None)
    )
    r["notes_top"] = notes

    # ── Bottom layers ─────────────────────────────────────────────────────────
    r["copper_bot"] = (
        _find("b.cu") or _find("layer_2", "bot") or _find("bottom_layer")
        or next((ml.name for ml in mls
                 if ml.layer_type in ("SIGNAL", "POWER")
                 and "bot" in ml.name), None)
        or next((ml.name for ml in mls
                 if "layer" in ml.name and "bot" in ml.name
                 and "assembly" not in ml.name and "silk" not in ml.name
                 and "overlay" not in ml.name), None)
    )
    silk_b = next((ml.name for ml in mls if "silk" in ml.name and "bot" in ml.name), None)
    r["silk_bot"] = (
        silk_b or _find("b.silk") or _find("overlay", "bot") or _find("bottom_overlay")
        or next((ml.name for ml in mls if ml.layer_type == "SILK_SCREEN"
                 and "mask" not in ml.name and "bot" in ml.name), None)
    )
    r["fab_bot"] = (
        _find("b.fab") or _find("assembly", "bot") or _find("assemb")
        or _find("bottom_assembly") or _find("fab", "bot")
        or _find("3d-body")          # Altium ODB++: single layer with all component body outlines
        or next((ml.name for ml in mls
                 if ml.layer_type == "DOCUMENT"
                 and ("bot" in ml.name or ml.name.endswith("b"))
                 and not any(k in ml.name for k in _NON_FAB)), None)
    )
    court_b = _find("b.courtyard") or next(
        (ml.name for ml in mls if "courtyard" in ml.name and "bot" in ml.name), None)
    r["courtyard_bot"] = court_b

    return {k: v for k, v in r.items() if v}

def _detect_units(content: str) -> str:
    """Return ``'INCH'`` or ``'MM'`` from an ODB++ file header.

    Handles both formats:
      • ``UNITS=INCH`` / ``UNITS=MM``   (KiCad / old-style)
      • ``U INCH`` / ``U MM``           (ODB++ features / profile files)
    """
    for ln in content.splitlines()[:20]:
        s = ln.strip().upper()
        if s.startswith("UNITS"):
            return "INCH" if "INCH" in s else "MM"
        # ODB++ features format: "U MM" or "U INCH"
        m = re.match(r'^U\s+(MM|INCH)', s)
        if m:
            return m.group(1)
    return "MM"

def _parse_profile(content: str) -> List[Tuple[float,float]]:
    pts=[]
    for ln in content.splitlines():
        ln=ln.strip()
        if ln.startswith(("OB ","OS ")):
            p=ln.split()
            try: pts.append((float(p[1]),float(p[2])))
            except: pass
    return pts


# ─────────────────────────────────────────────────────────────────────────────
# Polarity marker
# ─────────────────────────────────────────────────────────────────────────────


# --- Helper functions for marker boundary calculation ---
import sys
from typing import Optional

def _distance_to_rect_edge(px, py, ux, uy, rect_bbox):
    # rect_bbox: BoundingBox (x0, y0, x1, y1)
    left, right = rect_bbox.x0, rect_bbox.x1
    top, bottom = rect_bbox.y0, rect_bbox.y1
    t_values = []
    if abs(ux) > 1e-8:
        t1 = (left - px) / ux
        t2 = (right - px) / ux
        t_values.extend([t1, t2])
    if abs(uy) > 1e-8:
        t3 = (top - py) / uy
        t4 = (bottom - py) / uy
        t_values.extend([t3, t4])
    t_values = [t for t in t_values if t > 0]
    min_t = min(t_values) if t_values else 0.0
    return min_t

def _distance_to_circle_edge(px, py, ux, uy, cx, cy, r):
    # Ray from (px, py) in (ux, uy) direction, circle at (cx, cy) radius r
    dx = px - cx
    dy = py - cy
    # Solve (px + t*ux - cx)^2 + (py + t*uy - cy)^2 = r^2
    a = ux*ux + uy*uy
    b = 2 * (dx*ux + dy*uy)
    c = dx*dx + dy*dy - r*r
    disc = b*b - 4*a*c
    if disc < 0:
        return 0.0
    sqrt_disc = math.sqrt(disc)
    t1 = (-b + sqrt_disc) / (2*a)
    t2 = (-b - sqrt_disc) / (2*a)
    t_candidates = [t for t in (t1, t2) if t > 0]
    return min(t_candidates) if t_candidates else 0.0

def _distance_to_polygon_edge(px, py, ux, uy, points):
    # points: list of (x, y) tuples or Point objects
    min_dist = sys.float_info.max
    n = len(points)
    for i in range(n):
        x1, y1 = points[i][0], points[i][1]
        x2, y2 = points[(i+1)%n][0], points[(i+1)%n][1]
        denom = (x2 - x1) * uy - (y2 - y1) * ux
        if abs(denom) < 1e-8:
            continue
        t = ((x1 - px) * (y2 - y1) - (y1 - py) * (x2 - x1)) / denom
        u = ((x1 - px) * uy - (y1 - py) * ux) / denom
        if t > 0 and 0 <= u <= 1:
            min_dist = min(min_dist, t)
    return min_dist if min_dist != sys.float_info.max else 0.0

def _marker_color(oc, ovr: dict) -> tuple:
    """Return the highlight colour for a polarity marker.

    • Yellow  — detection_method == "fallback" and user has NOT yet reviewed it
    • Green   — everything else (high-confidence detection OR user accepted/flipped)
    """
    method = getattr(oc, "_detection_method", "")
    if method == "fallback" and not ovr:
        return _HIGHLIGHT_YELLOW
    return _HIGHLIGHT_GREEN


def _draw_polarity_marker(page, px:float, py:float, cx:float, cy:float,
                           r:float, color:tuple, is_two_pin:bool, oc:int,
                           body_shape:Optional[dict]=None) -> None:
    """Draw a small highlighter-like stroke inside the component body.
    The stroke is oriented perpendicular to the pin→center direction and
    shifted slightly inward so it looks like a marker dab on the component.
    If body_shape is provided, ensures the marker stays inside the body.
    body_shape: dict with keys: 'type' ('rect'|'circle'|'polygon'), and geometry.
    """
    _ = is_two_pin  # currently same visual style for 2-pin and multi-pin
    dx, dy = cx - px, cy - py
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        ux, uy = 0.0, -1.0
    else:
        ux, uy = dx / dist, dy / dist

    # --- Compute max distance so marker stays inside body ---
    margin = 0.3  # pt, so marker doesn't touch the edge
    marker_dist = max(0.9, r * 0.70)
    if body_shape is not None:
        try:
            if body_shape['type'] == 'rect':
                rect_bbox = body_shape['bbox']  # BoundingBox
                max_dist = _distance_to_rect_edge(px, py, ux, uy, rect_bbox)
                marker_dist = min(marker_dist, max_dist - margin)
            elif body_shape['type'] == 'circle':
                cx0, cy0, rad = body_shape['cx'], body_shape['cy'], body_shape['r']
                max_dist = _distance_to_circle_edge(px, py, ux, uy, cx0, cy0, rad)
                marker_dist = min(marker_dist, max_dist - margin)
            elif body_shape['type'] == 'polygon':
                points = body_shape['points']
                max_dist = _distance_to_polygon_edge(px, py, ux, uy, points)
                marker_dist = min(marker_dist, max_dist - margin)
        except Exception:
            pass
    marker_dist = max(0.5, marker_dist)  # always at least 0.5pt

    # Tangent direction gives an oriented highlight dash.
    tx, ty = -uy, ux
    mx = px + ux * marker_dist
    my = py + uy * marker_dist
    half_len = max(1.2, r * 0.85)

    p1 = fitz.Point(mx - tx * half_len, my - ty * half_len)
    p2 = fitz.Point(mx + tx * half_len, my + ty * half_len)
    width = max(1.4, r * 1.05)

    try:
        page.draw_line(
            p1, p2,
            color=color,
            width=width,
            stroke_opacity=0.42,
            oc=oc,
        )
    except TypeError:
        page.draw_line(p1, p2, color=color, width=width, oc=oc)


# ─────────────────────────────────────────────────────────────────────────────
# Main render function
# ─────────────────────────────────────────────────────────────────────────────

def render_odb_to_pdf(
    odb_path: str,
    output_pdf: str,
    *,
    draw_courtyard: bool = False,
    draw_fab: bool = True,
    draw_cu: bool = False,
    draw_silk: bool = True,
    draw_notes: bool = False,
    draw_refdes: bool = True,
    mark_pin1: bool = True,
    save_png: bool = True,
    margin_mm: float = 2.0,
    overrides: Optional[Dict[str, dict]] = None,
    dnp_refs: Optional[set] = None,
    odb_comps_cache: Optional[list] = None,
    capture_positions: Optional[dict] = None,
    log_fn=None,
) -> str:
    # ↑ signature unchanged — only the body is replaced below
    """Render ODB++ → two-page PDF.

    Page 0 = Top side (normal orientation).
    Page 1 = Bottom side (X-axis mirrored – view from below).

    ``capture_positions`` is filled with ``{ref: (page_idx, pdf_x, pdf_y)}``.
    All other parameters are identical to the previous single-page version.

    Returns absolute path to the saved PDF.
    """
    def _log(msg: str) -> None:
        if log_fn:
            try: log_fn(msg)
            except Exception: pass

    _log("   [render] Opening archive …")
    reader = _ArchiveReader(odb_path)

    _log("   [render] Reading matrix & profile …")
    mc = reader.read("matrix/matrix")
    role_map = _discover_layers(_parse_matrix(mc)) if mc else {
        "copper_top": "f.cu", "silk_top": "f.silkscreen",
        "fab_top": "f.fab", "courtyard_top": "f.courtyard", "notes_top": "f.user",
    }

    pc = reader.read("steps/pcb/profile")
    inch_mode = (pc is not None and _detect_units(pc) == "INCH")
    c2mm = 25.4 if inch_mode else 1.0

    board_outline = _parse_profile(pc) if pc else []
    if board_outline:
        xs = [p[0]*c2mm for p in board_outline]
        ys = [p[1]*c2mm for p in board_outline]
        bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    else:
        bx0, bx1, by0, by1 = 0, 100, 0, 100

    bw = bx1 - bx0 + 2*margin_mm
    bh = by1 - by0 + 2*margin_mm
    pw = bw * MM_TO_PT
    ph = bh * MM_TO_PT

    # Coordinate transforms
    # Top page  – normal X  (profile/layer coords, uses c2mm)
    def tx(x):   return (x*c2mm - bx0 + margin_mm) * MM_TO_PT
    def ty(y):   return (by1 - y*c2mm + margin_mm) * MM_TO_PT
    # Bottom page – X mirrored (viewing PCB from below)
    def tx_b(x): return (bx1 - x*c2mm + margin_mm) * MM_TO_PT

    # Component transforms – coords are ALWAYS in mm after parser normalisation
    # (ODBParser._normalize_to_mm converts inch files to mm internally).
    def txc(x):   return (x - bx0 + margin_mm) * MM_TO_PT
    def tyc(y):   return (by1 - y + margin_mm) * MM_TO_PT
    def txc_b(x): return (bx1 - x + margin_mm) * MM_TO_PT

    _log(f"   [render] Board {bw:.0f}×{bh:.0f} mm  →  page {pw:.0f}×{ph:.0f} pt")

    doc = fitz.open()
    doc.new_page(width=pw, height=ph)   # index 0 = Top
    doc.new_page(width=pw, height=ph)   # index 1 = Bottom

    # ── OCGs shared across both pages ─────────────────────────────────────────
    ocg_outline = doc.add_ocg("Board outline",        on=True)
    ocg_copper  = doc.add_ocg("Copper",               on=draw_cu)
    ocg_fab     = doc.add_ocg("Fab / Assembly",       on=True)
    ocg_silk    = doc.add_ocg("Silkscreen",           on=True)
    ocg_court   = doc.add_ocg("Courtyard",            on=draw_courtyard)
    ocg_notes   = doc.add_ocg("Notes / User Drawing", on=draw_notes)
    ocg_labels  = doc.add_ocg("Reference labels",     on=draw_refdes)
    ocg_markers = doc.add_ocg("Polarity markers",     on=True)
    ocg_dnp     = doc.add_ocg("DNP (Not Placed)",     on=True)

    # ── Page title labels ─────────────────────────────────────────────────────
    title_fs = max(5.0, min(8.0, bw / 20))
    for pi, title in [(0, "TOP SIDE"), (1, "BOTTOM SIDE (mirrored)")]:
        try:
            doc[pi].insert_text(
                fitz.Point(margin_mm * MM_TO_PT, margin_mm * 0.65 * MM_TO_PT),
                title, fontsize=title_fs, color=(0.35, 0.35, 0.35), fontname="helv",
            )
        except Exception:
            pass

    # ── Board outline on both pages ───────────────────────────────────────────
    _log(f"   [render] Drawing board outline ({len(board_outline)} pts) …")
    if len(board_outline) >= 2:
        for pi, tfx in [(0, tx), (1, tx_b)]:
            s = doc[pi].new_shape()
            for i in range(len(board_outline) - 1):
                s.draw_line(
                    fitz.Point(tfx(board_outline[i][0]),   ty(board_outline[i][1])),
                    fitz.Point(tfx(board_outline[i+1][0]), ty(board_outline[i+1][1])),
                )
            s.finish(color=_ROLE_COLORS["profile"], width=0.8, oc=ocg_outline)
            s.commit()

    # ── PCB layer rendering plan ──────────────────────────────────────────────
    # Each entry: (role_key, layer_folder, ocg, page_index, tfx)
    plan: list = []
    if draw_cu:
        if "copper_top" in role_map:
            plan.append(("copper_top", role_map["copper_top"], ocg_copper, 0, tx))
        if "copper_bot" in role_map:
            plan.append(("copper_bot", role_map["copper_bot"], ocg_copper, 1, tx_b))
    if draw_courtyard:
        if "courtyard_top" in role_map:
            plan.append(("courtyard_top", role_map["courtyard_top"], ocg_court, 0, tx))
        if "courtyard_bot" in role_map:
            plan.append(("courtyard_bot", role_map["courtyard_bot"], ocg_court, 1, tx_b))
    if draw_fab:
        if "fab_top" in role_map:
            plan.append(("fab_top", role_map["fab_top"], ocg_fab, 0, tx))
        if "fab_bot" in role_map:
            plan.append(("fab_bot", role_map["fab_bot"], ocg_fab, 1, tx_b))
    if draw_silk:
        if "silk_top" in role_map:
            plan.append(("silk_top", role_map["silk_top"], ocg_silk, 0, tx))
        if "silk_bot" in role_map:
            plan.append(("silk_bot", role_map["silk_bot"], ocg_silk, 1, tx_b))
    if draw_notes and "notes_top" in role_map:
        plan.append(("notes_top", role_map["notes_top"], ocg_notes, 0, tx))

    for role, folder, ocg, pi, tfx in plan:
        _log(f"   [render] Reading layer '{folder}' …")
        cnt = reader.read(f"steps/pcb/layers/{folder}/features")
        if not cnt:
            _log("   [render]   (not found, skipped)")
            continue
        color = _ROLE_COLORS.get(role, (0.4, 0.4, 0.4))
        syms, fl_lines, fl_pads, fl_arcs = _parse_features(cnt)
        _log(f"   [render]   {role}: {len(fl_lines)} lines, "
             f"{len(fl_pads)} pads, {len(fl_arcs)} arcs → drawing …")

        if role in ("copper_top", "copper_bot"):
            shape = doc[pi].new_shape()
            for fp in fl_pads:
                sym = syms.get(fp.sym_idx)
                if not sym: continue
                cx_, cy_ = tfx(fp.x), ty(fp.y)
                if sym.is_round:
                    r = max(0.3, sym.w_mm(inch_mode) * MM_TO_PT / 2)
                    shape.draw_circle(fitz.Point(cx_, cy_), r)
                elif sym.is_rect:
                    w = max(0.3, sym.w_mm(inch_mode) * MM_TO_PT)
                    h = max(0.3, sym.h_mm(inch_mode) * MM_TO_PT)
                    shape.draw_rect(fitz.Rect(cx_ - w/2, cy_ - h/2, cx_ + w/2, cy_ + h/2))
            shape.finish(color=color, fill=color, width=0, oc=ocg)
            shape.commit()
            _log("   [render]   copper done.")
            continue


        width_groups: Dict[float, list] = {}
        for fl in fl_lines:
            sym = syms.get(fl.sym_idx)
            lw = round(
                max(0.15, min((sym.w_mm(inch_mode) * MM_TO_PT) if sym else 0.3, 1.5)), 3)
            width_groups.setdefault(lw, []).append(fl)

        shape = doc[pi].new_shape()
        for lw, group in width_groups.items():
            for fl in group:
                shape.draw_line(
                    fitz.Point(tfx(fl.x1), ty(fl.y1)),
                    fitz.Point(tfx(fl.x2), ty(fl.y2)),
                )
            shape.finish(color=color, width=lw, oc=ocg)

        if fl_arcs:
            for arc in fl_arcs:
                for i in range(len(arc.points) - 1):
                    shape.draw_line(
                        fitz.Point(tfx(arc.points[i][0]),   ty(arc.points[i][1])),
                        fitz.Point(tfx(arc.points[i+1][0]), ty(arc.points[i+1][1])),
                    )
            shape.finish(color=color, width=0.3, oc=ocg)

        shape.commit()
        _log(f"   [render]   {role} done "
             f"({len(width_groups)} width group(s), {len(fl_arcs)} arc(s)).")

    # ── Component data ────────────────────────────────────────────────────────
    if odb_comps_cache is not None:
        odb_comps = odb_comps_cache
        _log(f"   [render] Using {len(odb_comps)} pre-parsed components.")
    else:
        _log("   [render] Parsing component data …")
        from core.odb_parser import parse_odb_raw
        try:
            odb_comps, _ = parse_odb_raw(odb_path)
            _log(f"   [render]   Got {len(odb_comps)} components.")
        except Exception as exc:
            _log(f"   [render]   Parse failed: {exc}")
            odb_comps = []

    top_comps = [c for c in odb_comps if c.side == "top"]
    bot_comps = [c for c in odb_comps if c.side != "top"]
    _log(f"   [render] Components: {len(top_comps)} top, {len(bot_comps)} bottom.")

    # Capture PDF coordinates: {ref: (page_idx, pdf_x, pdf_y)}
    if capture_positions is not None:
        for oc in top_comps:
            capture_positions[oc.ref] = (0, txc(oc.x), tyc(oc.y))
        for oc in bot_comps:
            capture_positions[oc.ref] = (1, txc_b(oc.x), tyc(oc.y))

    overrides = overrides or {}
    dnp_set   = {r.strip().upper() for r in dnp_refs} if dnp_refs else set()
    font_size = max(2.8, min(4.0, bw / 25))
    base_r    = max(1.8, min(4.0, bw / 22))

    # comp_render_pairs: [(components, page_index, txc_fn), ...]
    # Use the component-specific transforms (txc/txc_b) — coords are in mm.
    comp_render_pairs = [
        (top_comps, 0, txc),
        (bot_comps, 1, txc_b),
    ]

    # ── Reference labels ──────────────────────────────────────────────────────
    n_labels = sum(1 for c in odb_comps if not c.ref.upper().startswith(("TP", "FID")))
    _log(f"   [render] Drawing {n_labels} reference labels …")
    for comps, pi, tfx in comp_render_pairs:
        for oc in comps:
            if oc.ref.upper().startswith(("TP", "FID")): continue
            cx_, cy_ = tfx(oc.x), tyc(oc.y)
            try:
                doc[pi].insert_text(
                    fitz.Point(cx_ - font_size * 0.32 * len(oc.ref) / 2, cy_ + font_size * 0.35),
                    oc.ref, fontsize=font_size, color=(0.1, 0.1, 0.1),
                    fontname="helv", oc=ocg_labels,
                )
            except Exception:
                pass

    # ── Polarity markers ──────────────────────────────────────────────────────
    if mark_pin1:
        n_polar = sum(
            1 for oc in odb_comps
            if oc.ref.upper() not in dnp_set
            and overrides.get(oc.ref, {}).get("polar") is not False
            and (oc.is_polar if overrides.get(oc.ref, {}).get("polar") is None else True)
        )
        _log(f"   [render] Drawing polarity markers (~{n_polar} polar components) …")

        # Best-effort: load body shapes from an existing companion PDF (top only)
        comp_shape_dict: dict = {}
        try:
            from core.pdf_parser import PDFParser
            from core.component_shape_assign import assign_shapes_to_components
            from core.component_detector import Component as _Comp
            from utils.geometry import BoundingBox as _BB, Point as _Pt
            companion = odb_path.replace(".zip", ".pdf").replace(".tgz", ".pdf")
            if os.path.isfile(companion):
                _parser = PDFParser(companion)
                _pages  = _parser.parse()
                _parser.close()
                _shapes = [s for p in _pages for s in p.shapes]
                _dummy  = [
                    _Comp(ref=oc.ref, comp_type=oc.comp_type,
                          bbox=_BB(oc.x, oc.y, oc.x+1, oc.y+1),
                          center=_Pt(oc.x, oc.y), page=0)
                    for oc in top_comps
                ]
                comp_shape_dict = {
                    c.ref: shapes
                    for c, shapes in assign_shapes_to_components(
                        _dummy, _shapes, margin=2.0).items()
                }
        except Exception:
            pass

        for comps, pi, tfx in comp_render_pairs:
            for oc in comps:
                ovr         = overrides.get(oc.ref, {})
                force_polar = ovr.get("polar")
                if force_polar is False: continue
                if oc.ref.upper() in dnp_set: continue
                is_polar = oc.is_polar if force_polar is None else force_polar
                if not is_polar: continue

                if ovr.get("flip_pin"):
                    pp = oc.polarity_pin
                    if pp is not None:
                        p2 = oc.pin2
                        pp = oc.pin1 if (p2 and pp.number == p2.number) else (oc.pin2 or oc.pin1)
                else:
                    pp = oc.polarity_pin
                if pp is None: continue

                px_, py_ = tfx(pp.x), tyc(pp.y)
                cx_, cy_ = tfx(oc.x),  tyc(oc.y)
                # pin_span_mm is already in mm (parser normalises to mm)
                span_mm  = oc.pin_span_mm
                marker_r = max(1.0, min(base_r * 1.5, span_mm * 0.30 * MM_TO_PT)) if span_mm > 0 else base_r

                body_shape = None
                for s in comp_shape_dict.get(oc.ref, []):
                    if s.shape_type in ("rect", "filled_rect"):
                        body_shape = {"type": "rect", "bbox": s.bbox}; break
                    elif s.shape_type in ("circle", "filled_circle"):
                        cx0 = (s.bbox.x0 + s.bbox.x1) / 2.0
                        cy0 = (s.bbox.y0 + s.bbox.y1) / 2.0
                        rad = min(s.bbox.width, s.bbox.height) / 2.0
                        body_shape = {"type": "circle", "cx": cx0, "cy": cy0, "r": rad}; break
                    elif s.shape_type in ("polyline", "path") and len(s.points) >= 3:
                        body_shape = {"type": "polygon", "points": [(p.x, p.y) for p in s.points]}; break

                _draw_polarity_marker(
                    doc[pi], px_, py_, cx_, cy_, marker_r,
                    _marker_color(oc, ovr), len(oc.pins) == 2, ocg_markers, body_shape,
                )

        _log("   [render] Polarity markers done.")

    # ── DNP markers ───────────────────────────────────────────────────────────
    if dnp_refs:
        n_dnp = 0
        for comps, pi, tfx in comp_render_pairs:
            for oc in comps:
                if oc.ref.upper() not in dnp_set: continue
                n_dnp += 1
                cx_, cy_ = tfx(oc.x), tyc(oc.y)
                if oc.pins:
                    xs = [tfx(p.x) for p in oc.pins]
                    ys = [tyc(p.y)  for p in oc.pins]
                    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
                else:
                    half = (max(3.0, oc.pin_span_mm * MM_TO_PT * 0.5)
                            if oc.pin_span_mm > 0 else max(3.0, font_size * 1.5))
                    x0, y0, x1, y1 = cx_ - half, cy_ - half, cx_ + half, cy_ + half
                if (x1 - x0) < base_r:
                    mid = (x0 + x1) / 2; x0, x1 = mid - base_r/2, mid + base_r/2
                if (y1 - y0) < base_r:
                    mid = (y0 + y1) / 2; y0, y1 = mid - base_r/2, mid + base_r/2
                fs = doc[pi].new_shape()
                fs.draw_rect(fitz.Rect(x0, y0, x1, y1))
                fs.finish(fill=_HIGHLIGHT_ORANGE, fill_opacity=0.35, width=0, oc=ocg_dnp)
                fs.commit()
        _log(f"   [render] DNP markers done ({n_dnp} component(s)).")

    # ── Save ──────────────────────────────────────────────────────────────────
    _log("   [render] Saving PDF …")
    doc.save(output_pdf, deflate=True)
    _log(f"   [render] PDF saved ({os.path.getsize(output_pdf)//1024} KB).")

    if save_png:
        _log("   [render] Generating PNG previews …")
        for pi, suffix in [(0, "_preview.png"), (1, "_bot_preview.png")]:
            try:
                pix = doc[pi].get_pixmap(matrix=fitz.Matrix(2, 2))
                pix.save(output_pdf.replace(".pdf", suffix))
                _log(f"   [render] PNG saved: {suffix}")
            except Exception:
                pass

    doc.close()
    return os.path.abspath(output_pdf)



# ─────────────────────────────────────────────────────────────────────────────
# Archive reader  — pre-caches tgz content to avoid repeated decompression
# ─────────────────────────────────────────────────────────────────────────────

class _ArchiveReader:
    """Read files from an ODB++ archive (zip / tgz / dir).

    For .tgz archives, all text content is loaded into memory in a single
    sequential decompression pass during __init__.  This prevents the severe
    performance penalty of re-decompressing the gzip stream for every
    ``read()`` call (gzip streams are not efficiently seekable).

    The actual ODB++ step directory (usually ``pcb`` but may be ``board`` or
    any other name depending on the CAD tool) is auto-detected so that paths
    like ``steps/pcb/profile`` resolve correctly regardless of the step name.
    """

    def __init__(self, path: str):
        self.path = path
        self._zf: Optional[zipfile.ZipFile] = None
        self._is_dir: bool = False
        self._cache: Dict[str, str] = {}   # normalised lower-case path → text
        self._step_name: str = "pcb"       # auto-detected below

        low = path.lower()
        if os.path.isdir(path):
            self._is_dir = True
        elif low.endswith(".zip"):
            self._zf = zipfile.ZipFile(path, "r")
        elif low.endswith((".tgz", ".tar.gz", ".tar")):
            mode = "r:gz" if low.endswith((".gz", ".tgz")) else "r"
            with tarfile.open(path, mode) as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    if member.size > 30_000_000:   # skip files > 30 MB
                        continue
                    try:
                        f = tf.extractfile(member)
                        if f:
                            text = f.read().decode("utf-8", errors="replace")
                            key = member.name.replace("\\", "/").lower()
                            self._cache[key] = text
                    except Exception:
                        pass
        else:
            raise ValueError(f"Unsupported ODB++ path: {path}")

        # Auto-detect the actual step name
        self._step_name = self._detect_step_name()

    def _detect_step_name(self) -> str:
        """Find the step directory name (e.g. 'pcb', 'board', …)."""
        try:
            if self._cache:
                names = list(self._cache.keys())
            elif self._zf:
                names = [n.replace("\\", "/").lower() for n in self._zf.namelist()]
            elif self._is_dir:
                steps_dir = os.path.join(self.path, "steps")
                if os.path.isdir(steps_dir):
                    for entry in os.listdir(steps_dir):
                        if os.path.isdir(os.path.join(steps_dir, entry)):
                            return entry.lower()
                return "pcb"
            else:
                return "pcb"

            for n in names:
                idx = n.find("/steps/")
                if idx >= 0:
                    rest = n[idx + len("/steps/"):]
                    if rest and "/" in rest:
                        candidate = rest.split("/")[0]
                        if candidate:
                            return candidate
                # Path starts directly with steps/ (no leading directory)
                if n.startswith("steps/"):
                    rest = n[len("steps/"):]
                    if rest and "/" in rest:
                        candidate = rest.split("/")[0]
                        if candidate:
                            return candidate
        except Exception:
            pass
        return "pcb"

    def read(self, rel_path: str) -> Optional[str]:
        rn = rel_path.replace("\\", "/").lower()
        # Substitute the actual step name when paths reference "steps/pcb/"
        if self._step_name != "pcb":
            rn = rn.replace("steps/pcb/", f"steps/{self._step_name}/", 1)
        try:
            if self._zf:
                for n in self._zf.namelist():
                    if n.replace("\\", "/").lower().endswith(rn):
                        return self._zf.read(n).decode("utf-8", errors="replace")
            elif self._cache:
                for key, content in self._cache.items():
                    if key.endswith(rn):
                        return content
            elif self._is_dir:
                full = os.path.join(self.path, rel_path)
                if os.path.isfile(full):
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        return fh.read()
        except Exception:
            pass
        return None

    def list_layer_dirs(self) -> List[str]:
        dirs: set = set()
        prefix = f"steps/{self._step_name}/layers/"
        try:
            if self._zf:
                names = self._zf.namelist()
            elif self._cache:
                names = list(self._cache.keys())
            else:
                names = []
            for n in names:
                nn = n.replace("\\", "/").lower()
                idx = nn.find(prefix)
                if idx >= 0:
                    rest = nn[idx + len(prefix):]
                    if "/" in rest:
                        dirs.add(rest.split("/")[0])
        except Exception:
            pass
        return sorted(dirs)

    def __del__(self):
        if self._zf:
            self._zf.close()




