"""Project the bundled dashboard plugin into ``$HERMES_HOME`` so the Hermes web
dashboard discovers the Inbox Organizer tab + its backend routes.

Hermes dashboard plugins are *directory-discovered* — the dashboard scans
``$HERMES_HOME/plugins/<name>/dashboard/manifest.json`` and has no entry-point
hook (unlike the CLI/gateway plugin, which we ship via ``hermes_agent.plugins``).
So on every install method we copy this package's bundled ``dashboard/`` tree
into ``<HERMES_HOME>/plugins/inbox_organizer/dashboard/``:

* pip install  → :func:`register` calls :func:`project` on first plugin load.
* baked image  → the entrypoint *also* projects it (parity with the plugin.yaml
  shim + boot hook), so the tab exists before the first agent turn even though
  the data volume shadows image COPYs.

The copy is the single source of truth (no drift): re-running it overwrites the
projected files from the package. Backend routes mount once at dashboard
startup, so a freshly projected ``plugin_api.py`` needs a dashboard restart (or
``/api/dashboard/plugins/rescan`` for the front-end tab) to take effect.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The plugin *directory* name under $HERMES_HOME/plugins — matches the CLI/gateway
# plugin.yaml `name` and the dashboard-visibility shim dir, so one directory
# carries the row, the (dormant) re-export, and now the dashboard extension.
PLUGIN_DIR_NAME = "inbox_organizer"


def hermes_home() -> str:
    """Hermes home dir (``$HERMES_HOME`` or ``~/.hermes``) — where it scans plugins."""
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def packaged_dashboard_dir() -> str:
    """The ``dashboard/`` tree shipped inside this package (the copy source)."""
    return os.path.join(os.path.dirname(__file__), "dashboard")


def project(home: Optional[str] = None) -> Optional[str]:
    """Copy the packaged ``dashboard/`` into ``<home>/plugins/inbox_organizer/``.

    Returns the destination ``dashboard/`` path, or ``None`` if the packaged
    assets are missing. Idempotent (overwrites in place); ``__pycache__`` is not
    copied.
    """
    src = packaged_dashboard_dir()
    if not os.path.isdir(src):
        logger.warning("inbox-dashboard: packaged assets missing at %s — skipping projection", src)
        return None
    dest = os.path.join(home or hermes_home(), "plugins", PLUGIN_DIR_NAME, "dashboard")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    logger.info("inbox-dashboard: projected dashboard assets → %s", dest)
    return dest


# --- `hermes inbox-organizer ...` CLI subcommand (ctx.register_cli_command) ------

def setup_cli(subparser: Any) -> None:
    """Build the argparse tree for ``hermes inbox-organizer``."""
    subs = subparser.add_subparsers(dest="inbox_command")
    subs.add_parser(
        "install-dashboard",
        help="(Re)install the dashboard UI into $HERMES_HOME/plugins (then restart `hermes dashboard`)",
    )
    subparser.set_defaults(func=cli_handler)


def cli_handler(args: Any) -> None:
    """Handler for ``hermes inbox-organizer <subcommand>``."""
    if getattr(args, "inbox_command", None) == "install-dashboard":
        dest = project()
        if dest:
            print(f"inbox-organizer: dashboard installed → {dest}")
            print("Restart `hermes dashboard` to mount the backend routes (or hit /api/dashboard/plugins/rescan for the tab).")
        else:
            print("inbox-organizer: dashboard projection failed (see logs)")
    else:
        print("Usage: hermes inbox-organizer install-dashboard")
