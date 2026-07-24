# SPDX-License-Identifier: LGPL-2.1-or-later
"""GUI command registration for QET Routing."""

from __future__ import annotations

import traceback
from pathlib import Path

from . import ICON_DIR

COMMANDS = [
    "QetRouting_ImportQET",
    "QetRouting_PlaceTerminal",
    "QetRouting_CreateCorridor",
    "QetRouting_CreateCorridorFromPoints",
    "QetRouting_RouteWires",
    "QetRouting_WireSchedule",
]
_REGISTERED = False


class ImportQetCommand:
    def GetResources(self) -> dict[str, str]:
        return {
            "Pixmap": str(ICON_DIR / "ImportQET.svg"),
            "MenuText": "Import QElectroTech project",
            "ToolTip": (
                "Import QET devices and multiline connectivity, match FreeCAD "
                "parts by label, and create reusable terminal markers"
            ),
        }

    def IsActive(self) -> bool:
        return True

    def Activated(self) -> None:
        import FreeCAD as App
        from PySide import QtWidgets

        from .document import import_project
        from .qet import QetParseError, parse_qet

        filename, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            None,
            "Import QElectroTech project",
            "",
            "QElectroTech projects (*.qet *.xml);;All files (*)",
        )
        if not filename:
            return

        try:
            result = parse_qet(Path(filename))
        except QetParseError as exc:
            App.Console.PrintError(f"QET Routing: {exc}\n")
            QtWidgets.QMessageBox.critical(None, "QET Routing", str(exc))
            return

        document = App.ActiveDocument or App.newDocument("QETRouting")
        try:
            summary = import_project(document, result.project)
        except Exception as exc:
            App.Console.PrintError(
                "QET Routing import failed:\n" + traceback.format_exc() + "\n"
            )
            QtWidgets.QMessageBox.critical(
                None,
                "QET Routing",
                f"Import failed: {exc}",
            )
            return

        try:
            from .terminal_visibility import refresh_terminal_visibility

            refresh_terminal_visibility(document.Name)
        except Exception:
            App.Console.PrintWarning(
                "QET Routing could not refresh terminal visibility:\n"
                + traceback.format_exc()
                + "\n"
            )

        for diagnostic in result.diagnostics:
            message = (
                f"QET Routing [{diagnostic.severity.value}] "
                f"{diagnostic.code.value}: {diagnostic.message}"
            )
            if diagnostic.severity.value == "error":
                App.Console.PrintError(message + "\n")
            elif diagnostic.severity.value == "warning":
                App.Console.PrintWarning(message + "\n")
            else:
                App.Console.PrintMessage(message + "\n")

        details = (
            f"Imported {summary.element_count} devices and "
            f"{summary.terminal_count} terminals.\n\n"
            f"Matched parts: {summary.matched_count}\n"
            f"Missing parts: {summary.missing_count}\n"
            f"Ambiguous labels: {summary.ambiguous_count}\n"
            f"Routeable multiline conductors: {summary.routeable_conductor_count}\n"
            f"Blocked or unresolved conductors: {summary.blocked_conductor_count}\n"
            f"Diagnostics: {len(result.diagnostics)}"
        )
        has_import_issues = (
            result.has_errors
            or summary.missing_count > 0
            or summary.ambiguous_count > 0
            or summary.blocked_conductor_count > 0
            or any(
                diagnostic.severity.value in {"warning", "error"}
                for diagnostic in result.diagnostics
            )
        )
        if has_import_issues:
            QtWidgets.QMessageBox.warning(None, "QET Routing import completed", details)
        else:
            QtWidgets.QMessageBox.information(None, "QET Routing import completed", details)


class CreateCorridorCommand:
    def GetResources(self) -> dict[str, str]:
        return {
            "Pixmap": str(ICON_DIR / "CreateCorridor.svg"),
            "MenuText": "Create routing corridor",
            "ToolTip": "Create a translucent cuboid defining an available wire-routing volume",
        }

    def IsActive(self) -> bool:
        import FreeCAD as App

        return App.ActiveDocument is not None

    def Activated(self) -> None:
        import FreeCAD as App
        import FreeCADGui as Gui

        from .routing_document import create_corridor

        corridor = create_corridor(App.ActiveDocument)
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(corridor)
        Gui.activeDocument().activeView().fitAll()


class CreateCorridorFromPointsCommand:
    def GetResources(self) -> dict[str, str]:
        return {
            "Pixmap": str(ICON_DIR / "CreateCorridorFromPoints.svg"),
            "MenuText": "Create corridor from selected points",
            "ToolTip": (
                "Create an axis-aligned cable-tray routing corridor from "
                "two to eight selected vertices"
            ),
        }

    def IsActive(self) -> bool:
        import FreeCAD as App

        return App.ActiveDocument is not None

    def Activated(self) -> None:
        import FreeCAD as App
        import FreeCADGui as Gui
        from PySide import QtWidgets

        from .routing_document import create_corridor_from_points

        try:
            selections = Gui.Selection.getSelectionEx(App.ActiveDocument.Name, 0)
            points = _selection_world_vertices(selections)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(
                None,
                "Create QET corridor from points",
                str(exc),
            )
            return
        try:
            corridor = create_corridor_from_points(App.ActiveDocument, points)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(
                None,
                "Create QET corridor from points",
                f"Cannot create corridor: {exc}",
            )
            return
        except Exception as exc:
            App.Console.PrintError(
                "QET Routing corridor creation failed:\n"
                + traceback.format_exc()
                + "\n"
            )
            QtWidgets.QMessageBox.critical(
                None,
                "Create QET corridor from points",
                f"Corridor creation failed: {exc}",
            )
            return

        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(corridor)
        Gui.activeDocument().activeView().fitAll()


class PlaceTerminalCommand:
    def GetResources(self) -> dict[str, str]:
        return {
            "Pixmap": str(ICON_DIR / "PlaceTerminal.svg"),
            "MenuText": "Place terminal on selected geometry",
            "ToolTip": (
                "Select a device to reveal its terminals, then select one "
                "terminal and one target vertex, edge, face, or object"
            ),
        }

    def IsActive(self) -> bool:
        import FreeCAD as App

        return App.ActiveDocument is not None

    def Activated(self) -> None:
        import FreeCAD as App
        import FreeCADGui as Gui
        from PySide import QtWidgets

        selections = Gui.Selection.getSelectionEx(App.ActiveDocument.Name, 0)
        terminals, references = _terminal_placement_selections(selections)
        if len(terminals) != 1 or len(references) != 1:
            QtWidgets.QMessageBox.information(
                None,
                "Place QET terminal",
                (
                    "Select the owning device to reveal its terminals. Then, "
                    "while holding Ctrl, select exactly one orange terminal and "
                    "one target vertex, edge, face, or object."
                ),
            )
            return
        terminal = terminals[0]
        if getattr(terminal, "Owner", None) is None:
            QtWidgets.QMessageBox.warning(
                None,
                "Place QET terminal",
                "The terminal's QET device is not matched to a physical FreeCAD part.",
            )
            return
        point = _selection_world_point(references[0])
        if point is None:
            QtWidgets.QMessageBox.warning(
                None,
                "Place QET terminal",
                "The selected reference has no usable geometry.",
            )
            return

        document = App.ActiveDocument
        document.openTransaction("Place QET terminal")
        transaction_open = True
        try:
            placement = terminal.Placement
            placement.Base = point
            terminal.Placement = placement
            document.recompute()
            document.commitTransaction()
            transaction_open = False
        except Exception:
            if transaction_open:
                document.abortTransaction()
            raise


class RouteWiresCommand:
    def GetResources(self) -> dict[str, str]:
        return {
            "Pixmap": str(ICON_DIR / "RouteWires.svg"),
            "MenuText": "Route multiline conductors",
            "ToolTip": (
                "Automatically route all resolved QET multiline conductors "
                "through corridors"
            ),
        }

    def IsActive(self) -> bool:
        import FreeCAD as App

        return App.ActiveDocument is not None

    def Activated(self) -> None:
        import FreeCAD as App
        import FreeCADGui as Gui
        from PySide import QtWidgets

        from .routing import RoutingError
        from .routing_document import route_wires

        try:
            summary = route_wires(App.ActiveDocument)
        except RoutingError as exc:
            App.Console.PrintWarning(f"QET Routing: {exc}\n")
            QtWidgets.QMessageBox.warning(None, "QET Routing", str(exc))
            return
        except Exception as exc:
            App.Console.PrintError(
                "QET Routing failed:\n" + traceback.format_exc() + "\n"
            )
            QtWidgets.QMessageBox.critical(None, "QET Routing", f"Routing failed: {exc}")
            return

        Gui.activeDocument().activeView().fitAll()
        details = (
            f"Routed wires: {summary.routed_count}\n"
            f"Failed wires: {summary.failed_count}\n"
            f"Total geometric length: {summary.total_geometric_length_mm:.1f} mm\n"
            f"Total cut length: {summary.total_cut_length_mm:.1f} mm"
        )
        if summary.failures:
            details += "\n\n" + "\n".join(summary.failures[:10])
        if summary.failed_count:
            QtWidgets.QMessageBox.warning(None, "QET routing completed", details)
        else:
            QtWidgets.QMessageBox.information(None, "QET routing completed", details)


class WireScheduleCommand:
    def GetResources(self) -> dict[str, str]:
        return {
            "Pixmap": str(ICON_DIR / "WireSchedule.svg"),
            "MenuText": "Create wire-length schedule",
            "ToolTip": "Create or refresh a spreadsheet of geometric and cut wire lengths",
        }

    def IsActive(self) -> bool:
        import FreeCAD as App

        return App.ActiveDocument is not None

    def Activated(self) -> None:
        import FreeCAD as App
        import FreeCADGui as Gui

        from .routing_document import create_wire_schedule

        sheet = create_wire_schedule(App.ActiveDocument)
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(sheet)


def register() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    import FreeCADGui as Gui

    Gui.addCommand(COMMANDS[0], ImportQetCommand())
    Gui.addCommand(COMMANDS[1], PlaceTerminalCommand())
    Gui.addCommand(COMMANDS[2], CreateCorridorCommand())
    Gui.addCommand(COMMANDS[3], CreateCorridorFromPointsCommand())
    Gui.addCommand(COMMANDS[4], RouteWiresCommand())
    Gui.addCommand(COMMANDS[5], WireScheduleCommand())
    _REGISTERED = True


def _selection_world_point(selection: object) -> object | None:
    obj = getattr(selection, "Object", None)
    if obj is None:
        return None
    subobjects = list(getattr(selection, "SubObjects", []))
    if len(subobjects) > 1:
        return None
    subobject_selected = bool(subobjects)
    geometry = subobjects[0] if subobject_selected else getattr(obj, "Shape", None)
    if geometry is None:
        return None
    if hasattr(geometry, "Point"):
        local_point = geometry.Point
    elif hasattr(geometry, "CenterOfMass"):
        local_point = geometry.CenterOfMass
    else:
        try:
            local_point = geometry.BoundBox.Center
        except (AttributeError, RuntimeError):
            return None
    if subobject_selected:
        return local_point
    try:
        placement = obj.getGlobalPlacement()
    except (AttributeError, RuntimeError):
        placement = obj.Placement
    return placement.multVec(local_point)


def _terminal_placement_selections(
    selections: list[object] | tuple[object, ...],
) -> tuple[list[object], list[object]]:
    """Separate a terminal and its target from selections used to reveal it."""

    terminals = [
        item.Object
        for item in selections
        if getattr(item.Object, "QET_ObjectKind", "") == "TerminalInstance"
    ]
    references = [
        item
        for item in selections
        if getattr(item.Object, "QET_ObjectKind", "") != "TerminalInstance"
    ]
    if len(terminals) != 1 or len(references) <= 1:
        return terminals, references

    terminal = terminals[0]
    filtered = [
        item
        for item in references
        if not _is_terminal_reveal_selection(item, terminal)
    ]
    return terminals, filtered or references


def _is_terminal_reveal_selection(selection: object, terminal: object) -> bool:
    if (
        getattr(selection, "SubObjects", ())
        or getattr(selection, "SubElementNames", ())
    ):
        return False
    selected_object = getattr(selection, "Object", None)
    if selected_object is getattr(terminal, "Owner", None):
        return True
    return (
        getattr(selected_object, "QET_ObjectKind", "") == "DeviceBinding"
        and terminal in getattr(selected_object, "Group", ())
    )


def _selection_world_vertices(
    selections: list[object] | tuple[object, ...],
    *,
    tolerance: float = 1e-7,
) -> list[object]:
    """Collect unique world-space vertices from GUI selection records."""

    selected = [
        (getattr(selection, "Object", None), str(subelement_name))
        for selection in selections
        for subelement_name in list(
            getattr(selection, "SubElementNames", [])
        )
    ]
    if not 2 <= len(selected) <= 8:
        raise ValueError(
            "Select 2 to 8 vertices in the active document. "
            f"Two opposite corners are sufficient. Selected: {len(selected)}."
        )

    result: list[object] = []
    tolerance_squared = tolerance * tolerance
    for obj, subelement_name in selected:
        if obj is None:
            raise ValueError("A selected vertex no longer belongs to an object")
        leaf_name = subelement_name.rstrip(".").rsplit(".", 1)[-1]
        if not leaf_name.startswith("Vertex"):
            raise ValueError(
                "Select vertices only; edges, faces, and whole objects are not accepted"
            )
        try:
            geometry = obj.getSubObject(subelement_name)
        except (AttributeError, KeyError, RuntimeError, TypeError) as exc:
            raise ValueError(
                f"Could not resolve selected vertex {subelement_name!r}"
            ) from exc
        if (
            geometry is None
            or str(getattr(geometry, "ShapeType", "")) != "Vertex"
            or not hasattr(geometry, "Point")
        ):
            raise ValueError(
                f"Selected subelement {subelement_name!r} is not a vertex"
            )
        world_point = geometry.Point
        if any(
            (
                (float(world_point.x) - float(existing.x)) ** 2
                + (float(world_point.y) - float(existing.y)) ** 2
                + (float(world_point.z) - float(existing.z)) ** 2
            )
            <= tolerance_squared
            for existing in result
        ):
            continue
        result.append(world_point)
    if len(result) < 2:
        raise ValueError("Select at least two distinct vertex positions")
    return result
