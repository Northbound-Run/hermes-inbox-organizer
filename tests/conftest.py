"""Shared test fixtures.

``register(ctx)`` now projects the dashboard plugin assets into ``$HERMES_HOME``
on load (so pip installs get the UI tab). Point ``HERMES_HOME`` at a throwaway
dir for every test so the suite never writes into the developer's real
``~/.hermes``. Nothing else reads ``HERMES_HOME``, so this is inert elsewhere.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path_factory, monkeypatch) -> None:
    home = tmp_path_factory.mktemp("hermes_home")
    monkeypatch.setenv("HERMES_HOME", str(home))
