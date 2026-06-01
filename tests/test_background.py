"""InboxDaemon routing: classify -> label -> maybe-draft, with seams."""

from __future__ import annotations

from hermes_inbox_organizer.background import InboundMessage, InboxDaemon, NullSource


def _msg(thread_id: str = "t1", subject: str = "hi") -> InboundMessage:
    return InboundMessage(
        account_id="a1",
        message_id="m-" + thread_id,
        thread_id=thread_id,
        sender="x@example.com",
        subject=subject,
    )


def _daemon(category: str):
    labels: list[tuple[str, str]] = []
    drafted: list[str] = []
    daemon = InboxDaemon(
        source=NullSource(),
        classifier=lambda _m: category,
        apply_label=lambda m, c: labels.append((m.thread_id, c)),
        on_to_respond=lambda m: drafted.append(m.thread_id),
    )
    return daemon, labels, drafted


def test_to_respond_labels_and_triggers_draft() -> None:
    daemon, labels, drafted = _daemon("1: To Respond")
    daemon.handle(_msg("t1"))
    assert labels == [("t1", "1: To Respond")]
    assert drafted == ["t1"]
    assert daemon.pending() == ["t1"]


def test_non_to_respond_labels_but_no_draft() -> None:
    daemon, labels, drafted = _daemon("4: Notification")
    daemon.handle(_msg("t9"))
    assert labels == [("t9", "4: Notification")]
    assert drafted == []
    assert daemon.pending() == []


def test_pending_dedupes_and_clears() -> None:
    daemon, _labels, drafted = _daemon("1: To Respond")
    daemon.handle(_msg("t1"))
    daemon.handle(_msg("t1"))  # same thread again
    assert daemon.pending() == ["t1"]
    assert drafted == ["t1", "t1"]  # trigger fires each time; pending dedupes
    daemon.clear_pending("t1")
    assert daemon.pending() == []


def test_start_stop_safe_with_null_source() -> None:
    daemon, _labels, _drafted = _daemon("2: FYI")
    daemon.start()  # NullSource -> no thread, no creds
    daemon.stop()
