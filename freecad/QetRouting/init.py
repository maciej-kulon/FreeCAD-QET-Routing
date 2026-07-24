# SPDX-License-Identifier: LGPL-2.1-or-later
"""Application-side initialization for the QET Routing extension."""

# The domain parser intentionally has no FreeCAD dependency. Document objects
# are imported lazily by commands so headless parsing remains lightweight.
