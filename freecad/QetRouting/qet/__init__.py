# SPDX-License-Identifier: LGPL-2.1-or-later
"""QElectroTech project parsing and normalized domain models."""

from .diagnostics import Diagnostic, DiagnosticCode, Severity
from .model import (
    ConductorKind,
    DeviceTypeIdentity,
    QetConductor,
    QetElement,
    QetElementLink,
    QetEndpoint,
    QetProject,
    QetTerminal,
)
from .parser import ParseResult, QetParseError, parse_qet, parse_qet_bytes

__all__ = [
    "ConductorKind",
    "DeviceTypeIdentity",
    "Diagnostic",
    "DiagnosticCode",
    "ParseResult",
    "QetConductor",
    "QetElement",
    "QetElementLink",
    "QetEndpoint",
    "QetParseError",
    "QetProject",
    "QetTerminal",
    "Severity",
    "parse_qet",
    "parse_qet_bytes",
]
