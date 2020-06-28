# Created: 06.2020
# Copyright (c) 2020, Matthew Broadway
# License: MIT License
import copy
import math
from math import radians
from typing import Iterable, cast, Union, List, Callable

from ezdxf.addons.drawing.backend_interface import DrawingBackend
from ezdxf.addons.drawing.properties import RenderContext, VIEWPORT_COLOR, Properties
from ezdxf.addons.drawing.text import simplified_text_chunks
from ezdxf.addons.drawing.utils import normalize_angle, get_tri_or_quad_points, get_draw_angles
from ezdxf.entities import DXFGraphic, Insert, MText, Polyline, LWPolyline, Face3d, Solid, Trace, \
    Spline, Hatch, Attrib, Text, Ellipse, Polyface
from ezdxf.entities.dxfentity import DXFTagStorage
from ezdxf.layouts import Layout
from ezdxf.math import Vector, Z_AXIS, ConstructionEllipse, linspace, OCS
from ezdxf.render import MeshBuilder

__all__ = ['Frontend']
NEG_Z_AXIS = -Z_AXIS
INFINITE_LINE_LENGTH = 25

COMPOSITE_ENTITY_TYPES = {
    # Unsupported types, represented as DXFTagStorage(), will sorted out in Frontend.draw_entities().
    'INSERT', 'POLYLINE', 'LWPOLYLINE',
    # This types have a virtual_entities() method, which returns the content of the associated anonymous block
    'DIMENSION', 'ARC_DIMENSION', 'LARGE_RADIAL_DIMENSION', 'ACAD_TABLE',
}


class Frontend:
    """
    Drawing frontend, responsible for decomposing entities into graphic primitives and resolving entity properties.

    Args:
        ctx: actual render context of a DXF document
        out: backend
        visibility_filter: callback to override entity visibility, signature is ``func(entity: DXFGraphic) -> bool``,
                           entity enters processing pipeline if this function returns ``True``, independent
                           from visibility state stored in DXF properties or layer visibility.
                           Entity property `is_visible` is updated, but the backend can still ignore this decision
                           and check the visibility of the DXF entity by itself.

    """

    def __init__(self, ctx: RenderContext, out: DrawingBackend, visibility_filter: Callable[[DXFGraphic], bool] = None):
        self.ctx = ctx
        self.out = out
        self.visibility_filter = visibility_filter
        # approximate a full circle by `n` segments, arcs have proportional less segments
        self.circle_resolution = 100
        # The sagitta (also known as the versine) is a line segment drawn perpendicular to a chord, between the
        # midpoint of that chord and the arc of the circle. https://en.wikipedia.org/wiki/Circle
        # not used yet!
        self.approximation_max_sagitta = 0.01
        # approximate splines by `n` segments
        self.spline_resolution = 100

    def draw_layout(self, layout: 'Layout', finalize: bool = True) -> None:
        self.draw_entities(layout)
        self.out.set_background(self.ctx.current_layout.background_color)
        if finalize:
            self.out.finalize()

    def draw_entities(self, entities: Iterable[DXFGraphic]) -> None:
        for entity in entities:
            if isinstance(entity, DXFTagStorage):
                print(f'ignoring unsupported DXF entity: {str(entity)}')
                # unsupported DXF entity, just tag storage to preserve data
                continue
            if self.visibility_filter:
                # visibility depends only on filter result
                if self.visibility_filter(entity):
                    self.draw_entity(entity, [])
            # visibility depends only from DXF properties and layer state
            elif self.ctx.is_visible(entity):
                self.draw_entity(entity, [])

    def draw_entity(self, entity: DXFGraphic, parent_stack: List[DXFGraphic]) -> None:
        dxftype = entity.dxftype()
        self.out.set_current_entity(entity, tuple(parent_stack))
        if dxftype in {'LINE', 'XLINE', 'RAY'}:
            self.draw_line_entity(entity)
        elif dxftype in {'TEXT', 'MTEXT', 'ATTRIB'}:
            if is_spatial(Vector(entity.dxf.extrusion)):
                self.draw_text_entity_3d(entity)
            else:
                self.draw_text_entity_2d(entity)
        elif dxftype in {'CIRCLE', 'ARC', 'ELLIPSE'}:
            if is_spatial(Vector(entity.dxf.extrusion)):
                self.draw_elliptic_arc_entity_3d(entity)
            else:
                self.draw_elliptic_arc_entity_2d(entity)
        elif dxftype == 'SPLINE':
            self.draw_spline_entity(entity)
        elif dxftype == 'POINT':
            self.draw_point_entity(entity)
        elif dxftype == 'HATCH':
            self.draw_hatch_entity(entity)
        elif dxftype == 'MESH':
            self.draw_mesh_entity(entity)
        elif dxftype in {'3DFACE', 'SOLID', 'TRACE'}:
            self.draw_solid_entity(entity)
        elif dxftype in {'POLYLINE', 'LWPOLYLINE'}:
            self.draw_polyline_entity(entity, parent_stack)
        elif dxftype in COMPOSITE_ENTITY_TYPES:
            self.draw_composite_entity(entity, parent_stack)
        elif dxftype == 'VIEWPORT':
            self.draw_viewport_entity(entity)
        else:
            self.out.ignored_entity(entity)
        self.out.set_current_entity(None)

    def draw_line_entity(self, entity: DXFGraphic) -> None:
        d, dxftype = entity.dxf, entity.dxftype()
        properties = self._resolve_properties(entity)
        if dxftype == 'LINE':
            self.out.draw_line(d.start, d.end, properties)

        elif dxftype in ('XLINE', 'RAY'):
            start = d.start
            delta = Vector(d.unit_vector.x, d.unit_vector.y, 0) * INFINITE_LINE_LENGTH
            if dxftype == 'XLINE':
                self.out.draw_line(start - delta / 2, start + delta / 2, properties)
            elif dxftype == 'RAY':
                self.out.draw_line(start, start + delta, properties)
        else:
            raise TypeError(dxftype)

    def _resolve_properties(self, entity: DXFGraphic) -> Properties:
        properties = self.ctx.resolve_all(entity)
        if self.visibility_filter:  # override visibility by callback
            properties.is_visible = self.visibility_filter(entity)
        return properties

    def draw_text_entity_2d(self, entity: DXFGraphic) -> None:
        d, dxftype = entity.dxf, entity.dxftype()
        properties = self._resolve_properties(entity)
        if dxftype in ('TEXT', 'MTEXT', 'ATTRIB'):
            entity = cast(Union[Text, MText, Attrib], entity)
            for line, transform, cap_height in simplified_text_chunks(entity, self.out):
                self.out.draw_text(line, transform, properties, cap_height)
        else:
            raise TypeError(dxftype)

    def draw_text_entity_3d(self, entity: DXFGraphic) -> None:
        return  # not supported

    def draw_elliptic_arc_entity_3d(self, entity: DXFGraphic) -> None:
        dxf, dxftype = entity.dxf, entity.dxftype()
        properties = self._resolve_properties(entity)

        if dxftype in {'CIRCLE', 'ARC'}:
            center = dxf.center  # ocs transformation in .from_arc()
            radius = dxf.radius
            if dxftype == 'CIRCLE':
                start_angle = 0
                end_angle = 360
            else:
                start_angle = dxf.start_angle
                end_angle = dxf.end_angle
            e = ConstructionEllipse.from_arc(center, radius, dxf.extrusion, start_angle, end_angle)
        elif dxftype == 'ELLIPSE':
            e = cast(Ellipse, entity).construction_tool()
        else:
            raise TypeError(dxftype)

        # Approximate as 3D polyline
        segments = int((e.end_param - e.start_param) / math.tau * self.circle_resolution)
        points = list(e.vertices(linspace(e.start_param, e.end_param, max(4, segments + 1))))
        self.out.start_polyline()
        for a, b in zip(points, points[1:]):
            self.out.draw_line(a, b, properties)
        self.out.end_polyline()

    def draw_elliptic_arc_entity_2d(self, entity: DXFGraphic) -> None:
        dxf, dxftype = entity.dxf, entity.dxftype()
        properties = self._resolve_properties(entity)
        if dxftype == 'CIRCLE':
            center = _get_arc_wcs_center(entity)
            diameter = 2 * dxf.radius
            self.out.draw_arc(center, diameter, diameter, 0, None, properties)

        elif dxftype == 'ARC':
            center = _get_arc_wcs_center(entity)
            diameter = 2 * dxf.radius
            draw_angles = get_draw_angles(radians(dxf.start_angle), radians(dxf.end_angle), Vector(dxf.extrusion))
            self.out.draw_arc(center, diameter, diameter, 0, draw_angles, properties)

        elif dxftype == 'ELLIPSE':
            # 'param' angles are anticlockwise around the extrusion vector
            # 'param' angles are relative to the major axis angle
            # major axis angle always anticlockwise in global frame
            major_axis_angle = normalize_angle(math.atan2(dxf.major_axis.y, dxf.major_axis.x))
            width = 2 * dxf.major_axis.magnitude
            height = dxf.ratio * width  # ratio == height / width
            draw_angles = get_draw_angles(dxf.start_param, dxf.end_param, Vector(dxf.extrusion))
            self.out.draw_arc(dxf.center, width, height, major_axis_angle, draw_angles, properties)
        else:
            raise TypeError(dxftype)

    def draw_spline_entity(self, entity: DXFGraphic) -> None:
        properties = self._resolve_properties(entity)
        spline = cast(Spline, entity).construction_tool()
        if self.out.has_spline_support:
            self.out.draw_spline(spline, properties)
        else:
            points = list(spline.approximate(segments=self.spline_resolution))
            self.out.start_polyline()
            for a, b in zip(points, points[1:]):
                self.out.draw_line(a, b, properties)
            self.out.end_polyline()

    def draw_point_entity(self, entity: DXFGraphic) -> None:
        properties = self._resolve_properties(entity)
        self.out.draw_point(entity.dxf.location, properties)

    def draw_solid_entity(self, entity: DXFGraphic) -> None:
        dxf, dxftype = entity.dxf, entity.dxftype()
        properties = self._resolve_properties(entity)
        # TRACE is the same thing as SOLID according to the documentation
        # https://ezdxf.readthedocs.io/en/stable/dxfentities/trace.html
        # except TRACE has OCS coordinates and SOLID has WCS coordinates.
        entity = cast(Union[Face3d, Solid, Trace], entity)
        points = get_tri_or_quad_points(entity)
        if dxftype == 'TRACE' and dxf.hasattr('extrusion'):
            ocs = entity.ocs()
            points = list(ocs.points_to_wcs(points))
        if dxftype in ('SOLID', 'TRACE'):
            self.out.draw_filled_polygon(points, properties)
        else:
            for a, b in zip(points, points[1:]):
                self.out.draw_line(a, b, properties)

    def draw_hatch_entity(self, entity: DXFGraphic) -> None:
        properties = self._resolve_properties(entity)
        entity = cast(Hatch, entity)
        ocs = entity.ocs()
        # all OCS coordinates have the same z-axis stored as vector (0, 0, z), default (0, 0, 0)
        elevation = entity.dxf.elevation.z
        paths = copy.deepcopy(entity.paths)
        paths.polyline_to_edge_path(just_with_bulge=False)
        paths.all_to_line_edges(spline_factor=10)
        for p in paths:
            assert p.PATH_TYPE == 'EdgePath'
            vertices = []
            last_vertex = None
            for e in p.edges:
                assert e.EDGE_TYPE == 'LineEdge'
                # WCS transformation is only done if the extrusion vector is != (0, 0, 1)
                # else to_wcs() returns just the input - no big speed penalty!
                v = ocs.to_wcs(Vector(e.start[0], e.start[1], elevation))
                if last_vertex is not None and not last_vertex.isclose(v):
                    print(f'warning: hatch edges not contiguous: {last_vertex} -> {e.start}, {e.end}')
                    vertices.append(last_vertex)
                vertices.append(v)
                last_vertex = ocs.to_wcs(Vector(e.end[0], e.end[1], elevation)).replace(z=0.0)
            if vertices:
                if last_vertex.isclose(vertices[0]):
                    vertices.append(last_vertex)
                self.out.draw_filled_polygon(vertices, properties)

    def draw_viewport_entity(self, entity: DXFGraphic) -> None:
        assert entity.dxftype() == 'VIEWPORT'
        dxf = entity.dxf
        view_vector: Vector = dxf.view_direction_vector
        mag = view_vector.magnitude
        if math.isclose(mag, 0.0):
            print('warning: viewport with null view vector')
            return
        view_vector /= mag
        if not math.isclose(view_vector.dot(Vector(0, 0, 1)), 1.0):
            print(f'cannot render viewport with non-perpendicular view direction: {dxf.view_direction_vector}')
            return

        cx, cy = dxf.center.x, dxf.center.y
        dx = dxf.width / 2
        dy = dxf.height / 2
        minx, miny = cx - dx, cy - dy
        maxx, maxy = cx + dx, cy + dy
        points = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
        self.out.draw_filled_polygon([Vector(x, y, 0) for x, y in points], VIEWPORT_COLOR)

    def draw_mesh_entity(self, entity: DXFGraphic) -> None:
        properties = self._resolve_properties(entity)
        builder = MeshBuilder.from_mesh(entity)
        self.draw_mesh_builder_entity(builder, properties)

    def draw_mesh_builder_entity(self, builder: MeshBuilder, properties: Properties) -> None:
        for face in builder.faces_as_vertices():
            # todo: draw 4 edges as mesh face?
            self.out.draw_filled_polygon(face, properties)

    def draw_polyline_entity(self, entity: DXFGraphic, parent_stack: List[DXFGraphic]):
        dxftype = entity.dxftype()

        if dxftype == 'POLYLINE':
            e = cast(Polyface, entity)
            if e.is_polygon_mesh or e.is_poly_face_mesh:
                self.draw_mesh_builder_entity(
                    MeshBuilder.from_polyface(e),
                    self._resolve_properties(entity),
                )
                return

        entity = cast(Union[LWPolyline, Polyline], entity)
        parent_stack.append(entity)
        self.out.set_current_entity(entity, tuple(parent_stack))
        # self.out.start_polyline() todo: virtual entities are not in correct order
        for child in entity.virtual_entities():
            self.draw_entity(child, parent_stack)
        parent_stack.pop()
        # self.out.end_polyline()
        self.out.set_current_entity(None)

    def draw_composite_entity(self, entity: DXFGraphic, parent_stack: List[DXFGraphic]) -> None:
        dxftype = entity.dxftype()
        if dxftype == 'INSERT':
            entity = cast(Insert, entity)
            self.ctx.push_state(self._resolve_properties(entity))
            parent_stack.append(entity)
            for attrib in entity.attribs:
                self.draw_entity(attrib, parent_stack)
            try:
                children = list(entity.virtual_entities())
            except Exception as e:
                print(f'Exception {type(e)}({e}) failed to get children of insert entity: {e}')
                return
            for child in children:
                self.draw_entity(child, parent_stack)
            parent_stack.pop()
            self.ctx.pop_state()

        # DIMENSION, ARC_DIMENSION, LARGE_RADIAL_DIMENSION and ACAD_TABLE
        # All these entities have an associated anonymous geometry block.
        elif hasattr(entity, 'virtual_entities'):
            children = []
            try:
                for child in entity.virtual_entities():
                    child.transparency = 0.0  # todo: defaults to 1.0 (fully transparent)???
                    children.append(child)
            except Exception as e:
                print(f'Exception {type(e)}({e}) failed to get children of entity: {str(entity)}')
                return

            parent_stack.append(entity)
            for child in children:
                self.draw_entity(child, parent_stack)
            parent_stack.pop()

        else:
            raise TypeError(dxftype)


def _get_arc_wcs_center(arc: DXFGraphic) -> Vector:
    """ Returns the center of an ARC or CIRCLE as WCS coordinates. """
    center = arc.dxf.center
    if arc.dxf.hasattr('extrusion'):
        ocs = arc.ocs()
        return ocs.to_wcs(center)
    else:
        return center


def is_spatial(v: Vector) -> bool:
    return not v.isclose(Z_AXIS) and not v.isclose(NEG_Z_AXIS)
