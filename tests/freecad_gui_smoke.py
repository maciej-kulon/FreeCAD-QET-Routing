# SPDX-License-Identifier: LGPL-2.1-or-later
"""Short GUI registration smoke test for an installed FreeCAD runtime."""

import FreeCADGui as Gui
from PySide import QtCore

from freecad.QetRouting import init_gui
from freecad.QetRouting.commands import COMMANDS

assert "QetRoutingWorkbench" in Gui.listWorkbenches()
assert init_gui.QetRoutingWorkbench is not None
Gui.activateWorkbench("QetRoutingWorkbench")
registered_commands = set(Gui.listCommands())
assert set(COMMANDS) <= registered_commands
print("QET_ROUTING_GUI_SMOKE_OK")

QtCore.QTimer.singleShot(0, Gui.getMainWindow().close)
