"""
core/odb_renderer.py
────────────────────
Renders an ODB++ archive directly to a PDF using PyMuPDF (fitz).

Universal renderer — discovers layers from the ``matrix/matrix`` file so it
works with ODB++ data from **any** EDA tool (KiCad, Altium, Cadence, Mentor,
Zuken, etc.).

Draws these layers in order:
  1. Board outline (profile)                — black
  2. Top fabrication / assembly drawing      — blue lines
  3. Top silkscreen                         — dark magenta
  4. Top courtyard (if present)             — light cyan
  5. Component ref-des labels               — small black text
  6. Pin-1 polarity markers                 — green circles (adaptive size)
  7. Cathode markers (diodes/LEDs)          — orange circles (adaptive size)

Copper pads/vias are **not** drawn by default for a cleaner view.
Marker size adapts to each component's pin span so small parts get small
markers and large ICs get proportionally bigger ones.

Ceramic capacitors (CAPC*) are excluded from polarity marking.
Diodes mark the **cathode** (pin 2) instead of pin 1 (anode) to match
the physical PCB marking convention.

Supports both ``UNITS=MM`` and ``UNITS=INCH`` coordinate systems.
"""
from __future__ import annotations

import os
import re
import zipfile
import tarfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

MM_TO_PT = 72.0 / 25.4   # ≈ 2.8346
INCH_TO_PT = 72.0
MIL_TO_MM = 0.0254        # 1 mil = 0.0254 mm


# ─────────────────────────────────────────────────────────────────────────────
# Symbol parser  (symbol name → width/height)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Symbol:
    """Parsed ODB++ symbol (pad aperture).

    Numeric values stored in *raw* ODB++ units:
      - UNITS=MM  → µm  (divide by 1000 for mm)
      - UNITS=INCH → mils (multiply by 0.0254 for mm)
    """
    name: str
    width_raw: float = 0.0
    height_raw: float = 0.0
    is_round: bool = False
    is_rect: bool = False
    corner_r_raw: float = 0.0

    def w_mm(self, inch_mode: bool = False) -> float:
        if inch_mode:
            return self.width_raw * MIL_TO_MM
        return self.width_raw / 1000.0

    def h_mm(self, inch_mode: bool = False) -> float:
        if inch_mode:
            return self.height_raw * MIL_TO_MM
        return self.height_raw / 1000.0

    def cr_mm(self, inch_mode: bool = False) -> float:
        if inch_mode:
            return self.corner_r_raw * MIL_TO_MM
        return self.corner_r_raw / 1000.0


_RE_ROUND = re.compile(r"^r([\d.]+)$")                                    # r1000.0
_RE_SQUARE = re.compile(r"^s([\d.]+)$")                                   # s500
_RE_RECT  = re.compile(r"^rect([\d.]+)x([\d.]+)(?:xr([\d.]+))?$")        # rect800x1500
_RE_DONUT_R = re.compile(r"^donut_r([\d.]+)x([\d.]+)$")                   # donut_r2020.0x1840.0
_RE_DONUT_RC = re.compile(r"^donut_rc([\d.]+)x([\d.]+)x([\d.]+)(?:xr([\d.]+))?$")


def _parse_symbol(name: str) -> _Symbol:
    m = _RE_ROUND.match(name)
    if m:
        d = float(m.group(1))
        return _Symbol(name=name, width_raw=d, height_raw=d, is_round=True)
    m = _RE_SQUARE.match(name)
    if m:
        d = float(m.group(1))
        return _Symbol(name=name, width_raw=d, height_raw=d, is_rect=True)
    m = _RE_RECT.match(name)
    if m:
        w, h = float(m.group(1)), float(m.group(2))
        cr = float(m.group(3)) if m.group(3) else 0.0
        return _Symbol(name=name, width_raw=w, height_raw=h, is_rect=True, corner_r_raw=cr)
    m = _RE_DONUT_R.match(name)
    if m:
        od = float(m.group(1))
        return _Symbol(name=name, width_raw=od, height_raw=od, is_round=True)
    m = _RE_DONUT_RC.match(name)
    if m:
        w, h = float(m.group(1)), float(m.group(2))
        cr = float(m.group(4)) if m.group(4) else 0.0
        return _Symbol(name=name, width_raw=w, height_raw=h, is_rect=True, corner_r_raw=cr)
    return _Symbol(name=name, width_raw=150, height_raw=150, is_round=True)


# ─────────────────────────────────────────────────────────────────────────────
# Feature parser
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Line:
    x1: float; y1: float; x2: float; y2: float
    sym_idx: int


@dataclass
class _Pad:
    x: float; y: float
    sym_idx: int
    orient: float  # degrees


@dataclass
class _Arc:
    points: List[Tuple[float, float]]


def _parse_features(content: str):
    """Parse an ODB++ features file, return (symbols, lines, pads, arcs)."""
    symbols: Dict[int, _Symbol] = {}
    lines: List[_Line] = []
    pads: List[_Pad] = []
    arcs: List[_Arc] = []

    in_features = False
    current_arc: Optional[_Arc] = None

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            if "#Layer features" in raw:
                in_features = True
            continue

        # Symbol definitions: $0 r1000.0
        if line.startswith("$"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                idx = int(parts[0][1:])
                symbols[idx] = _parse_symbol(parts[1])
            continue

        if not in_features:
            continue

        # Line: L x1 y1 x2 y2 sym_idx P polarity [;attr]
        if line.startswith("L "):
            parts = line.split(";")[0].split()
            if len(parts) >= 6:
                try:
                    lines.append(_Line(
                        x1=float(parts[1]), y1=float(parts[2]),
                        x2=float(parts[3]), y2=float(parts[4]),
                        sym_idx=int(parts[5]),
                    ))
                except (ValueError, IndexError):
                    pass
            continue

        # Pad: P x y sym_idx P polarity orient rotation [;attr]
        if line.startswith("P "):
            parts = line.split(";")[0].split()
            if len(parts) >= 4:
                try:
                    orient = float(parts[7]) if len(parts) > 7 else 0.0
                    pads.append(_Pad(
                        x=float(parts[1]), y=float(parts[2]),
                        sym_idx=int(parts[3]),
                        orient=orient,
                    ))
                except (ValueError, IndexError):
                    pass
            continue

        # Arc/polygon: OB (begin), OS (segment), OE (end)
        if line.startswith("OB "):
            parts = line.split()
            try:
                current_arc = _Arc(points=[(float(parts[1]), float(parts[2]))])
            except (ValueError, IndexError):
                current_arc = None
            continue
        if line.startswith("OS ") and current_arc is not None:
            parts = line.split()
            try:
                current_arc.points.append((float(parts[1]), float(parts[2])))
            except (ValueError, IndexError):
                pass
            continue
        if line.startswith("OE") and current_arc is not None:
            arcs.append(current_arc)
            current_arc = None
            continue

    return symbols, lines, pads, arcs


# ─────────────────────────────────────────────────────────────────────────────
# Matrix parser — discovers layers by role
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _MatrixLayer:
    """One LAYER block from matrix/matrix."""
    row: int
    name: str           # layer folder name (lowercase for fs lookup)
    layer_type: str     # SIGNAL, SILK_SCREEN, SOLDER_MASK, COMPONENT, DOCUMENT, …
    context: str        # BOARD or MISC
    polarity: str = "POSITIVE"


def _parse_matrix(content: str) -> List[_MatrixLayer]:
    """Parse the ``matrix/matrix`` file and return a list of layer descriptors."""
    layers: List[_MatrixLayer] = []
    in_layer = False
    row = 0
    name = ""
    ltype = ""
    ctx = ""
    pol = "POSITIVE"

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line == "LAYER {":
            in_layer = True
            row = 0; name = ""; ltype = ""; ctx = ""; pol = "POSITIVE"
            continue
        if line == "}" and in_layer:
            if name:
                layers.append(_MatrixLayer(
                    row=row, name=name.lower(), layer_type=ltype.upper(),
                    context=ctx.upper(), polarity=pol.upper(),
                ))
            in_layer = False
            continue
        if not in_layer:
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().upper()
        val = val.strip()
        if key == "ROW":
            row = int(val) if val.isdigit() else 0
        elif key == "NAME":
            name = val
        elif key == "TYPE":
            ltype = val
        elif key == "CONTEXT":
            ctx = val
        elif key == "POLARITY":
            pol = val
    return layers


def _discover_layers(matrix_layers: List[_MatrixLayer]) -> Dict[str, str]:
    """
    Map rendering roles to actual layer folder names.

    Returns a dict like::

        {
            "copper_top":      "f.cu"          or "layer_1_top.doc",
            "silk_top":        "f.silkscreen"  or "silkscreen_top",
            "fab_top":         "f.fab"         or "assembly_layer_top.doc",
            "courtyard_top":   "f.courtyard"   or None,
        }

    Uses NAME-first heuristics because many EDA tools assign inaccurate TYPEs.
    """
    result: Dict[str, str] = {}
    all_layers = matrix_layers

    # Helper: find first layer whose name contains ALL given keywords
    def _find(*keywords) -> Optional[str]:
        for ml in all_layers:
            low = ml.name
            if all(kw in low for kw in keywords):
                return ml.name
        return None

    # ── Top copper ──
    # Prefer exact match (KiCad), then "layer_1" + "top", then first SIGNAL
    if _find("f.cu"):
        result["copper_top"] = _find("f.cu")
    elif _find("layer_1", "top"):
        result["copper_top"] = _find("layer_1", "top")
    elif _find("layer", "top"):
        # Be careful not to pick "assembly_layer_top" — require no "assembly"
        for ml in all_layers:
            low = ml.name
            if "layer" in low and "top" in low and "assembly" not in low and "silk" not in low:
                result["copper_top"] = ml.name
                break
    if "copper_top" not in result:
        # Last resort: first SIGNAL layer with "top" or "f.cu" in name
        for ml in all_layers:
            if ml.layer_type == "SIGNAL" and ("top" in ml.name or "f.cu" == ml.name):
                # But not "silkscreen_top"
                if "silk" not in ml.name:
                    result["copper_top"] = ml.name
                    break

    # ── Top silkscreen ──
    # Prefer name with "silkscreen" + "top" (but not "bottom")
    # Then TYPE=SILK_SCREEN if the name doesn't suggest something else
    found_silk = None
    for ml in all_layers:
        low = ml.name
        if "silk" in low and "top" in low and "bot" not in low:
            found_silk = ml.name
            break
    if not found_silk:
        # KiCad: f.silkscreen (TYPE=SILK_SCREEN)
        found_silk = _find("f.silk")
    if not found_silk:
        # Fallback: TYPE=SILK_SCREEN, but only if name doesn't suggest solder_mask
        for ml in all_layers:
            if ml.layer_type == "SILK_SCREEN" and "mask" not in ml.name and "solder" not in ml.name:
                if "bot" not in ml.name and "bottom" not in ml.name:
                    found_silk = ml.name
                    break
    if found_silk:
        result["silk_top"] = found_silk

    # ── Top fabrication / assembly ──
    found_fab = _find("f.fab")
    if not found_fab:
        found_fab = _find("assembly", "top")
    if found_fab:
        result["fab_top"] = found_fab

    # ── Top courtyard ──
    found_court = _find("f.courtyard")
    if not found_court:
        # Look for "courtyard" + "top" or just "courtyard" (not bottom)
        for ml in all_layers:
            if "courtyard" in ml.name and "bot" not in ml.name and "bottom" not in ml.name and "b." not in ml.name:
                found_court = ml.name
                break
    if found_court:
        result["courtyard_top"] = found_court

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Unit detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_units(content: str) -> str:
    """Detect UNITS=INCH or UNITS=MM from a features/profile file header."""
    for line in content.splitlines()[:20]:
        stripped = line.strip().upper()
        if stripped.startswith("UNITS"):
            if "INCH" in stripped:
                return "INCH"
            return "MM"
    return "MM"


# ─────────────────────────────────────────────────────────────────────────────
# Renderer
# ─────────────────────────────────────────────────────────────────────────────

# Role-based colours (R, G, B) 0–1
_ROLE_COLORS = {
    "copper_top":    (0.70, 0.16, 0.16),     # dark red
    "silk_top":      (0.55, 0.0, 0.55),       # magenta
    "fab_top":       (0.20, 0.20, 0.72),      # blue
    "courtyard_top": (0.0, 0.55, 0.55),       # cyan
    "profile":       (0.0, 0.0, 0.0),         # black
}

_PIN1_COLOR = (0.0, 0.75, 0.0)  # green for polarity markers


def _draw_pad(shape, fp: _Pad, symbols, tx, ty, color, filled: bool,
              inch_mode: bool = False):
    """Draw a single pad (round or rect) at its position."""
    sym = symbols.get(fp.sym_idx)
    if sym is None:
        return
    cx, cy = tx(fp.x), ty(fp.y)
    w_pt = sym.w_mm(inch_mode) * MM_TO_PT
    h_pt = sym.h_mm(inch_mode) * MM_TO_PT

    if sym.is_round:
        r = max(0.3, w_pt / 2)
        shape.draw_circle(fitz.Point(cx, cy), r)
        if filled:
            shape.finish(color=color, fill=color, width=0)
        else:
            shape.finish(color=color, width=0.3)
    elif sym.is_rect:
        w_pt = max(0.3, w_pt)
        h_pt = max(0.3, h_pt)
        rect = fitz.Rect(cx - w_pt / 2, cy - h_pt / 2,
                         cx + w_pt / 2, cy + h_pt / 2)
        shape.draw_rect(rect)
        if filled:
            shape.finish(color=color, fill=color, width=0)
        else:
            shape.finish(color=color, width=0.3)


def render_odb_to_pdf(
    odb_path: str,
    output_pdf: str,
    *,
    draw_courtyard: bool = False,
    draw_fab: bool = True,
    draw_cu: bool = False,
    draw_silk: bool = True,
    mark_pin1: bool = True,
    save_png: bool = True,
    margin_mm: float = 2.0,
) -> str:
    """
    Render an ODB++ archive to a clean PDF.

    Works with **any** ODB++ source (KiCad, Altium, Cadence, Zuken, …)
    by discovering layer names from the ``matrix/matrix`` file and
    auto-detecting coordinate units (MM or INCH).

    Returns the output PDF path.
    """
    # ── Read archive ──────────────────────────────────────────────────────
    reader = _ArchiveReader(odb_path)

    # ── Discover layers via matrix ────────────────────────────────────────
    matrix_content = reader.read("matrix/matrix")
    if matrix_content:
        matrix_layers = _parse_matrix(matrix_content)
        role_map = _discover_layers(matrix_layers)
    else:
        # Fallback: KiCad-style hardcoded names
        role_map = {
            "copper_top":    "f.cu",
            "silk_top":      "f.silkscreen",
            "fab_top":       "f.fab",
            "courtyard_top": "f.courtyard",
        }

    # ── Detect units from profile ─────────────────────────────────────────
    profile_content = reader.read("steps/pcb/profile")
    inch_mode = False
    if profile_content:
        units = _detect_units(profile_content)
        inch_mode = (units == "INCH")

    # Scale factor: source coordinate units → mm
    # INCH: coordinates are in inches, so × 25.4 → mm
    # MM:   coordinates are already mm, so × 1.0
    coord_to_mm = 25.4 if inch_mode else 1.0

    board_outline = _parse_profile(profile_content) if profile_content else []

    # Determine board bounds from profile (in mm after conversion)
    if board_outline:
        xs = [p[0] * coord_to_mm for p in board_outline]
        ys = [p[1] * coord_to_mm for p in board_outline]
        bx0, bx1 = min(xs), max(xs)
        by0, by1 = min(ys), max(ys)
    else:
        bx0, bx1, by0, by1 = 0, 100, 0, 100  # fallback

    board_w_mm = bx1 - bx0 + 2 * margin_mm
    board_h_mm = by1 - by0 + 2 * margin_mm

    # PDF page size in points
    page_w = board_w_mm * MM_TO_PT
    page_h = board_h_mm * MM_TO_PT

    def tx(x_coord: float) -> float:
        return (x_coord * coord_to_mm - bx0 + margin_mm) * MM_TO_PT

    def ty(y_coord: float) -> float:
        # Flip Y: ODB++ Y points up → PDF Y points down
        return (by1 - y_coord * coord_to_mm + margin_mm) * MM_TO_PT

    # ── Create PDF ────────────────────────────────────────────────────────
    doc = fitz.open()
    page = doc.new_page(width=page_w, height=page_h)
    shape = page.new_shape()

    # ── Board outline ─────────────────────────────────────────────────────
    if board_outline and len(board_outline) >= 2:
        color = _ROLE_COLORS["profile"]
        for i in range(len(board_outline) - 1):
            p1 = board_outline[i]
            p2 = board_outline[i + 1]
            shape.draw_line(
                fitz.Point(tx(p1[0]), ty(p1[1])),
                fitz.Point(tx(p2[0]), ty(p2[1])),
            )
        shape.finish(color=color, width=0.8)

    # ── Layer rendering ───────────────────────────────────────────────────
    render_order = []
    if draw_cu and "copper_top" in role_map:
        render_order.append(("copper_top", role_map["copper_top"]))
    if draw_courtyard and "courtyard_top" in role_map:
        render_order.append(("courtyard_top", role_map["courtyard_top"]))
    if draw_fab and "fab_top" in role_map:
        render_order.append(("fab_top", role_map["fab_top"]))
    if draw_silk and "silk_top" in role_map:
        render_order.append(("silk_top", role_map["silk_top"]))

    for role, layer_folder in render_order:
        content = reader.read(f"steps/pcb/layers/{layer_folder}/features")
        if not content:
            continue
        color = _ROLE_COLORS.get(role, (0.5, 0.5, 0.5))
        symbols, feat_lines, feat_pads, feat_arcs = _parse_features(content)

        # ── Copper: ONLY pads, skip traces / arcs / pours ─────────────
        if role == "copper_top":
            for fp in feat_pads:
                _draw_pad(shape, fp, symbols, tx, ty, color, filled=True,
                          inch_mode=inch_mode)
            continue

        # ── Other layers: draw lines (component outlines) + pads ──────
        for fl in feat_lines:
            sym = symbols.get(fl.sym_idx)
            lw = (sym.w_mm(inch_mode) * MM_TO_PT) if sym else 0.3
            lw = max(0.15, min(lw, 1.5))
            shape.draw_line(
                fitz.Point(tx(fl.x1), ty(fl.y1)),
                fitz.Point(tx(fl.x2), ty(fl.y2)),
            )
            shape.finish(color=color, width=lw)

        for fp in feat_pads:
            _draw_pad(shape, fp, symbols, tx, ty, color, filled=False,
                      inch_mode=inch_mode)

        # Arcs/polygons (silkscreen outlines etc., NOT copper pour)
        for arc in feat_arcs:
            if len(arc.points) < 2:
                continue
            for i in range(len(arc.points) - 1):
                p1, p2 = arc.points[i], arc.points[i + 1]
                shape.draw_line(
                    fitz.Point(tx(p1[0]), ty(p1[1])),
                    fitz.Point(tx(p2[0]), ty(p2[1])),
                )
            shape.finish(color=color, width=0.3)

    # ── Component labels ──────────────────────────────────────────────────
    from core.odb_parser import parse_odb_raw
    try:
        odb_comps, _scale = parse_odb_raw(odb_path)
    except Exception:
        odb_comps = []

    font_size = max(2.8, min(4.0, board_w_mm / 25))

    # Skip test points (TP*) in labels — they clutter the view
    for oc in odb_comps:
        if oc.ref.startswith("TP"):
            continue
        cx, cy = tx(oc.x), ty(oc.y)
        text_color = (0.1, 0.1, 0.1)
        try:
            tw = font_size * 0.32 * len(oc.ref)
            page.insert_text(
                fitz.Point(cx - tw / 2, cy + font_size * 0.35),
                oc.ref,
                fontsize=font_size,
                color=text_color,
                fontname="helv",
            )
        except Exception:
            pass

    # ── Pin-1 / cathode polarity markers (adaptive size) ─────────────────
    if mark_pin1:
        # Base marker radius scaled to board; will be overridden per-component
        base_r = max(1.8, min(4.0, board_w_mm / 22))

        for oc in odb_comps:
            if not oc.is_polar:
                continue
            p_pin = oc.polarity_pin
            if p_pin is None:
                continue
            px, py = tx(p_pin.x), ty(p_pin.y)

            # Adaptive marker size: scale to component's pin span
            span = oc.pin_span_mm * coord_to_mm  # convert to mm
            if span > 0:
                # Marker radius ≈ 30 % of component body, clamped
                marker_r = max(1.0, min(base_r * 1.5, span * 0.30 * MM_TO_PT))
            else:
                marker_r = base_r

            # For diodes: use a band/line instead of circle to indicate
            # cathode direction, but still use a circle for visibility
            is_diode = oc.comp_type in ("diode", "led")

            if is_diode:
                # Red-orange marker for cathode (distinct from green pin-1)
                diode_color = (0.85, 0.35, 0.0)
                shape.draw_circle(fitz.Point(px, py), marker_r)
                shape.finish(
                    color=diode_color,
                    fill=diode_color,
                    width=0.4,
                    fill_opacity=0.55,
                    stroke_opacity=0.9,
                )
            else:
                # Green filled circle for pin-1
                shape.draw_circle(fitz.Point(px, py), marker_r)
                shape.finish(
                    color=_PIN1_COLOR,
                    fill=_PIN1_COLOR,
                    width=0.4,
                    fill_opacity=0.5,
                    stroke_opacity=0.9,
                )

    # ── Commit all drawings ───────────────────────────────────────────────
    shape.commit()

    # Save
    doc.save(output_pdf)

    if save_png:
        png_path = output_pdf.replace(".pdf", "_preview.png")
        mat = fitz.Matrix(3.0, 3.0)  # 216 DPI
        pix = page.get_pixmap(matrix=mat)
        pix.save(png_path)

    doc.close()
    return os.path.abspath(output_pdf)


# ─────────────────────────────────────────────────────────────────────────────
# Profile parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_profile(content: str) -> List[Tuple[float, float]]:
    """Parse ``steps/pcb/profile`` and return outline polygon.

    Handles both ``OB`` (outline begin) and ``OS`` (outline segment) lines.
    """
    points: List[Tuple[float, float]] = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("OB "):
            parts = line.split()
            try:
                points.append((float(parts[1]), float(parts[2])))
            except (ValueError, IndexError):
                pass
        elif line.startswith("OS "):
            parts = line.split()
            try:
                points.append((float(parts[1]), float(parts[2])))
            except (ValueError, IndexError):
                pass
    return points


# ─────────────────────────────────────────────────────────────────────────────
# Archive reader (ZIP / TGZ / directory)
# ─────────────────────────────────────────────────────────────────────────────

class _ArchiveReader:
    def __init__(self, path: str):
        self.path = path
        self._zf: Optional[zipfile.ZipFile] = None
        self._tf = None
        self._is_dir = False
        low = path.lower()

        if os.path.isdir(path):
            self._is_dir = True
        elif low.endswith(".zip"):
            self._zf = zipfile.ZipFile(path, "r")
        elif low.endswith(".tgz") or low.endswith(".tar.gz") or low.endswith(".tar"):
            mode = "r:gz" if (low.endswith(".gz") or low.endswith(".tgz")) else "r"
            self._tf = tarfile.open(path, mode)
        else:
            raise ValueError(f"Unsupported ODB++ path: {path}")

    def read(self, rel_path: str) -> Optional[str]:
        """Read a file from the archive by its relative path."""
        # Normalise to forward slashes for matching
        rel_norm = rel_path.replace("\\", "/").lower()
        try:
            if self._zf:
                for name in self._zf.namelist():
                    name_norm = name.replace("\\", "/").lower()
                    if name_norm.endswith(rel_norm) or name_norm == rel_norm:
                        return self._zf.read(name).decode("utf-8", errors="replace")
            elif self._tf:
                for name in self._tf.getnames():
                    name_norm = name.replace("\\", "/").lower()
                    if name_norm.endswith(rel_norm) or name_norm == rel_norm:
                        f = self._tf.extractfile(self._tf.getmember(name))
                        if f is None:
                            continue
                        return f.read().decode("utf-8", errors="replace")
            elif self._is_dir:
                full = os.path.join(self.path, rel_path)
                if os.path.isfile(full):
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        return fh.read()
                # Try case-insensitive search
                parts = rel_path.replace("\\", "/").split("/")
                current = self.path
                for part in parts:
                    if os.path.isdir(current):
                        entries = os.listdir(current)
                        found = None
                        for e in entries:
                            if e.lower() == part.lower():
                                found = e
                                break
                        if found:
                            current = os.path.join(current, found)
                        else:
                            return None
                    else:
                        return None
                if os.path.isfile(current):
                    with open(current, encoding="utf-8", errors="replace") as fh:
                        return fh.read()
        except Exception:
            pass
        return None

    def list_layer_dirs(self) -> List[str]:
        """Return all layer folder names under steps/pcb/layers/."""
        dirs = set()
        prefix = "steps/pcb/layers/"
        try:
            if self._zf:
                for name in self._zf.namelist():
                    n = name.replace("\\", "/").lower()
                    idx = n.find(prefix)
                    if idx >= 0:
                        rest = n[idx + len(prefix):]
                        if "/" in rest:
                            dirs.add(rest.split("/")[0])
            elif self._tf:
                for name in self._tf.getnames():
                    n = name.replace("\\", "/").lower()
                    idx = n.find(prefix)
                    if idx >= 0:
                        rest = n[idx + len(prefix):]
                        if "/" in rest:
                            dirs.add(rest.split("/")[0])
            elif self._is_dir:
                layers_dir = os.path.join(self.path, "steps", "pcb", "layers")
                if os.path.isdir(layers_dir):
                    dirs = {d.lower() for d in os.listdir(layers_dir)
                            if os.path.isdir(os.path.join(layers_dir, d))}
        except Exception:
            pass
        return sorted(dirs)

    def __del__(self):
        if self._zf:
            self._zf.close()
        if self._tf:
            self._tf.close()






