# SPDX-License-Identifier: LGPL-2.1-or-later
"""QElectroTech-driven physical wire routing for FreeCAD."""

from pathlib import Path

__version__ = "0.1.0"

PACKAGE_DIR = Path(__file__).resolve().parent
ADDON_ROOT = PACKAGE_DIR.parents[1]
RESOURCE_DIR = ADDON_ROOT / "Resources"
ICON_DIR = RESOURCE_DIR / "Icons"
