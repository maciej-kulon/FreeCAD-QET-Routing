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
from freecad.QetRouting import terminal_visibility
from freecad.QetRouting.document import _ensure_native_part_view_provider

ROOT = Path(__file__).resolve().parents[1]


class _FakeSelection:
    class ResolveMode:
        NoResolve = 0

    def __init__(self) -> None:
        self.selected_by_document: dict[str, list[object]] = {}
        self.observers: list[object] = []
        self.observer_modes: list[object] = []
        self.requested_modes: list[object] = []

    def getSelectionEx(
        self,
        document_name: str,
        resolve_mode: object,
    ) -> list[object]:
        self.requested_modes.append(resolve_mode)
        return list(self.selected_by_document.get(document_name, ()))

    def addObserver(self, observer: object, resolve_mode: object) -> None:
        self.observers.append(observer)
        self.observer_modes.append(resolve_mode)

    def removeObserver(self, observer: object) -> None:
        self.observers.remove(observer)


class _FakeViewObject:
    def __init__(self) -> None:
        self.Visibility = True
        self.visibility_status: list[str] = []

    def setPropertyStatus(self, property_name: str, status: str) -> None:
        assert property_name == "Visibility"
        if status not in self.visibility_status:
            self.visibility_status.append(status)


def _selection_record(
    obj: object,
    *subelement_names: str,
) -> object:
    return types.SimpleNamespace(
        Object=obj,
        SubElementNames=subelement_names,
    )


def _visibility_fixture() -> tuple[object, object, object, object, object, object]:
    document = types.SimpleNamespace(Name="Panel")
    first_owner = types.SimpleNamespace(Name="K1", Document=document)
    second_owner = types.SimpleNamespace(Name="K2", Document=document)

    def marker(name: str, owner: object, *, status: str = "Current") -> object:
        return types.SimpleNamespace(
            Name=name,
            Document=document,
            QET_ObjectKind="TerminalInstance",
            Owner=owner,
            SyncStatus=status,
            ViewObject=_FakeViewObject(),
        )

    first_marker = marker("K1_A1", first_owner)
    second_marker = marker("K1_A2", first_owner)
    other_marker = marker("K2_A1", second_owner)
    obsolete_marker = marker("K2_A2", second_owner, status="Obsolete")
    first_binding = types.SimpleNamespace(
        Name="K1_Binding",
        Document=document,
        QET_ObjectKind="DeviceBinding",
        Group=[first_marker, second_marker],
    )
    document.Objects = [
        first_owner,
        second_owner,
        first_binding,
        first_marker,
        second_marker,
        other_marker,
        obsolete_marker,
    ]
    return (
        document,
        first_owner,
        first_binding,
        first_marker,
        second_marker,
        other_marker,
    )


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

    def test_terminal_placement_ignores_plain_reveal_selection(self) -> None:
        owner = types.SimpleNamespace(Name="K1")
        terminal = types.SimpleNamespace(
            QET_ObjectKind="TerminalInstance",
            Owner=owner,
        )
        binding = types.SimpleNamespace(
            QET_ObjectKind="DeviceBinding",
            Group=[terminal],
        )
        terminal_selection = types.SimpleNamespace(
            Object=terminal,
            SubObjects=(),
            SubElementNames=(),
        )
        geometry_selection = types.SimpleNamespace(
            Object=types.SimpleNamespace(QET_ObjectKind=""),
            SubObjects=(object(),),
            SubElementNames=("Face1",),
        )

        for reveal_object in (owner, binding):
            with self.subTest(reveal_object=reveal_object):
                reveal_selection = types.SimpleNamespace(
                    Object=reveal_object,
                    SubObjects=(),
                    SubElementNames=(),
                )
                terminals, references = commands._terminal_placement_selections(
                    [reveal_selection, terminal_selection, geometry_selection]
                )

                self.assertEqual(terminals, [terminal])
                self.assertEqual(references, [geometry_selection])

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


class TerminalVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        terminal_visibility._OBSERVER = None
        terminal_visibility._ACTIVE = False
        terminal_visibility._REFRESH_SCHEDULED = False
        terminal_visibility._PENDING_DOCUMENTS.clear()

    def tearDown(self) -> None:
        terminal_visibility._OBSERVER = None
        terminal_visibility._ACTIVE = False
        terminal_visibility._REFRESH_SCHEDULED = False
        terminal_visibility._PENDING_DOCUMENTS.clear()

    @staticmethod
    def _module_patch(
        document: object,
        selection: _FakeSelection,
    ) -> patch:
        fake_app = types.ModuleType("FreeCAD")
        fake_app.listDocuments = lambda: {document.Name: document}
        fake_gui = types.ModuleType("FreeCADGui")
        fake_gui.Selection = selection
        return patch.dict(
            sys.modules,
            {
                "FreeCAD": fake_app,
                "FreeCADGui": fake_gui,
            },
        )

    def test_refresh_shows_only_selected_owner_or_marker(self) -> None:
        (
            document,
            owner,
            binding,
            first_marker,
            second_marker,
            other_marker,
        ) = _visibility_fixture()
        selection = _FakeSelection()

        with self._module_patch(document, selection):
            selection.selected_by_document[document.Name] = [
                _selection_record(owner)
            ]
            terminal_visibility.refresh_terminal_visibility(document.Name)
            self.assertTrue(first_marker.ViewObject.Visibility)
            self.assertTrue(second_marker.ViewObject.Visibility)
            self.assertFalse(other_marker.ViewObject.Visibility)

            # The generated QET binding is not the physical element. Selecting
            # it must not reveal terminals belonging to the FreeCAD part.
            selection.selected_by_document[document.Name] = [
                _selection_record(binding)
            ]
            terminal_visibility.refresh_terminal_visibility(document.Name)
            self.assertFalse(first_marker.ViewObject.Visibility)
            self.assertFalse(second_marker.ViewObject.Visibility)
            self.assertFalse(other_marker.ViewObject.Visibility)

            binding.getSubObjectList = lambda _subname: (
                binding,
                first_marker,
            )
            selection.selected_by_document[document.Name] = [
                _selection_record(binding, "Terminal.")
            ]
            terminal_visibility.refresh_terminal_visibility(document.Name)
            self.assertTrue(first_marker.ViewObject.Visibility)
            self.assertFalse(second_marker.ViewObject.Visibility)
            self.assertFalse(other_marker.ViewObject.Visibility)

            selection.selected_by_document[document.Name] = [
                _selection_record(first_marker)
            ]
            terminal_visibility.refresh_terminal_visibility(document.Name)
            self.assertTrue(first_marker.ViewObject.Visibility)
            self.assertFalse(second_marker.ViewObject.Visibility)
            self.assertFalse(other_marker.ViewObject.Visibility)

            selection.selected_by_document[document.Name] = []
            terminal_visibility.refresh_terminal_visibility(document.Name)
            self.assertFalse(first_marker.ViewObject.Visibility)
            self.assertFalse(second_marker.ViewObject.Visibility)
            self.assertFalse(other_marker.ViewObject.Visibility)
            self.assertTrue(
                all(
                    mode == _FakeSelection.ResolveMode.NoResolve
                    for mode in selection.requested_modes
                )
            )
            for marker in (first_marker, second_marker, other_marker):
                self.assertIn(
                    "NoModify",
                    marker.ViewObject.visibility_status,
                )

    def test_transient_visibility_preserves_existing_gui_dirty_state(self) -> None:
        gui_document = types.SimpleNamespace(Modified=False)
        gui_module = types.SimpleNamespace(
            getDocument=lambda _name: gui_document,
        )

        with terminal_visibility._preserve_gui_modified_state(
            gui_module,
            "Panel",
        ):
            gui_document.Modified = True
        self.assertFalse(gui_document.Modified)

        gui_document.Modified = True
        with terminal_visibility._preserve_gui_modified_state(
            gui_module,
            "Panel",
        ):
            gui_document.Modified = True
        self.assertTrue(gui_document.Modified)

    def test_nested_subobject_selection_preserves_exact_link_instance(self) -> None:
        (
            document,
            first_link,
            _binding,
            first_marker,
            second_marker,
            other_marker,
        ) = _visibility_fixture()
        second_link = other_marker.Owner
        shared_source = object()
        first_link.LinkedObject = shared_source
        second_link.LinkedObject = shared_source
        assembly = types.SimpleNamespace(
            Name="Assembly",
            Document=document,
        )
        assembly.getSubObjectList = lambda subname: (
            (assembly, first_link)
            if subname == "K1.Face1"
            else (assembly, second_link)
        )
        document.Objects.insert(0, assembly)
        selection = _FakeSelection()

        with self._module_patch(document, selection):
            # Selecting only the assembly does not reveal every descendant.
            selection.selected_by_document[document.Name] = [
                _selection_record(assembly)
            ]
            terminal_visibility.refresh_terminal_visibility(document.Name)
            self.assertFalse(first_marker.ViewObject.Visibility)
            self.assertFalse(second_marker.ViewObject.Visibility)
            self.assertFalse(other_marker.ViewObject.Visibility)

            # A face selected through the nested path includes the exact link
            # instance, even though both instances share one linked source.
            selection.selected_by_document[document.Name] = [
                _selection_record(assembly, "K1.Face1")
            ]
            terminal_visibility.refresh_terminal_visibility(document.Name)
            self.assertTrue(first_marker.ViewObject.Visibility)
            self.assertTrue(second_marker.ViewObject.Visibility)
            self.assertFalse(other_marker.ViewObject.Visibility)

            selection.selected_by_document[document.Name] = [
                _selection_record(assembly, "K2.Face1")
            ]
            terminal_visibility.refresh_terminal_visibility(document.Name)
            self.assertFalse(first_marker.ViewObject.Visibility)
            self.assertFalse(second_marker.ViewObject.Visibility)
            self.assertTrue(other_marker.ViewObject.Visibility)

    def test_observer_activation_callbacks_and_deactivation(self) -> None:
        (
            document,
            owner,
            _binding,
            first_marker,
            second_marker,
            other_marker,
        ) = _visibility_fixture()
        selection = _FakeSelection()
        deferred: list[object] = []

        with (
            self._module_patch(document, selection),
            patch.object(
                terminal_visibility,
                "_defer_refresh",
                side_effect=deferred.append,
            ),
        ):
            terminal_visibility.activate_terminal_visibility()
            terminal_visibility.activate_terminal_visibility()
            self.assertEqual(len(selection.observers), 1)
            self.assertEqual(
                selection.observer_modes,
                [_FakeSelection.ResolveMode.NoResolve],
            )
            self.assertFalse(first_marker.ViewObject.Visibility)
            self.assertFalse(second_marker.ViewObject.Visibility)
            self.assertFalse(other_marker.ViewObject.Visibility)

            observer = selection.observers[0]
            for callback_name in (
                "addSelection",
                "removeSelection",
                "setSelection",
                "clearSelection",
            ):
                with self.subTest(callback=callback_name):
                    selection.selected_by_document[document.Name] = [
                        _selection_record(owner)
                    ]
                    first_marker.ViewObject.Visibility = False
                    second_marker.ViewObject.Visibility = False
                    getattr(observer, callback_name)(document.Name)
                    self.assertEqual(len(deferred), 1)
                    deferred.pop()()
                    self.assertTrue(first_marker.ViewObject.Visibility)
                    self.assertTrue(second_marker.ViewObject.Visibility)
                    self.assertFalse(other_marker.ViewObject.Visibility)

            # Selection bursts are collapsed into one pending GUI refresh.
            observer.clearSelection(document.Name)
            observer.addSelection(document.Name, owner.Name)
            self.assertEqual(len(deferred), 1)

            other_marker.ViewObject.Visibility = True
            terminal_visibility.deactivate_terminal_visibility()
            deferred.pop()()
            self.assertEqual(selection.observers, [])
            self.assertIsNone(terminal_visibility._OBSERVER)
            self.assertFalse(first_marker.ViewObject.Visibility)
            self.assertFalse(second_marker.ViewObject.Visibility)
            self.assertFalse(other_marker.ViewObject.Visibility)


if __name__ == "__main__":
    unittest.main()
