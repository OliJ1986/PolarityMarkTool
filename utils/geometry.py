"""
utils/geometry.py
─────────────────
Core geometry primitives for PolarityMark.

All coordinates are in PDF points (1 pt = 1/72 inch), using PyMuPDF's
top-left origin convention (y increases downward).
"""
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple
from shapely.geometry import MultiPoint


# ─────────────────────────────────────────────────────────────────────────────
# Primitive types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Point:
    """2-D point in PDF coordinate space."""
    x: float
    y: float

    def distance_to(self, other: "Point") -> float:
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)

    def midpoint(self, other: "Point") -> "Point":
        return Point((self.x + other.x) / 2, (self.y + other.y) / 2)

    def __iter__(self):
        yield self.x
        yield self.y

    def __eq__(self, other):
        if not isinstance(other, Point):
            return False
        return abs(self.x - other.x) < 1e-6 and abs(self.y - other.y) < 1e-6

    def __hash__(self):
        return hash((round(self.x, 4), round(self.y, 4)))


@dataclass
class BoundingBox:
    """Axis-aligned bounding box in PDF coordinate space."""
    x0: float
    y0: float
    x1: float
    y1: float

    # ── Factories ─────────────────────────────────────────────────────────
    @classmethod
    def from_tuple(cls, t: Tuple[float, float, float, float]) -> "BoundingBox":
        return cls(t[0], t[1], t[2], t[3])

    @classmethod
    def from_points(cls, points: List[Point]) -> "BoundingBox":
        if not points:
            raise ValueError("Cannot create BoundingBox from empty point list.")
        xs = [p.x for p in points]
        ys = [p.y for p in points]
        return cls(min(xs), min(ys), max(xs), max(ys))

    # ── Properties ────────────────────────────────────────────────────────
    @property
    def center(self) -> Point:
        return Point((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    @property
    def width(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def is_valid(self) -> bool:
        return self.x1 > self.x0 and self.y1 > self.y0

    # ── Spatial predicates ────────────────────────────────────────────────
    def overlaps(self, other: "BoundingBox") -> bool:
        """True if this bbox overlaps (intersects) another."""
        return (
            self.x0 < other.x1
            and self.x1 > other.x0
            and self.y0 < other.y1
            and self.y1 > other.y0
        )

    def contains_point(self, p: Point) -> bool:
        return self.x0 <= p.x <= self.x1 and self.y0 <= p.y <= self.y1

    def contains_bbox(self, other: "BoundingBox") -> bool:
        return (
            self.x0 <= other.x0
            and self.y0 <= other.y0
            and self.x1 >= other.x1
            and self.y1 >= other.y1
        )

    # ── Transformations ───────────────────────────────────────────────────
    def expand(self, margin: float) -> "BoundingBox":
        """Return a new bbox expanded by *margin* on all sides."""
        return BoundingBox(
            self.x0 - margin, self.y0 - margin,
            self.x1 + margin, self.y1 + margin,
        )

    def union(self, other: "BoundingBox") -> "BoundingBox":
        return BoundingBox(
            min(self.x0, other.x0), min(self.y0, other.y0),
            max(self.x1, other.x1), max(self.y1, other.y1),
        )

    def intersection(self, other: "BoundingBox") -> Optional["BoundingBox"]:
        x0 = max(self.x0, other.x0)
        y0 = max(self.y0, other.y0)
        x1 = min(self.x1, other.x1)
        y1 = min(self.y1, other.y1)
        if x1 > x0 and y1 > y0:
            return BoundingBox(x0, y0, x1, y1)
        return None

    # ── Distance helpers ──────────────────────────────────────────────────
    def distance_to_point(self, p: Point) -> float:
        """Euclidean distance from the bbox centre to *p*."""
        return self.center.distance_to(p)

    def distance_to_bbox(self, other: "BoundingBox") -> float:
        """Centre-to-centre Euclidean distance between two bboxes."""
        return self.center.distance_to(other.center)

    # ── Serialisation ─────────────────────────────────────────────────────
    def to_tuple(self) -> Tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    def to_dict(self) -> dict:
        return {"x0": round(self.x0, 2), "y0": round(self.y0, 2),
                "x1": round(self.x1, 2), "y1": round(self.y1, 2)}


# ─────────────────────────────────────────────────────────────────────────────
# Geometric utility functions
# ─────────────────────────────────────────────────────────────────────────────

def polygon_area(points: List[Point]) -> float:
    """Compute the area of a polygon using the Shoelace formula."""
    n = len(points)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += points[i].x * points[j].y
        area -= points[j].x * points[i].y
    return abs(area) / 2.0


def circularity(area: float, perimeter: float) -> float:
    """
    Isoperimetric ratio: 4π·A / P².
    Returns 1.0 for a perfect circle, <1 for elongated or jagged shapes.
    Returns 0.0 if perimeter is 0.
    """
    if perimeter <= 0:
        return 0.0
    return (4.0 * math.pi * area) / (perimeter ** 2)


def polyline_perimeter(points: List[Point]) -> float:
    """Total length of a polyline (open or closed chain of points)."""
    if len(points) < 2:
        return 0.0
    total = 0.0
    for i in range(len(points) - 1):
        total += points[i].distance_to(points[i + 1])
    # Close the loop
    total += points[-1].distance_to(points[0])
    return total


def deduplicate_points(points: List[Point], tolerance: float = 0.1) -> List[Point]:
    """Remove consecutive duplicate points within *tolerance*."""
    if not points:
        return []
    result = [points[0]]
    for p in points[1:]:
        if p.distance_to(result[-1]) > tolerance:
            result.append(p)
    return result


def is_approximately_square(bbox: BoundingBox, tolerance: float = 0.30) -> bool:
    """True if width/height ratio is within *tolerance* of 1.0."""
    if bbox.height == 0 or bbox.width == 0:
        return False
    ratio = bbox.width / bbox.height
    return abs(1.0 - ratio) < tolerance


def bbox_from_fitz_rect(rect) -> BoundingBox:
    """Convert a fitz.Rect (or fitz.IRect) to a BoundingBox."""
    return BoundingBox(rect.x0, rect.y0, rect.x1, rect.y1)


# Convex hull calculation using shapely

def convex_hull(points: List[Point]) -> List[Point]:
    """
    Returns the convex hull of the input points as a list of Point objects (in order).
    If less than 3 points, returns the input as-is.
    """
    if len(points) < 3:
        return points[:]
    mp = MultiPoint([(p.x, p.y) for p in points])
    hull = mp.convex_hull
    if hull.geom_type == 'Polygon':
        return [Point(x, y) for x, y in hull.exterior.coords[:-1]]  # skip closing point
    elif hull.geom_type == 'LineString':
        return [Point(x, y) for x, y in hull.coords]
    else:
        return points[:]
