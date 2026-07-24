# SPDX-License-Identifier: LGPL-2.1-or-later
"""FreeCAD objects and commands for corridor routing and wire schedules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .document import (
    ROOT_NAME,
    _ensure_group,
    _ensure_native_part_view_provider,
    _mark_conductor_stale,
    _object_name,
    _set_enumeration,
    _set_link,
    _set_string,
)
from .qet.parser import parse_section_mm2
from .routing import (
    Box3,
    CorridorRouter,
    CorridorSpec,
    NoRouteError,
    Point3,
    RoutingError,
    WireRequest,
    axis_aligned_box_from_points,
)

ROUTING_NETWORK_NAME = "QETRoutingNetwork"
CORRIDORS_NAME = "QETRoutingCorridors"
WIRE_ROUTES_NAME = "QETWireRoutes"
WIRE_SCHEDULE_NAME = "QETWireSchedule"
REPORTS_NAME = "QETRoutingReports"


@dataclass(frozen=True)
class RoutingSummary:
    routed_count: int
    failed_count: int
    total_geometric_length_mm: float
    total_cut_length_mm: float
    failures: tuple[str, ...] = ()


class RoutingCorridorProxy:
    Type = "QetRoutingCorridor"

    def __init__(self, obj: Any) -> None:
        obj.Proxy = self
        self._repair_view_provider(obj)

    def execute(self, obj: Any) -> None:
        import Part

        self._repair_view_provider(obj)
        length, width, height = _corridor_dimensions(obj)
        obj.Shape = Part.makeBox(length, width, height)

    def onChanged(self, obj: Any, property_name: str) -> None:
        if property_name not in {
            "Length",
            "Width",
            "Height",
            "Placement",
            "AllowedSections",
            "Capacity",
            "MaxFillPercent",
            "Enabled",
        }:
            return
        document = getattr(obj, "Document", None)
        if document is None:
            return
        for item in document.Objects:
            if (
                getattr(item, "QET_ObjectKind", "") == "Conductor"
                and str(getattr(item, "RouteStatus", "")) == "Routed"
            ):
                _mark_conductor_stale(item)

    def dumps(self) -> None:
        return None

    def loads(self, _state: Any) -> None:
        return None

    def onDocumentRestored(self, obj: Any) -> None:
        self._repair_view_provider(obj)

    @staticmethod
    def _repair_view_provider(obj: Any) -> None:
        _ensure_native_part_view_provider(obj)


class WireRouteProxy:
    """Keeps cut length reactive when route allowances are edited."""

    Type = "QetRoutingWireRoute"

    def __init__(self, obj: Any) -> None:
        self._updating = False
        obj.Proxy = self
        self._repair_view_provider(obj)

    def execute(self, obj: Any) -> None:
        self._repair_view_provider(obj)
        self._update(obj)

    def onChanged(self, obj: Any, property_name: str) -> None:
        if property_name == "Corridors":
            for dependent in getattr(obj, "InList", []):
                if (
                    getattr(dependent, "QET_ObjectKind", "") == "Conductor"
                    and getattr(dependent, "Route", None) is obj
                    and str(getattr(dependent, "RouteStatus", "")) == "Routed"
                ):
                    _mark_conductor_stale(dependent)
            return
        if property_name in {"GeometricLength", "SlackPercent", "EndAllowanceEach"}:
            self._update(obj)

    def _update(self, obj: Any) -> None:
        if self._updating or "CutLength" not in getattr(obj, "PropertiesList", []):
            return
        self._updating = True
        try:
            _refresh_cut_length(obj)
        finally:
            self._updating = False

    def dumps(self) -> None:
        return None

    def loads(self, _state: Any) -> None:
        self._updating = False

    def onDocumentRestored(self, obj: Any) -> None:
        self._updating = False
        self._repair_view_provider(obj)

    @staticmethod
    def _repair_view_provider(obj: Any) -> None:
        _ensure_native_part_view_provider(obj)


def create_corridor(
    document: Any,
    *,
    length: float = 100.0,
    width: float = 50.0,
    height: float = 50.0,
    use_transaction: bool = True,
) -> Any:
    """Create a translucent, axis-aligned routing corridor."""

    if document is None:
        raise ValueError("A FreeCAD document is required")
    transaction_open = False
    if use_transaction:
        document.openTransaction("Create QET routing corridor")
        transaction_open = True
    try:
        corridor = _create_corridor_object(document, length, width, height)
        if transaction_open:
            document.commitTransaction()
            transaction_open = False
        return corridor
    except Exception:
        if transaction_open:
            document.abortTransaction()
        raise


def create_corridor_from_points(
    document: Any,
    points: list[Any] | tuple[Any, ...],
    *,
    use_transaction: bool = True,
) -> Any:
    """Create an axis-aligned corridor spanning selected world-space points."""

    import FreeCAD as App

    if document is None:
        raise ValueError("A FreeCAD document is required")
    try:
        point_values = tuple(
            Point3(float(point.x), float(point.y), float(point.z)) for point in points
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Selected geometry does not provide valid 3D points") from exc
    box = axis_aligned_box_from_points(point_values)
    dimensions = (
        box.maximum.x - box.minimum.x,
        box.maximum.y - box.minimum.y,
        box.maximum.z - box.minimum.z,
    )

    transaction_open = False
    if use_transaction:
        document.openTransaction("Create QET routing corridor from points")
        transaction_open = True
    try:
        corridor = _create_corridor_object(
            document,
            *dimensions,
            base=App.Vector(*box.minimum.as_tuple()),
        )
        if transaction_open:
            document.commitTransaction()
            transaction_open = False
        return corridor
    except Exception:
        if transaction_open:
            document.abortTransaction()
        raise


def _create_corridor_object(
    document: Any,
    length: float,
    width: float,
    height: float,
    *,
    base: Any | None = None,
) -> Any:
    root = _ensure_group(document, ROOT_NAME, "QET Routing")
    network = _ensure_group(
        document,
        ROUTING_NETWORK_NAME,
        "Routing Network",
        parent=root,
    )
    corridors = _ensure_group(
        document,
        CORRIDORS_NAME,
        "Corridors",
        parent=network,
    )
    name = _next_name(document, "QETCorridor")
    obj = document.addObject("Part::FeaturePython", name)
    obj.Label = f"Routing Corridor {name[-3:]}"
    obj.addProperty("App::PropertyString", "QET_ObjectKind", "QET Internal")
    obj.QET_ObjectKind = "RoutingCorridor"
    obj.addProperty("App::PropertyLength", "Length", "Corridor Geometry")
    obj.addProperty("App::PropertyLength", "Width", "Corridor Geometry")
    obj.addProperty("App::PropertyLength", "Height", "Corridor Geometry")
    obj.Length = length
    obj.Width = width
    obj.Height = height
    obj.addProperty(
        "App::PropertyStringList",
        "AllowedSections",
        "Routing Rules",
        "Allowed conductor sections in mm²; an empty list allows every section",
    )
    obj.AllowedSections = []
    obj.addProperty(
        "App::PropertyArea",
        "Capacity",
        "Routing Rules",
        "Usable bundle cross-sectional capacity; zero means unlimited",
    )
    obj.Capacity = 0.0
    obj.addProperty(
        "App::PropertyPercent",
        "MaxFillPercent",
        "Routing Rules",
        "Maximum permitted fill of Capacity",
    )
    obj.MaxFillPercent = 80
    obj.addProperty(
        "App::PropertyArea",
        "UsedArea",
        "Routing Results",
        "Calculated occupied area after the latest routing run",
        1,
    )
    obj.UsedArea = 0.0
    obj.addProperty("App::PropertyBool", "Enabled", "Routing Rules")
    obj.Enabled = True
    RoutingCorridorProxy(obj)
    if base is not None:
        placement = obj.Placement
        placement.Base = base
        obj.Placement = placement
    corridors.addObject(obj)
    view_object, _repaired = _ensure_native_part_view_provider(obj)
    if view_object is not None:
        view_object.Visibility = True
        view_object.Selectable = True
        view_object.ShapeColor = (0.2, 0.65, 1.0)
        view_object.LineColor = (0.1, 0.35, 0.8)
        view_object.Transparency = 75
    document.recompute()
    return obj


def route_wires(document: Any) -> RoutingSummary:
    """Route every resolved multiline conductor and create visible centerlines."""

    import FreeCAD as App
    import Part

    document.recompute()
    all_corridor_objects = [
        obj
        for obj in document.Objects
        if getattr(obj, "QET_ObjectKind", "") == "RoutingCorridor"
    ]
    corridor_objects = [
        obj for obj in all_corridor_objects if bool(getattr(obj, "Enabled", True))
    ]
    if not corridor_objects:
        raise RoutingError("Create at least one enabled routing corridor first")

    specs = [_corridor_spec(obj) for obj in corridor_objects]
    corridor_by_key = {spec.key: obj for spec, obj in zip(specs, corridor_objects)}
    router = CorridorRouter(specs)
    conductors = [
        obj
        for obj in document.Objects
        if getattr(obj, "QET_ObjectKind", "") == "Conductor"
        and str(getattr(obj, "QETType", "")) == "multi"
        and str(getattr(obj, "RouteStatus", "")) != "Obsolete"
    ]
    conductors.sort(key=lambda obj: _quantity_value(getattr(obj, "Section", 0.0)), reverse=True)

    routed_count = 0
    failed_count = 0
    total_geometric = 0.0
    total_cut = 0.0
    failures: list[str] = []
    document.openTransaction("Route QET wires")
    transaction_open = True
    try:
        for corridor in all_corridor_objects:
            corridor.UsedArea = 0.0
        root = _ensure_group(document, ROOT_NAME, "QET Routing")
        network = _ensure_group(
            document,
            ROUTING_NETWORK_NAME,
            "Routing Network",
            parent=root,
        )
        routes_group = _ensure_group(
            document,
            WIRE_ROUTES_NAME,
            "Wire Routes",
            parent=network,
        )
        for conductor in conductors:
            endpoint_a = getattr(conductor, "EndpointA", None)
            endpoint_b = getattr(conductor, "EndpointB", None)
            if (
                not bool(getattr(conductor, "ConnectivityResolved", False))
                or endpoint_a is None
                or endpoint_b is None
            ):
                _invalidate_wire_route(conductor, Part, conductor_status="Unresolved")
                failed_count += 1
                failures.append(f"{conductor.Label}: QET endpoints are unresolved")
                continue
            if (
                getattr(endpoint_a, "Owner", None) is None
                or getattr(endpoint_b, "Owner", None) is None
            ):
                _invalidate_wire_route(conductor, Part, conductor_status="Unresolved")
                failed_count += 1
                failures.append(
                    f"{conductor.Label}: match both endpoint devices to FreeCAD parts"
                )
                continue
            section = _quantity_value(conductor.Section)
            if section <= 0:
                _invalidate_wire_route(conductor, Part)
                failed_count += 1
                failures.append(f"{conductor.Label}: conductor section is missing")
                continue
            start = _point_from_vector(endpoint_a.WorldPosition)
            end = _point_from_vector(endpoint_b.WorldPosition)
            diameter = _quantity_value(getattr(conductor, "OuterDiameter", 0.0))
            occupancy = math.pi * (diameter / 2) ** 2 if diameter > 0 else section
            try:
                result = router.route(
                    WireRequest(
                        key=conductor.WireKey,
                        start=start,
                        end=end,
                        section_mm2=section,
                        occupancy_area_mm2=occupancy,
                    )
                )
            except NoRouteError as exc:
                _invalidate_wire_route(conductor, Part)
                failed_count += 1
                failures.append(f"{conductor.Label}: {exc}")
                continue

            route = _ensure_wire_route(
                document,
                routes_group,
                conductor,
                result,
                [corridor_by_key[key] for key in result.corridor_keys],
                App,
                Part,
            )
            _set_link(conductor, "Route", route, "QET Conductor")
            conductor.RouteStatus = "Routed"
            routed_count += 1
            total_geometric += _quantity_value(route.GeometricLength)
            total_cut += _quantity_value(route.CutLength)

        for spec in specs:
            corridor_by_key[spec.key].UsedArea = router.usage_mm2(spec.key)
        document.recompute()
        document.commitTransaction()
        transaction_open = False
    except Exception:
        if transaction_open:
            document.abortTransaction()
        raise

    return RoutingSummary(
        routed_count=routed_count,
        failed_count=failed_count,
        total_geometric_length_mm=total_geometric,
        total_cut_length_mm=total_cut,
        failures=tuple(failures),
    )


def create_wire_schedule(document: Any, *, use_transaction: bool = True) -> Any:
    """Create or refresh a standard FreeCAD Spreadsheet wire-length report."""

    if document is None:
        raise ValueError("A FreeCAD document is required")
    transaction_open = False
    if use_transaction:
        document.openTransaction("Create QET wire schedule")
        transaction_open = True
    try:
        sheet = _refresh_wire_schedule(document)
        if transaction_open:
            document.commitTransaction()
            transaction_open = False
        return sheet
    except Exception:
        if transaction_open:
            document.abortTransaction()
        raise


def _refresh_wire_schedule(document: Any) -> Any:
    sheet = document.getObject(WIRE_SCHEDULE_NAME)
    if sheet is None:
        sheet = document.addObject("Spreadsheet::Sheet", WIRE_SCHEDULE_NAME)
    sheet.Label = "QET Wire Schedule"
    root = _ensure_group(document, ROOT_NAME, "QET Routing")
    reports = _ensure_group(document, REPORTS_NAME, "Reports", parent=root)
    if sheet not in reports.Group:
        reports.addObject(sheet)
    sheet.clearAll()
    _set_string(sheet, "QET_ObjectKind", "WireSchedule", "QET Internal")
    headers = (
        "Wire",
        "From",
        "To",
        "Section",
        "Geometric length [mm]",
        "Cut length [mm]",
        "Route status",
    )
    for column, header in enumerate(headers, start=1):
        sheet.set(f"{_column_name(column)}1", _spreadsheet_text(header))

    conductors = sorted(
        (
            obj
            for obj in document.Objects
            if getattr(obj, "QET_ObjectKind", "") == "Conductor"
            and str(getattr(obj, "RouteStatus", "")) != "Obsolete"
        ),
        key=lambda obj: str(getattr(obj, "WireNumber", "") or obj.Label),
    )
    for row, conductor in enumerate(conductors, start=2):
        conductor_status = str(getattr(conductor, "RouteStatus", ""))
        route = getattr(conductor, "Route", None)
        has_reportable_route = (
            route is not None
            and conductor_status in {"Routed", "Stale"}
            and not route.Shape.isNull()
        )
        if has_reportable_route:
            _refresh_cut_length(route)
        text_values = (
            str(getattr(conductor, "WireNumber", "") or conductor.Label),
            getattr(getattr(conductor, "EndpointA", None), "Label", ""),
            getattr(getattr(conductor, "EndpointB", None), "Label", ""),
            str(getattr(conductor, "RawSection", "")),
            conductor_status,
        )
        for column, value in zip((1, 2, 3, 4, 7), text_values):
            sheet.set(f"{_column_name(column)}{row}", _spreadsheet_text(value))
        if has_reportable_route:
            sheet.set(f"E{row}", f"{_quantity_value(route.GeometricLength):.2f}")
            sheet.set(f"F{row}", f"{_quantity_value(route.CutLength):.2f}")
    sheet.setStyle("A1:G1", "bold", "add")
    for column, width in enumerate((90, 130, 130, 90, 150, 130, 110), start=1):
        sheet.setColumnWidth(_column_name(column), width)
    document.recompute()
    return sheet


def _ensure_wire_route(
    document: Any,
    routes_group: Any,
    conductor: Any,
    result: Any,
    corridors: list[Any],
    app_module: Any,
    part_module: Any,
) -> Any:
    name = _object_name("QETWireRoute", conductor.WireKey)
    route = document.getObject(name)
    if route is None:
        route = document.addObject("Part::FeaturePython", name)
        route.addProperty("App::PropertyString", "QET_ObjectKind", "QET Internal")
        route.QET_ObjectKind = "WireRoute"
    if not isinstance(getattr(route, "Proxy", None), WireRouteProxy):
        WireRouteProxy(route)
    route.Label = f"Route {conductor.WireNumber or conductor.Label}"
    if route not in routes_group.Group:
        routes_group.addObject(route)
    # The conductor already links to its route. A reverse App::PropertyLink
    # would create a forbidden cycle in FreeCAD's dependency graph.
    if "Conductor" in route.PropertiesList:
        try:
            route.Conductor = None
            route.removeProperty("Conductor")
        except (AttributeError, RuntimeError):
            pass
    _set_string(route, "ConductorKey", conductor.WireKey, "Wire Route")
    if "Corridors" not in route.PropertiesList:
        route.addProperty("App::PropertyLinkList", "Corridors", "Wire Route")
    route.Corridors = corridors
    if "Points" not in route.PropertiesList:
        route.addProperty("App::PropertyVectorList", "Points", "Wire Route")
    vectors = [app_module.Vector(point.x, point.y, point.z) for point in result.points]
    route.Points = vectors
    route.Shape = part_module.makePolygon(vectors)
    if "GeometricLength" not in route.PropertiesList:
        route.addProperty(
            "App::PropertyLength",
            "GeometricLength",
            "Wire Length",
            "Centerline length of the routed wire",
            1,
        )
    route.GeometricLength = result.geometric_length
    if "SlackPercent" not in route.PropertiesList:
        route.addProperty(
            "App::PropertyPercent",
            "SlackPercent",
            "Wire Length",
            "Additional length added as percentage slack",
        )
        route.SlackPercent = 0
    if "EndAllowanceEach" not in route.PropertiesList:
        route.addProperty(
            "App::PropertyLength",
            "EndAllowanceEach",
            "Wire Length",
            "Additional termination allowance added at each endpoint",
        )
        route.EndAllowanceEach = 0.0
    if "CutLength" not in route.PropertiesList:
        route.addProperty(
            "App::PropertyLength",
            "CutLength",
            "Wire Length",
            "Geometric length plus slack and endpoint allowances",
            1,
        )
    _refresh_cut_length(route)
    _set_enumeration(
        route,
        "RouteStatus",
        ["Routed", "Stale", "NoPath", "Obsolete"],
        "Routed",
        "Wire Route",
    )
    view_object, _repaired = _ensure_native_part_view_provider(route)
    if view_object is not None:
        view_object.Visibility = True
        view_object.Selectable = True
        view_object.LineColor = (0.95, 0.65, 0.1)
        view_object.LineWidth = 3.0
    return route


def _invalidate_wire_route(
    conductor: Any,
    part_module: Any,
    *,
    conductor_status: str = "NoPath",
) -> None:
    """Make a failed generated route visibly and numerically truthful."""

    conductor.RouteStatus = conductor_status
    route = getattr(conductor, "Route", None)
    if route is None:
        return
    if "Points" in route.PropertiesList:
        route.Points = []
    if "Corridors" in route.PropertiesList:
        route.Corridors = []
    route.Shape = part_module.Shape()
    if "GeometricLength" in route.PropertiesList:
        route.GeometricLength = 0.0
    if "CutLength" in route.PropertiesList:
        route.CutLength = 0.0
    if "RouteStatus" in route.PropertiesList:
        _set_enumeration(
            route,
            "RouteStatus",
            ["Routed", "Stale", "NoPath", "Obsolete"],
            "NoPath",
            "Wire Route",
        )


def _corridor_spec(obj: Any) -> CorridorSpec:
    rotation_angle = float(obj.Placement.Rotation.Angle)
    if abs(rotation_angle) > 1e-9:
        raise RoutingError(
            f"{obj.Label} is rotated; the MVP router currently requires axis-aligned corridors"
        )
    base = obj.Placement.Base
    length, width, height = _corridor_dimensions(obj)
    box = Box3(
        Point3(base.x, base.y, base.z),
        Point3(
            base.x + length,
            base.y + width,
            base.z + height,
        ),
    )
    allowed: list[float] = []
    for raw in obj.AllowedSections:
        value = parse_section_mm2(str(raw))
        if value is None:
            raise RoutingError(
                f"{obj.Label} contains invalid AllowedSections value {raw!r}"
            )
        allowed.append(value)
    return CorridorSpec(
        key=obj.Name,
        box=box,
        allowed_sections_mm2=tuple(allowed),
        capacity_mm2=_quantity_value(obj.Capacity),
        max_fill_ratio=_quantity_value(obj.MaxFillPercent) / 100,
    )


def _refresh_cut_length(route: Any) -> None:
    slack = _quantity_value(route.SlackPercent) / 100
    allowance = _quantity_value(route.EndAllowanceEach)
    geometric = _quantity_value(route.GeometricLength)
    route.CutLength = geometric * (1 + slack) + 2 * allowance


def _quantity_value(value: Any) -> float:
    return float(getattr(value, "Value", value))


def _point_from_vector(vector: Any) -> Point3:
    return Point3(float(vector.x), float(vector.y), float(vector.z))


def _next_name(document: Any, prefix: str) -> str:
    index = 1
    while document.getObject(f"{prefix}{index:03d}") is not None:
        index += 1
    return f"{prefix}{index:03d}"


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _corridor_dimensions(obj: Any) -> tuple[float, float, float]:
    return tuple(
        max(_quantity_value(getattr(obj, name)), 0.01)
        for name in ("Length", "Width", "Height")
    )


def _spreadsheet_text(value: Any) -> str:
    return "'" + str(value).replace("\n", " ").replace("\r", " ")
