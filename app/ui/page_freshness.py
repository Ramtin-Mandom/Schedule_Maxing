"""
app/ui/page_freshness.py

A page re-reads its data when it is shown only if that data may have changed
since its last read: data_key() combines the application's data version
(AppServices.data_version: every commit or write on the shared connection --
local edits on any page, a sync's applies, imports -- and the workspace) with
what else the page's view depends on (its date or period, today's date, a
selection). A page records the key it read with and compares it in on_show();
equal keys mean nothing changed, so it neither re-queries nor re-renders.

Without a known version -- direct PostgreSQL storage (no shared connection),
a page used outside the app -- data_key() is None and every show re-reads, as
before. A page's own reload() always re-reads: the manual way to force it.
"""

from __future__ import annotations


def data_key(widget, *view) -> tuple | None:
    """The data version of `widget`'s application plus `view`, or None when the version is unknown."""
    try:
        # The application's root, also for a hidden page the shell detached into its own window (app/ui/shell.py).
        root = widget._root()
    except Exception:  # noqa: BLE001 - a destroyed widget: nothing can be known
        return None
    version = getattr(getattr(root, "services", None), "data_version", None)
    current = version() if callable(version) else None
    return None if current is None else (current, *view)


def unchanged(key: tuple | None, read_with: tuple | None) -> bool:
    """True when `key` is known and equal to the key the page's shown data was read with."""
    return key is not None and key == read_with
