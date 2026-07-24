# SPDX-License-Identifier: LGPL-2.1-or-later
"""Short GUI registration smoke test for an installed FreeCAD runtime."""

import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory

import FreeCAD
import FreeCADGui as Gui


probe_document = None
probe_document_name = None
probe_directory = None
try:
    probe_directory = TemporaryDirectory(prefix="qet-routing-gui-smoke-")
    probe_path = Path(probe_directory.name) / "probe.FCStd"

    # A positional .py file runs after FreeCADGuiInit. Assert auto-discovery
    # before importing any workbench module here, so the probe cannot mask a
    # broken namespaced-addon installation.
    assert "freecad.QetRouting.init_gui" in sys.modules
    assert "QetRoutingWorkbench" in Gui.listWorkbenches()

    from freecad.QetRouting.commands import COMMANDS
    from freecad.QetRouting.document import import_project
    from freecad.QetRouting.qet import parse_qet
    from freecad.QetRouting.routing_document import create_corridor, route_wires
    import Part

    Gui.activateWorkbench("QetRoutingWorkbench")
    registered_commands = set(Gui.listCommands())
    assert set(COMMANDS) <= registered_commands

    # Part::FeaturePython delays native scene-graph setup until its view-side
    # Proxy is non-null. Exercise a real visual object so a valid headless
    # Shape cannot mask an invisible GUI representation.
    probe_document = FreeCAD.newDocument("QetRoutingGuiSmoke")
    probe_document_name = probe_document.Name
    corridor = create_corridor(probe_document)

    k1 = probe_document.addObject("Part::Feature", "K1")
    k1.Label = "K1"
    k1.Shape = Part.makeBox(20, 10, 10)
    k2 = probe_document.addObject("Part::Feature", "K2")
    k2.Label = "K2"
    k2.Shape = Part.makeBox(20, 10, 10)
    k2.Placement.Base = FreeCAD.Vector(150, 0, 0)

    corridor_reference = probe_document.addObject(
        "Part::Feature",
        "CorridorReference",
    )
    corridor_reference.Label = "Corridor reference"
    corridor_reference.Shape = Part.makeBox(100, 50, 50)
    corridor_reference.Placement.Base = FreeCAD.Vector(90, 0, 0)
    probe_document.recompute()

    Gui.Selection.clearSelection()
    for vertex_index in range(1, 9):
        Gui.Selection.addSelection(
            corridor_reference,
            f"Vertex{vertex_index}",
        )
    Gui.runCommand("QetRouting_CreateCorridorFromPoints")
    selected_objects = Gui.Selection.getSelection()
    assert len(selected_objects) == 1
    second_corridor = selected_objects[0]
    assert second_corridor.QET_ObjectKind == "RoutingCorridor"
    assert tuple(second_corridor.Placement.Base) == (90.0, 0.0, 0.0)
    point_corridor_dimensions = (
        second_corridor.Length.Value,
        second_corridor.Width.Value,
        second_corridor.Height.Value,
    )
    assert point_corridor_dimensions == (100.0, 50.0, 50.0)
    assert abs(float(second_corridor.Placement.Rotation.Angle)) <= 1e-9

    fixture = Path(__file__).parent / "fixtures" / "current.qet"
    import_summary = import_project(probe_document, parse_qet(fixture).project)
    assert import_summary.terminal_count == 4
    probe_document.recompute()
    routing_summary = route_wires(probe_document)
    assert routing_summary.routed_count == 1

    Gui.updateGui()
    assert not corridor.Shape.isNull()
    dimensions = (
        corridor.Shape.BoundBox.XLength,
        corridor.Shape.BoundBox.YLength,
        corridor.Shape.BoundBox.ZLength,
    )
    assert all(
        abs(actual - expected) <= 1e-7
        for actual, expected in zip(dimensions, (100.0, 50.0, 50.0))
    )

    visual_objects = [
        obj
        for obj in probe_document.Objects
        if getattr(obj, "QET_ObjectKind", "")
        in {"RoutingCorridor", "TerminalInstance", "WireRoute"}
    ]
    assert len(visual_objects) == 7
    for visual in visual_objects:
        assert visual.ViewObject.Proxy is not None
        assert visual.ViewObject.DisplayMode != "None"
        assert visual.ViewObject.DisplayMode in visual.ViewObject.listDisplayModes()
        assert visual.ViewObject.Visibility
        assert visual.ViewObject.Selectable
        assert visual.ViewObject.isVisible()

    corridor.ViewObject.Visibility = False
    Gui.updateGui()
    assert not corridor.ViewObject.isVisible()
    corridor.ViewObject.Visibility = True
    Gui.updateGui()
    assert corridor.ViewObject.isVisible()

    # Simulate a document made before the view-provider fix and verify that
    # onDocumentRestored repairs it without requiring object recreation.
    # A deliberately hidden/unselectable object must retain those user choices.
    corridor_name = corridor.Name
    second_corridor_name = second_corridor.Name
    second_corridor.ViewObject.Visibility = False
    second_corridor.ViewObject.Selectable = False
    corridor.ViewObject.Proxy = None
    probe_document.saveAs(str(probe_path))
    FreeCAD.closeDocument(probe_document_name)
    probe_document = None
    probe_document_name = None
    probe_document = FreeCAD.openDocument(str(probe_path))
    probe_document_name = probe_document.Name
    restored_corridor = probe_document.getObject(corridor_name)
    restored_hidden_corridor = probe_document.getObject(second_corridor_name)
    Gui.updateGui()
    assert restored_corridor.ViewObject.Proxy is not None
    assert restored_corridor.ViewObject.DisplayMode != "None"
    assert restored_corridor.ViewObject.Visibility
    assert restored_corridor.ViewObject.isVisible()
    assert not restored_hidden_corridor.ViewObject.Visibility
    assert not restored_hidden_corridor.ViewObject.Selectable
    assert not restored_hidden_corridor.ViewObject.isVisible()

    FreeCAD.Console.PrintMessage("QET_ROUTING_GUI_SMOKE_OK\n")
except Exception:
    FreeCAD.Console.PrintError(traceback.format_exc() + "\n")
    raise
finally:
    try:
        if (
            probe_document_name is not None
            and probe_document_name in FreeCAD.listDocuments()
        ):
            FreeCAD.closeDocument(probe_document_name)
    finally:
        try:
            if probe_directory is not None:
                probe_directory.cleanup()
        finally:
            # Close synchronously through FreeCAD's normal window lifecycle. A
            # custom QTimer can outlive Python teardown in some macOS/Qt builds.
            Gui.getMainWindow().close()
