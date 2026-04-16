# PolarityMark – PCB Polarity Marker Tool

Desktop application for detecting and marking component polarity on PCB designs.  
Primary source: **ODB++ archive** → annotated PDF with polarity markers.

---

## Features

### ODB++ processing
- Supports `.tgz`, `.zip`, and extracted directory formats
- PDF rendering via PyMuPDF with toggleable OCG layers (Board outline, Fab, Silkscreen, Courtyard, Notes, Reference labels, Polarity markers, DNP)
- Polarity markers: D-shaped (semicircle) for 2-pin SMD components; small filled circle for multi-pin components
- Cathode pin determination from net names (diodes, LEDs)
- Silk layer cathode detection for diodes/LEDs
- **needs_review** status for low-confidence results (60%) with one-click Accept / Flip & Accept

### Manual corrections
- Per-component: force polarity / flip pin / add note
- Bulk correction for multiple components at once (Ctrl+click or rubber-band selection)
- Corrections auto-saved as `.corrections.json` next to the ODB++ file

### DNP (Do Not Place) marking
- Comma-separated refdes list → orange transparent fill in the PDF
- Immediate preview overlay; baked into PDF on re-render
- DNP components do not receive a polarity marker
- DNP list saved to session and reloaded automatically

### GUI
- **Multi-language UI**: English / Deutsch / Magyar (language selector in the toolbar)
- Threaded analysis (UI stays responsive during processing)
- Results table with search filter and status color highlights
- Session auto-save and restore (JSON sidecar files)
- PDF preview with pan/zoom, overlay, multi-selection, fullscreen support
- Re-render: regenerate PDF with updated corrections/DNP without re-running full analysis

---

## Quick Start

```bash
pip install -r requirements.txt
python app.py          # Launch GUI
```

---

## Project Structure

```
PolarityMarkTool/
├── app.py                         # entry point
├── main.py                        # delegates to app.py
├── requirements.txt
│
├── core/
│   ├── odb_parser.py              # ODB++ archive parsing, component data model
│   ├── odb_renderer.py            # ODB++ → PDF rendering with OCG layers
│   ├── odb_registration.py        # ODB++ → PDF coordinate registration
│   ├── odb_silk_cathode.py        # cathode detection from silk layer
│   ├── pdf_parser.py              # PDF text + vector shape extraction
│   ├── dxf_parser.py              # DXF file parsing
│   ├── component_detector.py      # regex-based refdes detection
│   ├── component_shape_assign.py  # shape assignment to components
│   ├── polarity_detector.py       # heuristic polarity detection rules
│   ├── pad_asymmetry_detector.py  # pad asymmetry-based pin-1 detector
│   ├── image_polarity_detector.py # OpenCV raster detector
│   ├── matcher.py                 # marker → component spatial matching
│   └── exporter.py                # JSON export + annotated PDF + PNG preview
│
├── gui/
│   ├── main_window.py             # PySide6 main window + worker threads
│   ├── pdf_preview.py             # embedded PDF preview widget
│   └── correction_dialog.py       # correction dialog (single and bulk mode)
│
└── utils/
    ├── config.py                  # tunable threshold values
    ├── geometry.py                # BoundingBox, Point, polygon helper functions
    └── translations.py            # UI string translations (EN / DE / HU)
```

---

## Workflow

```
ODB++ (.tgz / .zip / directory)
  → ODBParser       – extract component placement + pin data
  → ODBRenderer     – render PDF with OCG layers:
       ├─ Board outline
       ├─ Fab / Silkscreen / Courtyard / Notes / RefDes  (toggleable)
       ├─ Polarity markers  (green D-shape or filled circle at pin-1)
       └─ DNP markers  (orange transparent rectangle)
  → PDF preview + Results table + JSON export
```

---

## GUI Elements

### Input panel

| Field | Description |
|-------|-------------|
| **ODB++** | Source archive (`.tgz` / `.zip`) or extracted directory |
| **DNP** | Comma-separated refdes list of unpopulated components (e.g. `R5, C3, D12`) |

### Layer toggles

| Toggle | Layer description |
|--------|-------------------|
| **Fab** | Fab/assembly layer — component body outlines (~7000+ lines, slower) |
| **Silkscreen** | Silkscreen layer — labels and polarity symbols |
| **Courtyard** | Courtyard layer — component boundary outlines (few lines, fast) |
| **Notes/User Drawing** | Notes layer — often contains the title block and border |
| **Title block** | Drawing frame & title block (expands page to full frame) |
| **RefDes** | Reference designator labels (U1, C3, D5 …) |

### Buttons

| Button | Function |
|--------|----------|
| **🔍 Analyze** | Run ODB++ analysis and render PDF (threaded) |
| **🔄 Re-render** | Regenerate PDF with current corrections and DNP list (fast, no re-analysis) |
| **💾 Export JSON** | Save analysis result as JSON |
| **🔍 Open PDF Preview…** | Open the rendered PDF in a floating, resizable preview window |

### Language selector

Dropdown in the toolbar: **English** / **Deutsch** / **Magyar**  
Switches all static UI labels, buttons, tooltips, and status messages instantly.

---

## PDF Preview

### Mouse controls

| Gesture | Effect |
|---------|--------|
| Left drag | Pan (scroll) |
| **Ctrl + left drag** | Rubber-band area selection |
| Click on component | Select component |
| **Ctrl + click** | Toggle component in selection |
| Click on empty area | Clear selection |
| Double-click on component | Open correction dialog |
| Right-click | Context menu (Accept / Flip & Accept / Edit / Clear correction) |
| Ctrl + Scroll | Zoom (anchored to cursor position) |
| Scroll | Vertical scroll |
| Shift + Scroll | Horizontal scroll |
| F11 | Toggle fullscreen |

### Overlay indicators

| Indicator | Meaning |
|-----------|---------|
| 🟡 Yellow ring | Selected component |
| 🔵 Blue ring + ✎ | Manual correction saved (re-render to bake into PDF) |
| 🟠 Orange dot | DNP component |

### "Markers" checkbox (optional dot overlay)

| Color | Status |
|-------|--------|
| 🟢 Green | Polarity marker found (`marked`) |
| 🔴 Red | No polarity marker (`unmarked`) |
| 🟡 Amber | Ambiguous result |
| 🟣 Purple | Needs review (60% confidence — Accept or Flip & Accept) |
| 🔵 Blue | Manually corrected |

---

## needs_review status

Components where the cathode/pin-1 was detected with ~60% confidence are flagged as **needs_review** (purple highlight).

Quick resolution via right-click context menu:
- **✓ Accept current pin** — confirm the auto-detected pin as correct
- **↔ Flip & Accept** — mark the opposite pin and confirm

Accepting converts the status to `marked` and enables re-render to bake the final green marker into the PDF.

---

## Bulk Correction

1. **Ctrl+drag** on the preview (rubber-band) — or **Ctrl+click** individual components
2. Selected components are highlighted in the Results table (bidirectional sync)
3. **Right-click → "Edit correction for N components…"**
4. If all selected components share a value → pre-filled; mixed values → empty
5. **OK** → correction applied to all selected components

---

## Manual Corrections

All corrections are automatically saved next to the ODB++ file (`.corrections.json`).

| Setting | Effect |
|---------|--------|
| **Auto** | Use ODB++ detection result |
| **Force polar** | Always draw a polarity marker |
| **Force non-polar** | Never draw a polarity marker |
| **Flip pin** | Mark the opposite pin (pin1 ↔ pin2) |
| **Note** | Free-text comment (single component mode only) |

---

## Session Saving

On every analysis the application saves sidecar files next to the ODB++ source:

| File | Contents |
|------|----------|
| `*.session.json` | Last rendered PDF path, layer settings, corrections, DNP list, component positions |
| `*_polarity.json` | Analysis results (reloaded on next open without re-analysis) |
| `*.corrections.json` | Manual corrections |

On next open the PDF preview, results table, corrections, and DNP list are all restored automatically.

---

## JSON Output Format

```json
{
  "tool": "PolarityMark",
  "summary": { "total_components": 77, "marked": 26, "unmarked": 51 },
  "components": [
    {
      "reference": "D3",
      "type": "diode",
      "polarity_status": "marked",
      "confidence": 0.99,
      "position": { "x": 212.4, "y": 318.7 },
      "markers": [{ "type": "odb", "confidence": 0.99, "source": "odb" }]
    }
  ]
}
```

---

## Dependencies

```
PyMuPDF >= 1.23
PySide6 >= 6.6
opencv-python-headless
ezdxf
regex
numpy
```

Full version pinning: `requirements.txt`
