"""
core/exporter.py
────────────────
Exports analysis results as:
  1. Structured JSON file
  2. Annotated PDF (normal or debug mode)
  3. Companion PNG preview.

Also provides load_json_results() to restore a previous session.
"""
import json
import math
import os
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import fitz  # PyMuPDF

from core.matcher import MatchResult
from core.component_detector import Component
from core.polarity_detector import PolarityMarker
from utils.geometry import BoundingBox, Point


# ─────────────────────────────────────────────────────────────────────────────
# Debug-mode layer colour classification
# ─────────────────────────────────────────────────────────────────────────────

def _classify_layer_color(color: Optional[Tuple]) -> str:
    if not color:
        return "silkscreen"
    r, g, b = float(color[0]), float(color[1]), float(color[2])
    if r > 0.15 and r > g * 2.0 and r > b * 2.0:
        return "copper"
    if abs(r - g) < 0.12 and abs(g - b) < 0.12 and abs(r - b) < 0.12 and 0.20 < r < 0.85:
        return "via"
    if g > 0.30 and b > 0.30 and r < 0.20:
        return "courtyard"
    if b > 0.30 and r < 0.20 and g < 0.30:
        return "fab"
    if (r < 0.15 and g < 0.15 and b < 0.15) or (r > 0.85 and g > 0.85 and b > 0.85):
        return "silkscreen"
    if r > 0.30 and b > 0.30 and g < 0.15:
        return "silkscreen"
    return "other"


_DEBUG_LAYER_COLOR = {
    "copper":     (1.0,  0.15, 0.15),
    "courtyard":  (0.0,  0.75, 0.75),
    "fab":        (0.2,  0.2,  1.0),
    "via":        (0.55, 0.55, 0.55),
    "silkscreen": (0.9,  0.0,  0.9),
    "other":      (0.9,  0.9,  0.0),
}

_DEBUG_LAYER_FILL_OPACITY = {
    "copper": 0.18, "courtyard": 0.12, "fab": 0.15,
    "via": 0.06,    "silkscreen": 0.30, "other": 0.20,
}


def _fmt_color(c) -> str:
    if c is None:
        return "None"
    return f"({c[0]:.2f},{c[1]:.2f},{c[2]:.2f})"


# ─────────────────────────────────────────────────────────────────────────────
# Exporter
# ─────────────────────────────────────────────────────────────────────────────

class Exporter:
    """Handles JSON export, annotated-PDF export, and PNG companion preview."""

    _GREEN = (0.20, 0.85, 0.20)
    _RED   = (0.90, 0.15, 0.15)
    _BLUE  = (0.10, 0.35, 0.90)

    # ── JSON round-trip ───────────────────────────────────────────────────

    def export_json(
        self,
        results: List[MatchResult],
        output_path: str,
        source_path: str = "",
    ) -> str:
        """Write analysis results to a JSON file. Returns the written path."""
        marked    = sum(1 for r in results if r.polarity_status == "marked")
        unmarked  = sum(1 for r in results if r.polarity_status == "unmarked")
        ambiguous = sum(1 for r in results if r.polarity_status == "ambiguous")

        payload = {
            "tool":        "PolarityMark",
            "version":     "1.0.0",
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "source_file": os.path.basename(source_path),
            "summary": {
                "total_components": len(results),
                "marked":    marked,
                "unmarked":  unmarked,
                "ambiguous": ambiguous,
            },
            "components": [r.to_dict() for r in results],
        }

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        return os.path.abspath(output_path)

    def load_json_results(self, json_path: str) -> List[MatchResult]:
        """
        Load a previously exported JSON file and reconstruct MatchResult objects.
        This allows the GUI to restore the Results table without re-running analysis.
        """
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)

        if isinstance(data, dict):
            items = data.get("components", [])
        elif isinstance(data, list):
            items = data
        else:
            return []

        results: List[MatchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            comp    = self._component_from_json(item)
            markers = self._markers_from_json(item.get("markers", []), comp.page)
            status  = str(item.get("polarity_status", "unmarked"))
            conf    = float(item.get("confidence") or 0.0)
            results.append(MatchResult(
                component=comp,
                markers=markers,
                polarity_status=status,
                overall_confidence=conf,
            ))
        return results

    # ── JSON → object helpers ─────────────────────────────────────────────

    @staticmethod
    def _bbox_from_json(raw, fallback_center: Point, half: float = 3.0) -> BoundingBox:
        if isinstance(raw, dict):
            try:
                x0, y0 = float(raw["x0"]), float(raw["y0"])
                x1, y1 = float(raw["x1"]), float(raw["y1"])
                if x1 > x0 and y1 > y0:
                    return BoundingBox(x0, y0, x1, y1)
            except Exception:
                pass
        return BoundingBox(
            fallback_center.x - half, fallback_center.y - half,
            fallback_center.x + half, fallback_center.y + half,
        )

    def _component_from_json(self, item: dict) -> Component:
        pos    = item.get("position") or {}
        cx     = float(pos.get("x") or 0.0)
        cy     = float(pos.get("y") or 0.0)
        center = Point(cx, cy)
        bbox   = self._bbox_from_json(item.get("bbox"), center, half=3.0)
        # JSON stores 1-based page; Component uses 0-based
        page   = max(0, int(item.get("page") or 1) - 1)
        return Component(
            ref=str(item.get("reference") or "?"),
            comp_type=str(item.get("type") or "unknown"),
            bbox=bbox,
            center=center,
            page=page,
        )

    def _markers_from_json(self, raw: list, page: int) -> List[PolarityMarker]:
        result: List[PolarityMarker] = []
        for m in (raw or []):
            if not isinstance(m, dict):
                continue
            pos    = m.get("position") or {}
            mx, my = float(pos.get("x") or 0.0), float(pos.get("y") or 0.0)
            center = Point(mx, my)
            bbox   = self._bbox_from_json(m.get("bbox"), center, half=2.0)
            result.append(PolarityMarker(
                marker_type=str(m.get("type") or "unknown"),
                bbox=bbox,
                center=center,
                page=page,
                confidence=float(m.get("confidence") or 0.0),
                source=str(m.get("source") or "json"),
            ))
        return result

    # ── Annotated PDF export ──────────────────────────────────────────────

    def export_annotated_pdf(
        self,
        source_pdf_path: str,
        results: List[MatchResult],
        output_path: str,
        debug: bool = False,
        save_png: bool = True,
    ) -> str:
        """Create an annotated copy of the source PDF. Returns absolute path."""
        doc = fitz.open(source_pdf_path)

        by_page: dict = {}
        for r in results:
            by_page.setdefault(r.component.page, []).append(r)

        for page_idx in range(len(doc)):
            page_results = by_page.get(page_idx, [])
            if not page_results:
                continue
            pg = doc[page_idx]
            if debug:
                self._debug_annotate_page(pg, page_results)
            else:
                self._annotate_page(pg, page_results)

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        if save_png:
            try:
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2),
                                        alpha=False, annots=True)
                pix.save(output_path.replace(".pdf", "_preview.png"))
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
        color: tuple = (1, 0, 0),
        width: float = 0.7,
        save_png: bool = True,
    ) -> str:
        """Draw thin rectangles around each component bbox. Returns absolute path."""
        doc = fitz.open(source_pdf_path)
        for comp in components:
            pg   = doc[comp.page]
            bbox = fitz.Rect(comp.bbox.x0, comp.bbox.y0, comp.bbox.x1, comp.bbox.y1)
            ann  = pg.add_rect_annot(bbox)
            ann.set_colors(stroke=color)
            ann.set_border(width=width)
            ann.set_opacity(0.7)
            ann.set_info(title=comp.ref)
            ann.update()

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        if save_png:
            try:
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2),
                                        alpha=False, annots=True)
                pix.save(output_path.replace(".pdf", "_preview.png"))
            except Exception:
                pass
        doc.save(output_path, garbage=4, deflate=True)
        doc.close()
        return os.path.abspath(output_path)

    # ══════════════════════════════════════════════════════════════════════
    # NORMAL mode annotation
    # ══════════════════════════════════════════════════════════════════════

    def _annotate_page(self, page: fitz.Page, results: List[MatchResult]) -> None:
        """Normal mode: green mark at detected polarity position."""
        for result in results:
            comp = result.component
            if not comp.is_polar or not result.has_polarity:
                continue

            best = self._pick_best_marker(result)
            if best is None:
                continue

            mx, my = best.center.x, best.center.y
            hw = max(best.bbox.width  / 2.0, 3.0) + 2.0
            hh = max(best.bbox.height / 2.0, 3.0) + 2.0

            rect = fitz.Rect(mx - hw, my - hh, mx + hw, my + hh)
            if best.marker_type in (
                "filled_dot", "corner_rect", "cross_vector",
                "plus_text", "triangle", "pad_asymmetry",
            ):
                ann = page.add_circle_annot(rect)
            else:
                ann = page.add_rect_annot(rect)

            ann.set_colors(stroke=self._GREEN, fill=self._GREEN)
            ann.set_border(width=0.8)
            ann.set_opacity(0.50)
            ann.set_info(
                title="Polarity",
                content=f"{comp.ref}  type={best.marker_type}  conf={best.confidence:.0%}",
            )
            ann.update()

    # ══════════════════════════════════════════════════════════════════════
    # DEBUG mode
    # ══════════════════════════════════════════════════════════════════════

    DEBUG_RADIUS  = 20.0
    DEBUG_MIN_DIM = 0.3

    def _debug_annotate_page(
        self, page: fitz.Page, results: List[MatchResult]
    ) -> None:
        drawings = page.get_drawings()
        shape_infos = []
        for d in drawings:
            rect = d.get("rect")
            if rect is None:
                continue
            r = fitz.Rect(rect)
            if r.is_empty or r.is_infinite:
                continue
            if r.width < self.DEBUG_MIN_DIM and r.height < self.DEBUG_MIN_DIM:
                continue
            fill_c    = d.get("fill")
            stroke_c  = d.get("color")
            is_filled = fill_c is not None
            layer     = _classify_layer_color(fill_c if is_filled else stroke_c)
            items     = d.get("items", [])
            kinds     = [it[0] for it in items]
            if kinds == ["re"]:
                stype = "rect" + (" filled" if is_filled else "")
            elif kinds == ["l"]:
                stype = "line"
            elif all(k == "c" for k in kinds) and len(items) >= 4:
                stype = "circle" + (" filled" if is_filled else "")
            elif all(k == "l" for k in kinds) and d.get("closePath"):
                stype = f"polygon({len(items)}v)"
            else:
                stype = "path"
            detail = (
                f"{stype}  {r.width:.1f}×{r.height:.1f}  "
                f"sw={d.get('width') or 0:.2f}\n"
                f"fill={_fmt_color(fill_c)}  stroke={_fmt_color(stroke_c)}\n"
                f"layer={layer}"
            )
            shape_infos.append((r, layer, stype, detail, is_filled))

        shape_writer = page.new_shape()
        for result in results:
            comp = result.component
            if not comp.is_polar:
                continue
            cx, cy = comp.center.x, comp.center.y
            shape_writer.draw_circle(fitz.Point(cx, cy), self.DEBUG_RADIUS)
            shape_writer.finish(color=(0.3, 0.3, 0.3), width=0.4, dashes="[2 2]")
            for (r, layer, stype, detail, is_filled) in shape_infos:
                scx = (r.x0 + r.x1) / 2
                scy = (r.y0 + r.y1) / 2
                if math.hypot(cx - scx, cy - scy) > self.DEBUG_RADIUS:
                    continue
                lc = _DEBUG_LAYER_COLOR.get(layer, (0.5, 0.5, 0.0))
                fo = _DEBUG_LAYER_FILL_OPACITY.get(layer, 0.15)
                shape_writer.draw_rect(r)
                shape_writer.finish(color=lc, width=0.4, fill=lc, fill_opacity=fo)
        shape_writer.commit()

        for result in results:
            comp = result.component
            if not comp.is_polar:
                continue
            cx, cy = comp.center.x, comp.center.y
            for (r, layer, stype, detail, is_filled) in shape_infos:
                if r.width < 0.8 and r.height < 0.8:
                    continue
                scx = (r.x0 + r.x1) / 2
                scy = (r.y0 + r.y1) / 2
                if math.hypot(cx - scx, cy - scy) > self.DEBUG_RADIUS:
                    continue
                lc  = _DEBUG_LAYER_COLOR.get(layer, (0.5, 0.5, 0.0))
                ann = page.add_rect_annot(r)
                ann.set_colors(stroke=lc)
                ann.set_border(width=0.3)
                ann.set_opacity(0.60)
                ann.set_info(title=f"{comp.ref} | {layer}", content=detail)
                ann.update()

        self._debug_legend(page)

    def _debug_legend(self, page: fitz.Page) -> None:
        X0, Y0  = 8, 8
        BOX_W   = 120
        LINE_H  = 11
        entries = [
            ("COPPER (pad)",  _DEBUG_LAYER_COLOR["copper"]),
            ("COURTYARD",     _DEBUG_LAYER_COLOR["courtyard"]),
            ("FAB",           _DEBUG_LAYER_COLOR["fab"]),
            ("VIA / DRILL",   _DEBUG_LAYER_COLOR["via"]),
            ("SILKSCREEN",    _DEBUG_LAYER_COLOR["silkscreen"]),
            ("OTHER",         _DEBUG_LAYER_COLOR["other"]),
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
    def _pick_best_marker(result: MatchResult) -> Optional[PolarityMarker]:
        if not result.markers:
            return None
        def _key(m):
            real = max(m.bbox.width, m.bbox.height) > 0.5
            return (int(real), m.confidence)
        return max(result.markers, key=_key)

    @staticmethod
    def _to_fitz_rect(bbox: BoundingBox) -> fitz.Rect:
        return fitz.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)

