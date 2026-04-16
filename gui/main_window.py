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
import time
import traceback
from typing import Optional

from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot, QItemSelectionModel
from PySide6.QtGui import QFont, QColor, QTextCursor, QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QTextEdit, QLabel,
    QSplitter, QCheckBox, QMessageBox, QGroupBox,
    QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QProgressBar, QMenu, QAbstractItemView, QComboBox,
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
from utils.translations import TRANSLATIONS, LANGUAGE_NAMES
from gui.correction_dialog import CorrectionDialog
from gui.pdf_preview import PDFPreviewWidget, PreviewDialog

COL_REF = 0; COL_TYPE = 1; COL_PAGE = 2; COL_STATUS = 3; COL_CONF = 4; COL_MARKERS = 5
_STATUS_COLOR = {
    "marked":       QColor(200, 255, 200),
    "unmarked":     QColor(255, 210, 210),
    "ambiguous":    QColor(255, 235, 180),
    "needs_review": QColor(230, 210, 255),  # light purple — uncertain, needs user review
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
        dnp_refs: Optional[set] = None,
        draw_fab: bool = False,
        draw_silk: bool = True,
        draw_courtyard: bool = True,
        draw_notes: bool = False,
        draw_title_block: bool = True,
        draw_refdes: bool = True,
    ):
        super().__init__()
        self.file_path      = file_path
        self.odb_path       = odb_path
        self.config         = config
        self.corrections    = corrections or {}
        self.dnp_refs       = dnp_refs or set()
        self.draw_fab       = draw_fab
        self.draw_silk      = draw_silk
        self.draw_courtyard = draw_courtyard
        self.draw_notes     = draw_notes
        self.draw_title_block = draw_title_block
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
        n_silk = sum(
            1 for c in raw_comps
            if getattr(c, "_detection_method", "") == "silk"
        )
        n_needs_review = sum(
            1 for r in results if r.polarity_status == "needs_review"
        )
        if n_silk:
            self.log.emit(f"   🔍 Cathode from silk layer: {n_silk} diode(s)/LED(s)")
        if n_net_resolved:
            self.log.emit(
                f"   Cathode resolved via net names: {n_net_resolved} diode(s)/LED(s)"
            )
        self.log.emit(f"   Components: {len(components)}  |  Polar marked: {n_marked}")
        if n_needs_review:
            self.log.emit(
                f"   ⚠️  {n_needs_review} component(s) need review "
                "(60% confidence — right-click → Accept or Flip & Accept)"
            )
        if n_unmarked_cap:
            self.log.emit(f"   ℹ️  {n_unmarked_cap} ceramic capacitors excluded (non-polar)")
        if self.corrections:
            self.log.emit(f"   ✎  Applying {len(self.corrections)} manual correction(s)")
        n_confirmed = n_marked - n_needs_review
        self.log.emit(
            f"\n📊 Summary: {n_confirmed} marked (99% confidence)"
            + (f"  |  {n_needs_review} needs review (60% 🟣)" if n_needs_review else "")
        )

        layers_on = [n for n, v in [("fab", self.draw_fab),
                                     ("silk", self.draw_silk),
                                     ("courtyard", self.draw_courtyard),
                                     ("notes", self.draw_notes),
                                     ("title_block", self.draw_title_block),
                                     ("refdes", self.draw_refdes)] if v]
        self.log.emit(
            f"\n🖨️  Rendering ODB++ → PDF  "
            f"[layers: {', '.join(layers_on) if layers_on else 'outline+markers only'}] …"
        )
        base    = os.path.splitext(self.file_path)[0]
        out_pdf = base + "_polarity.pdf"
        comp_positions: dict = {}
        try:
            render_odb_to_pdf(
                self.file_path, out_pdf,
                draw_cu=False,
                draw_fab=self.draw_fab,
                draw_silk=self.draw_silk,
                draw_courtyard=self.draw_courtyard,
                draw_notes=self.draw_notes,
                draw_title_block=self.draw_title_block,
                draw_refdes=self.draw_refdes,
                mark_pin1=True, save_png=False,
                overrides=self.corrections,
                dnp_refs=self.dnp_refs,
                odb_comps_cache=raw_comps,
                capture_positions=comp_positions,
                log_fn=self.log.emit,
            )
            self.log.emit(f"   📄 PDF saved: {out_pdf}")
        except Exception as exc:
            self.log.emit(f"   ⚠️  Render failed: {exc}\n{traceback.format_exc()}")
            out_pdf = None

        self.progress.emit(100)
        self.log.emit("✅ Done.")
        self.finished.emit({
            "results":         results,
            "components":      components,
            "shapes":          [],
            "markers":         [],
            "pdf_path":        out_pdf,
            "comp_positions":  comp_positions,
            "_odb_rendered":   True,
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
        self._lang: str           = "hu"
        self._odb_path: str       = ""
        self._results: list       = []
        self._thread              = None
        self._worker              = None
        self._corrections: dict   = {}
        self._dnp_refs: set       = set()
        self._comp_positions: dict = {}   # {ref: (pdf_x, pdf_y)} for preview overlay
        self._last_odb_path: str  = ""
        self._last_out_pdf:  str  = ""
        self._last_json_path: str = ""
        self._syncing_selection: bool = False   # prevents selection sync loops
        self._setup_ui()

    def tr(self, key: str) -> str:  # type: ignore[override]
        """Return the translated string for *key* in the current language."""
        lang_dict = TRANSLATIONS.get(self._lang, TRANSLATIONS["en"])
        return lang_dict.get(key, TRANSLATIONS["en"].get(key, key))

    def _setup_ui(self) -> None:
        self.setWindowTitle(self.tr("window_title"))
        self.setMinimumSize(960, 660)
        self.resize(1200, 780)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(6)

        # ── Input ─────────────────────────────────────────────────────────
        self._input_group = QGroupBox(self.tr("group_input"))
        ig = QVBoxLayout(self._input_group)
        odb_row = QHBoxLayout()
        self._odb_edit = QLineEdit()
        self._odb_edit.setPlaceholderText(self.tr("placeholder_odb"))
        self._odb_edit.setReadOnly(True)
        self._odb_browse_btn = QPushButton(self.tr("btn_browse"))
        self._odb_browse_btn.setFixedWidth(110)
        self._odb_browse_btn.clicked.connect(self._browse_odb)
        odb_clear = QPushButton("✕")
        odb_clear.setFixedWidth(28)
        odb_clear.setToolTip(self.tr("tooltip_odb_clear"))
        odb_clear.clicked.connect(self._clear_odb)
        self._odb_clear_btn = odb_clear
        self._odb_label = QLabel(self.tr("label_odb"))
        odb_row.addWidget(self._odb_label)
        odb_row.addWidget(self._odb_edit)
        odb_row.addWidget(self._odb_browse_btn)
        odb_row.addWidget(odb_clear)
        ig.addLayout(odb_row)

        # ── DNP (Do-Not-Place) ───────────────────────────────────────────
        dnp_row = QHBoxLayout()
        self._dnp_label = QLabel(self.tr("label_dnp"))
        self._dnp_label.setFixedWidth(36)
        self._dnp_label.setToolTip(self.tr("tooltip_dnp_label"))
        self._dnp_edit = QLineEdit()
        self._dnp_edit.setPlaceholderText(self.tr("placeholder_dnp"))
        self._dnp_edit.setToolTip(self.tr("tooltip_dnp_edit"))
        self._dnp_edit.textChanged.connect(self._on_dnp_changed)
        dnp_clear_btn = QPushButton("✕")
        dnp_clear_btn.setFixedWidth(28)
        dnp_clear_btn.setToolTip(self.tr("tooltip_dnp_clear"))
        dnp_clear_btn.clicked.connect(lambda: self._dnp_edit.clear())
        self._dnp_clear_btn = dnp_clear_btn
        dnp_row.addWidget(self._dnp_label)
        dnp_row.addWidget(self._dnp_edit)
        dnp_row.addWidget(dnp_clear_btn)
        ig.addLayout(dnp_row)

        root.addWidget(self._input_group)

        # ── Options row ───────────────────────────────────────────────────
        opt_row = QHBoxLayout()
        self._debug_cb    = QCheckBox(self.tr("cb_debug"))
        self._save_pdf_cb = QCheckBox(self.tr("cb_save_pdf"))
        self._save_pdf_cb.setChecked(True)

        self._fab_cb = QCheckBox(self.tr("cb_fab"))
        self._fab_cb.setChecked(False)
        self._fab_cb.setToolTip(self.tr("tooltip_fab"))
        self._silk_cb = QCheckBox(self.tr("cb_silk"))
        self._silk_cb.setChecked(True)
        self._silk_cb.setToolTip(self.tr("tooltip_silk"))
        self._court_cb = QCheckBox(self.tr("cb_court"))
        self._court_cb.setChecked(True)
        self._court_cb.setToolTip(self.tr("tooltip_court"))
        #self._notes_cb = QCheckBox(self.tr("cb_notes"))
        #self._notes_cb.setChecked(True)
        #self._notes_cb.setToolTip(self.tr("tooltip_notes"))
        self._title_cb = QCheckBox(self.tr("cb_title"))
        self._title_cb.setChecked(True)
        self._title_cb.setToolTip(self.tr("tooltip_title"))
        self._refdes_cb = QCheckBox(self.tr("cb_refdes"))
        self._refdes_cb.setChecked(False)
        self._refdes_cb.setToolTip(self.tr("tooltip_refdes"))

        self._analyze_btn = QPushButton(self.tr("btn_analyze"))
        self._analyze_btn.setFixedWidth(140)
        self._analyze_btn.setEnabled(False)
        self._analyze_btn.clicked.connect(self._run_analysis)

        self._rerender_btn = QPushButton(self.tr("btn_rerender"))
        self._rerender_btn.setFixedWidth(120)
        self._rerender_btn.setEnabled(False)
        self._rerender_btn.setToolTip(self.tr("tooltip_rerender"))
        self._rerender_btn.clicked.connect(self._rerender_odb)

        # ── Language selector ─────────────────────────────────────────────
        self._lang_label = QLabel(self.tr("language_label"))
        self._lang_combo = QComboBox()
        for code, name in LANGUAGE_NAMES.items():
            self._lang_combo.addItem(name, code)
        self._lang_combo.setCurrentIndex(list(LANGUAGE_NAMES.keys()).index(self._lang))
        self._lang_combo.setFixedWidth(90)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)

        opt_row.addWidget(self._debug_cb)
        opt_row.addWidget(self._save_pdf_cb)
        opt_row.addWidget(self._fab_cb)
        opt_row.addWidget(self._silk_cb)
        opt_row.addWidget(self._court_cb)
        #opt_row.addWidget(self._notes_cb)
        opt_row.addWidget(self._title_cb)
        opt_row.addWidget(self._refdes_cb)
        opt_row.addStretch()
        opt_row.addWidget(self._lang_label)
        opt_row.addWidget(self._lang_combo)
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

        self._log_group = QGroupBox(self.tr("group_log"))
        log_layout = QVBoxLayout(self._log_group)
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self._log_view)
        splitter.addWidget(self._log_group)

        self._res_group = QGroupBox(self.tr("group_results"))
        res_layout = QVBoxLayout(self._res_group)
        self._results_search = QLineEdit()
        self._results_search.setPlaceholderText(self.tr("placeholder_search"))
        self._results_search.textChanged.connect(self._apply_results_filter)
        res_layout.addWidget(self._results_search)
        self._table_headers = [
            self.tr("col_ref"), self.tr("col_type"), self.tr("col_page"),
            self.tr("col_status"), self.tr("col_conf"), self.tr("col_markers"),
        ]
        self._table = QTableWidget(0, len(self._table_headers))
        self._table.setHorizontalHeaderLabels(self._table_headers)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setFont(QFont("Consolas", 9))
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        self._table.selectionModel().selectionChanged.connect(
            self._on_table_selection_changed
        )
        res_layout.addWidget(self._table)

        export_row = QHBoxLayout()
        self._export_btn = QPushButton(self.tr("btn_export_json"))
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_json)
        export_row.addStretch()
        export_row.addWidget(self._export_btn)
        res_layout.addLayout(export_row)

        splitter.addWidget(self._res_group)

        # ── PDF Preview — lives in a separate floating window ─────────────
        self._preview = PDFPreviewWidget()
        self._preview.component_clicked.connect(self._on_preview_component_clicked)
        self._preview.component_edit_requested.connect(self._on_preview_edit_requested)
        self._preview.selection_changed.connect(self._on_preview_selection_changed)
        self._preview.preview_context_menu_requested.connect(
            self._on_preview_context_menu
        )
        self._preview_dialog = PreviewDialog(self._preview, parent=self)

        # Small button below the results table to open the preview window
        open_prev_row = QHBoxLayout()
        self._open_preview_btn = QPushButton(self.tr("btn_open_preview"))
        self._open_preview_btn.setEnabled(False)
        self._open_preview_btn.setToolTip(self.tr("tooltip_open_preview"))
        self._open_preview_btn.clicked.connect(self._open_preview_window)
        open_prev_row.addStretch()
        open_prev_row.addWidget(self._open_preview_btn)
        res_layout.addLayout(open_prev_row)

        splitter.setSizes([300, 900])
        root.addWidget(splitter, 1)

        self.statusBar().showMessage(self.tr("status_ready"))

    # ── Language ──────────────────────────────────────────────────────────

    @Slot(int)
    def _on_language_changed(self, index: int) -> None:
        self._lang = self._lang_combo.itemData(index)
        self._retranslate_ui()

    def _retranslate_ui(self) -> None:
        """Update all static UI text to the currently selected language."""
        self.setWindowTitle(self.tr("window_title"))
        self._input_group.setTitle(self.tr("group_input"))
        self._odb_label.setText(self.tr("label_odb"))
        self._odb_edit.setPlaceholderText(self.tr("placeholder_odb"))
        self._odb_browse_btn.setText(self.tr("btn_browse"))
        self._odb_clear_btn.setToolTip(self.tr("tooltip_odb_clear"))
        self._dnp_label.setText(self.tr("label_dnp"))
        self._dnp_label.setToolTip(self.tr("tooltip_dnp_label"))
        self._dnp_edit.setPlaceholderText(self.tr("placeholder_dnp"))
        self._dnp_edit.setToolTip(self.tr("tooltip_dnp_edit"))
        self._dnp_clear_btn.setToolTip(self.tr("tooltip_dnp_clear"))
        self._debug_cb.setText(self.tr("cb_debug"))
        self._save_pdf_cb.setText(self.tr("cb_save_pdf"))
        self._fab_cb.setText(self.tr("cb_fab"))
        self._fab_cb.setToolTip(self.tr("tooltip_fab"))
        self._silk_cb.setText(self.tr("cb_silk"))
        self._silk_cb.setToolTip(self.tr("tooltip_silk"))
        self._court_cb.setText(self.tr("cb_court"))
        self._court_cb.setToolTip(self.tr("tooltip_court"))
        self._notes_cb.setText(self.tr("cb_notes"))
        self._notes_cb.setToolTip(self.tr("tooltip_notes"))
        self._title_cb.setText(self.tr("cb_title"))
        self._title_cb.setToolTip(self.tr("tooltip_title"))
        self._refdes_cb.setText(self.tr("cb_refdes"))
        self._refdes_cb.setToolTip(self.tr("tooltip_refdes"))
        self._lang_label.setText(self.tr("language_label"))
        self._analyze_btn.setText(self.tr("btn_analyze"))
        self._rerender_btn.setText(self.tr("btn_rerender"))
        self._rerender_btn.setToolTip(self.tr("tooltip_rerender"))
        self._log_group.setTitle(self.tr("group_log"))
        self._res_group.setTitle(self.tr("group_results"))
        self._results_search.setPlaceholderText(self.tr("placeholder_search"))
        headers = [
            self.tr("col_ref"), self.tr("col_type"), self.tr("col_page"),
            self.tr("col_status"), self.tr("col_conf"), self.tr("col_markers"),
        ]
        self._table.setHorizontalHeaderLabels(headers)
        self._export_btn.setText(self.tr("btn_export_json"))
        self._open_preview_btn.setText(self.tr("btn_open_preview"))
        self._open_preview_btn.setToolTip(self.tr("tooltip_open_preview"))

    # ── File browse slots ─────────────────────────────────────────────────

    def _parse_dnp_text(self, text: str) -> set:
        """Parse comma/semicolon/space-separated Refdes list → set of upper-case refs."""
        import re as _re
        parts = _re.split(r"[,;\s]+", text.strip())
        return {p.strip().upper() for p in parts if p.strip()}

    @Slot(str)
    def _on_dnp_changed(self, text: str) -> None:
        """Live: parse DNP field, update preview overlay immediately."""
        self._dnp_refs = self._parse_dnp_text(text)
        self._preview.set_dnp_refs(self._dnp_refs)
        if self._dnp_refs:
            self.statusBar().showMessage(
                self.tr("status_dnp_set").format(n=len(self._dnp_refs))
            )

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
            self.statusBar().showMessage(self.tr("status_odb_loaded").format(name=os.path.basename(path)))

    @Slot()
    def _clear_odb(self) -> None:
        self._odb_path = ""
        self._results  = []
        self._odb_edit.clear()
        self._analyze_btn.setEnabled(False)
        self._reset_output()
        self.statusBar().showMessage(self.tr("status_odb_cleared"))

    def _reset_output(self) -> None:
        self._log_view.clear()
        self._table.setRowCount(0)
        self._export_btn.setEnabled(False)
        self._open_preview_btn.setEnabled(False)
        self._progress.setValue(0)
        self._comp_positions = {}
        self._preview.clear()

    @Slot()
    def _open_preview_window(self) -> None:
        """Show / raise the floating PDF preview window."""
        self._preview_dialog.show()
        self._preview_dialog.raise_()
        self._preview_dialog.activateWindow()

    def _load_preview(self, pdf_path: str, comp_positions: dict) -> None:
        """Load the preview widget and enable the Open Preview button."""
        self._preview.load(pdf_path, comp_positions)
        self._open_preview_btn.setEnabled(True)

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
        self.statusBar().showMessage(self.tr("status_analyzing"))

        # Release any open PDF handle BEFORE the worker tries to overwrite the file.
        # Without this, Windows raises "Permission denied" on the second analysis run.
        self._preview.release()

        config = Config(debug=self._debug_cb.isChecked())
        self._thread = QThread(self)
        self._worker = AnalysisWorker(
            self._odb_path, config,
            odb_path=None,
            corrections=self._corrections,
            dnp_refs=self._dnp_refs,
            draw_fab=self._fab_cb.isChecked(),
            draw_silk=self._silk_cb.isChecked(),
            draw_courtyard=self._court_cb.isChecked(),
            draw_notes=self._notes_cb.isChecked(),
            draw_title_block=self._title_cb.isChecked(),
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
        self._analysis_start = time.perf_counter()
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
        elapsed = time.perf_counter() - getattr(self, "_analysis_start", time.perf_counter())
        self._analyze_btn.setEnabled(bool(self._odb_path))
        self._export_btn.setEnabled(True)

        results      = data["results"]
        pdf_path     = data.get("pdf_path")
        odb_rendered = data.get("_odb_rendered", False)
        self._results = results
        self._populate_table(results)
        n_marked = sum(1 for r in results if r.has_polarity)

        mins, secs = divmod(elapsed, 60)
        time_str = f"{int(mins)}:{secs:05.2f}" if mins >= 1 else f"{secs:.2f}s"
        self._append_log(f"\n⏱  Elapsed time: {time_str}")

        self.statusBar().showMessage(
            self.tr("status_done").format(
                n_results=len(results), n_marked=n_marked, time_str=time_str
            )
        )

        if odb_rendered and pdf_path:
            self._last_odb_path = self._odb_path
            self._last_out_pdf  = pdf_path
            self._rerender_btn.setEnabled(True)

            # Load preview
            comp_pos = data.get("comp_positions", {})
            if comp_pos:
                self._comp_positions = comp_pos
            if pdf_path and os.path.exists(pdf_path) and self._comp_positions:
                self._load_preview(pdf_path, self._comp_positions)
                self._preview.refresh(results, self._corrections)
                self._preview.set_dnp_refs(self._dnp_refs)

        if odb_rendered and pdf_path and os.path.exists(pdf_path):
            reply = QMessageBox.question(
                self, self.tr("dlg_open_rendered_title"),
                self.tr("dlg_open_rendered_msg").format(pdf_path=pdf_path),
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
                    self, self.tr("dlg_open_annotated_title"),
                    self.tr("dlg_open_annotated_msg").format(out_pdf=out_pdf),
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
        elapsed = time.perf_counter() - getattr(self, "_analysis_start", time.perf_counter())
        mins, secs = divmod(elapsed, 60)
        time_str = f"{int(mins)}:{secs:05.2f}" if mins >= 1 else f"{secs:.2f}s"
        self._append_log(f"❌ Error:\n{msg}")
        self._append_log(f"⏱  Running time before error: {time_str}")
        self._analyze_btn.setEnabled(bool(self._odb_path))
        self.statusBar().showMessage(self.tr("log_analysis_failed"))

    # ── Manual corrections ────────────────────────────────────────────────

    def _get_table_selected_refs(self) -> list:
        """Return the clean ref strings for all currently selected table rows."""
        seen, refs = set(), []
        for idx in self._table.selectionModel().selectedRows():
            item = self._table.item(idx.row(), COL_REF)
            if item:
                ref = item.text().rstrip(" ✎")
                if ref not in seen:
                    seen.add(ref)
                    refs.append(ref)
        return refs

    @Slot(object)
    def _on_table_context_menu(self, pos) -> None:
        selected_refs = self._get_table_selected_refs()
        if not selected_refs:
            # Fall back to the row under the cursor
            row = self._table.rowAt(pos.y())
            if row < 0:
                return
            item = self._table.item(row, COL_REF)
            if not item:
                return
            selected_refs = [item.text().rstrip(" ✎")]

        menu = QMenu(self)
        n = len(selected_refs)

        # ── needs_review quick-accept actions ─────────────────────────────
        nr_refs = [r for r in selected_refs if self._is_needs_review(r)]
        if nr_refs:
            nr_label = nr_refs[0] if len(nr_refs) == 1 else f"{len(nr_refs)} components"
            act_accept = QAction(self.tr("ctx_accept_pin").format(label=nr_label), self)
            _nr = list(nr_refs)  # capture for lambda
            act_accept.triggered.connect(lambda: self._quick_accept(_nr, flip=False))
            menu.addAction(act_accept)
            act_flip = QAction(self.tr("ctx_flip_accept").format(label=nr_label), self)
            act_flip.triggered.connect(lambda: self._quick_accept(_nr, flip=True))
            menu.addAction(act_flip)
            menu.addSeparator()

        if n == 1:
            ref = selected_refs[0]
            row = self._row_for_ref(ref)
            act_edit = QAction(self.tr("ctx_edit_single").format(ref=ref), self)
            act_edit.triggered.connect(lambda: self._open_correction_dialog(row, ref))
            menu.addAction(act_edit)
            if ref in self._corrections:
                act_clear = QAction(self.tr("ctx_clear_single").format(ref=ref), self)
                act_clear.triggered.connect(lambda: self._clear_correction(ref))
                menu.addAction(act_clear)
        else:
            act_edit = QAction(self.tr("ctx_edit_multi").format(n=n), self)
            act_edit.triggered.connect(lambda: self._open_bulk_correction_dialog(selected_refs))
            menu.addAction(act_edit)
            has_any_corr = any(r in self._corrections for r in selected_refs)
            if has_any_corr:
                act_clear = QAction(self.tr("ctx_clear_multi").format(n=n), self)
                act_clear.triggered.connect(lambda: self._clear_bulk_corrections(selected_refs))
                menu.addAction(act_clear)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _row_for_ref(self, ref: str) -> int:
        """Return the table row index for *ref*, or -1 if not found."""
        for row in range(self._table.rowCount()):
            item = self._table.item(row, COL_REF)
            if item and item.text().rstrip(" ✎") == ref:
                return row
        return -1

    def _result_for_ref(self, ref: str):
        """Return the MatchResult for *ref*, or None."""
        for r in self._results:
            if r.component.ref == ref:
                return r
        return None

    def _is_needs_review(self, ref: str) -> bool:
        """Return True if *ref* currently has 'needs_review' status."""
        r = self._result_for_ref(ref)
        return r is not None and r.polarity_status == "needs_review"

    def _open_correction_dialog(self, row: int, ref: str) -> None:
        result    = self._results[row] if 0 <= row < len(self._results) else None
        comp_type = result.component.comp_type if result else "unknown"
        current   = self._corrections.get(ref, {})
        dlg = CorrectionDialog(ref, comp_type, current, parent=self)
        if dlg.exec():
            correction = dlg.get_correction()
            self._apply_correction_to_refs([ref], correction)

    def _open_bulk_correction_dialog(self, refs: list) -> None:
        """Open the correction dialog for multiple refs at once."""
        # Gather comp_types and current corrections for each ref
        ref_set  = set(refs)
        comp_types = []
        for result in self._results:
            if result.component.ref in ref_set:
                comp_types.append(result.component.comp_type)
        if not comp_types:
            comp_types = ["unknown"] * len(refs)

        current_map = {r: self._corrections.get(r, {}) for r in refs}
        dlg = CorrectionDialog(refs, comp_types, current_map, parent=self)
        if dlg.exec():
            correction = dlg.get_correction()
            self._apply_correction_to_refs(refs, correction)

    def _apply_correction_to_refs(self, refs: list, correction: dict) -> None:
        """Apply *correction* to all *refs*, save, and refresh the preview."""
        for ref in refs:
            if correction:
                self._corrections[ref] = correction
            elif ref in self._corrections:
                del self._corrections[ref]

        self._save_corrections_sidecar()

        # Update table ref-cell text
        for ref in refs:
            row = self._row_for_ref(ref)
            if row >= 0:
                item = self._table.item(row, COL_REF)
                if item:
                    mark = " ✎" if correction else ""
                    item.setText(ref + mark)

        # Refresh preview — keep the current selection
        self._preview.refresh(self._results, self._corrections)
        if self._last_odb_path:
            self._rerender_btn.setEnabled(True)
        n = len(refs)
        label = refs[0] if n == 1 else f"{n} components"
        action = self.tr("log_correction_saved_action") if correction else self.tr("log_correction_cleared_action")
        self._append_log(
            self.tr("log_correction_saved").format(action=action, label=label)
        )

    def _clear_correction(self, ref: str) -> None:
        self._apply_correction_to_refs([ref], {})

    def _clear_bulk_corrections(self, refs: list) -> None:
        self._apply_correction_to_refs(refs, {})

    def _quick_accept(self, refs: list, flip: bool) -> None:
        """One-click accept for 'needs_review' components.

        Saves a correction (accepted=True, flip_pin=True/False), refreshes
        the overlay immediately (blue ring ✎), and updates the table row to
        show 'marked' so the user gets instant visual feedback.
        Re-rendering the PDF bakes the final green marker.
        """
        correction = {"accepted": True}
        if flip:
            correction["flip_pin"] = True
        self._apply_correction_to_refs(refs, correction)

        # Update status in self._results so _populate_table shows "marked"
        ref_set = set(refs)
        for r in self._results:
            if r.component.ref in ref_set:
                r.polarity_status    = "marked"
                r.overall_confidence = 0.99
        self._populate_table(self._results)

        n          = len(refs)
        label      = refs[0] if n == 1 else f"{n} components"
        flip_note  = self.tr("log_accepted_flip_note") if flip else ""
        self._append_log(
            self.tr("log_accepted").format(flip_note=flip_note, label=label)
        )

    # ── Selection sync (preview ↔ table) ─────────────────────────────────

    @Slot(str)
    def _on_preview_component_clicked(self, ref: str) -> None:
        """Scroll the table to the most-recently clicked component."""
        clean = ref.rstrip(" ✎")
        for row in range(self._table.rowCount()):
            item = self._table.item(row, COL_REF)
            if item and item.text().rstrip(" ✎") == clean:
                self._table.scrollToItem(item)
                break

    @Slot(list)
    def _on_preview_selection_changed(self, refs: list) -> None:
        """Preview selection changed → update table selection to match."""
        if self._syncing_selection:
            return
        self._syncing_selection = True
        try:
            sm = self._table.selectionModel()
            self._table.clearSelection()
            clean_set = {r.rstrip(" ✎") for r in refs}
            for row in range(self._table.rowCount()):
                item = self._table.item(row, COL_REF)
                if item and item.text().rstrip(" ✎") in clean_set:
                    sm.select(
                        self._table.model().index(row, 0),
                        QItemSelectionModel.Select | QItemSelectionModel.Rows,
                    )
        finally:
            self._syncing_selection = False

    @Slot()
    def _on_table_selection_changed(self) -> None:
        """Table selection changed → update preview highlight to match."""
        if self._syncing_selection:
            return
        self._syncing_selection = True
        try:
            selected_refs = self._get_table_selected_refs()
            self._preview.select_multi(selected_refs)
        finally:
            self._syncing_selection = False

    @Slot(str)
    def _on_preview_edit_requested(self, ref: str) -> None:
        """Double-click on preview → open correction dialog for selected components."""
        selected = self._preview.get_selected_refs()
        if len(selected) > 1 and ref in selected:
            # Multiple selected and the double-clicked one is among them → bulk
            self._open_bulk_correction_dialog(selected)
        else:
            # Single edit
            clean = ref.rstrip(" ✎")
            row   = self._row_for_ref(clean)
            self._open_correction_dialog(row, clean)

    @Slot(object)
    def _on_preview_context_menu(self, global_pos) -> None:
        """Right-click on preview → context menu for selected components."""
        selected_refs = self._preview.get_selected_refs()
        if not selected_refs:
            return
        menu = QMenu(self)
        n    = len(selected_refs)

        # ── needs_review quick-accept actions ─────────────────────────────
        nr_refs = [r for r in selected_refs if self._is_needs_review(r)]
        if nr_refs:
            nr_label = nr_refs[0] if len(nr_refs) == 1 else f"{len(nr_refs)} components"
            _nr = list(nr_refs)
            act_accept = QAction(self.tr("ctx_accept_pin").format(label=nr_label), self)
            act_accept.triggered.connect(lambda: self._quick_accept(_nr, flip=False))
            menu.addAction(act_accept)
            act_flip = QAction(self.tr("ctx_flip_accept").format(label=nr_label), self)
            act_flip.triggered.connect(lambda: self._quick_accept(_nr, flip=True))
            menu.addAction(act_flip)
            menu.addSeparator()

        if n == 1:
            ref = selected_refs[0]
            row = self._row_for_ref(ref)
            act_edit = QAction(self.tr("ctx_edit_preview_single").format(ref=ref), self)
            act_edit.triggered.connect(lambda: self._open_correction_dialog(row, ref))
            menu.addAction(act_edit)
            if ref in self._corrections:
                act_clear = QAction(self.tr("ctx_clear_single").format(ref=ref), self)
                act_clear.triggered.connect(lambda: self._clear_correction(ref))
                menu.addAction(act_clear)
        else:
            act_edit = QAction(self.tr("ctx_edit_multi").format(n=n), self)
            act_edit.triggered.connect(
                lambda: self._open_bulk_correction_dialog(selected_refs)
            )
            menu.addAction(act_edit)
            if any(r in self._corrections for r in selected_refs):
                act_clear = QAction(self.tr("ctx_clear_multi").format(n=n), self)
                act_clear.triggered.connect(
                    lambda: self._clear_bulk_corrections(selected_refs)
                )
                menu.addAction(act_clear)
        menu.exec(global_pos)

    @Slot()
    def _rerender_odb(self) -> None:
        odb_path = self._last_odb_path
        out_pdf  = self._last_out_pdf
        if not odb_path or not out_pdf:
            QMessageBox.warning(self, self.tr("dlg_rerender_no_odb_title"),
                                self.tr("dlg_rerender_no_odb_msg"))
            return
        self._rerender_btn.setEnabled(False)
        self._append_log(self.tr("log_rerender_start"))

        # Release the PDF file handle BEFORE overwriting the file on disk.
        # Without this, Windows raises "Permission denied" because PyMuPDF
        # keeps the file open via fitz.open().
        self._preview.release()

        try:
            render_odb_to_pdf(
                odb_path, out_pdf,
                draw_cu=False,
                draw_fab=self._fab_cb.isChecked(),
                draw_silk=self._silk_cb.isChecked(),
                draw_courtyard=self._court_cb.isChecked(),
                draw_notes=self._notes_cb.isChecked(),
                draw_title_block=self._title_cb.isChecked(),
                draw_refdes=self._refdes_cb.isChecked(),
                mark_pin1=True, save_png=False,
                overrides=self._corrections,
                dnp_refs=self._dnp_refs,
                log_fn=self._append_log,
            )
            self._append_log(self.tr("log_pdf_updated").format(out_pdf=out_pdf))
            # Reload preview with fresh render
            if self._comp_positions:
                self._load_preview(out_pdf, self._comp_positions)
                self._preview.refresh(self._results, self._corrections)
                self._preview.set_dnp_refs(self._dnp_refs)
            reply = QMessageBox.question(
                self, self.tr("dlg_open_rerendered_title"),
                self.tr("dlg_open_rerendered_msg").format(out_pdf=out_pdf),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                try:
                    os.startfile(out_pdf)
                except Exception:
                    import subprocess
                    subprocess.Popen(["cmd", "/c", "start", "", out_pdf])
        except Exception as exc:
            self._append_log(self.tr("log_rerender_failed").format(exc=exc))
            # Re-open the old PDF if render failed (so preview stays functional)
            if self._comp_positions and os.path.exists(out_pdf):
                self._load_preview(out_pdf, self._comp_positions)
                self._preview.refresh(self._results, self._corrections)
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
                        self.tr("status_corrections_loaded").format(n=n)
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
                "title_block": self._title_cb.isChecked() if hasattr(self, "_title_cb") else False,
            },
            "corrections":    self._corrections,
            # comp_positions enables auto-restoring the PDF preview next session
            "comp_positions": {
                ref: list(xy) for ref, xy in self._comp_positions.items()
            },
            "dnp_refs": sorted(self._dnp_refs),
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
            if hasattr(self, "_title_cb") and "title_block" in opts:
                self._title_cb.setChecked(bool(opts["title_block"]))
            sess_corr = data.get("corrections", {})
            if isinstance(sess_corr, dict) and sess_corr and not self._corrections:
                self._corrections = sess_corr
            self._last_out_pdf   = data.get("last_out_pdf",   "") or ""
            self._last_json_path = data.get("last_json_path", "") or ""
            if self._last_out_pdf:
                self._last_odb_path = odb_path
                self._rerender_btn.setEnabled(True)
            # Restore component positions so the preview can be reloaded
            raw_pos = data.get("comp_positions", {})
            if isinstance(raw_pos, dict) and raw_pos:
                self._comp_positions = {k: tuple(v) for k, v in raw_pos.items()}
            # Restore DNP list
            saved_dnp = data.get("dnp_refs", [])
            if isinstance(saved_dnp, list) and saved_dnp:
                self._dnp_refs = set(saved_dnp)
                # Update the text field (triggers _on_dnp_changed too)
                self._dnp_edit.blockSignals(True)
                self._dnp_edit.setText(", ".join(sorted(self._dnp_refs)))
                self._dnp_edit.blockSignals(False)
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

            # Auto-load PDF preview if we have all the pieces from the session
            pdf_path = self._last_out_pdf
            if pdf_path and os.path.exists(pdf_path) and self._comp_positions:
                self._load_preview(pdf_path, self._comp_positions)
                self._preview.refresh(results, self._corrections)
                self._preview.set_dnp_refs(self._dnp_refs)
                self._append_log("   🖼  PDF preview loaded.")
                self.statusBar().showMessage(
                    self.tr("status_json_restored_preview").format(
                        n=len(results), n_marked=n_marked
                    )
                )
            else:
                self.statusBar().showMessage(
                    self.tr("status_json_restored").format(n=len(results))
                )
            return

    # ── Export ────────────────────────────────────────────────────────────

    @Slot()
    def _export_json(self) -> None:
        if not self._results:
            return
        base = os.path.splitext(self._odb_path or "output")[0]
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("dlg_save_json_title"), base + "_polarity.json",
            "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        try:
            Exporter().export_json(self._results, path, self._odb_path or path)
            self.statusBar().showMessage(f"JSON saved: {path}")
            QMessageBox.information(self, self.tr("dlg_export_title"),
                                    self.tr("dlg_export_msg").format(path=path))
        except Exception as exc:
            QMessageBox.critical(self, self.tr("dlg_export_error_title"), str(exc))

    # ── Table ─────────────────────────────────────────────────────────────

    def _populate_table(self, results: list) -> None:
        import re as _re

        def _natural_key(r):
            ref = r.component.ref
            parts = _re.split(r'(\d+)', ref)
            return [int(p) if p.isdigit() else p.upper() for p in parts]

        sorted_results = sorted(results, key=_natural_key)

        # Block selection-sync while rebuilding the table to avoid preview flicker
        self._syncing_selection = True
        try:
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
        finally:
            self._syncing_selection = False

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
