"""
core/exporter.py
────────────────
Exports analysis results as:
  1. Structured JSON file
  2. Annotated PDF
       • Normal mode: 50%-transparent green filled shape on each detected
         polarity-indicator position.
       • Debug mode: colour-coded overlay of ALL vector shapes near polar
         components so you can visually inspect what the parser sees.
         Colour legend (drawn on the PDF):
           RED     = copper pad (filled, R-dominant)
           CYAN    = courtyard outline (G+B high, R low)
           BLUE    = fab layer (B-dominant)
           GRAY    = via / drill (neutral mid-gray)
           MAGENTA = silkscreen (black/white/magenta)
           YELLOW  = other / unknown
  3. Companion PNG preview.
"""
import json
import math
import os
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import fitz  # PyMuPDF

from core.matcher import MatchResult
from utils.geometry import BoundingBox, Point


# ─────────────────────────────────────────────────────────────────────────────
# Debug-mode layer colour classification
# ─────────────────────────────────────────────────────────────────────────────

def _classify_layer_color(color: Optional[Tuple]) -> str:
    """Return a human-readable layer name based on shape colour."""
    if not color:
        return "silkscreen"  # no colour → default black = silkscreen
    r, g, b = float(color[0]), float(color[1]), float(color[2])

    # Copper / solder mask (red-dominant)
    if r > 0.15 and r > g * 2.0 and r > b * 2.0:
        return "copper"

    # Via / drill gray (all channels similar, mid-range)
    if (abs(r - g) < 0.12 and abs(g - b) < 0.12
            and abs(r - b) < 0.12 and 0.20 < r < 0.85):
        return "via"

    # Courtyard / back-copper (cyan / teal)
    if g > 0.30 and b > 0.30 and r < 0.20:
        return "courtyard"

    # Fab layer (blue-dominant, not magenta)
    if b > 0.30 and r < 0.20 and g < 0.30:
        return "fab"

    # Near-black or near-white → silkscreen
    near_black = r < 0.15 and g < 0.15 and b < 0.15
    near_white = r > 0.85 and g > 0.85 and b > 0.85
    if near_black or near_white:
        return "silkscreen"

    # Magenta-ish (silkscreen on some themes)
    if r > 0.30 and b > 0.30 and g < 0.15:
        return "silkscreen"

    return "other"


# Overlay colours for each layer in debug mode (stroke colour, 0–1 RGB)
_DEBUG_LAYER_COLOR = {
    "copper":     (1.0, 0.15, 0.15),   # red
    "courtyard":  (0.0, 0.75, 0.75),   # cyan
    "fab":        (0.2, 0.2, 1.0),     # blue
    "via":        (0.55, 0.55, 0.55),  # gray
    "silkscreen": (0.9, 0.0, 0.9),    # magenta
    "other":      (0.9, 0.9, 0.0),    # yellow
}

_DEBUG_LAYER_FILL_OPACITY = {
    "copper":     0.18,
    "courtyard":  0.12,
    "fab":        0.15,
    "via":        0.06,
    "silkscreen": 0.30,
    "other":      0.20,
}


# ─────────────────────────────────────────────────────────────────────────────
# Exporter
# ─────────────────────────────────────────────────────────────────────────────

class Exporter:
    def export_footprints_and_polarity_pdf(
        self,
        source_pdf_path: str,
        components: list,
        shapes: list,
        polarity_markers: list,
        output_path: str,
        save_png: bool = True,
    ) -> str:
        """
        Annotated PDF export: minden komponens footprintjét (courtyard/fallback) zöld overlay-jel jelöli,
        és a footprinten belüli polaritásjelölőket is zöld, 50%-os átlátszóságú overlay-jel megjelöli.
        """
        import fitz
        import os
        from core.pad_asymmetry_detector import _build_footprint_areas
        doc = fitz.open(source_pdf_path)
        # 1. Footprint bounding boxok hozzárendelése
        footprint_map = _build_footprint_areas(components, shapes, fallback_radius=15.0)
        # 2. Polarity markerek csoportosítása oldalanként
        markers_by_page = {}
        for marker in polarity_markers:
            markers_by_page.setdefault(marker.page, []).append(marker)
        # 3. Minden komponens footprintjének kirajzolása
        GREEN = (0.20, 0.85, 0.20)
        for comp in components:
            key = f"{comp.ref}_{comp.page}"
            bbox = footprint_map.get(key)
            if bbox is None:
                continue
            page = doc[comp.page]
            fitz_rect = fitz.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)
            annot = page.add_rect_annot(fitz_rect)
            annot.set_colors(stroke=GREEN, fill=GREEN)
            annot.set_border(width=1.0)
            annot.set_opacity(0.50)
            annot.set_info(title=f"Footprint: {comp.ref}")
            annot.update()
        # 4. Polarity markerek overlay-e footprinten belül
        for comp in components:
            key = f"{comp.ref}_{comp.page}"
            bbox = footprint_map.get(key)
            if bbox is None:
                continue
            page = doc[comp.page]
            for marker in markers_by_page.get(comp.page, []):
                if bbox.contains_point(marker.center):
                    mx, my = marker.center.x, marker.center.y
                    pw, ph = marker.bbox.width, marker.bbox.height
                    pad_half_w = max(pw / 2.0, 3.0) + 2.0
                    pad_half_h = max(ph / 2.0, 3.0) + 2.0
                    mark_rect = fitz.Rect(
                        mx - pad_half_w, my - pad_half_h,
                        mx + pad_half_w, my + pad_half_h,
                    )
                    if marker.marker_type in (
                        "filled_dot", "corner_rect", "cross_vector",
                        "plus_text", "triangle", "pad_asymmetry",
                    ):
                        mark = page.add_circle_annot(mark_rect)
                    else:
                        mark = page.add_rect_annot(mark_rect)
                    mark.set_colors(stroke=GREEN, fill=GREEN)
                    mark.set_border(width=0.8)
                    mark.set_opacity(0.50)
                    mark.set_info(title="Polarity", content=f"{comp.ref}  type={marker.marker_type}  conf={marker.confidence:.0%}")
                    mark.update()
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        if save_png:
            try:
                preview_path = output_path.replace(".pdf", "_preview.png")
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, annots=True)
                pix.save(preview_path)
            except Exception:
                pass
        doc.save(output_path, garbage=4, deflate=True)
        doc.close()
        return os.path.abspath(output_path)

    def export_component_boxes_pdf(
        self,
        source_pdf_path: str,
        components: list,
        output_path: str,
        color: tuple = (1, 0, 0),  # piros
        width: float = 0.7,
        save_png: bool = True,
    ) -> str:
        """
        Draws a thin rectangle around each component's bounding box on the PDF.
        Returns the absolute path of the saved PDF.
        """
        import fitz
        import os
        doc = fitz.open(source_pdf_path)
        for comp in components:
            page = doc[comp.page]
            bbox = fitz.Rect(comp.bbox.x0, comp.bbox.y0, comp.bbox.x1, comp.bbox.y1)
            annot = page.add_rect_annot(bbox)
            annot.set_colors(stroke=color)
            annot.set_border(width=width)
            annot.set_opacity(0.7)
            annot.set_info(title=comp.ref)
            annot.update()
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        if save_png:
            try:
                preview_path = output_path.replace(".pdf", "_preview.png")
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, annots=True)
                pix.save(preview_path)
            except Exception:
                pass
        doc.save(output_path, garbage=4, deflate=True)
        doc.close()
        return os.path.abspath(output_path)

    # ── JSON export ───────────────────────────────────────────────────────

    def export_json(
        self,
        results: List[MatchResult],
        output_path: str,
        source_pdf_path: str = "",
    ) -> str:
        """Serialize analysis results to a JSON file.
        Returns the absolute path of the saved file.
        """
        now = datetime.now(timezone.utc).isoformat()
        data = {
            "generated":  now,
            "source_pdf": os.path.basename(source_pdf_path),
            "total":      len(results),
            "marked":     sum(1 for r in results if r.polarity_status == "marked"),
            "unmarked":   sum(1 for r in results if r.polarity_status == "unmarked"),
            "ambiguous":  sum(1 for r in results if r.polarity_status == "ambiguous"),
            "components": [r.to_dict() for r in results],
        }
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return os.path.abspath(output_path)

    # ── Annotated PDF export ──────────────────────────────────────────────

    def export_annotated_pdf(
        self,
        source_pdf_path: str,
        results: List[MatchResult],
        output_path: str,
        debug: bool = False,
        save_png: bool = True,
    ) -> str:
        """
        Create an annotated copy of the source PDF and optional PNG preview.
        Returns the absolute path of the saved PDF.
        """
        doc = fitz.open(source_pdf_path)

        by_page: dict = {}
        for result in results:
            by_page.setdefault(result.component.page, []).append(result)

        for page_idx in range(len(doc)):
            page         = doc[page_idx]
            page_results = by_page.get(page_idx, [])
            if not page_results:
                continue
            if debug:
                self._debug_annotate_page(page, page_results)
            else:
                self._annotate_page(page, page_results)

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        # Render PNG BEFORE saving (annotations are in-memory at this point)
        if save_png:
            try:
                preview_path = output_path.replace(".pdf", "_preview.png")
                pix = doc[0].get_pixmap(
                    matrix=fitz.Matrix(2, 2), alpha=False, annots=True
                )
                pix.save(preview_path)
            except Exception:
                pass

        doc.save(output_path, garbage=4, deflate=True)
        doc.close()
        return os.path.abspath(output_path)

    # ══════════════════════════════════════════════════════════════════════
    # NORMAL mode annotation
    # ══════════════════════════════════════════════════════════════════════

    _GREEN  = (0.20, 0.85, 0.20)   # marked
    _RED    = (0.90, 0.15, 0.15)   # unmarked polar
    _BLUE   = (0.10, 0.35, 0.90)   # individual marker location

    def _annotate_page(self, page: fitz.Page, results: List[MatchResult]) -> None:
        """Normal mode: green for marked, red for unmarked polar, blue crosshair for marker positions."""
        marked_refs: List[str] = []

        for result in results:
            comp = result.component
            if not comp.is_polar:
                continue

            if result.has_polarity:
                # ── Marked: green circle at marker position ───────────────
                best_marker = self._pick_best_marker(result)
                if best_marker is None:
                    continue

                mx, my = best_marker.center.x, best_marker.center.y
                pw, ph = best_marker.bbox.width, best_marker.bbox.height
                pad_half_w = max(pw / 2.0, 3.0) + 2.0
                pad_half_h = max(ph / 2.0, 3.0) + 2.0

                mark_rect = fitz.Rect(
                    mx - pad_half_w, my - pad_half_h,
                    mx + pad_half_w, my + pad_half_h,
                )
                if best_marker.marker_type in (
                    "filled_dot", "corner_rect", "cross_vector",
                    "plus_text", "triangle", "pad_asymmetry",
                ):
                    mark = page.add_circle_annot(mark_rect)
                else:
                    mark = page.add_rect_annot(mark_rect)
                mark.set_colors(stroke=self._GREEN, fill=self._GREEN)
                mark.set_border(width=0.8)
                mark.set_opacity(0.50)
                mark.set_info(
                    title="Polarity",
                    content=(
                        f"{comp.ref}  type={best_marker.marker_type}"
                        f"  conf={best_marker.confidence:.0%}"
                    ),
                )
                mark.update()

                # ── Blue crosshair at the exact marker centre ─────────────
                r_cross = max(pad_half_w, pad_half_h) + 1.5
                cross_rect = fitz.Rect(
                    mx - r_cross, my - r_cross, mx + r_cross, my + r_cross
                )
                cross = page.add_circle_annot(cross_rect)
                cross.set_colors(stroke=self._BLUE, fill=None)
                cross.set_border(width=0.5)
                cross.set_opacity(0.45)
                cross.set_info(
                    title=f"Marker: {best_marker.marker_type}",
                    content=f"{comp.ref}  conf={best_marker.confidence:.0%}",
                )
                cross.update()

                marked_refs.append(comp.ref)

            else:
                # ── Unmarked polar: red circle at component centre ─────────
                cx, cy = comp.center.x, comp.center.y
                r = 6.0
                red_rect = fitz.Rect(cx - r, cy - r, cx + r, cy + r)
                red = page.add_circle_annot(red_rect)
                red.set_colors(stroke=self._RED, fill=self._RED)
                red.set_border(width=1.0)
                red.set_opacity(0.40)
                red.set_info(
                    title=f"Unmarked: {comp.ref}",
                    content=f"{comp.ref} ({comp.comp_type}) — no polarity marker detected",
                )
                red.update()

        # ── White legend box (lower-right corner) ─────────────────────────
        if results:
            self._annotate_legend(page, results)

    def _annotate_legend(self, page: fitz.Page, results: List[MatchResult]) -> None:
        """Draw a compact summary legend in the lower-right corner."""
        n_polar    = sum(1 for r in results if r.component.is_polar)
        n_marked   = sum(1 for r in results if r.component.is_polar and r.has_polarity)
        n_unmarked = n_polar - n_marked

        pw, ph = page.rect.width, page.rect.height
        BOX_W, LINE_H = 145, 10
        entries = [
            ("● Marked polar components",     self._GREEN,  str(n_marked)),
            ("● Unmarked polar components",    self._RED,    str(n_unmarked)),
            ("○ Exact marker location",        self._BLUE,   ""),
        ]
        BOX_H = (len(entries) + 2) * LINE_H + 8

        X0 = pw - BOX_W - 6
        Y0 = ph - BOX_H - 6

        # Background rectangle
        bg = page.add_rect_annot(fitz.Rect(X0, Y0, X0 + BOX_W, Y0 + BOX_H))
        bg.set_colors(stroke=(0.3, 0.3, 0.3), fill=(1.0, 1.0, 1.0))
        bg.set_border(width=0.7)
        bg.set_opacity(0.90)
        bg.update()

        # Title
        y = Y0 + 3
        page.add_freetext_annot(
            fitz.Rect(X0 + 4, y, X0 + BOX_W - 4, y + LINE_H),
            f"PolarityMark  ({n_polar} polar components)",
            fontsize=5.5, text_color=(0.1, 0.1, 0.1),
            fill_color=None, border_color=None, border_width=0, opacity=0.92,
        ).update()
        y += LINE_H + 2

        for label, color, count in entries:
            suffix = f"  ({count})" if count else ""
            page.add_freetext_annot(
                fitz.Rect(X0 + 4, y, X0 + BOX_W - 4, y + LINE_H),
                label + suffix,
                fontsize=5.5, text_color=color,
                fill_color=None, border_color=None, border_width=0, opacity=0.92,
            ).update()
            y += LINE_H

    # ══════════════════════════════════════════════════════════════════════
    # DEBUG mode — visual shape inspection
    # ══════════════════════════════════════════════════════════════════════

    # Radius (pt) around each polar component to scan for shapes
    DEBUG_RADIUS = 20.0

    # Skip shapes smaller than this in both dimensions (noise filter)
    DEBUG_MIN_DIM = 0.3

    def _debug_annotate_page(
        self,
        page: fitz.Page,
        results: List[MatchResult],
    ) -> None:
        """
        Debug mode: re-read ALL vector drawings from the page and draw
        colour-coded overlays near each polar component so the user can
        see exactly what shapes exist and on which layer.

        Also labels each polar component and draws a search-radius circle.
        """
        # ── 1. Collect all drawings from the page ─────────────────────────
        drawings = page.get_drawings()

        # ── 2. Build a flat list of (bbox, layer, shape_type, details) ────
        shape_infos = []
        for d in drawings:
            rect = d.get("rect")
            if rect is None:
                continue
            r = fitz.Rect(rect)
            if r.is_empty or r.is_infinite:
                continue
            w, h = r.width, r.height
            if w < self.DEBUG_MIN_DIM and h < self.DEBUG_MIN_DIM:
                continue

            fill_c  = d.get("fill")
            stroke_c = d.get("color")
            is_filled = fill_c is not None
            sw = d.get("width") or 0

            # Classify layer from fill or stroke colour
            layer = _classify_layer_color(fill_c if is_filled else stroke_c)

            # Determine shape kind
            items = d.get("items", [])
            kinds = [item[0] for item in items]
            all_lines = all(k == "l" for k in kinds)
            all_curves = all(k == "c" for k in kinds)
            only_rect = kinds == ["re"]
            close_path = d.get("closePath", False)

            if only_rect:
                stype = "rect" + (" filled" if is_filled else "")
            elif kinds == ["l"]:
                stype = "line"
            elif all_curves and len(items) >= 4:
                stype = "circle" + (" filled" if is_filled else "")
            elif all_lines and close_path:
                n_pts = len(set((round(item[1].x,1), round(item[1].y,1)) for item in items if len(item) > 1))
                stype = f"polygon({n_pts}v)"
            elif all_lines:
                stype = f"polyline({len(items)}seg)"
            else:
                stype = "path"

            detail = (
                f"{stype}  {w:.1f}×{h:.1f}  sw={sw:.2f}\n"
                f"fill={_fmt_color(fill_c)}  stroke={_fmt_color(stroke_c)}\n"
                f"layer={layer}"
            )

            shape_infos.append((r, layer, stype, detail, is_filled))

        # ── 3. For each polar component draw debug overlays ───────────────
        shape_writer = page.new_shape()   # batch direct-drawing handle

        for result in results:
            comp = result.component
            if not comp.is_polar:
                continue

            cx, cy = comp.center.x, comp.center.y

            # Draw search-radius circle (dashed)
            shape_writer.draw_circle(
                fitz.Point(cx, cy), self.DEBUG_RADIUS
            )
            shape_writer.finish(
                color=(0.3, 0.3, 0.3), width=0.4,
                dashes="[2 2]", fill_opacity=0,
            )

            # Component label
            label_pt = fitz.Point(cx + 1, cy - 2)
            shape_writer.insert_text(
                label_pt,
                f"{comp.ref}",
                fontsize=5,
                color=(0.0, 0.0, 0.0),
            )

            # Draw every nearby shape
            for (r, layer, stype, detail, is_filled) in shape_infos:
                scx, scy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                dist = math.sqrt((cx - scx) ** 2 + (cy - scy) ** 2)
                if dist > self.DEBUG_RADIUS:
                    continue

                lc = _DEBUG_LAYER_COLOR.get(layer, (0.5, 0.5, 0.0))
                fo = _DEBUG_LAYER_FILL_OPACITY.get(layer, 0.15)

                # Draw filled overlay rectangle
                shape_writer.draw_rect(r)
                shape_writer.finish(
                    color=lc, width=0.4,
                    fill=lc, fill_opacity=fo,
                )

        shape_writer.commit()  # flush all direct drawings to the page

        # ── 4. Add hover annotations for each shape near components ───────
        # (more expensive but allows tooltip inspection on hover)
        for result in results:
            comp = result.component
            if not comp.is_polar:
                continue
            cx, cy = comp.center.x, comp.center.y

            for (r, layer, stype, detail, is_filled) in shape_infos:
                scx, scy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                dist = math.sqrt((cx - scx) ** 2 + (cy - scy) ** 2)
                if dist > self.DEBUG_RADIUS:
                    continue

                # Skip tiny shapes for annotations (too many = slow PDF)
                if r.width < 0.8 and r.height < 0.8:
                    continue

                lc = _DEBUG_LAYER_COLOR.get(layer, (0.5, 0.5, 0.0))
                ann = page.add_rect_annot(r)
                ann.set_colors(stroke=lc)
                ann.set_border(width=0.3)
                ann.set_opacity(0.60)
                ann.set_info(title=f"{comp.ref} | {layer}", content=detail)
                ann.update()

        # ── 5. Legend ─────────────────────────────────────────────────────
        self._debug_legend(page)

    def _debug_legend(self, page: fitz.Page) -> None:
        """Draw a colour legend for the debug overlays."""
        X0, Y0 = 8, 8
        BOX_W, LINE_H = 120, 11
        entries = [
            ("COPPER (pad)",     _DEBUG_LAYER_COLOR["copper"]),
            ("COURTYARD",        _DEBUG_LAYER_COLOR["courtyard"]),
            ("FAB",              _DEBUG_LAYER_COLOR["fab"]),
            ("VIA / DRILL",      _DEBUG_LAYER_COLOR["via"]),
            ("SILKSCREEN",       _DEBUG_LAYER_COLOR["silkscreen"]),
            ("OTHER",            _DEBUG_LAYER_COLOR["other"]),
        ]
        BOX_H = (len(entries) + 1) * LINE_H + 6

        bg = page.add_rect_annot(fitz.Rect(X0, Y0, X0 + BOX_W, Y0 + BOX_H))
        bg.set_colors(stroke=(0.2, 0.2, 0.2), fill=(1, 1, 1))
        bg.set_border(width=0.6)
        bg.set_opacity(0.92)
        bg.update()

        y = Y0 + 4
        page.add_freetext_annot(
            fitz.Rect(X0 + 4, y, X0 + BOX_W - 4, y + LINE_H),
            "DEBUG — Layer colours",
            fontsize=6, text_color=(0.1, 0.1, 0.1),
            fill_color=None, border_color=None, border_width=0, opacity=0.92,
        ).update()
        y += LINE_H

        for label, color in entries:
            page.add_freetext_annot(
                fitz.Rect(X0 + 4, y, X0 + BOX_W - 4, y + LINE_H),
                f"■ {label}",
                fontsize=6, text_color=color,
                fill_color=None, border_color=None, border_width=0, opacity=0.92,
            ).update()
            y += LINE_H

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _pick_best_marker(result: MatchResult):
        if not result.markers:
            return None
        def _key(m):
            real = max(m.bbox.width, m.bbox.height) > 0.5
            return (int(real), m.confidence)
        return max(result.markers, key=_key)

    @staticmethod
    def _to_fitz_rect(bbox: BoundingBox) -> fitz.Rect:
        return fitz.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)


def _fmt_color(c) -> str:
    """Format a colour tuple for debug display."""
    if c is None:
        return "None"
    return f"({c[0]:.2f},{c[1]:.2f},{c[2]:.2f})"
