"""Registration readiness — is AP registered, and ready to register, for the
current and next reward epoch?

RE423 (2026-08-10): `registerVoter` reverted `insufficient funds` (the Submit gas
account had drained below the tx cost) on both mainnets, so the node never entered
the registered voter set — and the WHOLE ~3.5-day epoch produced zero reward while
per-round submissions kept flowing "successfully". It was caught by an external
dashboard, not our tooling, because submission health stayed green (the client
submits regardless of registration). Corrected severity: a non-registered epoch
yields no passes.json entry, breaking the pass-inheritance chain (buffer → 0), not
a one-strike dip.

This module is the missing POSITIVE signal: read the on-chain registered set + the
registration window directly, and NEVER show green on a read error (silence looked
green in RE423). It is OBSERVE-only — it never signs or sends anything; the Go
flare-system-client still does registerVoter keyless via fwd. The funding-band
system defends the gas cause; this confirms the outcome and pre-flights the window.

Mechanics (from the flare-system-client fork): registration for epoch N+1 is
triggered by FlareSystemsManager.VotePowerBlockSelected (emitted in the tail of
epoch N), retried ~6.7 min, then abandoned. Prereqs to succeed: entity registered,
vote power > 0 at the vote-power block, the window enabled, a valid signing-policy
signature, and the SENDER (Submit) account funded for gas. `_voter` = the Identity
address.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from clif.funding import _GREEN, _RED, _RESET, _YELLOW, ACCOUNTS, SYMBOL
from clif.rpc import RpcClient, RpcError

# Submit must hold at least this many native tokens or a registerVoter can't be
# paid for (RE423 needed ~1.6 FLR / ~1.4 SGB). The floor is generous over that so a
# drain trips CRIT well before it is fatal; it is INDEPENDENT of the 250 funding
# band (a healthy target) — this is the hard "can we register at all" line.
_DEFAULT_GAS_FLOOR = 10.0
_ZERO = "0x0000000000000000000000000000000000000000"


def _addr_of(network: str, name: str) -> str | None:
    for a in ACCOUNTS.get(network, []):
        if a.name == name:
            return a.address
    return None


def _is_zero(addr: str) -> bool:
    try:
        return int(addr, 16) == 0
    except (TypeError, ValueError):
        return True


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    sign = "-" if s < 0 else ""
    s = abs(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{sign}{d}d{h}h"
    if h:
        return f"{sign}{h}h{m}m"
    return f"{sign}{m}m"


@dataclass
class RegistrationReadiness:
    network: str
    supported: bool = True  # False ⇒ no VoterRegistry pinned (e.g. coston2) — untracked
    current_epoch: int | None = None
    next_epoch: int | None = None
    current_registered: bool = False
    next_window_enabled: bool = False
    vote_power_block: int = 0
    next_registered: bool = False
    next_weight: int = 0  # wei; > 0 once the window is open ⇒ we'll register with weight
    sender_account: str = "Submit"
    sender_balance: float | None = None  # native units; None ⇒ read failed
    gas_floor: float = _DEFAULT_GAS_FLOOR
    entity_ok: bool = False
    time_to_boundary_sec: float | None = None
    error: str | None = None  # a read/transport failure ⇒ fail loud, never green

    # --- derived prereqs ---
    @property
    def gas_ok(self) -> bool:
        return self.sender_balance is not None and self.sender_balance >= self.gas_floor

    @property
    def gas_nearing(self) -> bool:
        return (
            self.sender_balance is not None
            and self.gas_ok
            and self.sender_balance < self.gas_floor * 2
        )

    @property
    def votepower_ok(self) -> bool:
        # Only a failure once the window is open with zero registration weight.
        return (not self.next_window_enabled) or self.next_weight > 0

    @property
    def severity(self) -> str:
        if not self.supported:
            return "OK"  # nothing to track on this network
        if self.error is not None:
            return "CRIT"  # unknown = treat as bad (RE423 was hidden by green-on-unknown)
        if not self.current_registered:
            return "CRIT"  # LIVE EXCLUSION — no rewards this epoch
        if not self.gas_ok:
            return "CRIT"  # Submit can't afford registerVoter — imminent RE423
        if not self.entity_ok:
            return "CRIT"  # entity gap — registration will revert
        if self.next_window_enabled and not self.next_registered:
            if not self.votepower_ok:
                return "CRIT"  # window open but 0 weight — excluded even if the tx lands
            return "WARN"  # window open, not yet registered, prereqs green (client retrying)
        if self.gas_nearing:
            return "WARN"
        return "OK"

    def to_dict(self) -> dict:
        return {
            "network": self.network,
            "supported": self.supported,
            "severity": self.severity,
            "current_epoch": self.current_epoch,
            "current_registered": self.current_registered,
            "next_epoch": self.next_epoch,
            "next_window_enabled": self.next_window_enabled,
            "next_registered": self.next_registered,
            "vote_power_block": self.vote_power_block,
            "next_weight": self.next_weight,
            "votepower_ok": self.votepower_ok,
            "entity_ok": self.entity_ok,
            "sender_account": self.sender_account,
            "sender_balance": (round(self.sender_balance, 4) if self.sender_balance is not None else None),
            "gas_floor": self.gas_floor,
            "gas_ok": self.gas_ok,
            "time_to_boundary_sec": (int(self.time_to_boundary_sec) if self.time_to_boundary_sec is not None else None),
            "error": self.error,
        }


def read_readiness(
    rpc: RpcClient,
    network: str,
    *,
    flare_systems_manager: str,
    voter_registry: str,
    entity_manager: str,
    gas_floor: float = _DEFAULT_GAS_FLOOR,
    sender_account: str = "Submit",
    now: float | None = None,
) -> RegistrationReadiness:
    """Read registration status for `network`. NEVER raises — a transport error
    becomes a CRIT readiness (silence is never green; RE423's blind spot). Reads
    are revert-tolerant: a not-yet-set-up N+1 epoch ⇒ window-not-open, not error."""
    now = time.time() if now is None else now
    identity = _addr_of(network, "Identity")
    sender = _addr_of(network, sender_account)

    if not voter_registry or not entity_manager or identity is None:
        # No registered-voter surface pinned (e.g. coston2) — untracked, not a failure.
        return RegistrationReadiness(
            network=network, supported=False, sender_account=sender_account, gas_floor=gas_floor,
        )

    r = RegistrationReadiness(network=network, sender_account=sender_account, gas_floor=gas_floor)
    try:
        current = rpc.get_current_reward_epoch_id(flare_systems_manager)
        first_ts, dur = rpc.reward_epoch_timing(flare_systems_manager)
        r.current_epoch = current
        r.next_epoch = current + 1
        # epoch_end_ts(N) = first + (N+1)*dur — the apgateway model (works for future N).
        r.time_to_boundary_sec = float(first_ts + (current + 1) * dur - now)

        r.current_registered = rpc.is_voter_registered(voter_registry, identity, current)
        r.vote_power_block, r.next_window_enabled = rpc.voter_registration_data(
            flare_systems_manager, r.next_epoch
        )
        r.next_registered = rpc.is_voter_registered(voter_registry, identity, r.next_epoch)
        r.next_weight = rpc.voter_registration_weight(voter_registry, identity, r.next_epoch)

        if sender is not None:
            r.sender_balance = rpc.get_balance(sender) / 1e18
        entity_addrs = rpc.get_voter_addresses(entity_manager, identity)
        r.entity_ok = all(not _is_zero(a) for a in entity_addrs)
    except RpcError as exc:  # transport/node error — fail loud, never green
        r.error = str(exc)
    return r


def render_readiness(r: RegistrationReadiness, *, active: bool) -> str:
    """One COLOR-CODED line for the epoch/registration daemons. `active` = the
    operator is in a reward-signing phase (turn the volume up on anything wrong)."""
    sym = SYMBOL.get(r.network, "")
    net = r.network

    if not r.supported:
        return f"\033[2mregistration: not tracked on {net}{_RESET}"

    sev = r.severity
    if r.error is not None:
        return (
            f"{_RED}🔴 registration STATE UNKNOWN on {net} ({r.error[:80]}) — "
            f"treat as NOT REGISTERED{_RESET}"
        )

    # Base: current-epoch membership — the RE423 detector. A live exclusion is the
    # loudest state there is, so it always gets the double marker.
    if not r.current_registered:
        head = (
            f"{_RED}🔴🔴 NOT REGISTERED — {net} RE{r.current_epoch} LIVE EXCLUSION "
            f"(ZERO rewards this epoch){_RESET}"
        )
    else:
        head = f"{_GREEN}✓ registered {net} RE{r.current_epoch}{_RESET}"

    # Next-epoch clause.
    tminus = _fmt_dur(r.time_to_boundary_sec)
    if r.next_registered:
        nxt = f"{_GREEN}· RE{r.next_epoch} ✓{_RESET}"
    elif r.next_window_enabled:
        problems = []
        if not r.votepower_ok:
            problems.append("0 vote power")
        if not r.gas_ok:
            problems.append(f"Submit<{r.gas_floor:g}")
        if not r.entity_ok:
            problems.append("entity gap")
        if problems:
            nxt = f"{_RED}· RE{r.next_epoch} window OPEN — will FAIL: {', '.join(problems)}{_RESET}"
        else:
            nxt = f"{_YELLOW}· RE{r.next_epoch} window OPEN — not yet registered (T{tminus}){_RESET}"
    else:
        nxt = f"\033[2m· RE{r.next_epoch} window not open (T{tminus} to boundary){_RESET}"

    # Gas clause (Submit / sender solvency for registerVoter).
    bal = r.sender_balance
    if bal is None:
        gas = f"{_RED}· {r.sender_account} balance UNKNOWN{_RESET}"
    elif not r.gas_ok:
        gas = f"{_RED}· {r.sender_account} {bal:.2f} {sym} < floor {r.gas_floor:g} — can't afford registerVoter{_RESET}"
    elif r.gas_nearing:
        gas = f"{_YELLOW}· {r.sender_account} {bal:.2f} {sym} nearing floor {r.gas_floor:g}{_RESET}"
    else:
        gas = ""

    parts = [head, nxt] + ([gas] if gas else [])
    line = " ".join(parts)
    if sev == "CRIT" and active:
        line = f"{_RED}‼ WHILE SIGNING ‼{_RESET} " + line
    return line
