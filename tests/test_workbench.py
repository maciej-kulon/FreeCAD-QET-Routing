# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import importlib
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from freecad.QetRouting import ICON_DIR
from freecad.QetRouting import commands
from freecad.QetRouting.document import _ensure_native_part_view_provider

ROOT = Path(__file__).resolve().parents[1]


class WorkbenchPackagingTests(unittest.TestCase):
    def test_native_part_view_provider_sentinel_repairs_missing_proxy(self) -> None:
        class ViewObject:
            Proxy = None
            DisplayMode = "None"

        obj = types.SimpleNamespace(ViewObject=ViewObject())
        view_object, repaired = _ensure_native_part_view_provider(obj)

        self.assertIs(view_object, obj.ViewObject)
        self.assertEqual(view_object.Proxy, 0)
        self.assertTrue(repaired)

    def test_manifest_declares_workbench_and_existing_icon(self) -> None:
        root = ET.parse(ROOT / "package.xml").getroot()
        namespace = {"m": "https://wiki.freecad.org/Package_Metadata"}

        self.assertEqual(root.findtext("m:name", namespaces=namespace), "QET Routing")
        self.assertEqual(
            root.findtext("m:content/m:workbench/m:classname", namespaces=namespace),
            "QetRoutingWorkbench",
        )
        icon = root.findtext("m:icon", namespaces=namespace)
        self.assertTrue((ROOT / icon).is_file())
        maintainer = root.find("m:maintainer", namespaces=namespace)
        self.assertIsNotNone(maintainer)
        self.assertTrue(maintainer.get("email"))
        repository = root.find("m:url[@type='repository']", namespaces=namespace)
        self.assertIsNotNone(repository)
        self.assertEqual(repository.get("branch"), "main")

    def test_all_commands_have_existing_icons(self) -> None:
        command_objects = (
            commands.ImportQetCommand(),
            commands.PlaceTerminalCommand(),
            commands.CreateCorridorCommand(),
            commands.CreateCorridorFromPointsCommand(),
            commands.RouteWiresCommand(),
            commands.WireScheduleCommand(),
        )
        self.assertEqual(len(command_objects), len(commands.COMMANDS))
        for command in command_objects:
            self.assertTrue(Path(command.GetResources()["Pixmap"]).is_file())
        self.assertEqual(ICON_DIR, ROOT / "Resources" / "Icons")

    def test_corridor_vertex_selection_flattens_world_space_subobjects(self) -> None:
        class Vector:
            def __init__(self, x: float, y: float, z: float) -> None:
                self.x = x
                self.y = y
                self.z = z

        class Vertex:
            ShapeType = "Vertex"

            def __init__(self, point: Vector) -> None:
                self.Point = point

        vertices = {
            "Parent.Box.Vertex1": Vertex(Vector(40, -20, 5)),
            "Parent.Box.Vertex2": Vertex(Vector(100, -20, 5)),
            "Parent.Box.Vertex3": Vertex(Vector(100, 30, 25)),
            "Parent.Box.Vertex4": Vertex(Vector(40, 30, 25)),
        }
        obj = types.SimpleNamespace(getSubObject=vertices.__getitem__)
        selection = types.SimpleNamespace(
            Object=obj,
            SubElementNames=[
                "Parent.Box.Vertex1",
                "Parent.Box.Vertex2",
                "Parent.Box.Vertex3",
                "Parent.Box.Vertex4",
            ],
        )

        points = commands._selection_world_vertices([selection])

        self.assertEqual(
            [(point.x, point.y, point.z) for point in points],
            [
                (40, -20, 5),
                (100, -20, 5),
                (100, 30, 25),
                (40, 30, 25),
            ],
        )

    def test_corridor_vertex_selection_rejects_non_vertices(self) -> None:
        first_vertex = types.SimpleNamespace(
            ShapeType="Vertex",
            Point=types.SimpleNamespace(x=1.0, y=2.0, z=3.0),
        )
        second_vertex = types.SimpleNamespace(
            ShapeType="Vertex",
            Point=types.SimpleNamespace(x=4.0, y=5.0, z=6.0),
        )
        edge = types.SimpleNamespace(ShapeType="Edge")
        geometry = {
            "Vertex1": first_vertex,
            "Vertex2": second_vertex,
            "Edge1": edge,
        }
        selection = types.SimpleNamespace(
            Object=types.SimpleNamespace(getSubObject=geometry.__getitem__),
            SubElementNames=["Vertex1", "Vertex2", "Edge1"],
        )

        with self.assertRaisesRegex(ValueError, "vertices only"):
            commands._selection_world_vertices([selection])

    def test_gui_entrypoint_registers_expected_workbench(self) -> None:
        registered = []
        fake_gui = types.ModuleType("FreeCADGui")

        class Workbench:
            pass

        fake_gui.Workbench = Workbench
        fake_gui.addWorkbench = registered.append
        module_name = "freecad.QetRouting.init_gui"
        sys.modules.pop(module_name, None)
        try:
            with patch.dict(sys.modules, {"FreeCADGui": fake_gui}):
                module = importlib.import_module(module_name)
            self.assertEqual(len(registered), 1)
            self.assertIsInstance(registered[0], module.QetRoutingWorkbench)
            self.assertEqual(registered[0].GetClassName(), "Gui::PythonWorkbench")
        finally:
            sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()
