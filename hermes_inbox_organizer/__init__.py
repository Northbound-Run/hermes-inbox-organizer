"""hermes-inbox-organizer — autonomous Gmail triage as a Hermes plugin.

Wiring grounded against the Hermes plugin API
(``NousResearch/hermes-agent`` → ``hermes_cli/plugins.py`` ``PluginContext`` and
``gateway/run.py``):

  * a native ``inbox_create_draft`` tool the agent calls (``register_tool``)
  * a ``pre_gateway_dispatch`` hook that lazily captures the live ``GatewayRunner``
    (so we can inject synthetic draft turns) — chat-recorder pattern
  * a ``pre_llm_call`` hook that proactively surfaces pending drafts
  * a continual in-process inbox daemon (Pub/Sub streaming pull) started here

Live Gmail / Pub/Sub / agent calls sit behind seams (``NullSource``,
``UnconfiguredWriter``, ``GatewayInjectionDispatcher`` with lazy getters) so
``register(ctx)`` runs — and the routing/wiring is unit-tested — without creds.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import threading
from typing import Any, Optional

from .background import InboundMessage, InboxDaemon, NullSource
from .draft_trigger import (
    DraftTrigger,
    GatewayInjectionDispatcher,
    pending_drafts_context,
)
from .inbox_tool import (
    INBOX_CREATE_DRAFT_SCHEMA,
    LoggingDraftWriter,
    make_inbox_create_draft_handler,
)
from .modules import ModuleRegistry
from .notifier import DeliveryNotifier
from .oauth import PendingStore, load_oauth_client
from .onboarding_tools import make_disconnect_tool, make_onboarding_tools
from .tools_read import (
    INBOX_LIST_EMAILS_SCHEMA,
    READ_TOOLS,
    make_inbox_list_emails_handler,
)

logger = logging.getLogger(__name__)

# Set when the autonomous runtime starts, so a just-connected account can be
# hot-added without a restart.
_RUNTIME: Any = None
# Accounts whose credentials died (revoked/expired) — surfaced to the owner as a
# reconnect nudge in pre_llm_call; cleared when the account is (re)connected.
_NEEDS_RECONNECT: set[str] = set()
# Owner-gated tools (connecting/disconnecting mailboxes).
CONNECT_TOOLS = {"inbox_connect_account", "inbox_complete_connection", "inbox_disconnect_account"}


class _GatewayCapture:
    """Lazily captures the live ``GatewayRunner`` + a session source.

    Hermes only hands the gateway object to plugins via the
    ``pre_gateway_dispatch`` hook's ``gateway=`` kwarg (not at register time), so
    we grab it on the first inbound message — same approach hermes-chat-recorder
    uses to find the live Matrix adapter.
    """

    def __init__(self) -> None:
        self.gateway: Any | None = None
        self.source: Any | None = None

    def on_pre_gateway_dispatch(self, **kwargs: Any) -> None:
        if self.gateway is None:
            self.gateway = kwargs.get("gateway")
        if self.source is None:
            event = kwargs.get("event")
            if event is not None:
                self.source = getattr(event, "source", None)
        return None  # observer only — never influences dispatch flow


class _AuthContext:
    """Resolves the Matrix sender behind a tool call, to owner-gate onboarding.

    Plugin hooks don't hand the sender to the tool layer, so we
    bind it per turn: ``pre_gateway_dispatch`` notes the inbound sender →
    ``pre_llm_call`` binds it to that turn's ``session_id`` → ``pre_tool_call``
    looks it up by ``task_id`` (== session_id). The gate stashes the resolved
    sender on a thread-local so the tool handler (same thread, next) can read it.
    """

    def __init__(self, owners: set[str]) -> None:
        self._owners = owners
        self._last_sender: Optional[str] = None
        self._by_session: dict[str, str] = {}
        self._lock = threading.Lock()
        self._tls = threading.local()

    def note_sender(self, sender: Optional[str]) -> None:  # pre_gateway_dispatch
        if sender:
            with self._lock:
                self._last_sender = sender

    def bind_session(self, session_id: Optional[str]) -> None:  # pre_llm_call
        if session_id:
            with self._lock:
                if self._last_sender:
                    self._by_session[session_id] = self._last_sender

    def sender_for(self, task_id: Optional[str]) -> Optional[str]:
        if not task_id:
            return None
        with self._lock:
            return self._by_session.get(task_id)

    def is_owner(self, sender: Optional[str]) -> bool:
        return bool(sender) and sender in self._owners

    def set_current(self, sender: Optional[str]) -> None:  # gate → handler (same thread)
        self._tls.sender = sender

    def current_sender(self) -> Optional[str]:
        return getattr(self._tls, "sender", None)


def register(ctx: Any) -> InboxDaemon:
    """Hermes plugin entry point (group ``hermes_agent.plugins``).

    Returns the constructed :class:`InboxDaemon` for tests/inspection.
    """
    # 1. Native tool: the write-back primitive the agent calls.
    ctx.register_tool(
        name="inbox_create_draft",
        toolset="inbox",
        schema=INBOX_CREATE_DRAFT_SCHEMA,
        handler=make_inbox_create_draft_handler(_resolve_writer(ctx)),
        description="Create a Gmail draft reply (never sends).",
        emoji="\U0001f4dd",
    )

    # 1b. Read tools: let the agent inspect the inbox before drafting.
    resolve_reader = _resolve_reader(ctx)
    for schema, make_handler in READ_TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset="inbox",
            schema=schema,
            handler=make_handler(resolve_reader),
            description=schema["description"],
        )

    # 1b-search. inbox_list_emails: Gmail search across one OR all connected accounts.
    ctx.register_tool(
        name=INBOX_LIST_EMAILS_SCHEMA["name"],
        toolset="inbox",
        schema=INBOX_LIST_EMAILS_SCHEMA,
        handler=make_inbox_list_emails_handler(
            resolve_reader, lambda: sorted(_load_all_tokens().keys())
        ),
        description=INBOX_LIST_EMAILS_SCHEMA["description"],
    )

    # 1c. Account discovery: lets the agent learn which mailboxes it can act on
    # (the email it returns is the account_id for every other inbox tool).
    ctx.register_tool(
        name="inbox_list_accounts",
        toolset="inbox",
        schema=INBOX_LIST_ACCOUNTS_SCHEMA,
        handler=_inbox_list_accounts_handler,
        description=INBOX_LIST_ACCOUNTS_SCHEMA["description"],
    )

    # 1d. The on-demand unread rollup is now a Module (see modules/rollup.py); it
    # contributes the inbox_unread_rollup tool through the registry below.

    # 2. Capture the gateway for synthetic-injection drafting; build the daemon.
    capture = _GatewayCapture()
    dispatcher = GatewayInjectionDispatcher(
        get_gateway=lambda: capture.gateway,
        get_source=lambda: capture.source,
    )
    trigger = DraftTrigger(dispatcher)
    daemon = _build_daemon(ctx, trigger)

    # 2a. Notifier: proactive direct push to the owner (2FA codes, shipping
    # updates) via the SAME captured gateway, without a draft turn. Defaults to
    # Hermes's own home channel (/sethome); INBOX_NOTIFY_TARGET is an optional
    # override. Phase 0 de-risk of DeliveryRouter; modules consume this seam.
    from .config import get_config

    notifier = DeliveryNotifier(
        get_gateway=lambda: capture.gateway,
        get_source=lambda: capture.source,
        target=get_config().notify_target,
    )

    # Module registry: feature modules (rollup today; 2FA + shipping later) hook
    # into the triage flow through this, and contribute agent tools — registered
    # via the loop below. The rollup module contributes inbox_unread_rollup.
    registry = ModuleRegistry(_build_modules(notifier))
    for spec in registry.tools():
        ctx.register_tool(
            name=spec.name,
            toolset=spec.toolset,
            schema=spec.schema,
            handler=spec.handler,
            description=spec.description,
        )

    # 2b. Onboarding: owner-gated chat tools to connect/complete Gmail accounts.
    owners = _load_owners()
    if not owners:
        logger.warning("inbox: INBOX_OWNER_MATRIX_IDS unset — account-connect tools will refuse all callers")
    auth = _AuthContext(owners)
    pending = PendingStore()
    for schema, handler in make_onboarding_tools(
        load_client=load_oauth_client,
        pending=pending,
        resolve_sender=auth.current_sender,
        save_token=_save_account_token,
        hot_add=_hot_add_account,
    ):
        ctx.register_tool(
            name=schema["name"],
            toolset="inbox",
            schema=schema,
            handler=handler,
            description=schema["description"],
        )

    dc_schema, dc_handler = make_disconnect_tool(
        resolve_sender=auth.current_sender,
        load_token=_load_token_for,
        delete_token=_delete_account_token,
        remove_account=lambda email: bool(_RUNTIME and _RUNTIME.remove_account(email)),
    )
    ctx.register_tool(
        name=dc_schema["name"],
        toolset="inbox",
        schema=dc_schema,
        handler=dc_handler,
        description=dc_schema["description"],
    )

    # 3. Hooks: capture the gateway, bind sender→session per turn, nudge, owner-gate.
    if hasattr(ctx, "register_hook"):

        def _on_pre_gateway_dispatch(**kw: Any):
            capture.on_pre_gateway_dispatch(**kw)
            event = kw.get("event")
            src = getattr(event, "source", None) if event is not None else None
            auth.note_sender(getattr(src, "user_id", None))
            return None

        ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)

        def _on_pre_llm_call(**kw: Any):
            auth.bind_session(kw.get("session_id"))
            parts: list[str] = []
            drafts = pending_drafts_context(daemon.pending())
            if drafts and drafts.get("context"):
                parts.append(drafts["context"])
            if _NEEDS_RECONNECT:
                emails = ", ".join(sorted(_NEEDS_RECONNECT))
                parts.append(
                    f"These mailboxes need reconnecting (their access expired or was "
                    f"revoked): {emails}. Ask me to reconnect one and I'll start the flow."
                )
            return {"context": "\n".join(parts)} if parts else None

        ctx.register_hook("pre_llm_call", _on_pre_llm_call)

        def _on_pre_tool_call(**kw: Any):
            if kw.get("tool_name") not in CONNECT_TOOLS:
                return None  # only gate the onboarding tools
            sender = auth.sender_for(kw.get("task_id") or kw.get("session_id"))
            if not auth.is_owner(sender):
                return {
                    "action": "block",
                    "message": "Connecting a mailbox is restricted to this assistant's owner.",
                }
            auth.set_current(sender)  # hand the resolved sender to the tool handler
            return None

        ctx.register_hook("pre_tool_call", _on_pre_tool_call)

    # 4. Diagnostic commands: fire a synthetic-injection draft turn, and (Phase 0)
    # push a test notification through DeliveryRouter to verify the Notifier path.
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            "inboxprobe",
            _make_probe_command(trigger),
            "Diagnostic: fire a synthetic draft turn (check logs for the PROBE line)",
        )
        ctx.register_command(
            "inboxnotifyprobe",
            _make_notify_probe_command(notifier),
            "Diagnostic: push a test message to the notify channel (home / INBOX_NOTIFY_TARGET)",
        )

    daemon.start()
    try:
        _maybe_start_runtime(registry)
    except Exception:
        logger.exception("inbox: autonomous runtime start failed (on-demand tools still work)")
    logger.info("hermes-inbox-organizer registered")
    return daemon


def _build_modules(notifier: DeliveryNotifier) -> list:
    """Construct the enabled feature modules.

    The rollup is the reference module (Phase 2); the optional 2FA + shipping
    modules are added later and will use the shared ``notifier``. The rollup gets
    the seams that depend on the plugin's token store + runtime (its account
    resolver + auth-error predicate) injected, so the module stays decoupled.
    """
    from . import rollup as _rollup
    from .config import get_config
    from .modules.rollup import RollupModule
    from .modules.shipping import ShippingModule
    from .modules.track17 import HttpTrack17Client
    from .modules.twofa import TwoFactorModule

    # Share the reconnect set BY REFERENCE so a dead token hit during a rollup
    # surfaces the same reconnect nudge as the runtime/onboarding paths.
    _rollup._NEEDS_RECONNECT = _NEEDS_RECONNECT
    cfg = get_config()
    track_key = _read_17track_key(cfg)
    return [
        RollupModule(
            resolve_accounts=_resolve_rollup_accounts,
            is_auth_error=_is_auth_error_for_rollup,
        ),
        TwoFactorModule(
            notifier=notifier,
            enabled=cfg.module_2fa_enabled,
            sender_allowlist=cfg.twofa_sender_allowlist,
        ),
        ShippingModule(
            notifier=notifier,
            client=HttpTrack17Client(track_key) if track_key else None,
            enabled=cfg.module_shipping_enabled,
            max_active=cfg.shipping_max_active,
            poll_interval_s=cfg.shipping_poll_interval_s,
        ),
    ]


def _read_17track_key(cfg: Any) -> Optional[str]:
    """Read the 17track API key from the read-only config mount (None if absent)."""
    try:
        if os.path.exists(cfg.track17_key_file):
            with open(cfg.track17_key_file) as f:
                return f.read().strip() or None
    except Exception:
        logger.exception("inbox: failed to read 17track key file")
    return None


def _maybe_start_runtime(registry: Any = None):
    """Start the autonomous Pub/Sub runtime if configured (SA key + config present).

    Reads `inbox-pubsub-sa.json` + `inbox-pubsub.json` from the config mount; if
    absent, the plugin runs in on-demand-only mode. Builds one ``Account`` per
    connected token (own service builder + own cursor file) behind one shared
    subscription. Starts on a daemon thread so the live watch()/subscribe calls
    never block plugin load.
    """
    import threading

    cfg_dir = os.environ.get("INBOX_CONFIG_DIR", "/opt/data/config")
    sa_key = os.path.join(cfg_dir, "inbox-pubsub-sa.json")
    cfg_path = os.path.join(cfg_dir, "inbox-pubsub.json")
    if not (os.path.exists(sa_key) and os.path.exists(cfg_path)):
        logger.info("inbox: Pub/Sub not configured — on-demand tools only")
        return
    tokens = _load_all_tokens()
    if not tokens:
        logger.warning("inbox: Pub/Sub configured but no account connected; runtime idle")
        return

    cfg = json.loads(open(cfg_path).read())
    from .config import get_config
    from .draft_trigger import wake_draft
    from .runtime import InboxRuntime

    runtime = InboxRuntime(
        accounts=[_build_account(email) for email in sorted(tokens)],
        project=cfg["project"],
        topic=cfg["topic"],
        subscription=cfg["subscription"],
        sa_key_path=sa_key,
        db_path=get_config().db_path,
        wake_fn=wake_draft,
        on_auth_failure=_NEEDS_RECONNECT.add,
        registry=registry,
    )
    global _RUNTIME
    _RUNTIME = runtime  # expose for hot-adding newly connected accounts

    def _start() -> None:
        try:
            runtime.start()
        except Exception:
            logger.exception("inbox: runtime.start() failed")

    threading.Thread(target=_start, name="inbox-runtime-start", daemon=True).start()


def _make_probe_command(trigger: DraftTrigger):
    """Build the /inboxprobe handler — fires a synthetic-injection draft turn.

    Validates the autonomous path: does injecting an internal MessageEvent
    produce a transcript-aware draft that calls inbox_create_draft? The
    instruction deliberately references "what we were just discussing" so the
    composed draft reveals whether the live transcript reached the agent.
    """

    def _handler(raw_args: str = "", **_kwargs: Any) -> str:
        try:
            trigger.request_draft(
                account_id="probe",
                thread_id="probe-thread",
                sender="probe@example.com",
                subject="(probe) draft a short reply about what we were just discussing",
            )
            return (
                "inbox probe: fired a synthetic draft turn — check the logs for the "
                "'inbox_create_draft PROBE' line to see the composed body."
            )
        except Exception as exc:  # never raise out of a command handler
            return f"inbox probe: dispatch failed: {exc}"

    return _handler


def _make_notify_probe_command(notifier: DeliveryNotifier):
    """Build the /inboxnotifyprobe handler — push a test message and report inline.

    The handler is ASYNC on purpose. Hermes dispatches plugin slash commands ON
    the gateway event loop (``gateway/run.py`` awaits the handler). The Notifier
    runs ``deliver()`` on that same loop via ``run_coroutine_threadsafe``, so a
    SYNC handler that blocked waiting for the result would DEADLOCK the loop — the
    delivery coroutine can't run until the handler returns, and the handler won't
    return until the delivery completes (this is exactly the TimeoutError the
    first cut hit: the push landed only *after* the 20s timeout).

    So we offload ``notifier.send_with_detail`` to a thread via
    ``run_in_executor`` and ``await`` it: the loop stays free to process the
    delivery coroutine, we still report the outcome inline, and the probe runs
    the SAME off-loop path the real module callers (worker threads / the Pub/Sub
    daemon) use — so it's a faithful end-to-end check.
    """

    async def _handler(raw_args: str = "", **_kwargs: Any) -> str:
        import asyncio

        text = (
            "\U0001f514 inbox notify probe — if you can read this, the "
            "DeliveryRouter push path works from the daemon."
        )
        try:
            loop = asyncio.get_running_loop()
            ok, det = await loop.run_in_executor(
                None, lambda: notifier.send_with_detail(text)
            )
        except Exception as exc:  # never raise out of a command handler
            return f"inbox notify probe: FAILED ❌ (probe error: {exc})"
        if ok:
            return "inbox notify probe: delivered ✅ — check the home channel for the push."
        d = det or {}
        return (
            "inbox notify probe: FAILED ❌ — diagnostics:\n"
            f"- gateway_captured: {d.get('gateway_captured')}\n"
            f"- loop: {d.get('loop')}\n"
            f"- target: {d.get('target')} (via {d.get('target_source')})\n"
            f"- error: {d.get('error')}\n"
            f"- result: {str(d.get('result'))[:400]}\n"
            f"- tb: {str(d.get('traceback'))[:400]}\n"
            "(If 'no destination', run /sethome here or set INBOX_NOTIFY_TARGET.)"
        )

    return _handler


def _safe_email(email: str) -> str:
    """Make an email safe for a filename (cursor-<email>.txt)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", email)


def _load_all_tokens() -> dict[str, Any]:
    """email -> AccountToken for every connected account.

    Reads the AES key from the config mount + the encrypted blobs from the token
    dir (container defaults, overridable via env). Returns {} when nothing connected.
    A bad/corrupt blob is skipped (logged) rather than failing the whole load.
    """
    key_path = os.environ.get("INBOX_KEY_FILE", "/opt/data/config/inbox-encryption-key")
    token_dir = os.environ.get("INBOX_TOKEN_DIR", "/opt/data/inbox-organizer/accounts")
    if not os.path.exists(key_path):
        return {}
    from .token_store import load_token

    key = open(key_path).read().strip()
    out: dict[str, Any] = {}
    for path in sorted(glob.glob(os.path.join(token_dir, "*.json"))):
        try:
            tok = load_token(key, path)
        except Exception:
            logger.exception("inbox: failed to load token %s", path)
            continue
        out[tok.email] = tok
    return out


def _load_token_for(account_id: str):
    """Token for ``account_id`` (email). Falls back to the sole account when only
    one is connected, so a single-account agent can omit/guess the account_id."""
    tokens = _load_all_tokens()
    if account_id in tokens:
        return tokens[account_id]
    if len(tokens) == 1:
        return next(iter(tokens.values()))
    return None


def _load_owners() -> set[str]:
    """Matrix user-ids allowed to connect mailboxes.

    Prefers ``INBOX_OWNER_MATRIX_IDS`` (comma-sep) but falls back to an
    ``owner_matrix_ids`` list in the OAuth client config — so the allowlist lives
    in the config mount and can change without recreating the container (a plain
    env var would need ``compose up``, which wipes the pip-installed plugin).
    """
    env = {s.strip() for s in os.environ.get("INBOX_OWNER_MATRIX_IDS", "").split(",") if s.strip()}
    if env:
        return env
    path = os.environ.get("INBOX_OAUTH_CLIENT_FILE", "/opt/data/config/inbox-oauth-client.json")
    try:
        data = json.loads(open(path).read())
    except Exception:
        return set()
    return {str(i).strip() for i in (data.get("owner_matrix_ids") or []) if str(i).strip()}


def _build_account(email: str):
    """Construct a runtime Account for an email (shared by startup + hot-add)."""
    from .gmail import service_from_token
    from .runtime import Account

    return Account(
        email=email,
        build_service=(lambda e=email: service_from_token(_load_token_for(e))),
    )


def _save_account_token(token: Any) -> None:
    """Encrypt + persist a freshly connected account's token to the accounts dir."""
    key_path = os.environ.get("INBOX_KEY_FILE", "/opt/data/config/inbox-encryption-key")
    token_dir = os.environ.get("INBOX_TOKEN_DIR", "/opt/data/inbox-organizer/accounts")
    from .token_store import save_token

    key = open(key_path).read().strip()
    save_token(token, key, os.path.join(token_dir, f"{_safe_email(token.email)}.json"))


def _delete_account_token(email: str) -> bool:
    """Delete the encrypted blob whose decrypted email matches (filename-agnostic)."""
    key_path = os.environ.get("INBOX_KEY_FILE", "/opt/data/config/inbox-encryption-key")
    token_dir = os.environ.get("INBOX_TOKEN_DIR", "/opt/data/inbox-organizer/accounts")
    if not os.path.exists(key_path):
        return False
    from .token_store import load_token

    key = open(key_path).read().strip()
    removed = False
    for path in glob.glob(os.path.join(token_dir, "*.json")):
        try:
            tok = load_token(key, path)
        except Exception:
            continue
        if tok.email == email:
            try:
                os.remove(path)
                removed = True
            except FileNotFoundError:
                pass
    return removed


def _hot_add_account(token: Any) -> bool:
    """Add a just-connected account to the running runtime; False if not running."""
    _NEEDS_RECONNECT.discard(token.email)  # (re)connected → clear any reconnect flag
    if _RUNTIME is None:
        return False
    return bool(_RUNTIME.add_account(_build_account(token.email)))


INBOX_LIST_ACCOUNTS_SCHEMA: dict[str, Any] = {
    "name": "inbox_list_accounts",
    "description": (
        "List the connected Gmail accounts this organizer manages. Returns their "
        "email addresses — pass one as the account_id for the other inbox tools."
    ),
    "parameters": {"type": "object", "properties": {}},
}


def _inbox_list_accounts_handler(args: dict, **_kwargs: Any) -> str:
    try:
        emails = sorted(_load_all_tokens().keys())
    except Exception as exc:  # contract: never raise out of a tool handler
        return json.dumps({"error": f"failed to list accounts: {exc}"})
    out: dict[str, Any] = {"accounts": emails, "count": len(emails)}
    if _NEEDS_RECONNECT:
        out["needs_reconnect"] = sorted(_NEEDS_RECONNECT)
    return json.dumps(out)


def _resolve_writer(ctx: Any):
    """account_id -> writer. Live GmailDraftWriter when connected; LoggingDraftWriter otherwise.

    Live writers are cached per account; the not-connected LoggingDraftWriter is
    not cached, so a writer is rebuilt once that account connects.
    """
    cache: dict[str, Any] = {}

    def _resolve(account_id: str):
        if account_id in cache:
            return cache[account_id]
        try:
            tok = _load_token_for(account_id)
            if tok is None:
                return LoggingDraftWriter()
            from .gmail import writer_from_token

            cache[account_id] = writer_from_token(tok)
            return cache[account_id]
        except Exception:
            logger.exception("inbox: failed to build Gmail draft writer for %s", account_id)
            return LoggingDraftWriter()

    return _resolve


def _resolve_reader(ctx: Any):
    """account_id -> GmailReader | None (read tools report not-connected on None).

    Live readers are cached per account; None (not connected) is not cached.
    """
    cache: dict[str, Any] = {}

    def _resolve(account_id: str):
        if account_id in cache:
            return cache[account_id]
        try:
            tok = _load_token_for(account_id)
            if tok is None:
                return None
            from .gmail import reader_from_token

            cache[account_id] = reader_from_token(tok)
            return cache[account_id]
        except Exception:
            logger.exception("inbox: failed to build Gmail reader for %s", account_id)
            return None

    return _resolve


def _resolve_rollup_accounts(account_id: Optional[str]) -> dict:
    """Resolve target mailboxes for the rollup -> {"accounts", "errors"}.

    Unlike ``_resolve_reader`` (which leans on ``_load_token_for``'s sole-account
    fallback), an explicit ``account_id`` resolves **only** that email: an unknown
    id — even with exactly one account connected — yields an
    ``error="not connected: <id>"`` rather than silently reading the wrong mailbox.
    Omitted ``account_id`` rolls up every connected account (parity with the other
    read tools).

    A reader that fails to *build* is classified with ``runtime._is_auth_error``;
    on a dead token the email is added to ``_NEEDS_RECONNECT`` so the on-demand
    path surfaces the same reconnect nudge the runtime does, and the account
    reports ``error="needs_reconnect"``.
    """
    from .gmail import reader_from_token
    from .runtime import _is_auth_error

    tokens = _load_all_tokens()
    if account_id:
        emails = [account_id] if account_id in tokens else []
        errors: list[dict] = []
        if not emails:
            errors.append(_account_error(account_id, f"not connected: {account_id}"))
    else:
        emails = sorted(tokens.keys())
        errors = []

    readers: dict[str, Any] = {}
    for email in emails:
        try:
            readers[email] = reader_from_token(tokens[email])
        except Exception as exc:  # build failure -> per-account error, never abort
            if _is_auth_error(exc):
                _NEEDS_RECONNECT.add(email)
                errors.append(_account_error(email, "needs_reconnect"))
            else:
                logger.exception("inbox: failed to build rollup reader for %s", email)
                errors.append(_account_error(email, str(exc)))
    return {"accounts": readers, "errors": errors}


def _account_error(email: str, message: str) -> dict:
    """A per-account summary stub carrying just an error (merged into output)."""
    return {
        "account": email,
        "scanned": 0,
        "meaningful": 0,
        "truncated": False,
        "classify_errors": 0,
        "error": message,
    }


def _is_auth_error_for_rollup(exc: Exception) -> bool:
    """``runtime._is_auth_error`` behind a lazy import (keeps register() clean)."""
    from .runtime import _is_auth_error

    return _is_auth_error(exc)


def _build_daemon(ctx: Any, trigger: DraftTrigger) -> InboxDaemon:
    # Real build: PubSubMessageSource + the cheap local classifier + a Gmail
    # label applier (queued mutation path).
    def _classify(msg: InboundMessage) -> str:
        # Real build: deterministic pre-classifier -> local OpenRouter classifier.
        # Fallback heuristic (keeps the daemon observable): questions need a reply.
        return "1: To Respond" if msg.subject.strip().endswith("?") else "2: FYI"

    def _apply_label(_msg: InboundMessage, _category: str) -> None:
        # Real build: Gmail users.messages.modify via the queued mutation path.
        return None

    def _on_to_respond(msg: InboundMessage) -> None:
        trigger.request_draft(
            account_id=msg.account_id,
            thread_id=msg.thread_id,
            sender=msg.sender,
            subject=msg.subject,
        )

    return InboxDaemon(
        source=NullSource(),
        classifier=_classify,
        apply_label=_apply_label,
        on_to_respond=_on_to_respond,
    )
