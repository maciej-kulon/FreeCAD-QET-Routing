# SPDX-License-Identifier: LGPL-2.1-or-later
"""GUI selection observer controlling physical terminal marker visibility."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from .document import _set_terminal_marker_visibility

_OBSERVER: TerminalVisibilityObserver | None = None
_ACTIVE = False
_REFRESH_SCHEDULED = False
_PENDING_DOCUMENTS: set[str] = set()


class TerminalVisibilityObserver:
    """Refresh marker visibility after any selection-set change."""

    def addSelection(self, *args: Any) -> None:
        _refresh_from_selection_callback(args)

    def removeSelection(self, *args: Any) -> None:
        _refresh_from_selection_callback(args)

    def setSelection(self, *args: Any) -> None:
        _refresh_from_selection_callback(args)

    def clearSelection(self, *args: Any) -> None:
        _refresh_from_selection_callback(args)


def activate_terminal_visibility() -> None:
    """Install the observer and synchronize every open document."""

    import FreeCADGui as Gui

    global _ACTIVE, _OBSERVER
    _ACTIVE = True
    if _OBSERVER is None:
        _OBSERVER = TerminalVisibilityObserver()
        try:
            Gui.Selection.addObserver(
                _OBSERVER,
                _no_resolve_mode(Gui.Selection),
            )
        except (AttributeError, TypeError):
            # FreeCAD versions predating ResolveMode still benefit for plain
            # document objects, although linked-instance identity is weaker.
            Gui.Selection.addObserver(_OBSERVER)
    refresh_terminal_visibility()


def deactivate_terminal_visibility() -> None:
    """Remove the observer and leave no terminal markers rendered."""

    import FreeCADGui as Gui

    global _ACTIVE, _OBSERVER, _REFRESH_SCHEDULED
    _ACTIVE = False
    _PENDING_DOCUMENTS.clear()
    _REFRESH_SCHEDULED = False
    if _OBSERVER is not None:
        try:
            Gui.Selection.removeObserver(_OBSERVER)
        except (AttributeError, RuntimeError):
            pass
        _OBSERVER = None
    hide_terminal_markers()


def refresh_terminal_visibility(document_name: str = "") -> None:
    """Show current terminals owned by exactly the selected device objects.

    NoResolve selection records preserve the identity of App::Link instances.
    An individually selected marker remains visible so it can still be edited
    after a normal click replaces the owner selection.
    """

    import FreeCAD as App
    import FreeCADGui as Gui

    for document in _documents(App, document_name):
        with _preserve_gui_modified_state(Gui, document.Name):
            selection_records = _selection_records(Gui.Selection, document.Name)
            selected_keys = _selected_object_keys(selection_records)
            for marker in _terminal_markers(document):
                owner = getattr(marker, "Owner", None)
                should_show = (
                    str(getattr(marker, "SyncStatus", "Current")) != "Obsolete"
                    and (
                        (owner is not None and _object_key(owner) in selected_keys)
                        or _object_key(marker) in selected_keys
                    )
                )
                view_object = getattr(marker, "ViewObject", None)
                if (
                    view_object is not None
                    and bool(view_object.Visibility) != should_show
                ):
                    _set_terminal_marker_visibility(view_object, should_show)


def hide_terminal_markers(document_name: str = "") -> None:
    """Hide all QET terminal markers in one document or every document."""

    import FreeCAD as App
    import FreeCADGui as Gui

    for document in _documents(App, document_name):
        with _preserve_gui_modified_state(Gui, document.Name):
            for marker in _terminal_markers(document):
                view_object = getattr(marker, "ViewObject", None)
                if view_object is not None and bool(view_object.Visibility):
                    _set_terminal_marker_visibility(view_object, False)


def _refresh_from_selection_callback(args: tuple[Any, ...]) -> None:
    if not _ACTIVE:
        return
    document_name = args[0] if args and isinstance(args[0], str) else ""
    _PENDING_DOCUMENTS.add(document_name)
    _schedule_pending_refresh()


def _schedule_pending_refresh() -> None:
    global _REFRESH_SCHEDULED
    if _REFRESH_SCHEDULED:
        return
    _REFRESH_SCHEDULED = True
    try:
        _defer_refresh(_flush_pending_refreshes)
    except (ImportError, RuntimeError):
        _flush_pending_refreshes()


def _defer_refresh(callback: Callable[[], None]) -> None:
    """Queue one refresh after FreeCAD finishes the current selection burst."""

    from PySide import QtCore

    QtCore.QTimer.singleShot(0, callback)


def _flush_pending_refreshes() -> None:
    global _REFRESH_SCHEDULED
    _REFRESH_SCHEDULED = False
    pending = set(_PENDING_DOCUMENTS)
    _PENDING_DOCUMENTS.clear()
    if not _ACTIVE or not pending:
        return
    if "" in pending:
        refresh_terminal_visibility()
        return
    for document_name in sorted(pending):
        refresh_terminal_visibility(document_name)


def _documents(app_module: Any, document_name: str) -> tuple[Any, ...]:
    documents = app_module.listDocuments()
    if not document_name:
        return tuple(documents.values())
    document = documents.get(document_name)
    return (document,) if document is not None else ()


@contextmanager
def _preserve_gui_modified_state(gui_module: Any, document_name: str) -> Any:
    """Keep transient marker visibility from creating a save prompt.

    App::DocumentObjectGroup marks its GUI document modified whenever a
    child's Visibility changes, even when that child property is NoModify.
    Preserve a pre-existing dirty state, but restore a clean state after this
    controller-owned, synchronous visibility batch.
    """

    try:
        gui_document = gui_module.getDocument(document_name)
        was_modified = bool(gui_document.Modified)
    except (AttributeError, RuntimeError):
        gui_document = None
        was_modified = True
    try:
        yield
    finally:
        if gui_document is not None and not was_modified:
            try:
                gui_document.Modified = False
            except (AttributeError, RuntimeError):
                pass


def _terminal_markers(document: Any) -> tuple[Any, ...]:
    return tuple(
        item
        for item in document.Objects
        if getattr(item, "QET_ObjectKind", "") == "TerminalInstance"
    )


def _selection_records(selection: Any, document_name: str) -> tuple[Any, ...]:
    try:
        # FreeCAD exposes NoResolve as enum value zero here, while the
        # observer registration accepts the enum object itself.
        return tuple(selection.getSelectionEx(document_name, 0))
    except (AttributeError, RuntimeError, TypeError):
        return ()


def _selected_object_keys(
    selection_records: tuple[Any, ...],
) -> set[tuple[str, str]]:
    """Return exact selected objects, including objects along subobject paths."""

    selected: set[tuple[str, str]] = set()
    for record in selection_records:
        root = getattr(record, "Object", None)
        _add_object_key(selected, root)
        for subelement_name in getattr(record, "SubElementNames", ()):
            try:
                object_path = root.getSubObjectList(subelement_name)
            except (AttributeError, RuntimeError):
                continue
            for obj in object_path:
                _add_object_key(selected, obj)
    return selected


def _add_object_key(selected: set[tuple[str, str]], obj: Any) -> None:
    key = _object_key(obj)
    if all(key):
        selected.add(key)


def _no_resolve_mode(selection: Any) -> Any:
    resolve_mode = getattr(selection, "ResolveMode", None)
    return getattr(resolve_mode, "NoResolve", 0)


def _object_key(obj: Any) -> tuple[str, str]:
    if obj is None:
        return "", ""
    document = getattr(obj, "Document", None)
    return (
        str(getattr(document, "Name", "")),
        str(getattr(obj, "Name", "")),
    )
