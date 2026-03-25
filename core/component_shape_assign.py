"""
Assigns vector shapes to detected components based on bounding box overlap or proximity.
"""
from typing import List, Dict
from core.component_detector import Component
from core.pdf_parser import VectorShape
from utils.geometry import BoundingBox

def assign_shapes_to_components(components: List[Component], shapes: List[VectorShape], margin: float = 2.0) -> Dict[Component, List[VectorShape]]:
    """
    For each component, find all VectorShapes whose bounding box overlaps or is within a margin of the component's bbox.
    Returns a dict: {Component: [VectorShape, ...]}
    """
    result: Dict[Component, List[VectorShape]] = {}
    for comp in components:
        comp_bbox = comp.bbox.expand(margin)
        relevant_shapes = [
            s for s in shapes
            if s.page == comp.page and (comp_bbox.overlaps(s.bbox) or comp_bbox.contains_bbox(s.bbox))
        ]
        result[comp] = relevant_shapes
    return result

