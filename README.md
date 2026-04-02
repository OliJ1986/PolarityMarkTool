# PolarityMark – PCB Polarity Marker Tool

Desktop application for marking polarity on PCB drawings.  
Primary source: **ODB++ archive** → PDF with orange/green polarity markers.

---

## Features

### ODB++ processing (primary workflow)
- Supports `.tgz`, `.zip`, and directory formats
- PDF rendering via PyMuPDF with toggleable OCG layers (Board outline, Fab, Silkscreen, Courtyard, Notes, Reference labels, Polarity markers, DNP)
- Polarity markers: D-shaped (semicircle) for 2-pin SMD components; small circle for multi-pin components
- Cathode pin determination from net list name (diodes, LEDs)
- **Manual correction** per component: force polarity / flip pin / add note
- **DNP (Do Not Place) marking**: comma-separated refdes list → orange fill color in the PDF

### Heuristic PDF / DXF pipeline (secondary)
- Analysis of vector PDF and DXF files based on text + shapes
- 10 heuristic polarity detector rules
- Pad asymmetry and OpenCV raster detector (3rd pass)
- PDF + ODB++ combined mode: ODB++ registration into PDF coordinate system

### GUI
- Threaded analysis (UI stays responsive)
- Results table with search, status color highlights
- Session auto-save and reload (JSON sidecar files)
- PDF preview with pan/zoom, overlay, multi-selection
- Bulk correction for multiple components at once

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
├── app.py                       # entry point
├── main.py                      # delegates to app.py
├── requirements.txt
│
├── core/
│   ├── odb_parser.py            # ODB++ archive parsing, _ODBComponent data model
│   ├── odb_renderer.py          # ODB++ → PDF rendering with OCG layers
│   ├── odb_registration.py      # ODB++ → PDF coordinate registration
│   ├── pdf_parser.py            # PDF text + vector shape extraction
│   ├── dxf_parser.py            # DXF file parsing
│   ├── component_detector.py    # regex-based refdes detection
│   ├── component_shape_assign.py# shape assignment to components
│   ├── polarity_detector.py     # 10 heuristic rules
│   ├── pad_asymmetry_detector.py# pad asymmetry-based pin-1 detector
│   ├── image_polarity_detector.py # OpenCV raster detector (3rd pass)
│   ├── matcher.py               # marker → component spatial matching
│   └── exporter.py              # JSON export + annotated PDF + PNG
│
├── gui/
│   ├── main_window.py           # PySide6 main window + worker threads
│   ├── pdf_preview.py           # embedded PDF preview widget
│   └── correction_dialog.py     # correction dialog (single and bulk mode)
│
└── utils/
    ├── config.py                # tunable threshold values
    └── geometry.py              # BoundingBox, Point, polygon helper functions
```

---

## Workflows

### 1. ODB++ only (recommended)
```
ODB++ (.tgz/.zip/directory)
  → ODBParser      – extract component + pin data
  → render_odb_to_pdf
       ├─ Draw OCG layers (outline, silk, fab, courtyard, notes, refdes)
       ├─ Polarity markers (green D-shape or circle)
       └─ DNP markers (orange transparent rectangle)
  → PDF preview + JSON export
```

### 2. PDF only
```
PDF  →  PDFParser  →  ComponentDetector  →  PolarityDetector (10 rules)
      →  PadAsymmetryDetector  →  ImagePolarityDetector  →  Matcher  →  Exporter
```

### 3. PDF + ODB++ combined
ODB++-based pin-1 coordinates transformed into the PDF coordinate system;  
heuristic fallback for components not present in the ODB++.

---

## GUI Elements

### Input panel
| Field | Description |
|-------|-------------|
| **ODB++** | Source archive (.tgz / .zip) or extracted directory |
| **DNP** | Refdes list of unpopulated components (e.g. `R5, C3, D12`) — comma-separated |

### Layer toggles
`Fab` · `Silkscreen` · `Courtyard` · `Notes/User Drawing` · `RefDes`

### Buttons
| Button | Function |
|--------|----------|
| **🔍 Analyze** | Start analysis (threaded) |
| **🔄 Re-render** | Regenerate PDF with current corrections and DNP list |
| **💾 Export JSON** | Save result as JSON |

---

## PDF Preview

### Mouse controls
| Gesture | Effect |
|---------|--------|
| Left drag | Pan (scroll) |
| **Ctrl + left drag** | Rubber-band area selection |
| Click on component | Select (single component) |
| **Ctrl + click** | Toggle selection |
| Click on empty area | Clear selection |
| Double-click | Open correction dialog |
| Right-click | Context menu (correct / delete) |
| Ctrl + Scroll | Zoom (anchored to cursor position) |
| Scroll | Vertical scroll |
| Shift + Scroll | Horizontal scroll |

### Overlay indicators (always visible, no checkbox)
| Indicator | Description |
|-----------|-------------|
| 🟡 Yellow ring | Selected component |
| 🔵 Blue ring + ✎ | Manual correction saved (re-render required) |
| 🟠 Orange dot | DNP component |

### "Markers" checkbox (optional overlay)
| Color | Status |
|-------|--------|
| 🟢 Green | Polarity marker found (`marked`) |
| 🔴 Red | Polarity marker not found (`unmarked`) |
| 🟡 Amber | Uncertain (`ambiguous`) |
| 🔵 Blue | Manually corrected (`corrected`) |

---

## Bulk Correction

1. **Ctrl+drag** on the preview (rubber-band) — or Ctrl+click individual items
2. Selected components are also highlighted in the Results table (bidirectional sync)
3. **Right-click → "Edit correction for N components…"**
4. Bulk correction dialog: if all selected components share the same value → pre-filled; mixed values → empty
5. OK → the correction is applied to all selected components

---

## DNP Marking

Components entered in the **DNP** (Do Not Place) field:
- **Immediate preview**: an orange transparent circle appears on the preview
- **After re-render in the PDF**: orange transparent rectangle over the exact pin bounding box area, on a separate OCG layer (`DNP (Not Placed)`)
- **Do not receive a polarity marker**, even if they are configured as polar
- The DNP list is **saved to the session** and reloaded on next open

---

## Manual Corrections

All corrections are automatically saved next to the ODB++ file (`.corrections.json`).

| Setting | Effect |
|---------|--------|
| **Auto** | Based on ODB++ / heuristics |
| **Force polar** | Always draw a marker |
| **Force non-polar** | Never draw a marker |
| **Flip pin** | Mark the other pin (e.g. pin1↔pin2) |
| **Note** | Free-text note (single component mode only) |

---

## Session Saving

The application saves the following files next to the ODB++ file:

| File | Contents |
|------|----------|
| `*.session.json` | Last PDF, layer settings, corrections, DNP list, component positions |
| `*_polarity.json` | Analysis result (can be reloaded without re-analysis) |
| `*.corrections.json` | Manual corrections |

On next open, the PDF preview and results table are automatically restored.

---

## JSON Output

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
shapely >= 2.0
ezdxf
regex
numpy
```

Full version pinning: `requirements.txt`
