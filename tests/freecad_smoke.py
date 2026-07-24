# SPDX-License-Identifier: LGPL-2.1-or-later
"""Headless integration smoke test executed by FreeCAD's bundled Python."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import FreeCAD as App
import Part

from freecad.QetRouting.document import import_project
from freecad.QetRouting.commands import _selection_world_point
from freecad.QetRouting.qet import parse_qet, parse_qet_bytes
from freecad.QetRouting.routing import RoutingError
from freecad.QetRouting.routing_document import (
    create_corridor,
    create_corridor_from_points,
    create_wire_schedule,
    route_wires,
)


def _near(vector, expected, tolerance=1e-7):
    return all(abs(actual - wanted) <= tolerance for actual, wanted in zip(vector, expected))


fixture = Path(__file__).parent / "fixtures" / "current.qet"
parsed = parse_qet(fixture)

document = App.newDocument("QetRoutingSmoke")
k1 = document.addObject("Part::Feature", "K1")
k1.Label = "K1"
k1.Shape = Part.makeBox(20, 10, 10)

k2 = document.addObject("Part::Feature", "K2")
k2.Label = "K2"
k2.Shape = Part.makeBox(20, 10, 10)
k2.Placement.Base = App.Vector(100, 0, 0)

# Assembly::JointGroup is a shape-less, placement-less document container.
# Even with a colliding label it must be ignored by physical-target matching.
assembly = document.addObject("Assembly::AssemblyObject", "Assembly")
joint_group = assembly.newObject("Assembly::JointGroup", "Joints")
joint_group.Label = "K1"
assert not hasattr(joint_group, "Placement")
assert not hasattr(joint_group, "getGlobalPlacement")
document.recompute()

vertex = k2.getSubObject("Vertex1")
picked_world = _selection_world_point(
    SimpleNamespace(Object=k2, SubObjects=[vertex])
)
expected_world = vertex.Point
assert _near(picked_world, expected_world)

summary = import_project(document, parsed.project)
assert summary.element_count == 2, summary
assert summary.matched_count == 2, summary
assert summary.ambiguous_count == 0, summary
assert summary.terminal_count == 4, summary
assert summary.routeable_conductor_count == 1, summary
assert "QET_Manufacturer" not in joint_group.PropertiesList
bindings = {
    item.QETLabel: item
    for item in document.Objects
    if getattr(item, "QET_ObjectKind", "") == "DeviceBinding"
}
assert bindings["K1"].Target is k1
assert list(bindings["K1"].MatchCandidates) == [k1.Name]
assert k1.QET_Manufacturer == "ACME"
assert k1.QET_ArticleNumber == "RX-2"
assert k1.QET_OrderNumber == "ORDER-42"
assert k1.QET_InternalNumber == "INT-K1"

device_types = document.getObject("QETDeviceTypes")
assert device_types is not None
assert len(device_types.Group) == 1
device_type = device_types.Group[0]
pin_definitions = {
    item.PinKey: item
    for item in device_type.Group
    if getattr(item, "QET_ObjectKind", "") == "PinDefinition"
}
assert set(pin_definitions) == {"A1", "A2"}

terminals = {
    item.Label: item
    for item in document.Objects
    if getattr(item, "QET_ObjectKind", "") == "TerminalInstance"
}
assert set(terminals) == {"K1.A1", "K1.A2", "K2.A1", "K2.A2"}
assert terminals["K1.A1"].Definition is pin_definitions["A1"]
assert terminals["K2.A1"].Definition is pin_definitions["A1"]
assert _near(terminals["K1.A1"].WorldPosition, (10, 5, 5))
assert _near(terminals["K2.A1"].WorldPosition, (110, 5, 5))

# Version 0.1.0 used scope-limited links. Reimport must migrate them without
# replacing the generated binding or terminal objects.
bindings["K1"].removeProperty("Target")
bindings["K1"].addProperty("App::PropertyLink", "Target", "QET Device Binding")
bindings["K1"].Target = k1
terminals["K1.A1"].removeProperty("Owner")
terminals["K1.A1"].addProperty("App::PropertyLink", "Owner", "QET Terminal")
terminals["K1.A1"].Owner = k1
assert bindings["K1"].getTypeIdOfProperty("Target") == "App::PropertyLink"
assert terminals["K1.A1"].getTypeIdOfProperty("Owner") == "App::PropertyLink"

pin_definitions["A1"].LocalPosition = App.Vector(1, 2, 3)
document.recompute()
assert _near(terminals["K1.A1"].WorldPosition, (1, 2, 3))
assert _near(terminals["K2.A1"].WorldPosition, (101, 2, 3))

object_count = len(document.Objects)
second_summary = import_project(document, parsed.project)
assert second_summary == summary
assert len(document.Objects) == object_count
assert bindings["K1"].getTypeIdOfProperty("Target") == "App::PropertyLinkGlobal"
assert terminals["K1.A1"].getTypeIdOfProperty("Owner") == "App::PropertyLinkGlobal"
assert bindings["K1"].Target is k1
assert terminals["K1.A1"].Owner is k1
assert _near(pin_definitions["A1"].LocalPosition, (1, 2, 3))

placement = terminals["K1.A1"].Placement
placement.Base = App.Vector(1000, 1000, 1000)
terminals["K1.A1"].Placement = placement
document.recompute()
assert _near(pin_definitions["A1"].LocalPosition, (20, 10, 10))
assert _near(terminals["K2.A1"].WorldPosition, (120, 10, 10))

placement = terminals["K1.A1"].Placement
placement.Base = App.Vector(4, 4, 4)
terminals["K1.A1"].Placement = placement
document.recompute()
assert _near(pin_definitions["A1"].LocalPosition, (4, 4, 4))
assert str(pin_definitions["A1"].PlacementStatus) == "Placed"
assert _near(terminals["K2.A1"].WorldPosition, (104, 4, 4))

terminals["K1.A1"].PositionMode = "Overridden"
document.recompute()
assert _near(terminals["K1.A1"].OverridePosition, (4, 4, 4))
terminals["K1.A1"].OverridePosition = App.Vector(6, 4, 4)
document.recompute()
assert _near(terminals["K1.A1"].WorldPosition, (6, 4, 4))
assert _near(terminals["K2.A1"].WorldPosition, (104, 4, 4))
terminals["K1.A1"].PositionMode = "Inherited"
document.recompute()
assert _near(terminals["K1.A1"].WorldPosition, (4, 4, 4))

corridor_1 = create_corridor(document, length=60, width=20, height=20)
corridor_2 = create_corridor_from_points(
    document,
    [App.Vector(120, 20, 20), App.Vector(50, 0, 0)],
)
assert _near(corridor_2.Placement.Base, (50, 0, 0))
assert _near(
    (corridor_2.Length.Value, corridor_2.Width.Value, corridor_2.Height.Value),
    (70, 20, 20),
)
document.recompute()

routing_summary = route_wires(document)
assert routing_summary.routed_count == 1, routing_summary
assert routing_summary.failed_count == 0, routing_summary
route_objects = [
    item
    for item in document.Objects
    if getattr(item, "QET_ObjectKind", "") == "WireRoute"
]
assert len(route_objects) == 1
assert route_objects[0].GeometricLength.Value > 0
conductor = next(
    item
    for item in document.Objects
    if getattr(item, "QET_ObjectKind", "") == "Conductor"
)
assert str(conductor.RouteStatus) == "Routed"
assert conductor.Route is route_objects[0]

# Moving a per-instance override through the same Placement path used by the
# workbench command must invalidate the old physical route.
terminals["K1.A2"].PositionMode = "Overridden"
document.recompute()
placement = terminals["K1.A2"].Placement
placement.Base = App.Vector(7, 4, 4)
terminals["K1.A2"].Placement = placement
document.recompute()
assert str(conductor.RouteStatus) == "Stale"
assert str(route_objects[0].RouteStatus) == "Stale"
assert route_wires(document).routed_count == 1
terminals["K1.A2"].PositionMode = "Inherited"
document.recompute()
assert str(conductor.RouteStatus) == "Stale"
assert route_wires(document).routed_count == 1

route_objects[0].SlackPercent = 10
route_objects[0].EndAllowanceEach = 5
document.recompute()
expected_cut_length = route_objects[0].GeometricLength.Value * 1.1 + 10
assert abs(route_objects[0].CutLength.Value - expected_cut_length) < 1e-7
route_objects[0].SlackPercent = 0
route_objects[0].EndAllowanceEach = 0
document.recompute()

schedule = create_wire_schedule(document)
assert schedule.QET_ObjectKind == "WireSchedule"
wire_cell = schedule.getContents("A2")
assert wire_cell.lstrip("'") == "W1", repr(wire_cell)
assert float(schedule.getContents("E2")) > 0

# A failed reroute must not leave the previous wire looking valid.
corridor_2.Placement.Base = App.Vector(70, 0, 0)
document.recompute()
failed_routing = route_wires(document)
assert failed_routing.routed_count == 0, failed_routing
assert failed_routing.failed_count == 1, failed_routing
assert str(conductor.RouteStatus) == "NoPath"
assert route_objects[0].Shape.isNull()
assert route_objects[0].GeometricLength.Value == 0

corridor_2.Placement.Base = App.Vector(50, 0, 0)
document.recompute()
recovered_routing = route_wires(document)
assert recovered_routing.routed_count == 1, recovered_routing
assert str(conductor.RouteStatus) == "Routed"
assert not route_objects[0].Shape.isNull()

# Reimport preserves a valid generated route, while removed schematic records
# are retained only as explicitly obsolete history.
same_project_summary = import_project(document, parsed.project)
assert same_project_summary.obsolete_conductor_count == 0
assert str(conductor.RouteStatus) == "Routed"

changed_section_project = parse_qet_bytes(
    fixture.read_bytes().replace(
        b'conductor_section="1.5 mm\xc2\xb2"',
        b'conductor_section="2.5"',
        1,
    )
).project
import_project(document, changed_section_project)
assert str(conductor.RouteStatus) == "Stale"
assert abs(conductor.Section.Value - 2.5) < 1e-9
import_project(document, parsed.project)
assert str(conductor.RouteStatus) == "Stale"
route_wires(document)
assert str(conductor.RouteStatus) == "Routed"

reduced_project = replace(
    parsed.project,
    elements=parsed.project.elements[:1],
    conductors=(),
)
reduced_summary = import_project(document, reduced_project)
assert reduced_summary.obsolete_element_count == 1
assert reduced_summary.obsolete_terminal_count == 2
assert reduced_summary.obsolete_conductor_count == 1
assert str(conductor.RouteStatus) == "Obsolete"
assert str(route_objects[0].RouteStatus) == "Obsolete"

restored_summary = import_project(document, parsed.project)
assert restored_summary.obsolete_element_count == 0
assert restored_summary.routeable_conductor_count == 1
rerouted_after_restore = route_wires(document)
assert rerouted_after_restore.routed_count == 1
assert str(conductor.RouteStatus) == "Routed"

corridor_2.Enabled = False
document.recompute()
route_wires(document)
assert corridor_2.UsedArea.Value == 0
corridor_2.Enabled = True
document.recompute()
route_wires(document)

# A rejected run is read-only: it must retain the last committed usage values.
previous_usage = corridor_1.UsedArea.Value
corridor_1.Enabled = False
corridor_2.Enabled = False
document.recompute()
try:
    route_wires(document)
    raise AssertionError("routing without an enabled corridor should fail")
except RoutingError:
    pass
assert corridor_1.UsedArea.Value == previous_usage
corridor_1.Enabled = True
corridor_2.Enabled = True
document.recompute()
route_wires(document)

route_name = conductor.Route.Name
document.removeObject(route_name)
document.recompute()
assert conductor.Route is None
assert str(conductor.RouteStatus) == "NoPath"
route_after_deletion = route_wires(document)
assert route_after_deletion.routed_count == 1
assert conductor.Route is not None

used_corridors = list(conductor.Route.Corridors)
assert used_corridors
document.removeObject(used_corridors[0].Name)
document.recompute()
assert str(conductor.RouteStatus) == "Stale"
route_after_corridor_deletion = route_wires(document)
assert route_after_corridor_deletion.routed_count == 1

pin_definitions["A1"].LocalPosition = App.Vector(5, 2, 3)
route_after_dirty_terminal = route_wires(document)
assert route_after_dirty_terminal.routed_count == 1
assert _near(conductor.Route.Points[-1], (105, 2, 3))
pin_definitions["A1"].LocalPosition = App.Vector(6, 2, 3)
document.recompute()
assert str(conductor.RouteStatus) == "Stale"
assert str(conductor.Route.RouteStatus) == "Stale"

with tempfile.TemporaryDirectory(prefix="qet-routing-") as temp_dir:
    file_path = str(Path(temp_dir) / "smoke.FCStd")
    document.saveAs(file_path)
    App.closeDocument(document.Name)
    reopened = App.openDocument(file_path)
    reopened.recompute()
    restored = {
        item.Label: item
        for item in reopened.Objects
        if getattr(item, "QET_ObjectKind", "") == "TerminalInstance"
    }
    restored_bindings = {
        item.QETLabel: item
        for item in reopened.Objects
        if getattr(item, "QET_ObjectKind", "") == "DeviceBinding"
    }
    assert restored_bindings["K1"].getTypeIdOfProperty("Target") == "App::PropertyLinkGlobal"
    assert restored["K1.A1"].getTypeIdOfProperty("Owner") == "App::PropertyLinkGlobal"
    assert restored_bindings["K1"].Target.Label == "K1"
    assert restored["K1.A1"].Owner.Label == "K1"
    assert _near(restored["K1.A1"].WorldPosition, (6, 2, 3))
    assert _near(restored["K2.A1"].WorldPosition, (106, 2, 3))
    restored_routes = [
        item
        for item in reopened.Objects
        if getattr(item, "QET_ObjectKind", "") == "WireRoute"
    ]
    assert len(restored_routes) == 1
    assert restored_routes[0].Shape.Length > 0
    App.closeDocument(reopened.Name)

# App::Link reports shape bounds after its own Placement but, unlike a regular
# GeoFeature, does not expose getGlobalPlacement(). Verify terminal definitions
# stay owner-local while nested link instances receive the full parent transform.
link_document = App.newDocument("QetRoutingAssemblyLinkSmoke")
link_source_k1 = link_document.addObject("Part::Feature", "K1Source")
link_source_k1.Label = "K1 Source"
link_source_k1.Shape = Part.makeBox(20, 10, 10)
link_source_k1.Placement.Base = App.Vector(50, 0, 0)
link_source_k2 = link_document.addObject("Part::Feature", "K2Source")
link_source_k2.Label = "K2 Source"
link_source_k2.Shape = Part.makeBox(20, 10, 10)
link_source_k2.Placement.Base = App.Vector(50, 0, 0)
link_container = link_document.addObject("App::Part", "LinkContainer")
link_container.Placement.Base = App.Vector(1000, 0, 0)
link_k1 = link_container.newObject("App::Link", "K1Link")
link_k1.Label = "K1"
link_k1.LinkedObject = link_source_k1
link_k1.LinkTransform = True
link_k1.Placement.Base = App.Vector(200, 0, 0)
link_k2 = link_container.newObject("App::Link", "K2Link")
link_k2.Label = "K2"
link_k2.LinkedObject = link_source_k2
link_k2.LinkTransform = True
link_k2.Placement.Base = App.Vector(400, 0, 0)
link_document.recompute()

link_summary = import_project(link_document, parsed.project)
assert link_summary.matched_count == 2, link_summary
assert link_summary.ambiguous_count == 0, link_summary
link_bindings = {
    item.QETLabel: item
    for item in link_document.Objects
    if getattr(item, "QET_ObjectKind", "") == "DeviceBinding"
}
assert link_bindings["K1"].Target is link_k1
assert link_bindings["K2"].Target is link_k2
assert link_bindings["K1"].getTypeIdOfProperty("Target") == "App::PropertyLinkGlobal"
link_terminals = {
    item.Label: item
    for item in link_document.Objects
    if getattr(item, "QET_ObjectKind", "") == "TerminalInstance"
}
assert link_terminals["K1.A1"].getTypeIdOfProperty("Owner") == "App::PropertyLinkGlobal"
assert _near(link_terminals["K1.A1"].Definition.LocalPosition, (60, 5, 5))
assert _near(link_terminals["K1.A1"].WorldPosition, (1260, 5, 5))
assert _near(link_terminals["K2.A1"].WorldPosition, (1460, 5, 5))
App.closeDocument(link_document.Name)

# Physically unmatched devices are never treated as wires at the document origin.
unmatched_document = App.newDocument("QetRoutingUnmatchedSmoke")
unmatched_summary = import_project(unmatched_document, parsed.project)
assert unmatched_summary.matched_count == 0
assert unmatched_summary.routeable_conductor_count == 0
create_corridor(unmatched_document)
unmatched_routing = route_wires(unmatched_document)
assert unmatched_routing.routed_count == 0
assert unmatched_routing.failed_count == 1
assert "match both endpoint devices" in unmatched_routing.failures[0]
App.closeDocument(unmatched_document.Name)

# Repeated schematic labels cannot silently claim the same physical target.
duplicate_document = App.newDocument("QetRoutingDuplicateLabelSmoke")
duplicate_part = duplicate_document.addObject("Part::Feature", "K1")
duplicate_part.Label = "K1"
duplicate_part.Shape = Part.makeBox(20, 10, 10)
duplicate_project = replace(
    parsed.project,
    elements=(
        parsed.project.elements[0],
        replace(parsed.project.elements[1], label="K1"),
    ),
)
duplicate_summary = import_project(duplicate_document, duplicate_project)
assert duplicate_summary.matched_count == 0
assert duplicate_summary.ambiguous_count == 2
assert duplicate_summary.routeable_conductor_count == 0
App.closeDocument(duplicate_document.Name)

# Free-space terminal lead-ins and lead-outs are included in route length.
lead_document = App.newDocument("QetRoutingLeadLengthSmoke")
lead_k1 = lead_document.addObject("Part::Feature", "K1")
lead_k1.Label = "K1"
lead_k1.Shape = Part.makeBox(20, 10, 10)
lead_k2 = lead_document.addObject("Part::Feature", "K2")
lead_k2.Label = "K2"
lead_k2.Shape = Part.makeBox(20, 10, 10)
lead_k2.Placement.Base = App.Vector(200, 0, 0)
lead_document.recompute()
import_project(lead_document, parsed.project)
create_corridor_from_points(
    lead_document,
    [App.Vector(50, 0, 0), App.Vector(150, 10, 10)],
)
lead_summary = route_wires(lead_document)
assert lead_summary.routed_count == 1
assert abs(lead_summary.total_geometric_length_mm - 200) < 1e-7
lead_route = next(
    item
    for item in lead_document.Objects
    if getattr(item, "QET_ObjectKind", "") == "WireRoute"
)
assert abs(lead_route.Shape.Length - 200) < 1e-7
assert abs(lead_route.GeometricLength.Value - 200) < 1e-7
lead_route.SlackPercent = 10
lead_route.EndAllowanceEach = 5
lead_document.recompute()
assert abs(lead_route.CutLength.Value - 230) < 1e-7
App.closeDocument(lead_document.Name)

print("QET_ROUTING_FREECAD_SMOKE_OK")
