# SPDX-License-Identifier: LGPL-2.1-or-later
"""Structured diagnostics emitted while normalizing a QET project."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticCode(str, Enum):
    LEGACY_ENDPOINT = "legacy_endpoint"
    MIXED_ENDPOINTS = "mixed_endpoints"
    MISSING_ELEMENT_UUID = "missing_element_uuid"
    UNRESOLVED_ELEMENT_DEFINITION = "unresolved_element_definition"
    INFERRED_TERMINAL_FROM_CONDUCTOR = "inferred_terminal_from_conductor"
    UNRESOLVED_ENDPOINT = "unresolved_endpoint"
    DUPLICATE_ELEMENT_UUID = "duplicate_element_uuid"
    DUPLICATE_TERMINAL_UUID = "duplicate_terminal_uuid"
    DUPLICATE_TERMINAL_ID = "duplicate_terminal_id"
    TERMINAL_COUNT_MISMATCH = "terminal_count_mismatch"
    LEGACY_TERMINAL_UNMATCHED = "legacy_terminal_unmatched"
    LEGACY_TERMINAL_AMBIGUOUS = "legacy_terminal_ambiguous"
    SINGLE_LINE_NOT_EXPANDED = "single_line_not_expanded"
    UNKNOWN_CONDUCTOR_TYPE = "unknown_conductor_type"
    UNSUPPORTED_ELEMENT_LINK_TYPE = "unsupported_element_link_type"
    UNPARSEABLE_SECTION = "unparseable_section"


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: DiagnosticCode
    message: str
    diagram: str = ""
    item: str = ""
