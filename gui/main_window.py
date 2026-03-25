"""
gui/main_window.py
──────────────────
PySide6 main window for PolarityMark.

Supported workflows
───────────────────
1. PDF only       — heuristic detection (PolarityDetector, PadAsymmetry, OpenCV)
2. PDF + ODB++    — exact pin-1 from ODB++, registered to PDF coordinates
                    → annotated PDF overlay
3. DXF only       — same heuristic pipeline as PDF (no raster pass)
4. ODB++ only     — no PDF overlay, results in JSON only
"""
import os
import traceback
from typing import Optional

from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtGui import QFont, QColor, QTextCursor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QTextEdit, QLabel,
    QSplitter, QCheckBox, QMessageBox, QGroupBox,
    QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QProgressBar,
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

COL_REF = 0; COL_TYPE = 1; COL_PAGE = 2; COL_STATUS = 3; COL_CONF = 4; COL_MARKERS = 5
_STATUS_COLOR = {
    "marked":    QColor(200, 255, 200),
    "unmarked":  QColor(255, 210, 210),
    "ambiguous": QColor(255, 235, 180),
}

_ODB_EXTENSIONS = {".zip", ".tgz", ".tar", ".gz"}   # .tar.gz handled by endswith check


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
    finished = Signal(object)   # dict
    error    = Signal(str)

    def __init__(
        self,
        file_path: str,
        config: Config,
        odb_path: Optional[str] = None,
    ):
        super().__init__()
        self.file_path = file_path
        self.odb_path  = odb_path
        self.config    = config

    @Slot()
    def run(self) -> None:
        ext = os.path.splitext(self.file_path)[1].lower()
        try:
            # Check if primary file is ODB++ (user loaded .zip/.tgz as source)
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
        n_texts  = sum(len(p.texts)  for p in pages)
        n_shapes = sum(len(p.shapes) for p in pages)
        self.log.emit(f"   Pages: {len(pages)}  |  Texts: {n_texts}  |  Shapes: {n_shapes}")
        self._run_heuristic_pipeline(pages, pdf_path=self.file_path)

    # ── PDF + ODB++ combined ──────────────────────────────────────────────

    def _run_pdf_plus_odb(self) -> None:
        # 1. Parse PDF
        self.log.emit(f"📂 Parsing PDF: {os.path.basename(self.file_path)}")
        parser = PDFParser(self.file_path)
        pages  = parser.parse()
        parser.close()
        self.progress.emit(15)
        all_texts  = [t for p in pages for t in p.texts]
        all_shapes = [s for p in pages for s in p.shapes]
        self.log.emit(f"   Shapes: {sum(len(p.shapes) for p in pages)}  "
                      f"|  Texts: {sum(len(p.texts) for p in pages)}")

        # 2. Detect PDF components (needed for coordinate registration)
        self.log.emit("🔍 Detecting components from PDF text …")
        pdf_comps = ComponentDetector().detect(all_texts)
        self.progress.emit(25)
        self.log.emit(f"   Found: {len(pdf_comps)} components "
                      f"({sum(1 for c in pdf_comps if c.is_polar)} polar)")

        # 3. Parse ODB++
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

        # 4. Register coordinates: ODB++ mm → PDF pt
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

        # 5. Build MatchResult list: ODB++ wins, heuristic as fallback
        #    Run heuristic pipeline silently for components not in ODB++
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
        n_total = n_odb + n_heur
        self.log.emit(
            f"\n📊 Summary: {n_total} marked  "
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

    # ── ODB++ only → render directly to PDF ─────────────────────────────

    def _run_odb_only(self) -> None:
        self.log.emit(f"📦 Parsing ODB++: {os.path.basename(self.file_path)}")

        # Log discovered layers and units
        from core.odb_renderer import (
            _ArchiveReader, _parse_matrix, _discover_layers, _detect_units,
        )
        try:
            _reader = _ArchiveReader(self.file_path)
            _mat = _reader.read("matrix/matrix")
            if _mat:
                _layers = _parse_matrix(_mat)
                _roles = _discover_layers(_layers)
                self.log.emit(f"   Layer discovery ({len(_layers)} layers in matrix):")
                for role, folder in _roles.items():
                    self.log.emit(f"     {role:16s} → {folder}")
            _prof = _reader.read("steps/pcb/profile")
            if _prof:
                _units = _detect_units(_prof)
                self.log.emit(f"   Units: {_units}")
        except Exception:
            pass

        results = ODBParser().parse(self.file_path)
        self.progress.emit(40)
        components = [r.component for r in results]
        n_marked = sum(1 for r in results if r.has_polarity)
        n_unmarked_cap = sum(
            1 for r in results
            if not r.has_polarity and r.component.comp_type == "capacitor"
        )
        self.log.emit(f"   Components: {len(components)}  |  Polar marked: {n_marked}")
        if n_unmarked_cap:
            self.log.emit(f"   ℹ️  {n_unmarked_cap} ceramic capacitors excluded (non-polar)")
        self.log.emit(f"\n📊 Summary: {n_marked} marked (ODB++ polarity, 99% confidence)")

        # Render ODB++ directly to PDF
        self.log.emit("\n🖨️  Rendering ODB++ → PDF with polarity markers …")
        base = os.path.splitext(self.file_path)[0]
        out_pdf = base + "_polarity.pdf"
        try:
            render_odb_to_pdf(
                self.file_path, out_pdf,
                draw_cu=False, draw_fab=True, draw_silk=True,
                draw_courtyard=False, mark_pin1=True, save_png=True,
            )
            self.log.emit(f"   📄 PDF saved: {out_pdf}")
            out_png = out_pdf.replace(".pdf", "_preview.png")
            if os.path.exists(out_png):
                self.log.emit(f"   🖼  Preview:  {out_png}")
        except Exception as exc:
            self.log.emit(f"   ⚠️  Render failed: {exc}")
            out_pdf = None

        self.progress.emit(100)
        self.log.emit("✅ Done.")
        self.finished.emit({
            "results": results, "components": components,
            "shapes": [], "markers": [],
            "pdf_path": out_pdf,  # the rendered PDF
            "_odb_rendered": True,  # flag: don't re-annotate
        })

    # ── Heuristic pipeline (PDF or DXF source) ────────────────────────────

    def _run_heuristic_pipeline(self, pages, pdf_path: Optional[str]) -> None:
        """Full 3-pass heuristic detection; emits finished signal."""
        results = self._heuristic_results(pages, None, pdf_path)
        n_marked    = sum(1 for r in results if r.polarity_status == "marked")
        n_unmarked  = sum(1 for r in results if r.polarity_status == "unmarked")
        n_ambiguous = sum(1 for r in results if r.polarity_status == "ambiguous")
        self.log.emit(
            f"\n📊 Summary: {n_marked} marked | {n_unmarked} unmarked"
            f" | {n_ambiguous} ambiguous"
        )
        if n_marked == 0:
            self.log.emit("   ℹ️  No polarity markers detected in the file.")
            self.log.emit("      Tip: load an ODB++ file alongside the PDF for exact pin-1 data.")
        self.progress.emit(100)
        self.log.emit("✅ Analysis complete.")
        all_texts  = [t for p in pages for t in p.texts]
        all_shapes = [s for p in pages for s in p.shapes]
        self.finished.emit({
            "results":    results,
            "components": [r.component for r in results],
            "shapes":     all_shapes,
            "markers":    [],
            "pdf_path":   pdf_path,
        })

    def _heuristic_results(
        self,
        pages,
        pre_detected_comps=None,
        pdf_path: Optional[str] = None,
    ) -> list:
        """
        Run the 3-pass heuristic detection and return merged MatchResult list.
        If *pre_detected_comps* is given, skip component detection.
        """
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

        # Pass 1
        self.log.emit("🔬 Pass 1 – text/shape …")
        markers = PolarityDetector(config=self.config).detect(all_texts, all_shapes)
        self.progress.emit(50)

        # Pass 2
        self.log.emit("🔬 Pass 2 – pad asymmetry …")
        pad_results = PadAsymmetryDetector(config=self.config).detect(all_shapes, components)
        n_pad = sum(1 for r in pad_results if r.has_polarity)
        if n_pad:
            self.log.emit(f"   pad_asymmetry / fab_pin1: {n_pad}")
        self.progress.emit(65)

        # Pass 3
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

        # Merge
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
        self._pdf_path: str = ""
        self._odb_path: str = ""
        self._results: list = []
        self._thread = None
        self._worker = None
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

        # ── Input files group ─────────────────────────────────────────────
        input_group = QGroupBox("Input files")
        ig = QVBoxLayout(input_group)

        # Row 1: PDF
        pdf_row = QHBoxLayout()
        self._pdf_edit = QLineEdit()
        self._pdf_edit.setPlaceholderText(
            "PCB source: ODB++ (.zip/.tgz — best) or assembly PDF …"
        )
        self._pdf_edit.setReadOnly(True)
        pdf_browse = QPushButton("Browse…")
        pdf_browse.setFixedWidth(90)
        pdf_browse.clicked.connect(self._browse_pdf)
        pdf_row.addWidget(QLabel("Source: "))
        pdf_row.addWidget(self._pdf_edit)
        pdf_row.addWidget(pdf_browse)
        ig.addLayout(pdf_row)

        # Row 2: ODB++
        odb_row = QHBoxLayout()
        self._odb_edit = QLineEdit()
        self._odb_edit.setPlaceholderText(
            "ODB++ source (optional — .tgz / .zip / directory) — "
            "provides exact pin-1 positions …"
        )
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
        self._analyze_btn = QPushButton("🔍  Analyze")
        self._analyze_btn.setFixedWidth(140)
        self._analyze_btn.setEnabled(False)
        self._analyze_btn.clicked.connect(self._run_analysis)
        opt_row.addWidget(self._debug_cb)
        opt_row.addWidget(self._save_pdf_cb)
        opt_row.addStretch()
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
        headers = ["Ref", "Type", "Page", "Status", "Confidence", "Marker types"]
        self._table = QTableWidget(0, len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setFont(QFont("Consolas", 9))
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

        self.statusBar().showMessage(
            "Ready.  Load a PDF, DXF, or ODB++ file to begin."
        )

    # ── File browse slots ─────────────────────────────────────────────────

    @Slot()
    def _browse_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PCB file", "",
            "All supported (*.pdf *.dxf *.dwg *.zip *.tgz *.tar.gz);;"
            "PDF (*.pdf);;"
            "ODB++ (*.zip *.tgz *.tar.gz);;"
            "DXF / DWG (*.dxf *.dwg);;"
            "All Files (*)"
        )
        if path:
            self._pdf_path = path
            self._pdf_edit.setText(path)
            self._analyze_btn.setEnabled(True)
            self._reset_output()
            self.statusBar().showMessage(f"Loaded: {os.path.basename(path)}")

    @Slot()
    def _browse_odb(self) -> None:
        # Try file first; if user picks a directory that won't work with getOpenFileName
        path, _ = QFileDialog.getOpenFileName(
            self, "Open ODB++ archive", "",
            "ODB++ archive (*.tgz *.tar.gz *.zip *.tar);;All Files (*)"
        )
        if not path:
            # Offer directory selection as fallback
            path = QFileDialog.getExistingDirectory(
                self, "Or select unzipped ODB++ directory", ""
            )
        if path:
            self._odb_path = path
            self._odb_edit.setText(path)
            self._analyze_btn.setEnabled(True)
            self._reset_output()
            self.statusBar().showMessage(
                f"ODB++: {os.path.basename(path)}  (will use for exact pin-1 positions)"
            )

    @Slot()
    def _clear_odb(self) -> None:
        self._odb_path = ""
        self._odb_edit.clear()
        self._analyze_btn.setEnabled(bool(self._pdf_path))
        self.statusBar().showMessage("ODB++ cleared — heuristic PDF-only mode")

    def _reset_output(self) -> None:
        self._log_view.clear()
        self._table.setRowCount(0)
        self._export_btn.setEnabled(False)
        self._progress.setValue(0)

    # ── Analysis ──────────────────────────────────────────────────────────

    @Slot()
    def _run_analysis(self) -> None:
        if not self._pdf_path and not self._odb_path:
            return
        self._analyze_btn.setEnabled(False)
        self._export_btn.setEnabled(False)
        self._log_view.clear()
        self._table.setRowCount(0)
        self._progress.setValue(0)
        self.statusBar().showMessage("Analyzing …")

        config = Config(debug=self._debug_cb.isChecked())
        odb_path = self._odb_path if self._odb_path else None

        # If no source PDF but ODB++ is set → pass ODB++ as primary file
        primary = self._pdf_path if self._pdf_path else self._odb_path

        self._thread = QThread(self)
        self._worker = AnalysisWorker(primary, config, odb_path=odb_path)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._append_log)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(
            lambda: self._analyze_btn.setEnabled(bool(self._pdf_path or self._odb_path))
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
        # Re-enable buttons FIRST so they never stay stuck disabled
        self._analyze_btn.setEnabled(bool(self._pdf_path or self._odb_path))
        self._export_btn.setEnabled(True)

        results   = data["results"]
        pdf_path  = data.get("pdf_path")
        odb_rendered = data.get("_odb_rendered", False)
        self._results = results
        self._populate_table(results)
        n_marked = sum(1 for r in results if r.has_polarity)
        self.statusBar().showMessage(
            f"Done — {len(results)} components, {n_marked} with polarity markers."
        )

        if odb_rendered and pdf_path and os.path.exists(pdf_path):
            # ODB++ was rendered directly — just offer to open
            reply = QMessageBox.question(
                self, "Open rendered PDF?",
                f"Polarity PDF rendered from ODB++:\n{pdf_path}\n\nOpen now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
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
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
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
        elif not pdf_path:
            self._append_log(
                "\nℹ️  No PDF source — annotated overlay skipped.\n"
                "   Use 💾 Export JSON to save the results."
            )

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self._append_log(f"❌ Error:\n{msg}")
        self._analyze_btn.setEnabled(bool(self._pdf_path or self._odb_path))
        self.statusBar().showMessage("Analysis failed.")

    @Slot()
    def _export_json(self) -> None:
        if not self._results:
            return
        base = os.path.splitext(self._pdf_path or self._odb_path or "output")[0]
        default_name = base + "_polarity.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save JSON", default_name,
            "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        try:
            Exporter().export_json(self._results, path, self._pdf_path or path)
            self.statusBar().showMessage(f"JSON saved: {path}")
            QMessageBox.information(self, "Export", f"JSON saved:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export Error", str(exc))

    def _populate_table(self, results: list) -> None:
        self._table.setRowCount(0)
        for result in results:
            comp = result.component
            row  = self._table.rowCount()
            self._table.insertRow(row)
            marker_types = ", ".join(sorted(set(m.marker_type for m in result.markers)))
            conf_str = f"{result.overall_confidence:.0%}" if result.has_polarity else "—"
            values = [
                comp.ref, comp.comp_type, str(comp.page + 1),
                result.polarity_status, conf_str, marker_types or "—",
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignCenter)
                bg = _STATUS_COLOR.get(result.polarity_status)
                if bg:
                    item.setBackground(bg)
                self._table.setItem(row, col, item)
