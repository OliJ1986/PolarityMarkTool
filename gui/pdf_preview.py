"""
gui/pdf_preview.py
──────────────────
Embedded PDF viewer widget for PolarityMark.

Features
--------
• Renders the PDF page pixel-perfectly via PyMuPDF.
  The rendered PDF already contains the green polarity markers —
  no extra drawing needed to "see" the results.

• Left-drag         → pan (scroll) the view
• Ctrl + left-drag  → rubber-band area selection (select all components inside the rect)
• Short click       → select component; emits component_clicked
• Ctrl + click      → toggle component in selection
• Double-click      → emits component_edit_requested
• Hover             → PointingHandCursor over a component,
                       OpenHandCursor elsewhere (when page is larger than viewport)
• Ctrl held         → CrossCursor (selection mode)
• Dragging          → ClosedHandCursor
• Ctrl + Wheel      → zoom in / out, anchored to the cursor position
• Wheel             → scroll vertically
• Shift + Wheel     → scroll horizontally
• "Fit" button      → fit page width to panel
• "Jelölők" checkbox → optional coloured-dot overlay on top of the PDF:
    green  = marked
    red    = unmarked
    amber  = ambiguous
    blue   = corrected (manual)
  When unchecked the view shows the PDF without any Python-drawn additions.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Set, Tuple

import fitz  # PyMuPDF
from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QMouseEvent,
    QPainter, QPen, QPixmap, QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QPushButton,
    QRubberBand, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)


# ── Overlay colours ───────────────────────────────────────────────────────────

_DOT_COLOR: Dict[str, QColor] = {
    "marked":    QColor( 30, 210,  50),
    "unmarked":  QColor(220,  50,  50),
    "ambiguous": QColor(220, 170,   0),
    "corrected": QColor( 60, 130, 255),
}
_SEL_COLOR   = QColor(255, 220,   0)   # yellow selection ring
_DOT_R_BASE  = 7.0                     # dot radius (px) at zoom = 1.0
_DRAG_THRESH = 5                       # Manhattan pixels before drag activates


# ─────────────────────────────────────────────────────────────────────────────
# Internal canvas  (the QLabel that owns the rendered pixmap)
# ─────────────────────────────────────────────────────────────────────────────

class _Canvas(QLabel):
    """
    Handles all mouse interaction and forwards results to PDFPreviewWidget.

    Cursor states
    -------------
    CrossCursor        – Ctrl held: rubber-band selection mode
    OpenHandCursor     – hovering, panning is possible
    PointingHandCursor – hovering over a component hit area
    ClosedHandCursor   – panning (left-drag without Ctrl)
    ArrowCursor        – page fits entirely in viewport

    Drag modes
    ----------
    Plain left-drag  → pan (scroll)
    Ctrl + left-drag → rubber-band area selection
    Ctrl + click     → toggle single component
    Plain click      → select single component
    Click empty area → clear selection
    """

    def __init__(self, preview: "PDFPreviewWidget") -> None:
        super().__init__()
        self._pv = preview
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)   # receive keyPress/Release events

        # Drag state
        self._drag_start:        Optional[QPoint] = None
        self._drag_scroll_start: Tuple[int, int]  = (0, 0)
        self._is_dragging:       bool             = False
        self._ctrl_at_press:     bool             = False   # Ctrl held at mousePress

        # Rubber-band widget (hidden until Ctrl+drag activates)
        self._rubber_band = QRubberBand(QRubberBand.Rectangle, self)

    # ── helpers ───────────────────────────────────────────────────────────
    def _hbar(self):
        return self._pv._scroll.horizontalScrollBar()

    def _vbar(self):
        return self._pv._scroll.verticalScrollBar()

    def _ctrl(self) -> bool:
        return bool(QApplication.keyboardModifiers() & Qt.ControlModifier)

    def _cursor_for_pos(self, pos: QPoint) -> Qt.CursorShape:
        """Return the appropriate hover cursor for canvas position *pos*."""
        if self._ctrl():
            return Qt.CrossCursor          # selection mode
        if self._pv._hit(pos.x(), pos.y()):
            return Qt.PointingHandCursor
        can_pan = self._hbar().maximum() > 0 or self._vbar().maximum() > 0
        return Qt.OpenHandCursor if can_pan else Qt.ArrowCursor

    # ── mouse press ───────────────────────────────────────────────────────
    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.LeftButton:
            self._ctrl_at_press = self._ctrl()
            self._drag_start    = ev.pos()
            self._is_dragging   = False
            if self._ctrl_at_press:
                # Rubber-band mode: initialise but don't show yet
                self._rubber_band.setGeometry(QRect(ev.pos(), QSize()))
                self.setCursor(Qt.CrossCursor)
            else:
                self._drag_scroll_start = (self._hbar().value(), self._vbar().value())
                self.setCursor(Qt.ClosedHandCursor)

    # ── mouse move ────────────────────────────────────────────────────────
    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if (ev.buttons() & Qt.LeftButton) and self._drag_start is not None:
            delta = ev.pos() - self._drag_start
            if not self._is_dragging and delta.manhattanLength() > _DRAG_THRESH:
                self._is_dragging = True

            if self._is_dragging:
                if self._ctrl_at_press:
                    # ── Rubber-band selection ──────────────────────────────
                    self._rubber_band.setGeometry(
                        QRect(self._drag_start, ev.pos()).normalized()
                    )
                    self._rubber_band.show()
                    self.setCursor(Qt.CrossCursor)
                else:
                    # ── Pan ───────────────────────────────────────────────
                    self._hbar().setValue(self._drag_scroll_start[0] - delta.x())
                    self._vbar().setValue(self._drag_scroll_start[1] - delta.y())
                    self.setCursor(Qt.ClosedHandCursor)
                return

        # Hover cursor (no button held)
        self.setCursor(self._cursor_for_pos(ev.pos()))

    # ── mouse release ─────────────────────────────────────────────────────
    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.LeftButton:
            was_dragging    = self._is_dragging
            ctrl_at_press   = self._ctrl_at_press
            drag_start      = self._drag_start

            self._drag_start    = None
            self._is_dragging   = False
            self._ctrl_at_press = False
            self._rubber_band.hide()

            if was_dragging and ctrl_at_press:
                # ── Rubber-band released → area selection ─────────────────
                rect = QRect(drag_start, ev.pos()).normalized()
                self._select_in_rect(rect)

            elif not was_dragging:
                # ── Short click ───────────────────────────────────────────
                ref  = self._pv._hit(ev.pos().x(), ev.pos().y())
                ctrl = self._ctrl()   # check Ctrl at release time

                if ref:
                    if ctrl:
                        # Ctrl+click: toggle this component
                        if ref in self._pv._selected_refs:
                            self._pv._selected_refs.discard(ref)
                        else:
                            self._pv._selected_refs.add(ref)
                    else:
                        # Plain click: select only this component
                        self._pv._selected_refs = {ref}
                    self._pv._repaint()
                    self._pv.component_clicked.emit(ref)
                    self._pv.selection_changed.emit(
                        sorted(self._pv._selected_refs)
                    )
                elif not ctrl:
                    # Click on empty space → clear selection
                    if self._pv._selected_refs:
                        self._pv._selected_refs.clear()
                        self._pv._repaint()
                        self._pv.selection_changed.emit([])

            # Restore hover cursor
            self.setCursor(self._cursor_for_pos(ev.pos()))

    def _select_in_rect(self, rect: QRect) -> None:
        """
        Add all components whose canvas position falls inside *rect*
        to the current selection, then emit selection_changed.
        If the rect is tiny (misfire), do nothing.
        """
        if rect.width() < 4 and rect.height() < 4:
            return
        scale = self._pv._pix_scale
        found: Set[str] = set()
        for ref, (pdf_x, pdf_y) in self._pv._comp_pos.items():
            if rect.contains(int(pdf_x * scale), int(pdf_y * scale)):
                found.add(ref)
        if found:
            self._pv._selected_refs.update(found)
            self._pv._repaint()
            self._pv.selection_changed.emit(sorted(self._pv._selected_refs))

    # ── double-click ──────────────────────────────────────────────────────
    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.LeftButton:
            ref = self._pv._hit(ev.pos().x(), ev.pos().y())
            if ref:
                # Double-click always selects that single component and opens edit
                self._pv._selected_refs = {ref}
                self._pv._repaint()
                self._pv.selection_changed.emit([ref])
                self._pv.component_edit_requested.emit(ref)

    # ── key events (update cursor immediately when Ctrl is pressed/released) ─
    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key_Control and not ev.isAutoRepeat():
            self.setCursor(Qt.CrossCursor)
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev) -> None:
        if ev.key() == Qt.Key_Control and not ev.isAutoRepeat():
            from PySide6.QtGui import QCursor
            canvas_pos = self.mapFromGlobal(QCursor.pos())
            self.setCursor(self._cursor_for_pos(canvas_pos))
        super().keyReleaseEvent(ev)

    # ── wheel – forward to parent for unified handling ─────────────────────
    def wheelEvent(self, ev: QWheelEvent) -> None:
        self._pv.wheelEvent(ev)

    # ── right-click context menu ───────────────────────────────────────────
    def contextMenuEvent(self, ev) -> None:
        self._pv.preview_context_menu_requested.emit(ev.globalPos())


# ─────────────────────────────────────────────────────────────────────────────
# Public widget
# ─────────────────────────────────────────────────────────────────────────────

class PDFPreviewWidget(QWidget):
    """
    Embedded PDF viewer — renders the PDF identically to the saved file.

    Public API
    ----------
    load(pdf_path, comp_positions)      – open PDF + set component positions
    refresh(results, corrections, ...)  – update overlay data, repaint
    select(ref)                         – highlight a component
    clear()                             – reset to placeholder state
    """

    component_clicked               = Signal(str)    # short click on a component
    component_edit_requested        = Signal(str)    # double-click on a component
    selection_changed               = Signal(list)   # emits list[str] of selected refs
    preview_context_menu_requested  = Signal(object) # right-click → global QPoint

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._doc:          Optional[fitz.Document]         = None
        self._zoom:         float                           = 1.0
        self._fit_zoom:     float                           = 1.0
        self._pix_scale:    float                           = 1.0   # pixels / PDF pt

        # Data for overlay / hit-testing
        self._comp_pos:     Dict[str, Tuple[float, float]]  = {}    # ref → (pdf_x, pdf_y)
        self._results:      list                            = []
        self._corrections:  dict                            = {}
        self._dnp_refs:     Set[str]                        = set()
        self._selected_refs: Set[str]                      = set()
        self._show_overlay: bool                            = False

        # Cached base pixmap (invalidated on zoom change or PDF load)
        self._base_pix: Optional[QPixmap] = None

        self._build_ui()

    # ═════════════════════════════════════════════════════════════════════
    # UI construction
    # ═════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Toolbar ──────────────────────────────────────────────────────
        tb = QHBoxLayout()
        tb.setContentsMargins(4, 3, 4, 3)
        tb.setSpacing(4)

        self._btn_out  = QPushButton("−")
        self._btn_in   = QPushButton("+")
        self._btn_fit  = QPushButton("⊡ Fit")
        self._zoom_lbl = QLabel("100%")

        for btn, w in [(self._btn_out, 26), (self._btn_in, 26), (self._btn_fit, 58)]:
            btn.setFixedSize(w, 22)
        self._zoom_lbl.setFixedWidth(44)
        self._zoom_lbl.setAlignment(Qt.AlignCenter)

        self._btn_in.clicked.connect(lambda: self._set_zoom(self._zoom * 1.25))
        self._btn_out.clicked.connect(lambda: self._set_zoom(self._zoom / 1.25))
        self._btn_fit.clicked.connect(self._fit_to_width)
        self._btn_fit.setToolTip("Igazítás a szélességhez  (Ctrl+0)")

        self._overlay_cb = QCheckBox("Jelölők")
        self._overlay_cb.setChecked(False)
        self._overlay_cb.setToolTip(
            "Színes státusz-pontok az alkatrészeken\n"
            "  zöld  = jelölt\n"
            "  piros = jelöletlen\n"
            "  kék   = manuálisan javított\n"
            "Ha ki van kapcsolva, csak a PDF látszik."
        )
        self._overlay_cb.toggled.connect(self._on_overlay_toggled)

        self._hint_lbl = QLabel(
            "🖐 Húzás = görgetés  │  Ctrl+Húzás = területkijelölés  │"
            "  Katt = kijelölés  │  Ctrl+Katt = toggle  │  Duplakatt = szerkesztés"
            "  │  Ctrl+Scroll = zoom"
        )
        self._hint_lbl.setStyleSheet("color: #777; font-size: 8px;")

        tb.addWidget(self._btn_out)
        tb.addWidget(self._btn_in)
        tb.addWidget(self._zoom_lbl)
        tb.addWidget(self._btn_fit)
        tb.addSpacing(10)
        tb.addWidget(self._overlay_cb)
        tb.addSpacing(10)
        tb.addWidget(self._hint_lbl)
        tb.addStretch()
        root.addLayout(tb)

        # ── Scroll area ───────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignCenter)
        self._scroll.setStyleSheet("QScrollArea { background: #3a3a3a; border: none; }")
        # Scrollbars are hidden; panning is driven by the hand-drag gesture
        # and by the wheel event handler.
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._canvas = _Canvas(self)
        self._canvas.setAlignment(Qt.AlignCenter)
        self._canvas.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._scroll.setWidget(self._canvas)

        # ── Placeholder shown before any PDF is loaded ────────────────────
        self._placeholder = QLabel(
            "A PDF előnézet itt jelenik meg az elemzés után.\n\n"
            "🖐 Bal egérgombbal húzhatod a képet.\n"
            "Ctrl+Scroll = zoom  │  Duplakatt = javítás"
        )
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(
            "color: #888; font-style: italic; font-size: 11px;"
        )

        root.addWidget(self._placeholder, 1)
        root.addWidget(self._scroll, 1)
        self._scroll.hide()

    # ═════════════════════════════════════════════════════════════════════
    # Public API
    # ═════════════════════════════════════════════════════════════════════

    def load(
        self,
        pdf_path: str,
        comp_positions: Dict[str, Tuple[float, float]],
    ) -> None:
        """
        Open *pdf_path* with PyMuPDF and store *comp_positions* for
        hit-testing and overlay rendering.

        Parameters
        ----------
        pdf_path       : path to the rendered polarity PDF
        comp_positions : {ref: (pdf_x, pdf_y)} in PDF-point coordinates
        """
        if self._doc:
            self._doc.close()
            self._doc = None
        self._base_pix = None

        try:
            self._doc = fitz.open(pdf_path)
        except Exception as exc:
            self._placeholder.setText(f"⚠️  PDF betöltési hiba:\n{exc}")
            self._placeholder.show()
            self._scroll.hide()
            return

        self._comp_pos = comp_positions or {}
        self._placeholder.hide()
        self._scroll.show()
        self._fit_to_width()

    def refresh(
        self,
        results: list,
        corrections: dict,
        selected_ref: str = "",
        selected_refs: Optional[List[str]] = None,
    ) -> None:
        """Update overlay data and repaint (does NOT reload the PDF).

        selected_refs (list)  takes priority over selected_ref (str).
        Pass neither to leave the current selection unchanged.
        """
        self._results     = results
        self._corrections = corrections
        if selected_refs is not None:
            self._selected_refs = set(selected_refs)
        elif selected_ref:
            self._selected_refs = {selected_ref}
        self._repaint()

    def select(self, ref: str) -> None:
        """Select a single component (replaces current selection)."""
        self._selected_refs = {ref} if ref else set()
        self._repaint()

    def select_multi(self, refs: List[str]) -> None:
        """Set the selection to the given list of refs."""
        self._selected_refs = set(refs)
        self._repaint()

    def get_selected_refs(self) -> List[str]:
        """Return the currently selected refs as a sorted list."""
        return sorted(self._selected_refs)

    def set_dnp_refs(self, refs: set) -> None:
        """Set the DNP (Do-Not-Place / nem beültetett) ref set and repaint."""
        self._dnp_refs = {r.strip().upper() for r in refs} if refs else set()
        self._repaint()

    def release(self) -> None:
        """
        Close the PDF file handle without resetting any view state.
        Call this before overwriting the PDF file on disk (e.g. re-render),
        so Windows doesn't raise "Permission denied" on the locked file.
        After the new file is written, call load() to re-open it.
        """
        if self._doc:
            self._doc.close()
            self._doc = None
        self._base_pix = None

    def clear(self) -> None:
        """Reset to the 'no PDF loaded' placeholder state."""
        if self._doc:
            self._doc.close()
            self._doc = None
        self._base_pix      = None
        self._comp_pos      = {}
        self._results       = []
        self._corrections   = {}
        self._dnp_refs      = set()
        self._selected_refs = set()
        self._scroll.hide()
        self._placeholder.setText(
            "A PDF előnézet itt jelenik meg az elemzés után.\n\n"
            "🖐 Bal egérgombbal húzhatod a képet.\n"
            "Ctrl+Scroll = zoom  │  Duplakatt = javítás"
        )
        self._placeholder.show()

    # ═════════════════════════════════════════════════════════════════════
    # Rendering
    # ═════════════════════════════════════════════════════════════════════

    def _render_base(self) -> None:
        """Rasterise PDF page 0 at the current zoom level → self._base_pix."""
        if not self._doc:
            return
        page = self._doc[0]
        dpi  = 96.0 * self._zoom
        mat  = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix  = page.get_pixmap(matrix=mat, alpha=False)
        img  = QImage(
            bytes(pix.samples), pix.width, pix.height,
            pix.stride, QImage.Format_RGB888,
        )
        self._base_pix  = QPixmap.fromImage(img)
        self._pix_scale = dpi / 72.0

    def _repaint(self) -> None:
        """
        Compose base PDF pixmap + optional overlay, then push to the canvas.

        Fast path: when neither a selection ring nor the dot overlay is
        needed, the cached base pixmap is used directly without allocating
        a new QPixmap.
        """
        if not self._doc:
            return
        if self._base_pix is None:
            self._render_base()
        if self._base_pix is None:
            return

        needs_overlay = (
            bool(self._selected_refs)
            or self._show_overlay
            or bool(self._corrections)   # always draw correction badges
            or bool(self._dnp_refs)      # always draw DNP markers
        )
        if not needs_overlay:
            self._canvas.setPixmap(self._base_pix)
            self._canvas.resize(self._base_pix.size())
            return

        combined = QPixmap(self._base_pix)
        painter  = QPainter(combined)
        painter.setRenderHint(QPainter.Antialiasing)
        self._draw_overlay(painter)
        painter.end()

        self._canvas.setPixmap(combined)
        self._canvas.resize(combined.size())

    # ── Overlay drawing ───────────────────────────────────────────────────

    def _draw_overlay(self, p: QPainter) -> None:
        """Draw DNP markers, status dots, correction badges, and selection rings."""
        scale = self._pix_scale
        r     = max(4.5, _DOT_R_BASE * min(self._zoom, 2.5))

        # ── DNP markers (drawn first / bottom layer) ──────────────────────
        # Orange semi-transparent filled ellipse + orange ring + "DNP" text.
        # Always visible regardless of the "Jelölők" overlay checkbox.
        if self._dnp_refs:
            dnp_r    = r * 1.1
            dnp_fill = QColor(255, 140, 0, 110)  # semi-transparent orange, no border
            for ref, (pdf_x, pdf_y) in self._comp_pos.items():
                if ref.upper() not in self._dnp_refs:
                    continue
                sx, sy = pdf_x * scale, pdf_y * scale
                p.setBrush(QBrush(dnp_fill))
                p.setPen(Qt.NoPen)
                p.drawEllipse(
                    int(sx - dnp_r), int(sy - dnp_r),
                    int(2 * dnp_r),  int(2 * dnp_r),
                )

        # Build ref → status map (manual correction overrides detect status)
        status_map: Dict[str, str] = {}
        for res in self._results:
            ref = res.component.ref
            status_map[ref] = (
                "corrected" if ref in self._corrections
                else res.polarity_status
            )

        font = QFont("Arial", max(6, int(7 * min(self._zoom, 2.5))))
        p.setFont(font)

        for ref, (pdf_x, pdf_y) in self._comp_pos.items():
            status  = status_map.get(ref)
            is_sel  = ref in self._selected_refs
            is_corr = ref in self._corrections
            sx, sy  = pdf_x * scale, pdf_y * scale

            # Selection ring — always drawn when this component is selected
            if is_sel:
                ring_r = r + 5
                p.setPen(QPen(_SEL_COLOR, 2.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(
                    int(sx - ring_r), int(sy - ring_r),
                    int(2 * ring_r),  int(2 * ring_r),
                )

            # Coloured status dot — only when "Jelölők" overlay is active
            if self._show_overlay and status is not None:
                col  = _DOT_COLOR.get(status, QColor(140, 140, 140))
                fill = QColor(col)
                fill.setAlpha(185)
                p.setBrush(QBrush(fill))
                p.setPen(QPen(col.darker(140), 1.0))
                p.drawEllipse(
                    int(sx - r), int(sy - r),
                    int(2 * r),  int(2 * r),
                )
                # Reference label when sufficiently zoomed in or selected
                if self._zoom > 1.8 or is_sel:
                    p.setPen(QPen(QColor(255, 255, 255)))
                    p.drawText(int(sx + r + 2), int(sy + 4), ref)

            # Correction badge — always visible even without "Jelölők" overlay.
            # Draws a blue outlined ring + "✎" so the user knows which
            # components have a manual correction waiting for re-render.
            elif is_corr:
                corr_col = _DOT_COLOR["corrected"]
                p.setPen(QPen(corr_col, 2.0))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(
                    int(sx - r), int(sy - r),
                    int(2 * r),  int(2 * r),
                )
                if self._zoom > 0.5:
                    p.setPen(QPen(corr_col))
                    p.drawText(int(sx + r + 2), int(sy + 4), "✎")

    # ═════════════════════════════════════════════════════════════════════
    # Hit-testing
    # ═════════════════════════════════════════════════════════════════════

    def _hit(self, cx: int, cy: int) -> Optional[str]:
        """
        Return the ref of the component whose dot is closest to canvas
        pixel (*cx*, *cy*), or None if none is within the hit radius.
        Works regardless of whether the overlay is visible.
        """
        if not self._comp_pos:
            return None
        scale  = self._pix_scale
        r      = max(4.5, _DOT_R_BASE * min(self._zoom, 2.5))
        thresh = (r + 8) ** 2
        best_ref: Optional[str] = None
        best_d  = thresh
        for ref, (pdf_x, pdf_y) in self._comp_pos.items():
            sx, sy = pdf_x * scale, pdf_y * scale
            d = (cx - sx) ** 2 + (cy - sy) ** 2
            if d < best_d:
                best_d, best_ref = d, ref
        return best_ref

    # ═════════════════════════════════════════════════════════════════════
    # Zoom
    # ═════════════════════════════════════════════════════════════════════

    def _set_zoom(
        self,
        new_zoom: float,
        anchor_canvas: Optional[Tuple[float, float]] = None,
    ) -> None:
        """
        Change the zoom level.

        Parameters
        ----------
        new_zoom      : target zoom factor (clamped to [0.1 … 8.0])
        anchor_canvas : (cx, cy) canvas-pixel that should stay fixed on
                        screen.  None → use the viewport centre.
        """
        new_zoom = max(0.10, min(8.0, new_zoom))
        if abs(new_zoom - self._zoom) < 0.005:
            return

        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        vp   = self._scroll.viewport()

        if anchor_canvas is None:
            anchor_canvas = (
                hbar.value() + vp.width()  / 2.0,
                vbar.value() + vp.height() / 2.0,
            )

        old_scale       = self._pix_scale if self._pix_scale else 1.0
        self._zoom      = new_zoom
        self._base_pix  = None          # invalidate cached base pixmap
        self._zoom_lbl.setText(f"{int(new_zoom * 100)}%")
        self._repaint()                 # re-renders at new zoom, resizes canvas

        ratio = self._pix_scale / old_scale if old_scale else 1.0
        hbar.setValue(int(anchor_canvas[0] * ratio - vp.width()  / 2.0))
        vbar.setValue(int(anchor_canvas[1] * ratio - vp.height() / 2.0))

        can_pan = hbar.maximum() > 0 or vbar.maximum() > 0
        self._canvas.setCursor(
            Qt.OpenHandCursor if can_pan else Qt.ArrowCursor
        )

    def _fit_to_width(self) -> None:
        """Zoom so that the page width exactly fills the scroll-area viewport."""
        if not self._doc:
            return
        avail = max(150, self._scroll.viewport().width() - 4)
        pt_w  = self._doc[0].rect.width
        zoom  = avail / (pt_w * 96.0 / 72.0)
        self._fit_zoom = zoom
        self._set_zoom(zoom)

    # ═════════════════════════════════════════════════════════════════════
    # Overlay toggle
    # ═════════════════════════════════════════════════════════════════════

    def _on_overlay_toggled(self, checked: bool) -> None:
        self._show_overlay = checked
        self._repaint()

    # ═════════════════════════════════════════════════════════════════════
    # Qt event overrides
    # ═════════════════════════════════════════════════════════════════════

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        # Keep "fit" zoom in sync when the host panel is resized
        if self._doc and abs(self._zoom - self._fit_zoom) < 0.02:
            self._fit_to_width()

    def wheelEvent(self, ev: QWheelEvent) -> None:
        delta = ev.angleDelta().y()
        mods  = ev.modifiers()

        if mods & Qt.ControlModifier:
            # Ctrl + Wheel → zoom, anchored to the cursor position
            vp_pos = self._scroll.viewport().mapFromGlobal(
                ev.globalPosition().toPoint()
            )
            hbar   = self._scroll.horizontalScrollBar()
            vbar   = self._scroll.verticalScrollBar()
            anchor = (
                hbar.value() + vp_pos.x(),
                vbar.value() + vp_pos.y(),
            )
            factor = 1.15 if delta > 0 else 1.0 / 1.15
            self._set_zoom(self._zoom * factor, anchor_canvas=anchor)

        elif mods & Qt.ShiftModifier:
            # Shift + Wheel → horizontal scroll
            step = -delta // 3
            self._scroll.horizontalScrollBar().setValue(
                self._scroll.horizontalScrollBar().value() + step
            )

        else:
            # Plain Wheel → vertical scroll
            step = -delta // 3
            self._scroll.verticalScrollBar().setValue(
                self._scroll.verticalScrollBar().value() + step
            )

        ev.accept()
