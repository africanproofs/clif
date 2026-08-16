"""Push alerting — the last mile from "detected + logged" to "you are paged".

The registration/fund/observe daemons already DETECT and LOG the RE423-family failures; nothing
yet PUSHES on CRIT. This turns the fail-safe severities from `read_readiness` + `read_health` (both
already CRIT-on-error) into a debounced webhook alert: page on entering a bad state, re-page while
it persists, and send a RESOLVED on recovery — with a confirm-cycles debounce so a transient RPC
blip doesn't page. Read-only + send-only; holds no key, signs nothing.
"""

from __future__ import annotations

import httpx

_ORDER = {"OK": 0, "WARN": 1, "CRIT": 2}


def alert_level(readiness: dict, funding) -> tuple[str, list[str]]:
    """Overall alert level (worst of registration + funding) + the concrete reasons. `readiness`
    is `read_readiness(...).to_dict()`; `funding` is a `FundingHealth`."""
    reasons: list[str] = []

    # Registration — the RE423 gate. Any bad flag is a page-worthy fact.
    if readiness.get("error"):
        reasons.append(f"registration read failed: {readiness['error']}")
    elif readiness.get("current_registered") is False:
        reasons.append("NOT REGISTERED for the current reward epoch — submissions earn ZERO")
    if readiness.get("gas_ok") is False:
        reasons.append(f"registerVoter prereq: gas below floor ({readiness.get('sender_account')} "
                       f"{readiness.get('sender_balance')} < {readiness.get('gas_floor')})")
    if readiness.get("entity_ok") is False:
        reasons.append("registerVoter prereq: entity/registration gap")
    if readiness.get("votepower_ok") is False:
        reasons.append("registerVoter prereq: zero vote power")

    # Funding.
    if funding.error is not None or funding.funder_balance is None:
        reasons.append(f"funding read failed: {funding.error}")
    for a in funding.below:
        reasons.append(f"{a.name} below band ({a.balance:.1f} < {a.lower})")
    if funding.funder_crit:
        reasons.append(f"ap-funder critically low ({funding.funder_balance:.1f})")
    elif funding.funder_warn:
        reasons.append(f"ap-funder runway low ({funding.funder_balance:.1f})")
    for a in funding.nearing:
        reasons.append(f"{a.name} nearing band floor ({a.balance:.1f})")

    level = max(readiness.get("severity", "CRIT"), funding.severity, key=lambda s: _ORDER.get(s, 2))
    return level, reasons


def format_alert(network: str, epoch, level: str, reasons: list[str], kind: str) -> str:
    """The message body posted to the webhook."""
    icon = {"CRIT": "🔴🔴", "WARN": "⚠️", "OK": "✅"}.get(level, "❓")
    head = f"{icon} clif {kind} — {network} RE{epoch}: {level}"
    if level == "OK":
        return f"{head}\n  all clear — registration + funding healthy"
    body = "\n".join(f"  • {r}" for r in reasons) or "  (no detail)"
    return f"{head}\n{body}"


def decide(state: dict, level: str, now: float, *, repeat_sec: float, confirm: int) -> tuple[bool, str, dict]:
    """Debounced send decision. Returns (send, kind, new_state). A level must hold for `confirm`
    consecutive reads before it (de)escalates — so a one-cycle transient never pages. While a bad
    state persists, re-page every `repeat_sec`. `kind` ∈ ALERT / UPDATE / RESOLVED / REMINDER."""
    prev = state.get("level", "OK")
    pending_n = state.get("pending_n", 0) + 1 if level == state.get("pending") else 1
    confirmed = level if pending_n >= confirm else prev
    new = {"level": confirmed, "last_sent": state.get("last_sent", 0.0),
           "pending": level, "pending_n": pending_n}

    send, kind = False, ""
    if confirmed != prev:
        kind = "ALERT" if _ORDER[confirmed] > _ORDER[prev] else ("RESOLVED" if confirmed == "OK" else "UPDATE")
        send = True
    elif _ORDER[confirmed] >= _ORDER["WARN"] and now - state.get("last_sent", 0.0) >= repeat_sec:
        send, kind = True, "REMINDER"
    if send:
        new["last_sent"] = now
    return send, kind, new


def post_webhook(url: str, text: str, timeout: float = 10.0) -> bool:
    """POST the message to a Slack/Discord/generic webhook (both `text` and `content` keys).
    Best-effort — a webhook failure never breaks the daemon (it retries next cycle)."""
    try:
        r = httpx.post(url, json={"text": text, "content": text}, timeout=timeout)
        return r.status_code < 300
    except httpx.HTTPError:
        return False


def heartbeat(url: str, level: str, timeout: float = 8.0) -> bool:
    """Dead-man's-switch ping to an EXTERNAL check (healthchecks.io / Better Uptime). GET the base
    URL when healthy (OK/WARN) or `<url>/fail` on CRIT — so the external service alarms on CRIT
    (it got a /fail) AND on silence (the daemon is dead / l-desktop is offline — no ping at all,
    which nothing on-box could ever report). Best-effort: a failed ping never breaks the daemon."""
    target = url.rstrip("/") + "/fail" if level == "CRIT" else url
    try:
        return httpx.get(target, timeout=timeout).status_code < 400
    except httpx.HTTPError:
        return False
