"""
gui/main_window.py
──────────────────
PySide6 main window for PolarityMark.

Supported workflows
───────────────────
1. PDF only       — heuristic detection
2. PDF + ODB++    — exact pin-1 from ODB++, registered to PDF coordinates
3. DXF only       — same heuristic pipeline as PDF
4. ODB++ only     — renders directly to PDF with polarity markers
"""
import json
import os
import traceback
from typing import Optional

from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtGui import QFont, QColor, QTextCursor, QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QTextEdit, QLabel,
    QSplitter, QCheckBox, QMessageBox, QGroupBox,
    QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QProgressBar, QMenu,
)

from core.pdf_parser import PDFParser
from core.dxf_parser import DXFParser
from core.odb_parser import ODBParser, parse_odb_raw
from core.odb_renderer import render_odb_to_pdf
from core.odb_registration import odb_to_pdf_markers, RegistrationError
from core.component_detector import ComponentDetector
from core.polarity_detector import PolarityDetector
from core.pad_asymmetry_detector import PadAsymmetryDetector
from core.image_polarity_detector import ImagePolarityDetector
from core.matcher import Matcher, MatchResult
from core.exporter import Exporter
from utils.config import Config
from gui.correction_dialog import CorrectionDialog

COL_REF = 0; COL_TYPE = 1; COL_PAGE = 2; COL_STATUS = 3; COL_CONF = 4; COL_MARKERS = 5
_STATUS_COLOR = {
    "marked":    QColor(200, 255, 200),
    "unmarked":  QColor(255, 210, 210),
    "ambiguous": QColor(255, 235, 180),
}

def _is_odb_path(path: str) -> bool:
    low = path.lower()
    return (low.endswith(".zip") or low.endswith(".tgz")
            or low.endswith(".tar.gz") or low.endswith(".tar")
            or os.path.isdir(path))


# ─────────────────────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────────────────────

class AnalysisWorker(QObject):
    log      = Signal(str)
    progress = Signal(int)
    finished = Signal(object)
    error    = Signal(str)

    def __init__(
        self,
        file_path: str,
        config: "Config",
        odb_path: Optional[str] = None,
        corrections: Optional[dict] = None,
        draw_fab: bool = False,
        draw_silk: bool = False,
        draw_courtyard: bool = True,
        draw_notes: bool = False,
        draw_refdes: bool = True,
    ):
        super().__init__()
        self.file_path      = file_path
        self.odb_path       = odb_path
        self.config         = config
        self.corrections    = corrections or {}
        self.draw_fab       = draw_fab
        self.draw_silk      = draw_silk
        self.draw_courtyard = draw_courtyard
        self.draw_notes     = draw_notes
        self.draw_refdes    = draw_refdes

    @Slot()
    def run(self) -> None:
        ext = os.path.splitext(self.file_path)[1].lower()
        try:
            if _is_odb_path(self.file_path) and ext != ".pdf":
                self._run_odb_only()
            elif ext == ".pdf":
                if self.odb_path:
                    self._run_pdf_plus_odb()
                else:
                    self._run_pdf()
            elif ext in (".dxf", ".dwg"):
                self._run_dxf()
            else:
                self.error.emit(f"Unsupported file type: {ext}")
        except Exception:
            self.error.emit(traceback.format_exc())

    # ── PDF only ──────────────────────────────────────────────────────────

    def _run_pdf(self) -> None:
        self.log.emit(f"📂 Parsing PDF: {os.path.basename(self.file_path)}")
        parser = PDFParser(self.file_path)
        pages  = parser.parse()
        parser.close()
        self.progress.emit(20)
        self.log.emit(f"   Pages: {len(pages)}  |  "
                      f"Texts: {sum(len(p.texts) for p in pages)}  |  "
                      f"Shapes: {sum(len(p.shapes) for p in pages)}")
        self._run_heuristic_pipeline(pages, pdf_path=self.file_path)

    # ── PDF + ODB++ combined ──────────────────────────────────────────────

    def _run_pdf_plus_odb(self) -> None:
        self.log.emit(f"📂 Parsing PDF: {os.path.basename(self.file_path)}")
        parser = PDFParser(self.file_path)
        pages  = parser.parse()
        parser.close()
        self.progress.emit(15)
        all_texts  = [t for p in pages for t in p.texts]
        all_shapes = [s for p in pages for s in p.shapes]
        self.log.emit(f"   Shapes: {sum(len(p.shapes) for p in pages)}  "
                      f"|  Texts: {sum(len(p.texts) for p in pages)}")

        self.log.emit("🔍 Detecting components from PDF text …")
        pdf_comps = ComponentDetector().detect(all_texts)
        self.progress.emit(25)
        self.log.emit(f"   Found: {len(pdf_comps)} components "
                      f"({sum(1 for c in pdf_comps if c.is_polar)} polar)")

        self.log.emit(f"📦 Parsing ODB++: {os.path.basename(self.odb_path)}")
        try:
            odb_comps, unit_scale = parse_odb_raw(self.odb_path)
        except Exception as exc:
            self.log.emit(f"⚠️  ODB++ parse error: {exc}")
            self.log.emit("   → Falling back to heuristic PDF-only detection")
            self._run_heuristic_pipeline(pages, pdf_path=self.file_path)
            return
        self.progress.emit(40)

        polar_odb = [c for c in odb_comps if c.is_polar and c.pin1 is not None]
        self.log.emit(f"   Components: {len(odb_comps)}  |  Polar with pin-1: {len(polar_odb)}")

        self.log.emit("📐 Registering ODB++ → PDF coordinate system …")
        try:
            odb_markers, transform, by_ref = odb_to_pdf_markers(
                odb_comps, pdf_comps, unit_scale
            )
            self.log.emit(
                f"   Registration OK — y_sign={transform.y_sign:+.0f}  "
                f"tx={transform.tx:.1f}pt  ty={transform.ty:.1f}pt"
            )
            self.log.emit(f"   Markers resolved: {len(odb_markers)}")
        except RegistrationError as exc:
            self.log.emit(f"⚠️  Registration failed: {exc}")
            self.log.emit("   → Falling back to heuristic PDF-only detection")
            self._run_heuristic_pipeline(pages, pdf_path=self.file_path)
            return
        self.progress.emit(60)

        self.log.emit("🔬 Heuristic fallback for components not in ODB++ …")
        heuristic = self._heuristic_results(pages, pdf_comps, pdf_path=self.file_path)
        self.progress.emit(85)

        results = []
        for r in heuristic:
            odb_m = by_ref.get(r.component.ref)
            if odb_m is not None:
                results.append(MatchResult(
                    component=r.component,
                    markers=[odb_m],
                    polarity_status="marked",
                    overall_confidence=0.99,
                ))
            else:
                results.append(r)

        n_odb  = sum(1 for r in results if r.has_polarity and
                     any(m.source == "odb" for m in r.markers))
        n_heur = sum(1 for r in results if r.has_polarity and
                     all(m.source != "odb" for m in r.markers))
        self.log.emit(
            f"\n📊 Summary: {n_odb + n_heur} marked  "
            f"({n_odb} from ODB++ · {n_heur} heuristic)  "
            f"| {sum(1 for r in results if not r.has_polarity)} unmarked"
        )
        self.progress.emit(100)
        self.log.emit("✅ Analysis complete.")
        self.finished.emit({
            "results":    results,
            "components": pdf_comps,
            "shapes":     all_shapes,
            "markers":    [],
            "pdf_path":   self.file_path,
        })

    # ── DXF only ──────────────────────────────────────────────────────────

    def _run_dxf(self) -> None:
        self.log.emit(f"📐 Parsing DXF: {os.path.basename(self.file_path)}")
        pages = DXFParser(self.file_path).parse()
        self.progress.emit(20)
        self.log.emit(f"   Shapes: {sum(len(p.shapes) for p in pages)}  "
                      f"|  Texts: {sum(len(p.texts) for p in pages)}")
        self._run_heuristic_pipeline(pages, pdf_path=None)

    # ── ODB++ only → render directly to PDF ──────────────────────────────

    def _run_odb_only(self) -> None:
        self.log.emit(f"📦 Parsing ODB++: {os.path.basename(self.file_path)}")

        # Single archive read — raw_comps passed as odb_comps_cache to the
        # renderer so it does NOT re-read the archive a second time.
        try:
            raw_comps, unit_scale = parse_odb_raw(self.file_path)
        except Exception as exc:
            self.error.emit(f"ODB++ parse failed:\n{exc}")
            return

        results = ODBParser._build_results(raw_comps, unit_scale)
        self.progress.emit(40)

        components     = [r.component for r in results]
        n_marked       = sum(1 for r in results if r.has_polarity)
        n_unmarked_cap = sum(
            1 for r in results
            if not r.has_polarity and r.component.comp_type == "capacitor"
        )
        n_net_resolved = sum(
            1 for c in raw_comps
            if c.comp_type in ("diode", "led")
            and getattr(c, "_cathode_pin_num", None) is not None
        )
        if n_net_resolved:
            self.log.emit(
                f"   Cathode resolved via net names: {n_net_resolved} diode(s)/LED(s)"
            )
        self.log.emit(f"   Components: {len(components)}  |  Polar marked: {n_marked}")
        if n_unmarked_cap:
            self.log.emit(f"   ℹ️  {n_unmarked_cap} ceramic capacitors excluded (non-polar)")
        if self.corrections:
            self.log.emit(f"   ✎  Applying {len(self.corrections)} manual correction(s)")
        self.log.emit(f"\n📊 Summary: {n_marked} marked (ODB++ polarity, 99% confidence)")

        layers_on = [n for n, v in [("fab", self.draw_fab),
                                     ("silk", self.draw_silk),
                                     ("courtyard", self.draw_courtyard),
                                     ("notes", self.draw_notes),
                                     ("refdes", self.draw_refdes)] if v]
        self.log.emit(
            f"\n🖨️  Rendering ODB++ → PDF  "
            f"[rétegek: {', '.join(layers_on) if layers_on else 'csak outline+markers'}] …"
        )
        base    = os.path.splitext(self.file_path)[0]
        out_pdf = base + "_polarity.pdf"
        try:
            render_odb_to_pdf(
                self.file_path, out_pdf,
                draw_cu=False,
                draw_fab=self.draw_fab,
                draw_silk=self.draw_silk,
                draw_courtyard=self.draw_courtyard,
                draw_notes=self.draw_notes,
                draw_refdes=self.draw_refdes,
                mark_pin1=True, save_png=False,
                overrides=self.corrections,
                odb_comps_cache=raw_comps,
                log_fn=self.log.emit,
            )
            self.log.emit(f"   📄 PDF saved: {out_pdf}")
        except Exception as exc:
            self.log.emit(f"   ⚠️  Render failed: {exc}\n{traceback.format_exc()}")
            out_pdf = None

        self.progress.emit(100)
        self.log.emit("✅ Done.")
        self.finished.emit({
            "results":       results,
            "components":    components,
            "shapes":        [],
            "markers":       [],
            "pdf_path":      out_pdf,
            "_odb_rendered": True,
        })

    # ── Heuristic pipeline (PDF or DXF source) ────────────────────────────

    def _run_heuristic_pipeline(self, pages, pdf_path: Optional[str]) -> None:
        results     = self._heuristic_results(pages, None, pdf_path)
        n_marked    = sum(1 for r in results if r.polarity_status == "marked")
        n_unmarked  = sum(1 for r in results if r.polarity_status == "unmarked")
        n_ambiguous = sum(1 for r in results if r.polarity_status == "ambiguous")
        self.log.emit(
            f"\n📊 Summary: {n_marked} marked | {n_unmarked} unmarked"
            f" | {n_ambiguous} ambiguous"
        )
        if n_marked == 0:
            self.log.emit("   ℹ️  No polarity markers detected in the file.")
        self.progress.emit(100)
        self.log.emit("✅ Analysis complete.")
        self.finished.emit({
            "results":    results,
            "components": [r.component for r in results],
            "shapes":     [s for p in pages for s in p.shapes],
            "markers":    [],
            "pdf_path":   pdf_path,
        })

    def _heuristic_results(self, pages, pre_detected_comps=None,
                            pdf_path: Optional[str] = None) -> list:
        all_texts  = [t for p in pages for t in p.texts]
        all_shapes = [s for p in pages for s in p.shapes]

        if pre_detected_comps is None:
            self.log.emit("🔍 Detecting components …")
            components = ComponentDetector().detect(all_texts)
            self.progress.emit(35)
            type_counts: dict = {}
            for c in components:
                type_counts[c.comp_type] = type_counts.get(c.comp_type, 0) + 1
            for ctype, cnt in sorted(type_counts.items()):
                self.log.emit(f"   {ctype}: {cnt}")
            self.log.emit(f"   Total: {len(components)} "
                          f"({sum(1 for c in components if c.is_polar)} polar)")
        else:
            components = pre_detected_comps

        self.log.emit("🔬 Pass 1 – text/shape …")
        markers = PolarityDetector(config=self.config).detect(all_texts, all_shapes)
        self.progress.emit(50)

        self.log.emit("🔬 Pass 2 – pad asymmetry …")
        pad_results = PadAsymmetryDetector(config=self.config).detect(all_shapes, components)
        n_pad = sum(1 for r in pad_results if r.has_polarity)
        if n_pad:
            self.log.emit(f"   pad_asymmetry / fab_pin1: {n_pad}")
        self.progress.emit(65)

        img_results = []
        if pdf_path:
            self.log.emit("🔬 Pass 3 – OpenCV raster …")
            img_results = ImagePolarityDetector(config=self.config).detect(
                pdf_path, all_shapes, components
            )
            n_img = sum(1 for r in img_results if r.has_polarity)
            if n_img:
                self.log.emit(f"   image markers: {n_img}")
        self.progress.emit(80)

        self.log.emit("🔗 Matching …")
        text_results = Matcher(config=self.config).match(components, markers)
        pad_by_ref = {r.component.ref: r for r in pad_results}
        img_by_ref = {r.component.ref: r for r in img_results}

        results = []
        for text_r in text_results:
            ref   = text_r.component.ref
            img_r = img_by_ref.get(ref)
            pad_r = pad_by_ref.get(ref)
            if img_r and img_r.has_polarity:
                results.append(img_r)
            elif pad_r and pad_r.has_polarity:
                results.append(pad_r)
            else:
                results.append(text_r)
        self.progress.emit(90)
        return results


# ─────────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._odb_path: str      = ""
        self._results: list      = []
        self._thread             = None
        self._worker             = None
        self._corrections: dict  = {}
        self._last_odb_path: str = ""
        self._last_out_pdf:  str = ""
        self._last_json_path: str = ""
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("PolarityMark – PCB Polarity Detector")
        self.setMinimumSize(960, 660)
        self.resize(1200, 780)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(6)

        # ── Input ─────────────────────────────────────────────────────────
        input_group = QGroupBox("Input files")
        ig = QVBoxLayout(input_group)
        odb_row = QHBoxLayout()
        self._odb_edit = QLineEdit()
        self._odb_edit.setPlaceholderText("ODB++ forrás (.tgz / .zip / könyvtár) …")
        self._odb_edit.setReadOnly(True)
        odb_browse = QPushButton("Browse…")
        odb_browse.setFixedWidth(90)
        odb_browse.clicked.connect(self._browse_odb)
        odb_clear = QPushButton("✕")
        odb_clear.setFixedWidth(28)
        odb_clear.setToolTip("Clear ODB++ selection")
        odb_clear.clicked.connect(self._clear_odb)
        odb_row.addWidget(QLabel("ODB++:"))
        odb_row.addWidget(self._odb_edit)
        odb_row.addWidget(odb_browse)
        odb_row.addWidget(odb_clear)
        ig.addLayout(odb_row)
        root.addWidget(input_group)

        # ── Options row ───────────────────────────────────────────────────
        opt_row = QHBoxLayout()
        self._debug_cb    = QCheckBox("Debug mode")
        self._save_pdf_cb = QCheckBox("Save annotated PDF")
        self._save_pdf_cb.setChecked(True)

        self._fab_cb = QCheckBox("Fab")
        self._fab_cb.setChecked(False)
        self._fab_cb.setToolTip(
            "Fab/assembly réteg — komponens testek körvonala.\n"
            "Sok vonal (~7000+), lassabb render."
        )
        self._silk_cb = QCheckBox("Silkscreen")
        self._silk_cb.setChecked(False)
        self._silk_cb.setToolTip("Silkscreen réteg — feliratok, polaritás-szimbólumok.")
        self._court_cb = QCheckBox("Courtyard")
        self._court_cb.setChecked(True)
        self._court_cb.setToolTip(
            "Courtyard réteg — komponens-területek határvonala.\n"
            "Kevés vonal, gyors."
        )
        self._notes_cb = QCheckBox("Notes/User Drawing")
        self._notes_cb.setChecked(True)
        self._notes_cb.setToolTip(
            "Notes/User Drawing réteg — gyakran itt van a fejléc/lábléc, rajzkeret."
        )
        self._refdes_cb = QCheckBox("RefDes")
        self._refdes_cb.setChecked(True)
        self._refdes_cb.setToolTip(
            "Referencia-jelölők (pl. U1, C3, D5) megjelenítése a PDF-en.\n"
            "Kikapcsolva a rajz tisztább, de az alkatrészek nem azonosíthatók."
        )

        self._analyze_btn = QPushButton("🔍  Analyze")
        self._analyze_btn.setFixedWidth(140)
        self._analyze_btn.setEnabled(False)
        self._analyze_btn.clicked.connect(self._run_analysis)

        self._rerender_btn = QPushButton("🔄  Re-render")
        self._rerender_btn.setFixedWidth(120)
        self._rerender_btn.setEnabled(False)
        self._rerender_btn.setToolTip(
            "Re-render the ODB++ PDF with current manual corrections applied"
        )
        self._rerender_btn.clicked.connect(self._rerender_odb)

        opt_row.addWidget(self._debug_cb)
        opt_row.addWidget(self._save_pdf_cb)
        opt_row.addWidget(self._fab_cb)
        opt_row.addWidget(self._silk_cb)
        opt_row.addWidget(self._court_cb)
        opt_row.addWidget(self._notes_cb)
        opt_row.addWidget(self._refdes_cb)
        opt_row.addStretch()
        opt_row.addWidget(self._rerender_btn)
        opt_row.addWidget(self._analyze_btn)
        root.addLayout(opt_row)

        # ── Progress bar ──────────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(14)
        root.addWidget(self._progress)

        # ── Splitter: log | results ───────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)

        log_group = QGroupBox("Analysis Log")
        log_layout = QVBoxLayout(log_group)
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self._log_view)
        splitter.addWidget(log_group)

        res_group = QGroupBox("Results")
        res_layout = QVBoxLayout(res_group)
        self._results_search = QLineEdit()
        self._results_search.setPlaceholderText(
            "Keresés: ref / type / status / marker …"
        )
        self._results_search.textChanged.connect(self._apply_results_filter)
        res_layout.addWidget(self._results_search)
        headers = ["Ref", "Type", "Page", "Status", "Confidence", "Marker types"]
        self._table = QTableWidget(0, len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setFont(QFont("Consolas", 9))
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        res_layout.addWidget(self._table)

        export_row = QHBoxLayout()
        self._export_btn = QPushButton("💾  Export JSON")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_json)
        export_row.addStretch()
        export_row.addWidget(self._export_btn)
        res_layout.addLayout(export_row)

        splitter.addWidget(res_group)
        splitter.setSizes([380, 620])
        root.addWidget(splitter, 1)

        self.statusBar().showMessage("Ready.  Load an ODB++ file to begin.")

    # ── File browse slots ─────────────────────────────────────────────────

    @Slot()
    def _browse_odb(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open ODB++ archive", "",
            "ODB++ archive (*.tgz *.tar.gz *.zip *.tar);;All Files (*)"
        )
        if not path:
            path = QFileDialog.getExistingDirectory(
                self, "Or select unzipped ODB++ directory", ""
            )
        if path:
            self._odb_path = path
            self._odb_edit.setText(path)
            self._analyze_btn.setEnabled(True)
            self._reset_output()
            self._load_corrections_sidecar(path)
            self._load_session_json(path)
            self._restore_results_json(path)
            self.statusBar().showMessage(f"ODB++: {os.path.basename(path)}")

    @Slot()
    def _clear_odb(self) -> None:
        self._odb_path = ""
        self._results  = []
        self._odb_edit.clear()
        self._analyze_btn.setEnabled(False)
        self._reset_output()
        self.statusBar().showMessage("ODB++ cleared.")

    def _reset_output(self) -> None:
        self._log_view.clear()
        self._table.setRowCount(0)
        self._export_btn.setEnabled(False)
        self._progress.setValue(0)

    # ── Analysis ──────────────────────────────────────────────────────────

    @Slot()
    def _run_analysis(self) -> None:
        if not self._odb_path:
            return
        self._analyze_btn.setEnabled(False)
        self._export_btn.setEnabled(False)
        self._log_view.clear()
        self._table.setRowCount(0)
        self._progress.setValue(0)
        self.statusBar().showMessage("Analyzing …")

        config = Config(debug=self._debug_cb.isChecked())
        self._thread = QThread(self)
        self._worker = AnalysisWorker(
            self._odb_path, config,
            odb_path=None,
            corrections=self._corrections,
            draw_fab=self._fab_cb.isChecked(),
            draw_silk=self._silk_cb.isChecked(),
            draw_courtyard=self._court_cb.isChecked(),
            draw_notes=self._notes_cb.isChecked(),
            draw_refdes=self._refdes_cb.isChecked(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._append_log)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(
            lambda: self._analyze_btn.setEnabled(bool(self._odb_path))
        )
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    # ── Result handlers ───────────────────────────────────────────────────

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self._log_view.append(message)
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._log_view.setTextCursor(cursor)

    @Slot(object)
    def _on_finished(self, data: object) -> None:
        self._analyze_btn.setEnabled(bool(self._odb_path))
        self._export_btn.setEnabled(True)

        results      = data["results"]
        pdf_path     = data.get("pdf_path")
        odb_rendered = data.get("_odb_rendered", False)
        self._results = results
        self._populate_table(results)
        n_marked = sum(1 for r in results if r.has_polarity)
        self.statusBar().showMessage(
            f"Done — {len(results)} components, {n_marked} with polarity markers."
        )

        if odb_rendered and pdf_path:
            self._last_odb_path = self._odb_path
            self._last_out_pdf  = pdf_path
            self._rerender_btn.setEnabled(True)

        if odb_rendered and pdf_path and os.path.exists(pdf_path):
            reply = QMessageBox.question(
                self, "Open rendered PDF?",
                f"Polarity PDF rendered from ODB++:\n{pdf_path}\n\nOpen now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                try:
                    os.startfile(pdf_path)
                except Exception:
                    import subprocess
                    subprocess.Popen(["cmd", "/c", "start", "", pdf_path])

        elif self._save_pdf_cb.isChecked() and pdf_path and pdf_path.endswith(".pdf"):
            out_pdf = pdf_path.replace(".pdf", "_annotated.pdf")
            out_png = out_pdf.replace(".pdf", "_preview.png")
            try:
                Exporter().export_annotated_pdf(
                    pdf_path, results, out_pdf,
                    debug=self._debug_cb.isChecked(),
                    save_png=True,
                )
                self._append_log(f"\n📄 Annotated PDF → {out_pdf}")
                if os.path.exists(out_png):
                    self._append_log(f"🖼  Preview       → {out_png}")
                reply = QMessageBox.question(
                    self, "Open annotated PDF?",
                    f"Saved:\n{out_pdf}\n\nOpen now?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
                )
                if reply == QMessageBox.Yes:
                    try:
                        os.startfile(out_pdf)
                    except Exception:
                        import subprocess
                        subprocess.Popen(["cmd", "/c", "start", "", out_pdf])
            except Exception:
                self._append_log(
                    f"⚠️  Annotated PDF export failed:\n{traceback.format_exc()}"
                )

        # Auto-save results JSON and session state so they can be restored later
        self._auto_save_results_json()
        self._save_session_json()

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self._append_log(f"❌ Error:\n{msg}")
        self._analyze_btn.setEnabled(bool(self._odb_path))
        self.statusBar().showMessage("Analysis failed.")

    # ── Manual corrections ────────────────────────────────────────────────

    @Slot(object)
    def _on_table_context_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._results):
            return
        ref_item = self._table.item(row, COL_REF)
        if not ref_item:
            return
        ref = ref_item.text().rstrip(" ✎")
        menu = QMenu(self)
        act_edit  = QAction(f"✏️  Edit polarity correction for {ref}", self)
        act_clear = QAction(f"✕  Clear correction for {ref}", self)
        act_edit.triggered.connect(lambda: self._open_correction_dialog(row, ref))
        act_clear.triggered.connect(lambda: self._clear_correction(ref))
        menu.addAction(act_edit)
        if ref in self._corrections:
            menu.addAction(act_clear)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _open_correction_dialog(self, row: int, ref: str) -> None:
        result    = self._results[row] if row < len(self._results) else None
        comp_type = result.component.comp_type if result else "unknown"
        current   = self._corrections.get(ref, {})
        dlg = CorrectionDialog(ref, comp_type, current, parent=self)
        if dlg.exec():
            correction = dlg.get_correction()
            if correction:
                self._corrections[ref] = correction
            elif ref in self._corrections:
                del self._corrections[ref]
            self._save_corrections_sidecar()
            mark = " ✎" if correction else ""
            ref_item = self._table.item(row, COL_REF)
            if ref_item:
                ref_item.setText(ref + mark)
            if self._last_odb_path:
                self._rerender_btn.setEnabled(True)
                self._append_log(
                    f"✎ Correction saved for {ref}. "
                    "Click '🔄 Re-render' to update the PDF."
                )

    def _clear_correction(self, ref: str) -> None:
        if ref in self._corrections:
            del self._corrections[ref]
            self._save_corrections_sidecar()
            self._append_log(f"✕ Correction cleared for {ref}.")
            self._populate_table(self._results)

    @Slot()
    def _rerender_odb(self) -> None:
        odb_path = self._last_odb_path
        out_pdf  = self._last_out_pdf
        if not odb_path or not out_pdf:
            QMessageBox.warning(self, "Re-render",
                                "No ODB++ render to update yet.\nRun Analyze first.")
            return
        self._rerender_btn.setEnabled(False)
        self._append_log("\n🔄 Re-rendering with corrections …")
        try:
            render_odb_to_pdf(
                odb_path, out_pdf,
                draw_cu=False,
                draw_fab=self._fab_cb.isChecked(),
                draw_silk=self._silk_cb.isChecked(),
                draw_courtyard=self._court_cb.isChecked(),
                draw_notes=self._notes_cb.isChecked(),
                draw_refdes=self._refdes_cb.isChecked(),
                mark_pin1=True, save_png=False,
                overrides=self._corrections,
                log_fn=self._append_log,
            )
            self._append_log(f"   PDF updated: {out_pdf}")
            reply = QMessageBox.question(
                self, "Open updated PDF?",
                f"Re-rendered:\n{out_pdf}\n\nOpen now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                try:
                    os.startfile(out_pdf)
                except Exception:
                    import subprocess
                    subprocess.Popen(["cmd", "/c", "start", "", out_pdf])
        except Exception as exc:
            self._append_log(f"❌ Re-render failed: {exc}")
        finally:
            self._rerender_btn.setEnabled(True)

    # ── Corrections sidecar ───────────────────────────────────────────────

    def _corrections_sidecar_path(self, odb_path: str) -> str:
        return odb_path + ".corrections.json"

    def _load_corrections_sidecar(self, odb_path: str) -> None:
        path = self._corrections_sidecar_path(odb_path)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                self._corrections = data.get("corrections", {})
                n = len(self._corrections)
                if n:
                    self.statusBar().showMessage(
                        f"Loaded {n} manual correction(s) from sidecar."
                    )
            except Exception:
                self._corrections = {}

    def _save_corrections_sidecar(self) -> None:
        odb_path = self._last_odb_path or self._odb_path
        if not odb_path:
            return
        path = self._corrections_sidecar_path(odb_path)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"corrections": self._corrections}, f,
                          ensure_ascii=False, indent=2)
        except Exception as exc:
            self._append_log(f"⚠️  Could not save corrections: {exc}")


    # ── Session / auto-save ───────────────────────────────────────────────

    def _results_json_path(self, odb_path: str) -> str:
        return os.path.splitext(odb_path)[0] + "_polarity.json"

    def _session_json_path(self, odb_path: str) -> str:
        return odb_path + ".session.json"

    def _auto_save_results_json(self) -> None:
        if not self._odb_path or not self._results:
            return
        path = self._results_json_path(self._odb_path)
        try:
            out = Exporter().export_json(self._results, path, self._odb_path)
            self._last_json_path = out
            self._append_log(f"💾 Results JSON saved: {out}")
        except Exception as exc:
            self._append_log(f"⚠️  Auto JSON save failed: {exc}")

    def _save_session_json(self) -> None:
        if not self._odb_path:
            return
        path = self._session_json_path(self._odb_path)
        payload = {
            "version":        1,
            "odb_path":       self._odb_path,
            "last_out_pdf":   self._last_out_pdf,
            "last_json_path": self._last_json_path,
            "options": {
                "fab":       self._fab_cb.isChecked(),
                "silk":      self._silk_cb.isChecked(),
                "courtyard": self._court_cb.isChecked(),
                "notes":     self._notes_cb.isChecked() if hasattr(self, "_notes_cb") else False,
            },
            "corrections": self._corrections,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._append_log(f"⚠️  Could not save session: {exc}")

    def _load_session_json(self, odb_path: str) -> None:
        path = self._session_json_path(odb_path)
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            opts = data.get("options", {})
            if "fab"       in opts: self._fab_cb.setChecked(bool(opts["fab"]))
            if "silk"      in opts: self._silk_cb.setChecked(bool(opts["silk"]))
            if "courtyard" in opts: self._court_cb.setChecked(bool(opts["courtyard"]))
            if hasattr(self, "_notes_cb") and "notes" in opts:
                self._notes_cb.setChecked(bool(opts["notes"]))
            sess_corr = data.get("corrections", {})
            if isinstance(sess_corr, dict) and sess_corr and not self._corrections:
                self._corrections = sess_corr
            self._last_out_pdf   = data.get("last_out_pdf",   "") or ""
            self._last_json_path = data.get("last_json_path", "") or ""
            if self._last_out_pdf:
                self._last_odb_path = odb_path
                self._rerender_btn.setEnabled(True)
            self._append_log(f"↩ Session loaded: {os.path.basename(path)}")
        except Exception as exc:
            self._append_log(f"⚠️  Session load failed: {exc}")

    def _restore_results_json(self, odb_path: str) -> None:
        """Try to load a previously saved results JSON and populate the table."""
        candidates = []
        if self._last_json_path and os.path.isfile(self._last_json_path):
            candidates.append(self._last_json_path)
        default = self._results_json_path(odb_path)
        if default not in candidates:
            candidates.append(default)

        for path in candidates:
            if not path or not os.path.isfile(path):
                continue
            try:
                results = Exporter().load_json_results(path)
            except Exception as exc:
                self._append_log(f"⚠️  JSON load failed ({os.path.basename(path)}): {exc}")
                continue
            if not results:
                self._append_log(f"⚠️  JSON empty: {os.path.basename(path)}")
                continue
            self._results = results
            self._populate_table(results)
            self._export_btn.setEnabled(True)
            self._last_json_path = os.path.abspath(path)
            n_marked = sum(1 for r in results if r.has_polarity)
            self._append_log(
                f"↩ Results restored: {len(results)} components "
                f"({n_marked} marked) from {os.path.basename(path)}"
            )
            self.statusBar().showMessage(
                f"Restored {len(results)} components from JSON — "
                f"click '🔄 Re-render' to rebuild the PDF."
            )
            return

    # ── Export ────────────────────────────────────────────────────────────

    @Slot()
    def _export_json(self) -> None:
        if not self._results:
            return
        base = os.path.splitext(self._odb_path or "output")[0]
        path, _ = QFileDialog.getSaveFileName(
            self, "Save JSON", base + "_polarity.json",
            "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        try:
            Exporter().export_json(self._results, path, self._odb_path or path)
            self.statusBar().showMessage(f"JSON saved: {path}")
            QMessageBox.information(self, "Export", f"JSON saved:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export Error", str(exc))

    # ── Table ─────────────────────────────────────────────────────────────

    def _populate_table(self, results: list) -> None:
        import re as _re

        def _natural_key(r):
            ref = r.component.ref
            parts = _re.split(r'(\d+)', ref)
            return [int(p) if p.isdigit() else p.upper() for p in parts]

        sorted_results = sorted(results, key=_natural_key)

        self._table.setRowCount(0)
        for result in sorted_results:
            comp = result.component
            row  = self._table.rowCount()
            self._table.insertRow(row)
            marker_types = ", ".join(sorted(set(m.marker_type for m in result.markers)))
            conf_str = f"{result.overall_confidence:.0%}" if result.has_polarity else "—"
            ref_text = comp.ref + (" ✎" if comp.ref in self._corrections else "")
            values = [
                ref_text, comp.comp_type, str(comp.page + 1),
                result.polarity_status, conf_str, marker_types or "—",
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignCenter)
                bg = _STATUS_COLOR.get(result.polarity_status)
                if bg:
                    item.setBackground(bg)
                self._table.setItem(row, col, item)
        self._apply_results_filter()

    @Slot(str)
    def _apply_results_filter(self, _text: str = "") -> None:
        needle = self._results_search.text().strip().lower() if hasattr(self, "_results_search") else ""
        for row in range(self._table.rowCount()):
            if not needle:
                self._table.setRowHidden(row, False)
                continue
            hay = []
            for col in (COL_REF, COL_TYPE, COL_STATUS, COL_MARKERS):
                item = self._table.item(row, col)
                if item and item.text():
                    hay.append(item.text().lower())
            self._table.setRowHidden(row, needle not in " | ".join(hay))
