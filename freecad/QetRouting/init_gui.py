# SPDX-License-Identifier: LGPL-2.1-or-later
"""GUI initialization for the QET Routing workbench."""

import FreeCADGui as Gui

from . import ICON_DIR


class QetRoutingWorkbench(Gui.Workbench):
    MenuText = "QET Routing"
    ToolTip = "Create physical wire routes from QElectroTech connectivity"
    Icon = str(ICON_DIR / "Logo.svg")

    def Initialize(self) -> None:
        from . import commands

        commands.register()
        self.appendToolbar("QET Routing", commands.COMMANDS)
        self.appendMenu("QET Routing", commands.COMMANDS)

    def Activated(self) -> None:
        from .terminal_visibility import activate_terminal_visibility

        activate_terminal_visibility()

    def Deactivated(self) -> None:
        from .terminal_visibility import deactivate_terminal_visibility

        deactivate_terminal_visibility()

    def GetClassName(self) -> str:
        return "Gui::PythonWorkbench"


Gui.addWorkbench(QetRoutingWorkbench())
