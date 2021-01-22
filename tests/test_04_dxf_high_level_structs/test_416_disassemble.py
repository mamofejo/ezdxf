#  Copyright (c) 2021, Manfred Moitzi
#  License: MIT License

import pytest
from ezdxf.math import Vec3
from ezdxf import disassemble
from ezdxf.entities import factory


def test_do_nothing():
    assert list(disassemble.recursive_decompose([])) == []
    assert list(disassemble.to_primitives([])) == []
    assert list(disassemble.to_vertices([])) == []


def test_convert_unsupported_entity_to_primitive():
    p = disassemble.make_primitive(factory.new('WIPEOUT'))
    assert p.path is None
    assert p.mesh is None
    assert list(p.vertices()) == []


def test_multiple_unsupported_entities_to_vertices():
    w = factory.new('WIPEOUT')
    primitives = list(disassemble.to_primitives([w, w, w]))
    assert len(primitives) == 3, "3 empty primitives expected"
    vertices = list(disassemble.to_vertices(primitives))
    assert len(vertices) == 0, "no vertices expected"


def test_point_to_primitive():
    e = factory.new('POINT', dxfattribs={'location': (1, 2, 3)})
    p = disassemble.make_primitive(e)
    assert p.path is not None
    assert p.mesh is None
    assert list(p.vertices()) == [(1, 2, 3)]


def test_line_to_primitive():
    start = Vec3(1, 2, 3)
    end = Vec3(4, 5, 6)
    e = factory.new('LINE', dxfattribs={'start': start, 'end': end})
    p = disassemble.make_primitive(e)
    assert p.path is not None
    assert p.mesh is None
    assert list(p.vertices()) == [start, end]


def test_lwpolyline_to_primitive():
    p1 = Vec3(1, 1)
    p2 = Vec3(2, 2)
    p3 = Vec3(2, 2)
    e = factory.new('LWPOLYLINE')
    e.append_points([p1, p2, p3], format="xy")
    p = disassemble.make_primitive(e)
    assert p.path is not None
    assert p.mesh is None
    assert list(p.vertices()) == [p1, p2, p3]


def test_circle_to_primitive():
    e = factory.new('CIRCLE', dxfattribs={'radius': 5})
    p = disassemble.make_primitive(e)
    assert p.path is not None
    assert p.mesh is None
    assert len(list(p.vertices())) > 32


def test_arc_to_primitive():
    e = factory.new('ARC', dxfattribs={'radius': 5})
    p = disassemble.make_primitive(e)
    assert p.path is not None
    assert p.mesh is None
    assert len(list(p.vertices())) > 32


def test_ellipse_to_primitive():
    e = factory.new('ELLIPSE', dxfattribs={'major_axis': (5, 0)})
    p = disassemble.make_primitive(e)
    assert p.path is not None
    assert p.mesh is None
    assert len(list(p.vertices())) > 32


def test_spline_to_primitive():
    e = factory.new('SPLINE')
    e.control_points = [(0, 0), (3, 2), (6, -2), (9, 4)]
    p = disassemble.make_primitive(e)
    assert p.path is not None
    assert p.mesh is None
    assert len(list(p.vertices())) > 20
    assert len(list(p.path.flattening(0.01))) > 20


def test_mesh_entity_to_primitve():
    from ezdxf.layouts import VirtualLayout
    from ezdxf.render.forms import cube
    vl = VirtualLayout()
    mesh_entity = cube().render(vl)
    assert mesh_entity.dxftype() == "MESH"

    p = disassemble.make_primitive(mesh_entity)
    assert p.path is None
    mesh_builder = p.mesh
    assert mesh_builder is not None

    assert len(mesh_builder.vertices) == 8
    assert len(mesh_builder.faces) == 6
    assert len(list(p.vertices())) == 8


@pytest.mark.parametrize('dxftype', ['SOLID', 'TRACE', '3DFACE'])
def test_from_quadrilateral_with_4_points(dxftype):
    entity = factory.new(dxftype)
    entity.dxf.vtx0 = (0, 0, 0)
    entity.dxf.vtx1 = (1, 0, 0)
    entity.dxf.vtx2 = (1, 1, 0)
    entity.dxf.vtx3 = (0, 1, 0)
    p = disassemble.make_primitive(entity)
    assert p.path is not None
    assert p.mesh is None
    assert len(list(p.vertices())) == 5, "Expected closed path"


if __name__ == '__main__':
    pytest.main([__file__])
