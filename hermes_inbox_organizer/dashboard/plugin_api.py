"""Hermes dashboard backend routes for the Inbox Organizer plugin.

Thin on purpose: the real logic lives in the installed package
(``hermes_inbox_organizer.dashboard_api``) so it versions and unit-tests with the
plugin instead of drifting inside this projected copy. Hermes mounts the
module-level ``router`` under ``/api/plugins/inbox_organizer/``.

This file is projected into ``$HERMES_HOME/plugins/inbox_organizer/dashboard/``
from the package — see ``hermes_inbox_organizer.dashboard_assets``.
"""

from hermes_inbox_organizer.dashboard_api import build_router

router = build_router()
