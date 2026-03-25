"""
utils/config.py
───────────────
Central configuration for all tunable detection thresholds in PolarityMark.
Edit these values to tune sensitivity without touching core logic.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Config:
    # ── Dot / filled-circle marker ─────────────────────────────────────────
    min_dot_radius: float = 1.5       # Minimum radius (pt) for a polarity dot
    max_dot_radius: float = 12.0      # Maximum radius (pt) for a polarity dot
    circularity_threshold: float = 0.70  # Minimum circularity score (0–1); 1 = perfect circle

    # ── Cathode bar / thick line ────────────────────────────────────────────
    # Note: KiCad PCB PDFs use very thin strokes (max ~1.13 pt on silkscreen).
    # The original default of 1.8 was too high for this type of export.
    thick_line_min_stroke: float = 0.9   # Minimum stroke width (pt) to call a line "thick"
    band_min_length: float = 8.0         # Minimum length (pt) for a filled band/bar
    band_max_width: float = 4.0          # Maximum narrow dimension (pt) for a band

    # ── Triangle marker ─────────────────────────────────────────────────────
    triangle_min_area: float = 4.0    # Minimum area (pt²) to count as a triangle
    triangle_max_aspect: float = 2.5  # Maximum aspect ratio (long/short side)

    # ── Corner rectangle / IC pin-1 ─────────────────────────────────────────
    corner_rect_max_area: float = 30.0  # Maximum area (pt²) for a corner mark rect

    # ── Matching ────────────────────────────────────────────────────────────
    max_match_distance: float = 80.0   # Max centre-to-centre distance (pt)
    search_expand_margin: float = 25.0 # Extra margin (pt) added to component bbox

    # ── Cross/plus vector-glyph detection ───────────────────────────────────
    # Tight thresholds are critical here: PCB drawings contain thousands of
    # short line segments that can accidentally form perpendicular pairs.
    # cross_arm_max_len  – maximum half-arm length; a genuine "+" polarity mark
    #                      at 1–2 pt font scale has arms much shorter than
    #                      component outline lines.
    # cross_midpoint_tol – how close the two midpoints must be; very tight to
    #                      avoid false positives from outline corner junctions.
    # cross_arm_min_len  – minimum arm length (filters degenerate zero-pt segs).
    cross_arm_max_len:  float = 2.0   # Max arm length (pt)  — was 3.0
    cross_arm_min_len:  float = 0.4   # Min arm length (pt)  — new guard
    cross_midpoint_tol: float = 0.15  # Midpoint tolerance (pt) — was 0.3

    # ── Silkscreen color filter ─────────────────────────────────────────────
    # Set to None to disable filtering (accept any color)
    silkscreen_stroke_colors: Optional[List[Optional[Tuple]]] = field(
        default_factory=lambda: [None, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)]
    )

    # ── Debug ───────────────────────────────────────────────────────────────
    debug: bool = False   # When True, annotated PDF includes ALL detected shapes


# Module-level singleton used as the default everywhere
DEFAULT_CONFIG = Config()

