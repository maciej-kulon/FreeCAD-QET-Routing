# SPDX-License-Identifier: LGPL-2.1-or-later
"""Normalized, immutable QElectroTech domain model."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from enum import Enum


class ConductorKind(str, Enum):
    MULTILINE = "multi"
    SINGLE_LINE = "single"
    UNKNOWN = "unknown"


def _identity_token(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", value)


@dataclass(frozen=True)
class DeviceTypeIdentity:
    """Stable reusable layout key, independent from a placed device label."""

    key: str
    manufacturer: str = ""
    article_number: str = ""
    variant: str = ""

    @classmethod
    def from_element(cls, element: QetElement) -> DeviceTypeIdentity:
        manufacturer = element.manufacturer.strip()
        article = element.article_number.strip()
        variant = element.information.get("variant", "").strip()
        if manufacturer and article:
            key = (
                f"manufacturer={_identity_token(manufacturer)}"
                f"|article={_identity_token(article)}"
                f"|variant={_identity_token(variant)}"
            )
        elif element.definition_uuid:
            key = f"qet-definition={_identity_token(element.definition_uuid)}"
        elif element.type_path:
            key = f"qet-type={_identity_token(element.type_path)}"
        else:
            key = f"untyped={_identity_token(element.label or element.uuid)}"
        return cls(
            key=key,
            manufacturer=manufacturer,
            article_number=article,
            variant=variant,
        )


@dataclass(frozen=True)
class QetTerminal:
    element_uuid: str
    definition_uuid: str = ""
    local_id: str = ""
    name: str = ""
    schematic_position: tuple[float, float] | None = None
    orientation: str = ""

    @property
    def pin_key(self) -> str:
        return self.name or self.definition_uuid or self.local_id

    @property
    def layout_key(self) -> str:
        """Stable internal pin identity; display names are not assumed unique."""

        if self.definition_uuid:
            return f"uuid={self.definition_uuid}"
        position = self.schematic_position
        if position is not None:
            return (
                f"name={_identity_token(self.name)}"
                f"|x={position[0]:.12g}|y={position[1]:.12g}"
                f"|orientation={_identity_token(self.orientation)}"
            )
        if self.name:
            return f"name={_identity_token(self.name)}"
        if self.local_id:
            return f"legacy-id={_identity_token(self.local_id)}"
        return ""


@dataclass(frozen=True)
class QetElementLink:
    """A QET cross-reference from one schematic element to another."""

    element_uuid: str
    group_index: int | None = None


@dataclass(frozen=True)
class QetElement:
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
    information: dict[str, str] = field(default_factory=dict)
    terminals: tuple[QetTerminal, ...] = ()
    links: tuple[QetElementLink, ...] = ()
    physical_device_uuid: str = ""
    physical_fragment_slot: str = ""
    fragment_uuids: tuple[str, ...] = ()

    @property
    def device_type(self) -> DeviceTypeIdentity:
        return DeviceTypeIdentity.from_element(self)

    @property
    def occurrence_key(self) -> tuple[str, str, str]:
        return self.plant, self.location, self.label


@dataclass(frozen=True)
class QetEndpoint:
    element_uuid: str
    terminal_uuid: str = ""
    terminal_name: str = ""
    legacy_terminal_id: str = ""
    resolved: bool = False

    @property
    def identity(self) -> str:
        if self.element_uuid and self.terminal_uuid:
            return f"{self.element_uuid}:{self.terminal_uuid}"
        if self.element_uuid and self.legacy_terminal_id:
            return f"{self.element_uuid}:legacy:{self.legacy_terminal_id}"
        if self.element_uuid and self.terminal_name:
            return f"{self.element_uuid}:name:{self.terminal_name}"
        return f"unresolved:{self.legacy_terminal_id or self.terminal_name}"


@dataclass(frozen=True)
class QetConductor:
    key: str
    diagram: str
    kind: ConductorKind
    endpoint_a: QetEndpoint
    endpoint_b: QetEndpoint
    number: str = ""
    function: str = ""
    voltage: str = ""
    color: str = ""
    raw_section: str = ""
    section_mm2: float | None = None
    cable: str = ""
    bus: str = ""

    @property
    def is_routeable(self) -> bool:
        return (
            self.kind is ConductorKind.MULTILINE
            and self.endpoint_a.resolved
            and self.endpoint_b.resolved
            and self.endpoint_a.identity != self.endpoint_b.identity
        )


@dataclass(frozen=True)
class QetProject:
    title: str
    version: str
    source_path: str
    fingerprint: str
    elements: tuple[QetElement, ...]
    conductors: tuple[QetConductor, ...]

    @property
    def routeable_conductors(self) -> tuple[QetConductor, ...]:
        return tuple(conductor for conductor in self.conductors if conductor.is_routeable)

    @property
    def physical_devices(self) -> tuple[QetElement, ...]:
        """Return validated physical devices while preserving raw fragments.

        A simple element is a one-fragment device. A linked master and its
        slaves become one aggregate whose UUID and metadata come from the
        master. Terminal objects are not rewritten, so conductor endpoint
        identities continue to refer to their original schematic fragments.
        """

        grouped: dict[str, list[QetElement]] = {}
        for element in self.elements:
            physical_uuid = element.physical_device_uuid
            if (
                not physical_uuid
                and not element.fragment_uuids
                and element.link_type.strip().casefold()
                in {"", "simple", "master", "terminal"}
            ):
                # Preserve compatibility with callers that construct the
                # pre-grouping model directly. Parsed fragments always carry
                # fragment_uuids, including deliberately excluded symbols.
                physical_uuid = element.uuid
            if physical_uuid:
                grouped.setdefault(physical_uuid, []).append(element)

        devices: list[QetElement] = []
        for physical_uuid, fragments in grouped.items():
            master = next(
                (fragment for fragment in fragments if fragment.uuid == physical_uuid),
                None,
            )
            if master is None:
                # Parser validation should make this impossible. Treat malformed
                # manually-created models conservatively instead of inventing an
                # aggregate identity.
                continue
            ordered = sorted(
                fragments,
                key=lambda fragment: (
                    0 if fragment.uuid == physical_uuid else 1,
                    fragment.physical_fragment_slot,
                    fragment.uuid,
                ),
            )
            devices.append(
                replace(
                    master,
                    terminals=tuple(
                        terminal
                        for fragment in ordered
                        for terminal in fragment.terminals
                    ),
                    fragment_uuids=tuple(fragment.uuid for fragment in ordered),
                )
            )
        return tuple(devices)

    def element_by_uuid(self) -> dict[str, QetElement]:
        return {element.uuid: element for element in self.elements}
