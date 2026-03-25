# PolarityMark – PCB Polarity Marker Detector

A Python desktop tool that analyzes **vector-based PCB PDF drawings** and automatically
detects polarity markers of SMT components, without any image processing.

---

## Features

- Parses vector PDF data (text + drawing paths) with PyMuPDF
- Regex-based component reference designator detection
- 8 heuristic polarity-marker detection rules
- Spatial matching of markers to nearest component
- JSON export with full coordinate data
- Annotated PDF output — markers drawn as proper PDF annotations visible **above** any raster overlays
- PNG preview of the annotated first page
- Clean PySide6 GUI with threaded analysis and results table

---

## Quick Start

```bash
pip install -r requirements.txt
python main.py          # launches the GUI
```

---

## Project Structure

```
PolarityMarkTool/
├── app.py                    # GUI entry point
├── main.py                   # delegates to app.py
├── requirements.txt
│
├── core/
│   ├── pdf_parser.py         # PDF text + vector-shape extraction (PyMuPDF)
│   ├── component_detector.py # regex-based ref-des detection
│   ├── polarity_detector.py  # 8 heuristic polarity-marker rules
│   ├── matcher.py            # spatial marker → component assignment
│   └── exporter.py           # JSON + annotated-PDF + PNG export
│
├── gui/
│   └── main_window.py        # PySide6 main window + threaded worker
│
└── utils/
    ├── config.py             # all tunable thresholds in one place
    └── geometry.py           # BoundingBox, Point, polygon helpers
```

---

## Detection Pipeline

```
PDF  →  PDFParser  →  ComponentDetector  →  PolarityDetector  →  Matcher  →  Exporter
          text            regex                  8 rules           spatial     JSON
          shapes          ref-des                                  matching    PDF / PNG
```

### Polarity-marker rules

| Rule | Shape type | Confidence |
|------|-----------|------------|
| `plus_text` | Standalone "+" text ≤5 pt | 0.95 |
| `minus_text` | Standalone "−" text ≤3.5 pt | 0.75 |
| `anode_text` | Standalone "A" text ≤3.5 pt | 0.65 |
| `cathode_text` | Standalone "K" text ≤3.5 pt | 0.65 |
| `filled_dot` | Small filled / near-black-outline circle | 0.90 / 0.70 |
| `cathode_band` | Narrow filled rectangle (≥8 pt long) | 0.85 |
| `thick_line` | Short stroke with width ≥0.9 pt | 0.75 |
| `triangle` | Closed 3-vertex polygon | 0.80 |
| `cross_vector` | Two perpendicular line segments | 0.88 |
| `corner_rect` | Small filled rectangle / square pad (pin-1) | 0.82 |

A component is `marked` when its best marker confidence ≥ 0.75.

---

## KiCad PDF Export – Debug Findings & Fixes

These bugs caused **zero visible annotations** in the original output.
All have been fixed.

| Bug | Root Cause | Fix Applied |
|-----|-----------|-------------|
| Annotations invisible | Content-stream drawings render **under** Stamp image annotations in the original PDF | Rewrote exporter to use only PDF annotation objects (Circle/Line/Square/FreeText) — these render **above** stamps |
| Bezier circles not classified | KiCad exports circles with `closePath=False`; classifier required `True` | Removed `closePath` requirement; require ≥4 bezier items + square bbox |
| `thick_line` never fired | `thick_line_min_stroke=1.8` but KiCad max stroke is 1.13 pt | Lowered to 0.9 pt |
| 8 false "A/K" polarity labels | Drawing-border grid letters at 4.9 pt matched `anode_text`/`cathode_text` | Added font-size ≤3.5 pt guard |
| 24 courtyard circle false positives | Cyan `(0,0.517,0.517)` and blue `(0,0,0.517)` circles passed gray filter | Tightened guard: non-filled circles must be near-black (`<0.20`) or near-white (`>0.80`) |
| 259 `cross_vector` false positives | PCB outline corner intersections satisfied loose arm/midpoint tolerances | Tightened `cross_arm_max_len` 3.0→2.0 pt, `cross_midpoint_tol` 0.3→0.15 pt, added min-arm guard |
| `corner_rect` too low confidence | `confidence=0.65` below `marked` threshold of 0.75 | Raised to 0.82 — square pads are the definitive KiCad pin-1 indicator |

---

## Configuration (`utils/config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_dot_radius` | 1.5 pt | Minimum radius for polarity dot |
| `max_dot_radius` | 12.0 pt | Maximum radius for polarity dot |
| `thick_line_min_stroke` | **0.9 pt** | Minimum stroke to be a cathode bar (was 1.8) |
| `band_min_length` | 8.0 pt | Minimum cathode-band length |
| `triangle_min_area` | 4.0 pt² | Minimum triangle area |
| `corner_rect_max_area` | 30.0 pt² | Maximum pin-1 rect area |
| `max_match_distance` | 80.0 pt | Max centre-to-centre distance for matching |
| `search_expand_margin` | 25.0 pt | Bbox expansion for marker search |
| `cross_arm_max_len` | **2.0 pt** | Max cross arm length (was 3.0) |
| `cross_arm_min_len` | **0.4 pt** | Min cross arm length (new guard) |
| `cross_midpoint_tol` | **0.15 pt** | Max midpoint distance for cross detection (was 0.3) |

---

## Output

### JSON (`*_polarity.json`)

```json
{
  "tool": "PolarityMark",
  "summary": { "total_components": 77, "marked": 26, "unmarked": 51, "ambiguous": 0 },
  "components": [
    {
      "reference": "C1",
      "type": "capacitor",
      "polarity_status": "marked",
      "confidence": 0.82,
      "position": { "x": 337.2, "y": 283.9 },
      "markers": [{ "type": "corner_rect", "confidence": 0.82, "source": "shape" }]
    }
  ]
}
```

### Annotated PDF visual legend

- 🟢 **Green circle + box** = polar component with detected marker (`marked`)
- 🔵 **Blue crosshair + box** = individual polarity marker location
- 🔴 **Red circle + box** = polar component with no marker found (`unmarked`)
- White legend box (lower-right corner) = summary of all marked components
- Hover over any colored rectangle in a PDF viewer to see component details

All annotations are proper **PDF annotation objects** (Circle, Line, Square, FreeText)
and render on top of any embedded raster-image overlays in the original PDF.

---

## Requirements

```
PyMuPDF>=1.23.0
PySide6>=6.6.0
shapely>=2.0.0
regex>=2023.0.0
```
