"""Dashboard asset projection: packaged ``dashboard/`` → ``$HERMES_HOME/plugins``."""

from __future__ import annotations

import json
import os

from hermes_inbox_organizer import dashboard_assets


def test_packaged_assets_present():
    src = dashboard_assets.packaged_dashboard_dir()
    assert os.path.isfile(os.path.join(src, "manifest.json"))
    assert os.path.isfile(os.path.join(src, "plugin_api.py"))
    assert os.path.isfile(os.path.join(src, "dist", "index.js"))


def test_project_copies_tree(tmp_path):
    dest = dashboard_assets.project(home=str(tmp_path))
    assert dest == os.path.join(str(tmp_path), "plugins", "inbox_organizer", "dashboard")
    assert os.path.isfile(os.path.join(dest, "manifest.json"))
    assert os.path.isfile(os.path.join(dest, "dist", "index.js"))
    # idempotent re-copy returns the same destination
    assert dashboard_assets.project(home=str(tmp_path)) == dest


def test_project_respects_hermes_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dest = dashboard_assets.project()
    assert dest == os.path.join(str(tmp_path), "plugins", "inbox_organizer", "dashboard")


def test_manifest_and_bundle_agree():
    # Guard against name drift across the three projected files — they all hinge on
    # the registration name "inbox_organizer" and the build_router() re-export.
    src = dashboard_assets.packaged_dashboard_dir()
    with open(os.path.join(src, "manifest.json")) as f:
        manifest = json.load(f)
    assert manifest["name"] == "inbox_organizer"
    assert manifest["entry"] == "dist/index.js"
    assert manifest["api"] == "plugin_api.py"

    js = open(os.path.join(src, "dist", "index.js")).read()
    assert '__HERMES_PLUGINS__.register("inbox_organizer"' in js
    assert "/api/plugins/inbox_organizer" in js  # BASE the bundle calls

    api = open(os.path.join(src, "plugin_api.py")).read()
    assert "from hermes_inbox_organizer.dashboard_api import build_router" in api
