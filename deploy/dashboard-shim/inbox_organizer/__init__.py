"""Dashboard-visibility shim for hermes-inbox-organizer.

The real plugin loads via the ``hermes_agent.plugins`` entry-point
(``inbox_organizer = "hermes_inbox_organizer"`` in ``pyproject.toml``). Hermes's
web dashboard, however, lists only plugins it finds as *directories* under
``$HERMES_HOME/plugins`` — its ``_discover_all_plugins`` scan
(``hermes_cli/plugins_cmd.py``) never enumerates entry-points — so the plugin
runs fine but is invisible on the Plugins page. This directory exists purely so
that scan finds a ``plugin.yaml`` (with ``name: inbox_organizer``) and renders a
row.

This module is normally never imported. A flat plugin's dedup key is its
manifest ``name`` (``plugins.py:_parse_manifest``), so this directory and the
entry-point share the key ``inbox_organizer``; the real loader applies the
entry-point source last, so it wins dedup and imports the installed package —
not this file. The re-export below is insurance only: if a future Hermes ever
loaded the directory instead of the entry-point, ``register`` still resolves to
the real implementation, so there is exactly one ``register()`` either way (and
never a "no register() function" load error).

Deployment: this directory is baked into the image and projected into the
``/opt/data`` volume by the entrypoint at boot (the volume shadows image COPYs,
exactly like the gateway:startup boot hook). The ``plugin.yaml`` that lands next
to this file at runtime is copied from the *installed* package, so the row's
name/version/description/provides_tools never drift from the real plugin. See
``hermes-template/scripts/entrypoint.sh`` and ``Dockerfile.hermes``.
"""

from __future__ import annotations

# Re-exported so the directory is a valid, loadable plugin if it is ever the
# dedup winner. Dormant under normal operation (the entry-point wins).
from hermes_inbox_organizer import register

__all__ = ["register"]
