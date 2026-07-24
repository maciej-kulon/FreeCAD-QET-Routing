# SPDX-License-Identifier: LGPL-2.1-or-later
"""Short GUI registration smoke test for an installed FreeCAD runtime."""

import sys

import FreeCAD
import FreeCADGui as Gui


try:
    # A positional .py file runs after FreeCADGuiInit. Assert auto-discovery
    # before importing any workbench module here, so the probe cannot mask a
    # broken namespaced-addon installation.
    assert "freecad.QetRouting.init_gui" in sys.modules
    assert "QetRoutingWorkbench" in Gui.listWorkbenches()

    from freecad.QetRouting.commands import COMMANDS

    Gui.activateWorkbench("QetRoutingWorkbench")
    registered_commands = set(Gui.listCommands())
    assert set(COMMANDS) <= registered_commands
    FreeCAD.Console.PrintMessage("QET_ROUTING_GUI_SMOKE_OK\n")
finally:
    # Close synchronously through FreeCAD's normal window lifecycle. A custom
    # QTimer can outlive Python teardown in some macOS/Qt builds.
    Gui.getMainWindow().close()
