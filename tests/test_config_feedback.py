"""Tests for the draft-feedback Config knobs added in the reinforcement loop.

Covers:
- Default values when no env vars are set.
- Correct parsing (bool coercion, int coercion) when env vars are set.
- reset_config() flushes the cache so the next get_config() re-reads the env.
"""


import pytest

from hermes_inbox_organizer.config import get_config, reset_config


@pytest.fixture(autouse=True)
def _clear_cache():
    """Guarantee a fresh Config for each test."""
    reset_config()
    yield
    reset_config()


# ---------------------------------------------------------------------------
# Defaults (env vars absent)
# ---------------------------------------------------------------------------

class TestFeedbackDefaults:
    def test_enabled_default_true(self):
        cfg = get_config()
        assert cfg.draft_feedback_enabled is True

    def test_capture_all_sent_default_true(self):
        cfg = get_config()
        assert cfg.draft_feedback_capture_all_sent is True

    def test_no_reply_hours_default(self):
        cfg = get_config()
        assert cfg.draft_feedback_no_reply_hours == 72

    def test_sweep_interval_default(self):
        cfg = get_config()
        assert cfg.draft_feedback_sweep_interval_s == 6 * 3600

    def test_max_examples_default(self):
        cfg = get_config()
        assert cfg.draft_feedback_max_examples == 3

    def test_max_lessons_default(self):
        cfg = get_config()
        assert cfg.draft_feedback_max_lessons == 8

    def test_retention_days_default_nonzero(self):
        # G6: must be non-zero because capture_all_sent is default-on
        cfg = get_config()
        assert cfg.draft_feedback_retention_days == 90
        assert cfg.draft_feedback_retention_days > 0

    def test_verbatim_threshold_default(self):
        cfg = get_config()
        assert cfg.draft_feedback_verbatim_threshold == 92

    def test_edit_threshold_default(self):
        cfg = get_config()
        assert cfg.draft_feedback_edit_threshold == 45


# ---------------------------------------------------------------------------
# Override via environment (bool + int coercion, reset between reads)
# ---------------------------------------------------------------------------

class TestFeedbackEnvOverride:
    def test_bool_disabled_via_zero(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_ENABLED", "0")
        reset_config()
        assert get_config().draft_feedback_enabled is False

    def test_bool_disabled_via_false(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_ENABLED", "false")
        reset_config()
        assert get_config().draft_feedback_enabled is False

    def test_bool_enabled_via_true(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_CAPTURE_ALL_SENT", "true")
        reset_config()
        assert get_config().draft_feedback_capture_all_sent is True

    def test_bool_enabled_via_one(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_CAPTURE_ALL_SENT", "1")
        reset_config()
        assert get_config().draft_feedback_capture_all_sent is True

    def test_capture_all_sent_off(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_CAPTURE_ALL_SENT", "off")
        reset_config()
        assert get_config().draft_feedback_capture_all_sent is False

    def test_no_reply_hours_override(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_NO_REPLY_HOURS", "48")
        reset_config()
        assert get_config().draft_feedback_no_reply_hours == 48

    def test_sweep_interval_override(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_SWEEP_INTERVAL_S", "1800")
        reset_config()
        assert get_config().draft_feedback_sweep_interval_s == 1800

    def test_max_examples_override(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_MAX_EXAMPLES", "5")
        reset_config()
        assert get_config().draft_feedback_max_examples == 5

    def test_max_lessons_override(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_MAX_LESSONS", "12")
        reset_config()
        assert get_config().draft_feedback_max_lessons == 12

    def test_retention_days_off(self, monkeypatch):
        # 0 = prune disabled
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_RETENTION_DAYS", "0")
        reset_config()
        assert get_config().draft_feedback_retention_days == 0

    def test_verbatim_threshold_override(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_VERBATIM_THRESHOLD", "95")
        reset_config()
        assert get_config().draft_feedback_verbatim_threshold == 95

    def test_edit_threshold_override(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_EDIT_THRESHOLD", "60")
        reset_config()
        assert get_config().draft_feedback_edit_threshold == 60

    def test_int_bad_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("INBOX_DRAFT_FEEDBACK_NO_REPLY_HOURS", "not-a-number")
        reset_config()
        assert get_config().draft_feedback_no_reply_hours == 72
