# SPDX-License-Identifier: LGPL-2.1-or-later
"""Safe normalization of QElectroTech project XML.

QET has used two conductor endpoint encodings. Each endpoint is detected
independently: current files provide an element UUID plus a terminal-definition
UUID, while legacy endpoints contain only a diagram-local numeric terminal ID.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from .diagnostics import Diagnostic, DiagnosticCode, Severity
from .model import (
    ConductorKind,
    QetConductor,
    QetElement,
    QetEndpoint,
    QetProject,
    QetTerminal,
)

DEFAULT_MAX_PROJECT_BYTES = 64 * 1024 * 1024
MAX_TERMINALS_PER_ELEMENT = 4096
_NUMBER_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+))"
    r"\s*(?:mm\s*(?:²|\^?2)|sq\.?\s*mm)?\s*$",
    re.IGNORECASE,
)


class QetParseError(ValueError):
    """Raised when a file cannot safely be interpreted as a QET project."""


@dataclass(frozen=True)
class ParseResult:
    project: QetProject
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def has_errors(self) -> bool:
        return any(item.severity is Severity.ERROR for item in self.diagnostics)


@dataclass(frozen=True)
class _TerminalDefinition:
    uuid: str
    name: str
    schematic_position: tuple[float, float] | None
    orientation: str


@dataclass(frozen=True)
class _ElementDefinition:
    path: str
    uuid: str
    link_type: str
    terminals: tuple[_TerminalDefinition, ...]


@dataclass
class _ElementBuilder:
    uuid: str
    type_path: str
    definition_uuid: str
    link_type: str
    label: str
    manufacturer: str
    article_number: str
    order_number: str
    internal_number: str
    plant: str
    location: str
    diagram: str
    folio_order: str
    information: dict[str, str]
    definition_available: bool
    terminals: list[QetTerminal] = field(default_factory=list)

    def freeze(self) -> QetElement:
        return QetElement(
            uuid=self.uuid,
            type_path=self.type_path,
            definition_uuid=self.definition_uuid,
            link_type=self.link_type,
            label=self.label,
            manufacturer=self.manufacturer,
            article_number=self.article_number,
            order_number=self.order_number,
            internal_number=self.internal_number,
            plant=self.plant,
            location=self.location,
            diagram=self.diagram,
            folio_order=self.folio_order,
            information=dict(self.information),
            terminals=tuple(self.terminals),
        )


def parse_qet(
    path: str | Path,
    *,
    max_project_bytes: int = DEFAULT_MAX_PROJECT_BYTES,
) -> ParseResult:
    """Parse a QET project from disk without resolving external element libraries."""

    if max_project_bytes < 0:
        raise QetParseError("Maximum project size must not be negative")
    source = Path(path)
    try:
        with source.open("rb") as stream:
            data = stream.read(max_project_bytes + 1)
    except OSError as exc:
        raise QetParseError(f"Cannot read QET project {source}: {exc}") from exc
    if len(data) > max_project_bytes:
        raise QetParseError(
            f"QET project exceeds the configured maximum of {max_project_bytes} bytes"
        )
    return parse_qet_bytes(
        data,
        source_path=str(source.resolve()),
        max_project_bytes=max_project_bytes,
    )


def parse_qet_bytes(
    data: bytes,
    *,
    source_path: str = "",
    max_project_bytes: int = DEFAULT_MAX_PROJECT_BYTES,
) -> ParseResult:
    """Parse QET XML bytes into an immutable normalized model."""

    if max_project_bytes < 0:
        raise QetParseError("Maximum project size must not be negative")
    if len(data) > max_project_bytes:
        raise QetParseError(
            f"QET project is {len(data)} bytes; configured maximum is {max_project_bytes}"
        )
    # ElementTree does not resolve external entities, but rejecting a DTD also
    # prevents entity-expansion payloads and keeps accepted input deterministic.
    declaration_scan = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise QetParseError("QET projects containing DTD/entity declarations are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise QetParseError(f"Invalid QET XML: {exc}") from exc
    if _local_name(root.tag) != "project":
        raise QetParseError(f"Expected <project> root, got <{_local_name(root.tag)}>")
    project_version = root.get("version", "").strip()
    _validate_project_version(project_version)

    fingerprint = hashlib.sha256(data).hexdigest()
    diagnostics: list[Diagnostic] = []
    definitions = _index_embedded_definitions(root)
    builders: list[_ElementBuilder] = []
    builders_by_uuid: dict[str, _ElementBuilder] = {}
    builders_by_diagram: dict[str, dict[str, _ElementBuilder]] = {}
    ambiguous_element_uuids: set[str] = set()
    conductor_nodes: list[tuple[ET.Element, str, str]] = []
    legacy_by_diagram: dict[str, dict[str, QetTerminal | None]] = {}
    current_terminals_by_diagram: dict[
        str,
        dict[tuple[str, str], QetTerminal | None],
    ] = {}
    ambiguous_terminals_by_diagram: dict[str, set[tuple[str, str]]] = {}

    for diagram_index, diagram_node in enumerate(_diagram_nodes(root), start=1):
        folio_order = diagram_node.get("order", str(diagram_index))
        diagram_name = (
            diagram_node.get("title")
            or diagram_node.get("name")
            or f"folio-{folio_order}"
        )
        diagram_key = f"{diagram_index}:{folio_order}:{diagram_name}"
        legacy_terminals: dict[str, QetTerminal | None] = {}
        diagram_builders: dict[str, _ElementBuilder] = {}
        diagram_current_terminals: dict[tuple[str, str], QetTerminal | None] = {}
        diagram_ambiguous_terminals: set[tuple[str, str]] = set()
        elements_parent = _first_child(diagram_node, "elements")
        element_nodes = (
            _direct_children(elements_parent, "element") if elements_parent is not None else []
        )
        for element_index, node in enumerate(element_nodes, start=1):
            raw_uuid = node.get("uuid", "")
            element_uuid = _canonical_uuid(raw_uuid)
            if not element_uuid:
                element_uuid = f"legacy-element:{diagram_index}:{element_index}"
                diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        DiagnosticCode.MISSING_ELEMENT_UUID,
                        f"Element has no UUID; assigned compatibility key {element_uuid}",
                        diagram_name,
                        node.get("type", ""),
                    )
                )
            if element_uuid in builders_by_uuid:
                ambiguous_element_uuids.add(element_uuid)
                diagnostics.append(
                    Diagnostic(
                        Severity.ERROR,
                        DiagnosticCode.DUPLICATE_ELEMENT_UUID,
                        f"Element UUID {element_uuid} occurs more than once",
                        diagram_name,
                        element_uuid,
                    )
                )
                element_uuid = f"{element_uuid}#duplicate-{element_index}"

            information = _element_information(node)
            type_path = node.get("type", "").strip()
            definition = definitions.get(_normalize_type_path(type_path))
            builder = _ElementBuilder(
                uuid=element_uuid,
                type_path=type_path,
                definition_uuid=definition.uuid if definition is not None else "",
                link_type=definition.link_type if definition is not None else "",
                label=information.get("label", "").strip(),
                manufacturer=_information_value(information, "manufacturer"),
                article_number=_information_value(information, "designation"),
                order_number=_information_value(
                    information,
                    "manufacturer_reference",
                ),
                internal_number=_information_value(
                    information,
                    "machine_manufacturer_reference",
                ),
                plant=_information_value(information, "plant"),
                location=_information_value(information, "location"),
                diagram=diagram_name,
                folio_order=folio_order,
                information=information,
                definition_available=definition is not None,
            )
            terminals_parent = _first_child(node, "terminals")
            placed_nodes = (
                _direct_children(terminals_parent, "terminal")
                if terminals_parent is not None
                else []
            )
            if definition is None and placed_nodes:
                diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        DiagnosticCode.UNRESOLVED_ELEMENT_DEFINITION,
                        (
                            f"Element definition {type_path or '<missing>'} is unavailable; "
                            "connected terminal names will be recovered from conductors"
                        ),
                        diagram_name,
                        builder.label or builder.uuid,
                    )
                )
            if definition is not None and not _supports_physical_terminal(definition.link_type):
                diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        DiagnosticCode.UNSUPPORTED_ELEMENT_LINK_TYPE,
                        (
                            f"Element link type {definition.link_type!r} is not mapped "
                            "to a physical FreeCAD device in this MVP"
                        ),
                        diagram_name,
                        builder.label or builder.uuid,
                    )
                )
            definition_terminals = definition.terminals if definition is not None else ()
            if (
                len(placed_nodes) > MAX_TERMINALS_PER_ELEMENT
                or len(definition_terminals) > MAX_TERMINALS_PER_ELEMENT
            ):
                raise QetParseError(
                    f"Element {builder.label or builder.uuid} exceeds "
                    f"{MAX_TERMINALS_PER_ELEMENT} terminals"
                )
            if definition is not None and len(definition_terminals) != len(placed_nodes):
                diagnostics.append(
                    Diagnostic(
                        Severity.WARNING,
                        DiagnosticCode.TERMINAL_COUNT_MISMATCH,
                        (
                            f"{type_path} defines {len(definition_terminals)} terminals "
                            f"but its instance contains {len(placed_nodes)}"
                        ),
                        diagram_name,
                        builder.label or builder.uuid,
                    )
                )

            if definition is not None:
                unmatched_placed = set(range(len(placed_nodes)))
                for terminal_definition in definition_terminals:
                    matches = [
                        index
                        for index in unmatched_placed
                        if _legacy_terminal_matches(placed_nodes[index], terminal_definition)
                    ]
                    local_id = ""
                    if len(matches) == 1:
                        match_index = matches[0]
                        unmatched_placed.remove(match_index)
                        local_id = _normalize_legacy_id(
                            placed_nodes[match_index].get("id", "")
                        )
                    elif len(matches) > 1:
                        diagnostics.append(
                            Diagnostic(
                                Severity.ERROR,
                                DiagnosticCode.LEGACY_TERMINAL_AMBIGUOUS,
                                (
                                    f"Several legacy terminal records match pin "
                                    f"{terminal_definition.name or terminal_definition.uuid}"
                                ),
                                diagram_name,
                                builder.label or builder.uuid,
                            )
                        )
                    terminal = QetTerminal(
                        element_uuid=element_uuid,
                        definition_uuid=terminal_definition.uuid,
                        local_id=local_id,
                        name=_normalize_pin_name(terminal_definition.name),
                        schematic_position=terminal_definition.schematic_position,
                        orientation=terminal_definition.orientation,
                    )
                    builder.terminals.append(terminal)
                    if local_id:
                        _add_legacy_terminal(
                            local_id,
                            terminal,
                            legacy_terminals,
                            diagnostics,
                            diagram_name,
                        )
                    if terminal.definition_uuid:
                        terminal_key = (element_uuid, terminal.definition_uuid)
                        if terminal_key in diagram_current_terminals:
                            diagram_current_terminals[terminal_key] = None
                            diagram_ambiguous_terminals.add(terminal_key)
                            diagnostics.append(
                                Diagnostic(
                                    Severity.ERROR,
                                    DiagnosticCode.DUPLICATE_TERMINAL_UUID,
                                    (
                                        f"Terminal UUID {terminal.definition_uuid} "
                                        f"is duplicated on element {element_uuid}"
                                    ),
                                    diagram_name,
                                    builder.label or builder.uuid,
                                )
                            )
                        else:
                            diagram_current_terminals[terminal_key] = terminal
                for unmatched_index in sorted(unmatched_placed):
                    terminal_node = placed_nodes[unmatched_index]
                    local_id = _normalize_legacy_id(terminal_node.get("id", ""))
                    builder.terminals.append(
                        QetTerminal(
                            element_uuid=element_uuid,
                            local_id=local_id,
                            schematic_position=_schematic_position(terminal_node),
                            orientation=terminal_node.get("orientation", ""),
                        )
                    )
                    diagnostics.append(
                        Diagnostic(
                            Severity.WARNING,
                            DiagnosticCode.LEGACY_TERMINAL_UNMATCHED,
                            (
                                f"Legacy terminal ID {local_id or '<missing>'} does not "
                                "uniquely match a definition pin"
                            ),
                            diagram_name,
                            builder.label or builder.uuid,
                        )
                    )
            else:
                # Placed terminal records do not contain stable terminal UUIDs
                # or names. Connected current-format pins are inferred later
                # from conductor UUID endpoints; anonymous spheres would be
                # misleading and are intentionally not created here.
                pass

            builders.append(builder)
            builders_by_uuid[element_uuid] = builder
            diagram_builders[element_uuid] = builder

        legacy_by_diagram[diagram_key] = legacy_terminals
        builders_by_diagram[diagram_key] = diagram_builders
        current_terminals_by_diagram[diagram_key] = diagram_current_terminals
        ambiguous_terminals_by_diagram[diagram_key] = diagram_ambiguous_terminals
        conductors_parent = _first_child(diagram_node, "conductors")
        if conductors_parent is not None:
            conductor_nodes.extend(
                (node, diagram_name, diagram_key)
                for node in _direct_children(conductors_parent, "conductor")
            )

    conductors: list[QetConductor] = []
    occurrence_by_signature: dict[str, int] = {}
    for node, diagram_name, diagram_key in conductor_nodes:
        endpoint_a, mode_a = _resolve_endpoint(
            node,
            1,
            diagram_name,
            builders_by_diagram[diagram_key],
            legacy_by_diagram[diagram_key],
            current_terminals_by_diagram[diagram_key],
            ambiguous_element_uuids,
            ambiguous_terminals_by_diagram[diagram_key],
            diagnostics,
        )
        endpoint_b, mode_b = _resolve_endpoint(
            node,
            2,
            diagram_name,
            builders_by_diagram[diagram_key],
            legacy_by_diagram[diagram_key],
            current_terminals_by_diagram[diagram_key],
            ambiguous_element_uuids,
            ambiguous_terminals_by_diagram[diagram_key],
            diagnostics,
        )
        if mode_a == "legacy" or mode_b == "legacy":
            diagnostics.append(
                Diagnostic(
                    Severity.INFO,
                    DiagnosticCode.LEGACY_ENDPOINT,
                    "Conductor uses a legacy diagram-local terminal ID",
                    diagram_name,
                    node.get("num", ""),
                )
            )
        if mode_a != mode_b:
            diagnostics.append(
                Diagnostic(
                    Severity.INFO,
                    DiagnosticCode.MIXED_ENDPOINTS,
                    "Conductor mixes UUID and legacy endpoint encodings",
                    diagram_name,
                    node.get("num", ""),
                )
            )

        raw_kind = node.get("type", "")
        kind = _conductor_kind(raw_kind)
        if kind is ConductorKind.SINGLE_LINE:
            diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    DiagnosticCode.SINGLE_LINE_NOT_EXPANDED,
                    "Single-line conductor was imported but is not a physical route",
                    diagram_name,
                    node.get("num", ""),
                )
            )
        elif raw_kind.strip().casefold() not in {"", "multi", "multiline"}:
            diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    DiagnosticCode.UNKNOWN_CONDUCTOR_TYPE,
                    (
                        f"Unknown conductor type {raw_kind!r}; "
                        "physical routing was blocked"
                    ),
                    diagram_name,
                    node.get("num", ""),
                )
            )

        raw_section = node.get("conductor_section", "").strip()
        section_mm2 = parse_section_mm2(raw_section)
        if raw_section and section_mm2 is None:
            diagnostics.append(
                Diagnostic(
                    Severity.WARNING,
                    DiagnosticCode.UNPARSEABLE_SECTION,
                    f"Wire section {raw_section!r} is not a supported mm² value",
                    diagram_name,
                    node.get("num", ""),
                )
            )
        signature = "|".join(sorted((endpoint_a.identity, endpoint_b.identity)))
        occurrence = occurrence_by_signature.get(signature, 0)
        occurrence_by_signature[signature] = occurrence + 1
        key_material = f"{signature}|parallel-edge={occurrence}"
        conductor_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:24]
        conductors.append(
            QetConductor(
                key=conductor_key,
                diagram=diagram_name,
                kind=kind,
                endpoint_a=endpoint_a,
                endpoint_b=endpoint_b,
                number=node.get("num", "").strip(),
                function=node.get("function", "").strip(),
                voltage=node.get("tension_protocol", "").strip(),
                color=node.get("conductor_color", "").strip(),
                raw_section=raw_section,
                section_mm2=section_mm2,
                cable=node.get("cable", "").strip(),
                bus=node.get("bus", "").strip(),
            )
        )

    project = QetProject(
        title=root.get("title", "").strip(),
        version=project_version,
        source_path=source_path,
        fingerprint=fingerprint,
        elements=tuple(builder.freeze() for builder in builders),
        conductors=tuple(conductors),
    )
    return ParseResult(project=project, diagnostics=tuple(diagnostics))


def _validate_project_version(raw: str) -> None:
    match = re.fullmatch(r"0\.(\d+)(?:\.\d+)*", raw)
    if match is None:
        raise QetParseError(
            "QET project version is missing or invalid; open and save the project "
            "with QElectroTech 0.7 through 0.100 before importing"
        )
    minor = int(match.group(1))
    release = minor * 10 if minor in {7, 8, 9} else minor
    if not 70 <= release <= 100:
        raise QetParseError(
            f"QET project version {raw!r} is unsupported; this workbench accepts "
            "QElectroTech project formats 0.7 through 0.100"
        )


def parse_section_mm2(raw: str) -> float | None:
    """Parse the conservative subset of QET section strings that denotes mm²."""

    if not raw.strip():
        return None
    match = _NUMBER_RE.fullmatch(raw)
    if match is None:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def _resolve_endpoint(
    conductor: ET.Element,
    side: int,
    diagram_name: str,
    builders_by_uuid: dict[str, _ElementBuilder],
    legacy_terminals: dict[str, QetTerminal | None],
    current_terminals: dict[tuple[str, str], QetTerminal | None],
    ambiguous_element_uuids: set[str],
    ambiguous_terminals: set[tuple[str, str]],
    diagnostics: list[Diagnostic],
) -> tuple[QetEndpoint, str]:
    raw_element_uuid = conductor.get(f"element{side}", "").strip()
    raw_terminal = conductor.get(f"terminal{side}", "").strip()
    terminal_name = conductor.get(f"terminalname{side}", "").strip()
    if raw_element_uuid:
        element_uuid = _canonical_uuid(raw_element_uuid)
        terminal_uuid = _canonical_uuid(raw_terminal)
        terminal_key = (element_uuid, terminal_uuid)
        terminal = current_terminals.get(terminal_key)
        builder = (
            None
            if element_uuid in ambiguous_element_uuids
            else builders_by_uuid.get(element_uuid)
        )
        if builder is not None and not _supports_physical_terminal(builder.link_type):
            builder = None
        if (
            terminal is None
            and builder is not None
            and not builder.definition_available
            and terminal_uuid
            and terminal_key not in ambiguous_terminals
        ):
            # Current QET conductors carry the stable terminal-definition UUID.
            # An external symbol library is therefore not required merely to
            # create the connected physical pin; only its full pin inventory is
            # unavailable.
            recovered = next(
                (
                    item
                    for item in builder.terminals
                    if item.definition_uuid == terminal_uuid and terminal_uuid
                ),
                None,
            )
            if recovered is None:
                recovered = QetTerminal(
                    element_uuid=element_uuid,
                    definition_uuid=terminal_uuid,
                    name=_normalize_pin_name(terminal_name),
                )
                builder.terminals.append(recovered)
                diagnostics.append(
                    Diagnostic(
                        Severity.INFO,
                        DiagnosticCode.INFERRED_TERMINAL_FROM_CONDUCTOR,
                        (
                            f"Endpoint {side} terminal {terminal_uuid} was inferred "
                            "from the conductor because its element definition is unavailable"
                        ),
                        diagram_name,
                        conductor.get("num", ""),
                    )
                )
            current_terminals[terminal_key] = recovered
            terminal = recovered
        resolved = terminal is not None and builder is not None
        if not resolved:
            diagnostics.append(
                Diagnostic(
                    Severity.ERROR,
                    DiagnosticCode.UNRESOLVED_ENDPOINT,
                    (
                        f"Cannot resolve endpoint {side}: element={raw_element_uuid!r}, "
                        f"terminal={raw_terminal!r}"
                    ),
                    diagram_name,
                    conductor.get("num", ""),
                )
            )
        return (
            QetEndpoint(
                element_uuid=element_uuid,
                terminal_uuid=terminal_uuid,
                terminal_name=terminal_name or (terminal.name if terminal else ""),
                resolved=resolved,
            ),
            "uuid",
        )

    normalized_legacy_id = _normalize_legacy_id(raw_terminal)
    terminal = legacy_terminals.get(normalized_legacy_id)
    resolved = terminal is not None
    if not resolved:
        diagnostics.append(
            Diagnostic(
                Severity.ERROR,
                DiagnosticCode.UNRESOLVED_ENDPOINT,
                f"Cannot resolve legacy endpoint {side}: terminal ID {raw_terminal!r}",
                diagram_name,
                conductor.get("num", ""),
            )
        )
    return (
        QetEndpoint(
            element_uuid=terminal.element_uuid if terminal else "",
            terminal_uuid=terminal.definition_uuid if terminal else "",
            terminal_name=terminal_name or (terminal.name if terminal else ""),
            legacy_terminal_id=normalized_legacy_id,
            resolved=resolved,
        ),
        "legacy",
    )


def _index_embedded_definitions(root: ET.Element) -> dict[str, _ElementDefinition]:
    result: dict[str, _ElementDefinition] = {}
    for collection in _direct_children(root, "collection"):
        _walk_collection(collection, (), result)
    return result


def _walk_collection(
    node: ET.Element,
    path: tuple[str, ...],
    result: dict[str, _ElementDefinition],
    depth: int = 0,
) -> None:
    if depth > 256:
        raise QetParseError("QET element collection nesting exceeds the supported limit")
    for child in list(node):
        tag = _local_name(child.tag)
        if tag == "category":
            name = child.get("name", "").strip()
            _walk_collection(
                child,
                (*path, name) if name else path,
                result,
                depth + 1,
            )
            continue
        if tag == "element":
            definition = _first_descendant(child, "definition")
            if definition is not None:
                filename = (
                    child.get("name", "").strip()
                    or child.get("file", "").strip()
                    or child.get("filename", "").strip()
                )
                definition_path = "/".join((*path, filename)) if filename else "/".join(path)
                terminals_parent = _first_child(definition, "description")
                terminals = []
                if terminals_parent is not None:
                    for terminal in _descendants(terminals_parent, "terminal"):
                        if len(terminals) >= MAX_TERMINALS_PER_ELEMENT:
                            raise QetParseError(
                                f"Element definition {definition_path or '<unknown>'} "
                                f"exceeds {MAX_TERMINALS_PER_ELEMENT} terminals"
                            )
                        terminals.append(
                            _TerminalDefinition(
                                uuid=_canonical_uuid(terminal.get("uuid", "")),
                                name=_normalize_pin_name(terminal.get("name", "")),
                                schematic_position=_schematic_position(terminal),
                                orientation=terminal.get("orientation", "").strip(),
                            )
                        )
                uuid_node = _first_child(definition, "uuid")
                definition_uuid = (
                    _canonical_uuid(uuid_node.get("uuid", "")) if uuid_node is not None else ""
                )
                item = _ElementDefinition(
                    path=definition_path,
                    uuid=definition_uuid,
                    link_type=definition.get("link_type", "").strip().casefold(),
                    terminals=tuple(terminals),
                )
                if definition_path:
                    result[_normalize_type_path(f"embed://{definition_path}")] = item
            continue
        _walk_collection(child, path, result, depth + 1)


def _diagram_nodes(root: ET.Element) -> list[ET.Element]:
    diagrams: list[ET.Element] = []
    for container_name in ("newdiagrams", "diagrams"):
        container = _first_child(root, container_name)
        if container is not None:
            diagrams.extend(_direct_children(container, "diagram"))
    if diagrams:
        return diagrams
    return _direct_children(root, "diagram")


def _element_information(node: ET.Element) -> dict[str, str]:
    parent = _first_child(node, "elementInformations")
    if parent is None:
        return {}
    result: dict[str, str] = {}
    for item in _direct_children(parent, "elementInformation"):
        name = item.get("name", "").strip()
        if not name:
            continue
        value = "".join(item.itertext()).strip() or item.get("value", "").strip()
        result[name.replace("-", "_")] = value
    return result


def _information_value(information: dict[str, str], *names: str) -> str:
    for name in names:
        value = information.get(name.replace("-", "_"), "").strip()
        if value:
            return value
    return ""


def _schematic_position(node: ET.Element) -> tuple[float, float] | None:
    try:
        position = float(node.get("x", "")), float(node.get("y", ""))
    except ValueError:
        return None
    return position if all(math.isfinite(value) for value in position) else None


def _conductor_kind(raw: str) -> ConductorKind:
    normalized = raw.strip().casefold()
    if normalized in {"", "multi", "multiline"}:
        return ConductorKind.MULTILINE
    if normalized in {"single", "singleline"}:
        return ConductorKind.SINGLE_LINE
    return ConductorKind.UNKNOWN


def _supports_physical_terminal(link_type: str) -> bool:
    # QET may add new schematic-only link types over time. Treating an
    # unfamiliar type as a physical device could silently create a wire to a
    # cross-reference symbol, so physical routing is intentionally opt-in.
    return link_type.strip().casefold() in {"", "simple", "master", "terminal"}


def _canonical_uuid(raw: str) -> str:
    value = raw.strip().strip("{}")
    if not value:
        return ""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return ""
    return "" if parsed.int == 0 else str(parsed)


def _normalize_pin_name(raw: str) -> str:
    value = raw.strip()
    return "" if value == "_" else value


def _normalize_legacy_id(raw: str) -> str:
    value = raw.strip()
    try:
        parsed = int(value, 10)
    except ValueError:
        return ""
    if parsed < 0 or parsed > 2_147_483_647:
        return ""
    return str(parsed)


def _orientation_number(raw: str) -> str:
    value = raw.strip().casefold()
    return {"n": "0", "e": "1", "s": "2", "w": "3"}.get(value, value)


def _legacy_terminal_matches(
    placed: ET.Element,
    definition: _TerminalDefinition,
    *,
    tolerance: float = 1e-6,
) -> bool:
    placed_position = _schematic_position(placed)
    definition_position = definition.schematic_position
    if placed_position is None or definition_position is None:
        return False
    if abs(placed_position[0] - definition_position[0]) > tolerance:
        return False
    if abs(placed_position[1] - definition_position[1]) > tolerance:
        return False
    return _orientation_number(placed.get("orientation", "")) == _orientation_number(
        definition.orientation
    )


def _add_legacy_terminal(
    local_id: str,
    terminal: QetTerminal,
    legacy_terminals: dict[str, QetTerminal | None],
    diagnostics: list[Diagnostic],
    diagram_name: str,
) -> None:
    if local_id in legacy_terminals:
        legacy_terminals[local_id] = None
        diagnostics.append(
            Diagnostic(
                Severity.ERROR,
                DiagnosticCode.DUPLICATE_TERMINAL_ID,
                f"Diagram-local terminal ID {local_id} is duplicated",
                diagram_name,
                local_id,
            )
        )
    else:
        legacy_terminals[local_id] = terminal


def _normalize_type_path(raw: str) -> str:
    value = unquote(raw.strip()).replace("\\", "/")
    if "://" in value:
        scheme, path = value.split("://", 1)
        return f"{scheme.casefold()}://{path.strip('/').casefold()}"
    return value.strip("/").casefold()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_children(node: ET.Element | None, name: str) -> list[ET.Element]:
    if node is None:
        return []
    return [child for child in list(node) if _local_name(child.tag) == name]


def _first_child(node: ET.Element, name: str) -> ET.Element | None:
    return next(iter(_direct_children(node, name)), None)


def _descendants(node: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in node.iter() if item is not node and _local_name(item.tag) == name]


def _first_descendant(node: ET.Element, name: str) -> ET.Element | None:
    return next(iter(_descendants(node, name)), None)
