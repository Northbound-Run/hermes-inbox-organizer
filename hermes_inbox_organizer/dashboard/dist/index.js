/*
 * Inbox Organizer — Hermes dashboard tab.
 *
 * Plain IIFE, no build step: React and the shadcn/ui primitives come from the
 * Hermes Plugin SDK on window.__HERMES_PLUGIN_SDK__ (never bundled). The bundle
 * registers under the same name as dashboard/manifest.json ("inbox_organizer")
 * and calls the package's backend routes under /api/plugins/inbox_organizer/.
 *
 * Flows: list connected accounts; copy-paste connect (start → approve in a new
 * tab → paste the code → finish); disconnect (revoke + delete). Connect/disconnect
 * only mutate the shared token files — the daemon's poll reconciler converges the
 * live account set within a tick (or on restart).
 *
 * fetchJSON(url, init?) takes a standard RequestInit as its 2nd arg and injects
 * the dashboard session token, so POSTs work (and stay authed in gated mode).
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var hooks = SDK.hooks;
  var c = SDK.components;
  var h = React.createElement;

  var BASE = "/api/plugins/inbox_organizer";

  function patch(obj, upd) { return Object.assign({}, obj, upd); }
  function errText(e) { return String((e && e.message) || e); }
  function postJSON(path, payload) {
    return SDK.fetchJSON(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  }

  function InboxOrganizerPage() {
    var acctSt = hooks.useState({ loading: true, error: null, accounts: [], count: 0, busyEmail: null });
    var acct = acctSt[0], setAcct = acctSt[1];

    var connSt = hooks.useState({ state: "", authUrl: "", busy: false, code: "", error: null, notice: null });
    var conn = connSt[0], setConn = connSt[1];

    var loadAccounts = hooks.useCallback(function () {
      setAcct(function (s) { return patch(s, { loading: true, error: null }); });
      SDK.fetchJSON(BASE + "/accounts")
        .then(function (d) {
          setAcct({ loading: false, error: null, accounts: (d && d.accounts) || [], count: (d && d.count) || 0, busyEmail: null });
        })
        .catch(function (e) {
          setAcct({ loading: false, error: errText(e), accounts: [], count: 0, busyEmail: null });
        });
    }, []);

    hooks.useEffect(function () { loadAccounts(); }, [loadAccounts]);

    function startConnect() {
      setConn(function (s) { return patch(s, { busy: true, error: null, notice: null }); });
      postJSON(BASE + "/connect/start", {})
        .then(function (d) {
          if (!d || d.error) { setConn(function (s) { return patch(s, { busy: false, error: (d && d.error) || "could not start" }); }); return; }
          try { window.open(d.auth_url, "_blank", "noopener,noreferrer"); } catch (_e) { /* popup blocked — link is shown below */ }
          setConn(function (s) { return patch(s, { busy: false, state: d.state, authUrl: d.auth_url, notice: d.instructions || "" }); });
        })
        .catch(function (e) { setConn(function (s) { return patch(s, { busy: false, error: errText(e) }); }); });
    }

    function finishConnect() {
      var code = (conn.code || "").trim();
      if (!code) { setConn(function (s) { return patch(s, { error: "Paste the code from the connect page first." }); }); return; }
      setConn(function (s) { return patch(s, { busy: true, error: null, notice: null }); });
      postJSON(BASE + "/connect/complete", { code: code, state: conn.state })
        .then(function (d) {
          if (!d || d.error) { setConn(function (s) { return patch(s, { busy: false, error: (d && d.error) || "could not connect" }); }); return; }
          setConn({ state: "", authUrl: "", busy: false, code: "", error: null, notice: "Connected " + d.email + "." });
          loadAccounts();
        })
        .catch(function (e) { setConn(function (s) { return patch(s, { busy: false, error: errText(e) }); }); });
    }

    function removeAccount(email) {
      if (!window.confirm("Disconnect " + email + "?\nThis revokes access at Google and stops triaging it.")) return;
      setAcct(function (s) { return patch(s, { busyEmail: email, error: null }); });
      postJSON(BASE + "/disconnect", { email: email })
        .then(function (d) {
          if (!d || d.error) { setAcct(function (s) { return patch(s, { busyEmail: null, error: (d && d.error) || "could not disconnect" }); }); return; }
          loadAccounts();
        })
        .catch(function (e) { setAcct(function (s) { return patch(s, { busyEmail: null, error: errText(e) }); }); });
    }

    // ── Connect card ──────────────────────────────────────────────────────────
    var connecting = !!conn.state;
    var connectChildren = [
      h("div", { key: "row", className: "flex items-center gap-2" },
        h(c.Button, { onClick: startConnect, disabled: conn.busy },
          conn.busy ? "Working…" : (connecting ? "Restart sign-in" : "Connect a Google account"))),
    ];
    if (connecting) {
      connectChildren.push(
        h("div", { key: "step", className: "flex flex-col gap-2" },
          conn.notice ? h("p", { className: "text-xs text-muted-foreground" }, conn.notice) : null,
          conn.authUrl ? h("a", { href: conn.authUrl, target: "_blank", rel: "noopener noreferrer", className: "text-xs underline text-muted-foreground" }, "Re-open the Google sign-in page") : null,
          h("div", { className: "flex items-center gap-2" },
            h(c.Input, {
              placeholder: "Paste the code from the connect page",
              value: conn.code,
              onChange: function (e) { var v = e.target.value; setConn(function (s) { return patch(s, { code: v }); }); },
            }),
            h(c.Button, { onClick: finishConnect, disabled: conn.busy }, "Finish"))));
    }
    if (conn.error) connectChildren.push(h("p", { key: "err", className: "text-sm text-destructive" }, conn.error));
    if (conn.notice && !connecting) connectChildren.push(h("p", { key: "ok", className: "text-sm text-muted-foreground" }, conn.notice));

    var connectCard = h(c.Card, null,
      h(c.CardHeader, null, h(c.CardTitle, null, "Connect a Google account")),
      h(c.CardContent, { className: "flex flex-col gap-3" }, connectChildren));

    // ── Accounts card ─────────────────────────────────────────────────────────
    var accountsBody;
    if (!acct.loading && acct.accounts.length === 0) {
      accountsBody = acct.error
        ? h("p", { className: "text-sm text-destructive" }, "Failed to load accounts: " + acct.error)
        : h("p", { className: "text-sm text-muted-foreground" }, "No Google accounts connected yet.");
    } else {
      accountsBody = h("ul", { className: "space-y-1" },
        acct.accounts.map(function (a) {
          var removing = acct.busyEmail === a.email;
          return h("li", { key: a.email, className: "flex items-center justify-between gap-2" },
            h("span", { className: "text-sm font-mono" }, a.email),
            h(c.Button, { onClick: function () { removeAccount(a.email); }, disabled: removing, variant: "destructive" },
              removing ? "Removing…" : "Remove"));
        }));
    }

    var accountsChildren = [
      h("div", { key: "row", className: "flex items-center gap-2 mb-3" },
        h(c.Button, { onClick: loadAccounts, disabled: acct.loading }, acct.loading ? "Loading…" : "Refresh"),
        h(c.Badge, null, String(acct.count) + (acct.count === 1 ? " account" : " accounts"))),
      h("div", { key: "body" }, accountsBody),
    ];
    if (acct.accounts.length > 0 && acct.error) {
      accountsChildren.push(h("p", { key: "err", className: "text-sm text-destructive mt-2" }, acct.error));
    }

    var accountsCard = h(c.Card, null,
      h(c.CardHeader, null, h(c.CardTitle, null, "Connected Google accounts")),
      h(c.CardContent, null, accountsChildren));

    return h("div", { className: "flex flex-col gap-4" }, connectCard, accountsCard);
  }

  window.__HERMES_PLUGINS__.register("inbox_organizer", InboxOrganizerPage);
})();
