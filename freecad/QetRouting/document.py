# SPDX-License-Identifier: LGPL-2.1-or-later
"""FreeCAD document adapter for normalized QET projects.

This module is the persistence boundary. Domain parsing does not import
FreeCAD; only this adapter creates document objects and geometry.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .qet.model import ConductorKind, QetElement, QetProject, QetTerminal

ROOT_NAME = "QETRouting"
PROJECT_NAME = "QETProjectData"
DEVICE_TYPES_NAME = "QETDeviceTypes"
DEVICE_INSTANCES_NAME = "QETDeviceInstances"
CONDUCTORS_NAME = "QETConductors"


def _ensure_native_part_view_provider(obj: Any) -> tuple[Any | None, bool]:
    """Attach FreeCAD's native Part view provider to a FeaturePython object.

    FreeCAD delays the native scene-graph setup for ``Part::FeaturePython``
    until its view-side Proxy is non-null. QET visual objects do not need a
    custom view provider, so the standard sentinel used by FreeCAD's own
    scripted Part features is sufficient.

    Returns the view object and whether an unattached legacy object was
    repaired.
    """

    view_object = getattr(obj, "ViewObject", None)
    if view_object is None:
        return None, False
    missing_proxy = getattr(view_object, "Proxy", None) is None
    if missing_proxy:
        view_object.Proxy = 0
    return view_object, missing_proxy


@dataclass(frozen=True)
class ImportSummary:
    element_count: int
    matched_count: int
    missing_count: int
    ambiguous_count: int
    terminal_count: int
    routeable_conductor_count: int
    blocked_conductor_count: int
    obsolete_element_count: int = 0
    obsolete_terminal_count: int = 0
    obsolete_conductor_count: int = 0


class TerminalMarkerProxy:
    """FeaturePython proxy deriving a marker from owner and type-local position."""

    Type = "QetRoutingTerminalMarker"

    def __init__(self, obj: Any) -> None:
        self._updating_placement = False
        obj.Proxy = self
        self._repair_view_provider(obj)

    def execute(self, obj: Any) -> None:
        import FreeCAD as App
        import Part

        self._repair_view_provider(obj)
        definition = getattr(obj, "Definition", None)
        mode = str(getattr(obj, "PositionMode", "Inherited"))
        if mode == "Overridden":
            local_position = obj.OverridePosition
        elif definition is not None:
            local_position = definition.LocalPosition
        else:
            local_position = App.Vector()

        owner = getattr(obj, "Owner", None)
        if owner is None:
            world_position = local_position
        else:
            world_position = _global_placement(owner).multVec(local_position)

        radius = max(float(obj.MarkerRadius), 0.01)
        previous_position = getattr(obj, "WorldPosition", App.Vector())
        self._updating_placement = True
        try:
            obj.Shape = Part.makeSphere(radius)
            placement = obj.Placement
            placement.Base = world_position
            obj.Placement = placement
            obj.WorldPosition = world_position
        finally:
            self._updating_placement = False
        if (world_position - previous_position).Length > 1e-7:
            _mark_terminal_dependents_stale(obj)

    def onChanged(self, obj: Any, property_name: str) -> None:
        if property_name == "PositionMode":
            self._handle_position_mode_change(obj)
            return
        if property_name != "Placement" or getattr(self, "_updating_placement", False):
            return
        definition = getattr(obj, "Definition", None)
        if definition is None:
            return
        # A user-authored Placement assignment is a physical routing input.
        # FreeCAD may expose Placement subproperties as live values, so an old
        # coordinate is not always available here for a reliable comparison.
        _mark_terminal_dependents_stale(obj)
        owner = getattr(obj, "Owner", None)
        world_position = obj.Placement.Base
        if owner is None:
            local_position = world_position
        else:
            local_position = _global_placement(owner).inverse().multVec(world_position)
            local_position = _clamp_to_owner(owner, local_position)
            world_position = _global_placement(owner).multVec(local_position)
        if str(getattr(obj, "PositionMode", "Inherited")) == "Overridden":
            obj.OverridePosition = local_position
        else:
            definition.LocalPosition = local_position
            if "PlacementStatus" in definition.PropertiesList:
                definition.PlacementStatus = "Placed"
        obj.WorldPosition = world_position
        if (obj.Placement.Base - world_position).Length > 1e-7:
            self._updating_placement = True
            try:
                placement = obj.Placement
                placement.Base = world_position
                obj.Placement = placement
            finally:
                self._updating_placement = False

    def _handle_position_mode_change(self, obj: Any) -> None:
        if "LastPositionMode" not in getattr(obj, "PropertiesList", []):
            return
        current = str(getattr(obj, "PositionMode", "Inherited"))
        previous = str(getattr(obj, "LastPositionMode", "Inherited"))
        if current == previous:
            return
        if current == "Overridden":
            definition = getattr(obj, "Definition", None)
            if definition is not None:
                obj.OverridePosition = definition.LocalPosition
        obj.LastPositionMode = current

    def dumps(self) -> None:
        return None

    def loads(self, _state: Any) -> None:
        self._updating_placement = False

    def onDocumentRestored(self, obj: Any) -> None:
        self._updating_placement = False
        self._repair_view_provider(obj)

    @staticmethod
    def _repair_view_provider(obj: Any) -> None:
        _ensure_native_part_view_provider(obj)


class ConductorProxy:
    """Marks generated geometry stale when physical wire inputs change."""

    Type = "QetRoutingConductor"

    def __init__(self, obj: Any) -> None:
        obj.Proxy = self

    def execute(self, _obj: Any) -> None:
        return None

    def onChanged(self, obj: Any, property_name: str) -> None:
        if property_name == "Route":
            if (
                getattr(obj, "Route", None) is None
                and str(getattr(obj, "RouteStatus", "")) in {"Routed", "Stale"}
            ):
                obj.RouteStatus = "NoPath"
            return
        if property_name not in {"Section", "OuterDiameter", "EndpointA", "EndpointB"}:
            return
        if str(getattr(obj, "RouteStatus", "")) == "Routed":
            _mark_conductor_stale(obj)

    def dumps(self) -> None:
        return None

    def loads(self, _state: Any) -> None:
        return None


def import_project(
    document: Any,
    project: QetProject,
    *,
    use_transaction: bool = True,
) -> ImportSummary:
    """Create or update QET bindings while preserving authored terminal layouts."""

    import FreeCAD as App

    if document is None:
        raise ValueError("A FreeCAD document is required")

    transaction_open = False
    if use_transaction:
        document.openTransaction("Import QElectroTech project")
        transaction_open = True
    try:
        # Take the inventory before creating workbench objects, and exclude
        # objects from an earlier import by their explicit marker property.
        target_inventory = [
            obj
            for obj in document.Objects
            if "QET_ObjectKind" not in getattr(obj, "PropertiesList", [])
            and _is_supported_target(obj)
        ]
        existing_bindings = {
            obj.Name: obj
            for obj in document.Objects
            if getattr(obj, "QET_ObjectKind", "") == "DeviceBinding"
        }
        existing_terminals = {
            obj.Name: obj
            for obj in document.Objects
            if getattr(obj, "QET_ObjectKind", "") == "TerminalInstance"
        }
        existing_conductors = {
            obj.Name: obj
            for obj in document.Objects
            if getattr(obj, "QET_ObjectKind", "") == "Conductor"
        }
        seen_bindings: set[str] = set()
        seen_terminals: set[str] = set()
        seen_conductors: set[str] = set()

        root = _ensure_group(document, ROOT_NAME, "QET Routing")
        types_group = _ensure_group(
            document,
            DEVICE_TYPES_NAME,
            "Part Types",
            parent=root,
        )
        instances_group = _ensure_group(
            document,
            DEVICE_INSTANCES_NAME,
            "Device Instances",
            parent=root,
        )
        conductors_group = _ensure_group(
            document,
            CONDUCTORS_NAME,
            "Conductors",
            parent=root,
        )
        project_object = _ensure_object(
            document,
            "App::FeaturePython",
            PROJECT_NAME,
            "QET Project",
            parent=root,
            kind="Project",
        )[0]
        _set_string(project_object, "SourceFile", project.source_path, "QET Project")
        _set_string(project_object, "ProjectTitle", project.title, "QET Project")
        _set_string(project_object, "QETVersion", project.version, "QET Project")
        _set_string(project_object, "Fingerprint", project.fingerprint, "QET Project")
        _set_string(
            project_object,
            "ImportedAtUTC",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "QET Project",
        )

        matched = 0
        missing = 0
        ambiguous = 0
        terminal_markers: dict[str, Any] = {}
        physical_devices = tuple(
            getattr(project, "physical_devices", project.elements)
        )
        qet_label_counts = Counter(
            element.label for element in physical_devices if element.label
        )

        for element in physical_devices:
            candidates = [
                obj for obj in target_inventory if element.label and obj.Label == element.label
            ]
            label_is_unique = qet_label_counts[element.label] == 1
            target = candidates[0] if len(candidates) == 1 and label_is_unique else None
            if target is not None:
                status = "Matched"
                matched += 1
            elif candidates:
                status = "Ambiguous"
                ambiguous += 1
            else:
                status = "Missing"
                missing += 1

            layout_keys = _unique_terminal_layout_keys(element.terminals)
            type_key, terminal_signature = _resolved_type_key(
                element,
                layout_keys,
                document,
                types_group,
            )
            type_object = _ensure_device_type(
                document,
                types_group,
                element,
                type_key,
                terminal_signature,
            )
            binding = _ensure_binding(
                document,
                instances_group,
                element,
                type_object,
                target,
                status,
                candidates,
            )
            seen_bindings.add(binding.Name)
            if target is not None:
                _write_target_metadata(target, element, type_key)

            default_center = _shape_center(target, App)
            for index, (terminal, layout_key) in enumerate(
                zip(element.terminals, layout_keys),
                start=1,
            ):
                display_key = terminal.pin_key or f"unnamed-{index}"
                definition = _ensure_pin_definition(
                    document,
                    type_object,
                    type_key,
                    terminal,
                    layout_key,
                    display_key,
                    default_center,
                    target is not None,
                )
                marker = _ensure_terminal_marker(
                    document,
                    binding,
                    element,
                    terminal,
                    definition,
                    target,
                    display_key,
                )
                seen_terminals.add(marker.Name)
                terminal_markers[_terminal_identity(terminal)] = marker

        ready_count = 0
        blocked_count = 0
        for conductor in project.conductors:
            endpoint_a = terminal_markers.get(conductor.endpoint_a.identity)
            endpoint_b = terminal_markers.get(conductor.endpoint_b.identity)
            if conductor.kind is ConductorKind.SINGLE_LINE:
                route_status = "BlockedSingleLine"
            elif (
                conductor.is_routeable
                and endpoint_a is not None
                and endpoint_b is not None
                and getattr(endpoint_a, "Owner", None) is not None
                and getattr(endpoint_b, "Owner", None) is not None
            ):
                route_status = "Ready"
                ready_count += 1
            else:
                route_status = "Unresolved"
            if route_status != "Ready":
                blocked_count += 1
            record = _ensure_conductor_record(
                document,
                conductors_group,
                conductor,
                endpoint_a,
                endpoint_b,
                route_status,
            )
            seen_conductors.add(record.Name)

        obsolete_elements, obsolete_terminals, obsolete_conductors = (
            _mark_obsolete_import_records(
                existing_bindings,
                existing_terminals,
                existing_conductors,
                seen_bindings,
                seen_terminals,
                seen_conductors,
            )
        )

        document.recompute()
        if transaction_open:
            document.commitTransaction()
            transaction_open = False
    except Exception:
        if transaction_open:
            document.abortTransaction()
        raise

    return ImportSummary(
        element_count=len(physical_devices),
        matched_count=matched,
        missing_count=missing,
        ambiguous_count=ambiguous,
        terminal_count=len(terminal_markers),
        routeable_conductor_count=ready_count,
        blocked_conductor_count=blocked_count,
        obsolete_element_count=obsolete_elements,
        obsolete_terminal_count=obsolete_terminals,
        obsolete_conductor_count=obsolete_conductors,
    )


def _resolved_type_key(
    element: QetElement,
    terminal_layout_keys: tuple[str, ...],
    document: Any,
    types_group: Any,
) -> tuple[str, str]:
    base_key = element.device_type.key
    signature = "|".join(sorted(terminal_layout_keys))
    base_name = _object_name("QETType", base_key)
    existing = document.getObject(base_name)
    if existing is None or existing not in types_group.Group:
        return base_key, signature
    existing_signature = str(getattr(existing, "TerminalSignature", ""))
    if existing_signature in {"", signature}:
        return base_key, signature
    suffix = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    return f"{base_key}|terminal-signature={suffix}", signature


def _ensure_device_type(
    document: Any,
    types_group: Any,
    element: QetElement,
    type_key: str,
    terminal_signature: str,
) -> Any:
    name = _object_name("QETType", type_key)
    label = (
        f"{element.manufacturer} {element.article_number}".strip()
        or element.type_path
        or "Unspecified device type"
    )
    obj, created = _ensure_object(
        document,
        "App::DocumentObjectGroup",
        name,
        label,
        parent=types_group,
        kind="DeviceType",
    )
    _set_string(obj, "TypeKey", type_key, "QET Part Type")
    _set_string(obj, "Manufacturer", element.manufacturer, "QET Part Type")
    _set_string(obj, "ArticleNumber", element.article_number, "QET Part Type")
    _set_string(obj, "OrderNumber", element.order_number, "QET Part Type")
    _set_string(obj, "InternalNumber", element.internal_number, "QET Part Type")
    _set_string(obj, "Variant", element.device_type.variant, "QET Part Type")
    _set_string(obj, "QETElementType", element.type_path, "QET Part Type")
    _set_string(obj, "QETLinkType", element.link_type, "QET Part Type")
    _set_string(obj, "TerminalSignature", terminal_signature, "QET Part Type")
    if "LayoutRevision" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyInteger",
            "LayoutRevision",
            "QET Part Type",
            "Revision of the reusable terminal layout",
        )
        obj.LayoutRevision = 1 if created else 0
    return obj


def _ensure_pin_definition(
    document: Any,
    type_object: Any,
    type_key: str,
    terminal: QetTerminal,
    layout_key: str,
    display_key: str,
    default_center: Any,
    initialize_from_geometry: bool,
) -> Any:
    name = _object_name("QETPin", f"{type_key}|{layout_key}")
    obj, created = _ensure_object(
        document,
        "App::FeaturePython",
        name,
        display_key,
        parent=type_object,
        kind="PinDefinition",
    )
    _set_string(obj, "PinKey", display_key, "QET Terminal Definition")
    _set_string(obj, "TerminalIdentity", layout_key, "QET Terminal Definition")
    _set_string(
        obj,
        "QETTerminalDefinitionUUID",
        terminal.definition_uuid,
        "QET Terminal Definition",
    )
    if "Aliases" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyStringList",
            "Aliases",
            "QET Terminal Definition",
            "Alternative QET terminal names",
        )
    if terminal.name and terminal.name not in obj.Aliases:
        obj.Aliases = [*obj.Aliases, terminal.name]
    if "LocalPosition" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyVector",
            "LocalPosition",
            "QET Terminal Definition",
            "Terminal position in the owning part's local coordinates",
        )
        obj.LocalPosition = default_center
    if "InitializedFromGeometry" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyBool",
            "InitializedFromGeometry",
            "QET Terminal Definition",
            "Whether the initial position came from a matched FreeCAD part",
        )
        obj.InitializedFromGeometry = bool(initialize_from_geometry)
    elif initialize_from_geometry and not bool(obj.InitializedFromGeometry):
        obj.LocalPosition = default_center
        obj.InitializedFromGeometry = True
    if "ExitDirection" not in obj.PropertiesList:
        import FreeCAD as App

        obj.addProperty(
            "App::PropertyVector",
            "ExitDirection",
            "QET Terminal Definition",
            "Preferred local direction in which the wire leaves the terminal",
        )
        obj.ExitDirection = App.Vector(1, 0, 0)
    _set_enumeration(
        obj,
        "PlacementStatus",
        ["Unplaced", "Placed"],
        "Unplaced" if created else str(obj.PlacementStatus),
        "QET Terminal Definition",
    )
    return obj


def _ensure_binding(
    document: Any,
    instances_group: Any,
    element: QetElement,
    type_object: Any,
    target: Any,
    status: str,
    candidates: list[Any],
) -> Any:
    name = _object_name("QETDevice", element.uuid)
    obj, _created = _ensure_object(
        document,
        "App::DocumentObjectGroup",
        name,
        element.label or element.uuid,
        parent=instances_group,
        kind="DeviceBinding",
    )
    _set_global_link(obj, "Target", target, "QET Device Binding")
    _set_link(obj, "DeviceType", type_object, "QET Device Binding")
    _set_string(obj, "QETElementUUID", element.uuid, "QET Device Binding")
    _set_string(obj, "QETElementType", element.type_path, "QET Device Binding")
    _set_string(obj, "QETLinkType", element.link_type, "QET Device Binding")
    _set_string(obj, "QETLabel", element.label, "QET Device Binding")
    _set_string(obj, "Manufacturer", element.manufacturer, "QET Device Binding")
    _set_string(obj, "ArticleNumber", element.article_number, "QET Device Binding")
    _set_string(obj, "OrderNumber", element.order_number, "QET Device Binding")
    _set_string(obj, "InternalNumber", element.internal_number, "QET Device Binding")
    _set_string(obj, "Plant", element.plant, "QET Device Binding")
    _set_string(obj, "Location", element.location, "QET Device Binding")
    _set_string(obj, "FolioOrder", element.folio_order, "QET Device Binding")
    if "QETFragmentUUIDs" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyStringList",
            "QETFragmentUUIDs",
            "QET Device Binding",
            "Placed QET element UUIDs represented by this physical device",
        )
    obj.QETFragmentUUIDs = list(_physical_fragment_uuids(element))
    _set_enumeration(
        obj,
        "BindingStatus",
        ["Matched", "Missing", "Ambiguous", "Obsolete"],
        status,
        "QET Device Binding",
    )
    if "MatchCandidates" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyStringList",
            "MatchCandidates",
            "QET Device Binding",
            "FreeCAD objects sharing the requested label",
        )
    obj.MatchCandidates = [candidate.Name for candidate in candidates]
    return obj


def _physical_fragment_uuids(element: QetElement) -> tuple[str, ...]:
    """Return the placed QET fragments represented by a physical-device record."""

    declared = tuple(getattr(element, "fragment_uuids", ()))
    candidates = (
        declared
        if declared
        else (
            element.uuid,
            *(terminal.element_uuid for terminal in element.terminals),
        )
    )
    return tuple(dict.fromkeys(uuid for uuid in candidates if uuid))


def _move_terminal_marker_to_binding(
    document: Any,
    marker: Any,
    binding: Any,
) -> None:
    """Make the current physical binding the sole device group for a marker."""

    for candidate in document.Objects:
        if (
            candidate is binding
            or getattr(candidate, "QET_ObjectKind", "") != "DeviceBinding"
        ):
            continue
        if marker in getattr(candidate, "Group", ()):
            candidate.removeObject(marker)
    if marker not in binding.Group:
        binding.addObject(marker)


def _ensure_terminal_marker(
    document: Any,
    binding: Any,
    element: QetElement,
    terminal: QetTerminal,
    definition: Any,
    target: Any,
    pin_key: str,
) -> Any:
    identity = _terminal_identity(terminal)
    name = _object_name("QETTerminal", identity)
    obj, created = _ensure_object(
        document,
        "Part::FeaturePython",
        name,
        f"{element.label}.{pin_key}" if element.label else pin_key,
        parent=binding,
        kind="TerminalInstance",
    )
    _move_terminal_marker_to_binding(document, obj, binding)
    previous_definition = getattr(obj, "Definition", None)
    _migrate_terminal_definition_layout(previous_definition, definition)
    if created or not isinstance(getattr(obj, "Proxy", None), TerminalMarkerProxy):
        TerminalMarkerProxy(obj)
    _set_global_link(obj, "Owner", target, "QET Terminal")
    _set_link(obj, "Definition", definition, "QET Terminal")
    _set_string(obj, "QETElementUUID", terminal.element_uuid, "QET Terminal")
    _set_string(obj, "QETTerminalUUID", terminal.definition_uuid, "QET Terminal")
    _set_string(obj, "PinKey", pin_key, "QET Terminal")
    if "LastPositionMode" not in obj.PropertiesList:
        obj.addProperty("App::PropertyString", "LastPositionMode", "QET Internal")
        obj.LastPositionMode = "Inherited"
        try:
            obj.setEditorMode("LastPositionMode", 2)
        except (AttributeError, RuntimeError):
            pass
    previous_sync_status = str(getattr(obj, "SyncStatus", ""))
    _set_enumeration(
        obj,
        "PositionMode",
        ["Inherited", "Overridden"],
        "Inherited" if created else str(obj.PositionMode),
        "QET Terminal",
    )
    _set_enumeration(
        obj,
        "SyncStatus",
        ["Current", "Obsolete"],
        "Current",
        "QET Synchronization",
    )
    if "OverridePosition" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyVector",
            "OverridePosition",
            "QET Terminal",
            "Instance-only local terminal position",
        )
    if "MarkerRadius" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyLength",
            "MarkerRadius",
            "QET Terminal",
            "Radius of the terminal editing marker",
        )
        obj.MarkerRadius = 2.0
    if "WorldPosition" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyVector",
            "WorldPosition",
            "QET Terminal",
            "Derived terminal position in document coordinates",
            1,
        )
    view_object, _repaired = _ensure_native_part_view_provider(obj)
    if view_object is not None:
        if created or previous_sync_status == "Obsolete":
            view_object.Visibility = True
            view_object.Selectable = True
        view_object.ShapeColor = (1.0, 0.55, 0.0)
        view_object.LineColor = (1.0, 0.85, 0.2)
    return obj


def _migrate_terminal_definition_layout(previous: Any, current: Any) -> None:
    """Carry an authored pin layout into a replacement aggregate device type."""

    if previous is None or previous is current:
        return
    previous_properties = getattr(previous, "PropertiesList", ())
    current_properties = getattr(current, "PropertiesList", ())
    if (
        "PlacementStatus" not in previous_properties
        or str(previous.PlacementStatus) != "Placed"
        or "PlacementStatus" not in current_properties
        or str(current.PlacementStatus) == "Placed"
        or "LocalPosition" not in previous_properties
        or "LocalPosition" not in current_properties
    ):
        return
    current.LocalPosition = previous.LocalPosition
    if (
        "ExitDirection" in previous_properties
        and "ExitDirection" in current_properties
    ):
        current.ExitDirection = previous.ExitDirection
    current.PlacementStatus = "Placed"


def _ensure_conductor_record(
    document: Any,
    conductors_group: Any,
    conductor: Any,
    endpoint_a: Any,
    endpoint_b: Any,
    route_status: str,
) -> Any:
    name = _object_name("QETConductor", conductor.key)
    obj, created = _ensure_object(
        document,
        "App::FeaturePython",
        name,
        conductor.number or conductor.key,
        parent=conductors_group,
        kind="Conductor",
    )
    previous_status = str(getattr(obj, "RouteStatus", ""))
    previous_section = (
        float(getattr(obj.Section, "Value", obj.Section))
        if "Section" in getattr(obj, "PropertiesList", [])
        else None
    )
    imported_section = conductor.section_mm2 or 0.0
    if created or not isinstance(getattr(obj, "Proxy", None), ConductorProxy):
        ConductorProxy(obj)
    _set_string(obj, "WireKey", conductor.key, "QET Conductor")
    _set_string(obj, "QETType", conductor.kind.value, "QET Conductor")
    _set_string(obj, "WireNumber", conductor.number, "QET Conductor")
    _set_string(obj, "Function", conductor.function, "QET Conductor")
    _set_string(obj, "Voltage", conductor.voltage, "QET Conductor")
    _set_string(obj, "WireColor", conductor.color, "QET Conductor")
    _set_string(obj, "RawSection", conductor.raw_section, "QET Conductor")
    _set_string(obj, "Cable", conductor.cable, "QET Conductor")
    _set_string(obj, "Bus", conductor.bus, "QET Conductor")
    _set_link(obj, "EndpointA", endpoint_a, "QET Conductor")
    _set_link(obj, "EndpointB", endpoint_b, "QET Conductor")
    _set_bool(
        obj,
        "ConnectivityResolved",
        bool(conductor.is_routeable),
        "QET Conductor",
    )
    selected_status = route_status
    if (
        route_status == "Ready"
        and previous_status in {"Routed", "Stale"}
        and getattr(obj, "Route", None) is not None
    ):
        section_unchanged = (
            previous_section is not None
            and abs(previous_section - imported_section) <= 1e-9
        )
        selected_status = previous_status if section_unchanged else "Stale"
    _set_enumeration(
        obj,
        "RouteStatus",
        [
            "Ready",
            "BlockedSingleLine",
            "Unresolved",
            "Routed",
            "Stale",
            "NoPath",
            "Obsolete",
        ],
        selected_status,
        "QET Conductor",
    )
    if route_status in {"BlockedSingleLine", "Unresolved"}:
        route = getattr(obj, "Route", None)
        if route is not None:
            if "RouteStatus" in getattr(route, "PropertiesList", []):
                route.RouteStatus = "Stale"
            view_object = getattr(route, "ViewObject", None)
            if view_object is not None:
                view_object.Visibility = False
    if "Section" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyArea",
            "Section",
            "QET Conductor",
            "Parsed conductor cross-sectional area",
        )
    obj.Section = imported_section
    if "OuterDiameter" not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyLength",
            "OuterDiameter",
            "QET Conductor",
            "Insulated outside diameter used for corridor fill calculations",
        )
        obj.OuterDiameter = 0.0
    # Assigning physical properties can trigger ConductorProxy on an existing
    # object. Restore the synchronization decision after all fields are updated.
    _set_enumeration(
        obj,
        "RouteStatus",
        [
            "Ready",
            "BlockedSingleLine",
            "Unresolved",
            "Routed",
            "Stale",
            "NoPath",
            "Obsolete",
        ],
        selected_status,
        "QET Conductor",
    )
    return obj


def _mark_obsolete_import_records(
    existing_bindings: dict[str, Any],
    existing_terminals: dict[str, Any],
    existing_conductors: dict[str, Any],
    seen_bindings: set[str],
    seen_terminals: set[str],
    seen_conductors: set[str],
) -> tuple[int, int, int]:
    obsolete_bindings = [
        obj for name, obj in existing_bindings.items() if name not in seen_bindings
    ]
    obsolete_terminals = [
        obj for name, obj in existing_terminals.items() if name not in seen_terminals
    ]
    obsolete_conductors = [
        obj for name, obj in existing_conductors.items() if name not in seen_conductors
    ]
    for obj in obsolete_bindings:
        _set_enumeration(
            obj,
            "BindingStatus",
            ["Matched", "Missing", "Ambiguous", "Obsolete"],
            "Obsolete",
            "QET Device Binding",
        )
    for obj in obsolete_terminals:
        _set_enumeration(
            obj,
            "SyncStatus",
            ["Current", "Obsolete"],
            "Obsolete",
            "QET Synchronization",
        )
        view_object = getattr(obj, "ViewObject", None)
        if view_object is not None:
            view_object.Visibility = False
    for obj in obsolete_conductors:
        _set_enumeration(
            obj,
            "RouteStatus",
            [
                "Ready",
                "BlockedSingleLine",
                "Unresolved",
                "Routed",
                "Stale",
                "NoPath",
                "Obsolete",
            ],
            "Obsolete",
            "QET Conductor",
        )
        route = getattr(obj, "Route", None)
        if route is not None and "RouteStatus" in getattr(route, "PropertiesList", []):
            _set_enumeration(
                route,
                "RouteStatus",
                ["Routed", "Stale", "NoPath", "Obsolete"],
                "Obsolete",
                "Wire Route",
            )
            view_object = getattr(route, "ViewObject", None)
            if view_object is not None:
                view_object.Visibility = False
    return len(obsolete_bindings), len(obsolete_terminals), len(obsolete_conductors)


def _ensure_group(
    document: Any,
    name: str,
    label: str,
    *,
    parent: Any = None,
) -> Any:
    return _ensure_object(
        document,
        "App::DocumentObjectGroup",
        name,
        label,
        parent=parent,
        kind="Container",
    )[0]


def _ensure_object(
    document: Any,
    type_name: str,
    name: str,
    label: str,
    *,
    parent: Any = None,
    kind: str,
) -> tuple[Any, bool]:
    obj = document.getObject(name)
    created = obj is None
    if obj is None:
        obj = document.addObject(type_name, name)
    obj.Label = label
    _set_string(obj, "QET_ObjectKind", kind, "QET Internal")
    if parent is not None and obj not in parent.Group:
        parent.addObject(obj)
    return obj, created


def _write_target_metadata(target: Any, element: QetElement, type_key: str) -> None:
    values = {
        "QET_Manufacturer": element.manufacturer,
        "QET_ArticleNumber": element.article_number,
        "QET_OrderNumber": element.order_number,
        "QET_InternalNumber": element.internal_number,
        "QET_PartTypeId": type_key,
        "QET_DeviceDesignation": element.label,
        "QET_ElementUUID": element.uuid,
    }
    for name, value in values.items():
        try:
            _set_string(target, name, value, "QET Routing")
        except Exception:
            # App::Link and externally-owned objects are not always writable;
            # DeviceBinding remains the authoritative metadata holder.
            continue


def _set_string(obj: Any, name: str, value: str, group: str) -> None:
    if name not in obj.PropertiesList:
        obj.addProperty("App::PropertyString", name, group)
    setattr(obj, name, value or "")


def _set_link(obj: Any, name: str, value: Any, group: str) -> None:
    if name not in obj.PropertiesList:
        obj.addProperty("App::PropertyLink", name, group)
    setattr(obj, name, value)


def _set_global_link(obj: Any, name: str, value: Any, group: str) -> None:
    """Set a link that may cross an App::Part/Assembly coordinate scope."""

    if name in obj.PropertiesList:
        try:
            property_type = obj.getTypeIdOfProperty(name)
        except (AttributeError, RuntimeError):
            property_type = ""
        if property_type != "App::PropertyLinkGlobal":
            obj.removeProperty(name)
    if name not in obj.PropertiesList:
        obj.addProperty("App::PropertyLinkGlobal", name, group)
    setattr(obj, name, value)


def _set_bool(obj: Any, name: str, value: bool, group: str) -> None:
    if name not in obj.PropertiesList:
        obj.addProperty("App::PropertyBool", name, group)
    setattr(obj, name, bool(value))


def _set_enumeration(
    obj: Any,
    name: str,
    values: list[str],
    selected: str,
    group: str,
) -> None:
    if name not in obj.PropertiesList:
        obj.addProperty("App::PropertyEnumeration", name, group)
        existing_values: list[str] = []
    else:
        try:
            existing_values = list(obj.getEnumerationsOfProperty(name))
        except (AttributeError, RuntimeError):
            existing_values = []
    if existing_values != values:
        setattr(obj, name, values)
    if selected not in values:
        selected = values[0]
    setattr(obj, name, selected)


def _shape_center(target: Any, app_module: Any) -> Any:
    bounds = _target_local_bounds(target)
    if bounds is None:
        return app_module.Vector()
    minimum, maximum = bounds
    return app_module.Vector(
        (minimum.x + maximum.x) / 2,
        (minimum.y + maximum.y) / 2,
        (minimum.z + maximum.z) / 2,
    )


def _is_supported_target(obj: Any) -> bool:
    # Physical terminal owners need a coordinate system. Assembly joint/BOM/
    # view groups and plain document groups are containers without Placement;
    # they must not become candidates merely because they contain other data.
    if "Placement" not in getattr(obj, "PropertiesList", []):
        return False
    return _target_local_bounds(obj) is not None


def _target_local_bounds(target: Any) -> tuple[Any, Any] | None:
    if target is None:
        return None
    import FreeCAD as App

    try:
        shape = target.Shape
        if shape is not None and not shape.isNull():
            box = _shape_owner_local_bound_box(target, shape)
            return (
                App.Vector(box.XMin, box.YMin, box.ZMin),
                App.Vector(box.XMax, box.YMax, box.ZMax),
            )
    except (AttributeError, RuntimeError):
        pass

    world_points: list[Any] = []
    visited: set[str] = set()
    pending = list(getattr(target, "Group", []))
    while pending:
        child = pending.pop()
        child_name = str(getattr(child, "Name", id(child)))
        if child_name in visited:
            continue
        visited.add(child_name)
        pending.extend(getattr(child, "Group", []))
        try:
            shape = child.Shape
            if shape is None or shape.isNull():
                continue
            box = _shape_owner_local_bound_box(child, shape)
            child_placement = _global_placement(child)
            for x in (box.XMin, box.XMax):
                for y in (box.YMin, box.YMax):
                    for z in (box.ZMin, box.ZMax):
                        world = child_placement.multVec(App.Vector(x, y, z))
                        world_points.append(world)
        except (AttributeError, RuntimeError):
            continue
    if not world_points:
        return None
    owner_inverse = _global_placement(target).inverse()
    points = [owner_inverse.multVec(point) for point in world_points]
    minimum = App.Vector(
        min(point.x for point in points),
        min(point.y for point in points),
        min(point.z for point in points),
    )
    maximum = App.Vector(
        max(point.x for point in points),
        max(point.y for point in points),
        max(point.z for point in points),
    )
    return minimum, maximum


def _shape_owner_local_bound_box(owner: Any, shape: Any) -> Any:
    """Return shape bounds in the owning object's local coordinate system.

    FreeCAD exposes ``Shape.BoundBox`` after the shape's Placement, including
    for App::Link. Terminal definitions, however, are stored in owner-local
    coordinates and are transformed by ``_global_placement`` later. Preserve
    any transform inherited from an App::Link source while removing the
    owner's own local Placement.
    """

    local_shape = shape.copy()
    local_shape.Placement = owner.Placement.inverse().multiply(shape.Placement)
    return local_shape.BoundBox


def _clamp_to_owner(owner: Any, local_position: Any) -> Any:
    bounds = _target_local_bounds(owner)
    if bounds is None:
        return local_position
    minimum, maximum = bounds
    import FreeCAD as App

    return App.Vector(
        min(max(local_position.x, minimum.x), maximum.x),
        min(max(local_position.y, minimum.y), maximum.y),
        min(max(local_position.z, minimum.z), maximum.z),
    )


def _global_placement(owner: Any) -> Any:
    try:
        return owner.getGlobalPlacement()
    except (AttributeError, RuntimeError):
        local_placement = owner.Placement

    # App::Link does not expose getGlobalPlacement() in FreeCAD 1.1. Its
    # Placement is relative to the containing App::Part/Assembly, so compose
    # that parent coordinate system explicitly.
    try:
        parent = owner.getParentGeoFeatureGroup()
    except (AttributeError, RuntimeError):
        parent = None
    if parent is None:
        return local_placement
    return _global_placement(parent).multiply(local_placement)


def _mark_terminal_dependents_stale(terminal: Any) -> None:
    for dependent in getattr(terminal, "InList", []):
        if (
            getattr(dependent, "QET_ObjectKind", "") == "Conductor"
            and str(getattr(dependent, "RouteStatus", "")) == "Routed"
        ):
            _mark_conductor_stale(dependent)


def _mark_conductor_stale(conductor: Any) -> None:
    conductor.RouteStatus = "Stale"
    route = getattr(conductor, "Route", None)
    if route is not None and "RouteStatus" in getattr(route, "PropertiesList", []):
        route.RouteStatus = "Stale"


def _terminal_identity(terminal: QetTerminal) -> str:
    if terminal.definition_uuid:
        return f"{terminal.element_uuid}:{terminal.definition_uuid}"
    if terminal.local_id:
        return f"{terminal.element_uuid}:legacy:{terminal.local_id}"
    if terminal.name:
        return f"{terminal.element_uuid}:name:{terminal.name}"
    return f"{terminal.element_uuid}:anonymous"


def _unique_terminal_layout_keys(
    terminals: tuple[QetTerminal, ...],
) -> tuple[str, ...]:
    """Return deterministic, collision-free keys within one symbol definition."""

    bases = [
        terminal.layout_key or f"anonymous-index={index}"
        for index, terminal in enumerate(terminals, start=1)
    ]
    totals: dict[str, int] = {}
    for base in bases:
        totals[base] = totals.get(base, 0) + 1
    seen: dict[str, int] = {}
    result: list[str] = []
    for base in bases:
        if totals[base] == 1:
            result.append(base)
            continue
        occurrence = seen.get(base, 0) + 1
        seen[base] = occurrence
        result.append(f"{base}|occurrence={occurrence}")
    return tuple(result)


def _object_name(prefix: str, key: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_]+", "_", key).strip("_")[:28]
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{readable}_{digest}" if readable else f"{prefix}_{digest}"
