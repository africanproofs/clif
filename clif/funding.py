"""Keyless gas-funding — keep AP's FSP accounts within their balance band.

RE423 (2026-08-10): a Submit account drained below the registerVoter gas cost at
the epoch boundary, unnoticed behind green submit metrics, and the whole ~3.5-day
epoch produced ZERO reward. This module keeps every FSP account funded: when a
balance breaches its LOWER bound, top it up to its UPPER bound via fwd's
native-transfer capability, sending from the `ap-funder` wallet. Keyless — clif
holds no key; fwd signs, clif broadcasts + reports back, exactly like the reward
path.

Two surfaces:
  - `run_funding(...)`  — the enforcement pass (the `clif-fund-<net>` daemon).
  - `funding_health(...)` + `render_health(...)` — read-only status the epoch
    daemon surfaces, COLOR-CODED, so a funding problem is impossible to miss while
    the operator is watching reward-signing.

Bands (operator, 2026-08-11), in native token units:
  gas-payers (Submit, SubmitSignatures, SigningPolicy, FastUpdates x3): 250 -> 400
  Identity, Delegation:                                                 150 -> 200
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from clif.fwd_client import FwdClient, FwdError, FwdRetryableError, FwdTerminalError
from clif.rpc import RpcClient, RpcError

# --- ANSI color (the epoch daemon logs plain lines; we color the message body) --
_RESET = "\033[0m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[1;31m"  # bold red — reserved for "something is going wrong"
_DIM = "\033[2m"
_CYAN = "\033[36m"

_WEI = 10**18
_AP_FUNDER_ADDRESS = "0xEa1355cC14f91092E135Eb2B43092DAe61Ebba27"


@dataclass(frozen=True)
class Band:
    lower: float  # breach this ⇒ fund
    upper: float  # top back up to this


GAS = Band(250.0, 400.0)
IDENTITY = Band(150.0, 200.0)


@dataclass(frozen=True)
class Account:
    name: str
    address: str
    band: Band


# Per-network registry. Addresses from proofs.africa/CLAUDE.md § On-Chain Identity.
ACCOUNTS: dict[str, list[Account]] = {
    "flare": [
        Account("Submit", "0x366BCb8c23490327A1C63880875a35de41705876", GAS),
        Account("SubmitSignatures", "0x81acBFb044b7fbc4177e3b033FBf10fa8d335Dc0", GAS),
        Account("SigningPolicy", "0x3FA07a1d5Cb3B34C7A04782C5dd8bE9D910ed0e5", GAS),
        Account("FastUpdates-1", "0x8AF4f6Eb1A26516Ccb0fe02AD7182f96c0CA5bBb", GAS),
        Account("FastUpdates-2", "0xD04cFE38B880A22C44b5189fD39b889F4a7978FA", GAS),
        Account("FastUpdates-3", "0x85BA3aF484Fe8ee8bAcE1470AeBd88d57A0078F3", GAS),
        Account("Identity", "0x26534aC74153E3257dDD3471f96faA33D5D3B575", IDENTITY),
        Account("Delegation", "0x7808b9e0f7c488172b54b30f98c2fcf36d903b2c", IDENTITY),
    ],
    "songbird": [
        Account("Submit", "0xa8BBBA017b2ce496bBED4CFC0FB5D1aFd4F23772", GAS),
        Account("SubmitSignatures", "0xf417294C5a65535d4a33259c73eeA39373977C65", GAS),
        Account("SigningPolicy", "0xfB021c01f924C416D1FEd247161963EfC2Ae7604", GAS),
        Account("FastUpdates-1", "0x83AFf99BD804a342588a7c7E9016a056D93d0A3C", GAS),
        Account("FastUpdates-2", "0xb0de315D06f955608Db6d56bbC71d653AE1bC939", GAS),
        Account("FastUpdates-3", "0x0DED6921aB23f447729deed86bb20C50EE8e05BB", GAS),
        Account("Identity", "0xcf3A3e5797A960C67e0E4B23D4594246ffB9d935", IDENTITY),
        Account("Delegation", "0xaf31ca175bbe0c6dd667c8403b65a33b28238afa", IDENTITY),
    ],
}

# Native-token symbol per network (for messaging).
SYMBOL = {"flare": "FLR", "songbird": "SGB"}

# ap-funder is flagged when it can no longer comfortably cover a worst-case pass.
# Worst case = every account below band → sum of (upper - 0) ≈ 8*400 = 3200; we
# want a cushion, so WARN below 2x a single top-up and CRIT below one top-up.
_FUNDER_WARN = 800.0
_FUNDER_CRIT = 400.0  # can't guarantee even one 400-cap top-up


@dataclass
class AccountStatus:
    name: str
    address: str
    balance: float
    band: Band

    @property
    def below(self) -> bool:
        return self.balance < self.band.lower

    @property
    def nearing(self) -> bool:
        # within 10% above the floor — not breached, but worth a heads-up
        return not self.below and self.balance < self.band.lower * 1.10


@dataclass
class FundingHealth:
    network: str
    accounts: list[AccountStatus]
    funder_balance: float | None  # None ⇒ RPC read failed
    error: str | None = None  # a read/transport failure ⇒ fail loud, never green

    @property
    def below(self) -> list[AccountStatus]:
        return [a for a in self.accounts if a.below]

    @property
    def nearing(self) -> list[AccountStatus]:
        return [a for a in self.accounts if a.nearing]

    @property
    def funder_crit(self) -> bool:
        return self.funder_balance is not None and self.funder_balance < _FUNDER_CRIT

    @property
    def funder_warn(self) -> bool:
        return self.funder_balance is not None and self.funder_balance < _FUNDER_WARN

    @property
    def severity(self) -> str:
        # CRIT: something is (or is about to be) gas-starved, or the funder itself
        # can't cover, or a read failed (unknown = treat as bad).
        if self.error is not None or self.funder_balance is None:
            return "CRIT"
        if self.below or self.funder_crit:
            return "CRIT"
        if self.nearing or self.funder_warn:
            return "WARN"
        return "OK"


def read_health(rpc: RpcClient, network: str) -> FundingHealth:
    """Read balances for every account + the funder. Never raises — a transport
    error becomes a CRIT FundingHealth (silence is never green; RE423 was hidden
    by green-looking metrics)."""
    accts: list[AccountStatus] = []
    funder: float | None = None
    err: str | None = None
    try:
        for a in ACCOUNTS[network]:
            bal = rpc.get_balance(a.address) / _WEI
            accts.append(AccountStatus(a.name, a.address, bal, a.band))
        funder = rpc.get_balance(_AP_FUNDER_ADDRESS) / _WEI
    except RpcError as exc:  # transport/node error — fail loud
        err = str(exc)
    return FundingHealth(network=network, accounts=accts, funder_balance=funder, error=err)


def render_health(h: FundingHealth, *, active: bool) -> str:
    """One COLOR-CODED line for the epoch daemon. `active` = the operator is in a
    reward-signing phase (turn the volume up on anything wrong)."""
    sym = SYMBOL.get(h.network, "")
    funder = "?" if h.funder_balance is None else f"{h.funder_balance:.0f}"
    sev = h.severity

    if sev == "OK":
        n = len(h.accounts)
        return (
            f"{_GREEN}💰 funding ✓ all {n} {h.network} accounts in band "
            f"[gas 250–400 · id 150–200] · ap-funder {funder} {sym}{_RESET}"
        )

    if sev == "WARN":
        bits = []
        for a in h.nearing:
            bits.append(f"{a.name} {a.balance:.0f} nearing {a.band.lower:.0f}")
        if h.funder_warn:
            bits.append(f"ap-funder {funder} {sym} getting low")
        detail = "; ".join(bits) or "approaching a floor"
        return f"{_YELLOW}💰 funding ⚠ {detail} — top-up due soon{_RESET}"

    # CRIT — make it shout. Distinct banner so it can't blend into the scroll.
    lead = "🔴🔴 FUNDING ALERT" if active else "🔴 FUNDING ALERT"
    if h.error is not None or h.funder_balance is None:
        why = h.error or "balance read failed"
        return f"{_RED}{lead} — cannot read funding state ({why}); STATE UNKNOWN{_RESET}"
    parts: list[str] = []
    for a in h.below:
        parts.append(f"{a.name} {a.balance:.0f} {sym} BELOW {a.band.lower:.0f} FLOOR")
    if h.funder_crit:
        parts.append(
            f"ap-funder {funder} {sym} EXHAUSTED — REFILL {_AP_FUNDER_ADDRESS}"
        )
    body = " · ".join(parts)
    tail = " (gas-starved risk WHILE SIGNING)" if active else " (gas-starved risk)"
    return f"{_RED}{lead} — {body}{tail}{_RESET}"


@dataclass
class TopUp:
    name: str
    address: str
    before: float
    after: float
    sent: float
    tx_hash: str
    status: str  # "funded" | "failed"
    detail: str = ""


@dataclass
class FundingResult:
    network: str
    topups: list[TopUp] = field(default_factory=list)
    skipped_ok: int = 0
    error: str | None = None

    @property
    def failed(self) -> list[TopUp]:
        return [t for t in self.topups if t.status == "failed"]

    @property
    def funded(self) -> list[TopUp]:
        return [t for t in self.topups if t.status == "funded"]


def _execute_topup(
    *,
    rpc: RpcClient,
    fwd: FwdClient,
    account: Account,
    before: float,
    value_wei: int,
    network: str,
    wallet: str,
    chain_id: int,
    max_fee: int,
    max_priority: int,
    poll_timeout: float,
) -> TopUp:
    """The one keyless execute path: fwd signs → we broadcast → report back →
    verify the balance rose (mined ≠ success). Shared by the auto pass and the
    agent-driven plan so custody handling lives in exactly ONE place."""
    idem = f"clif-funding:{network}:{account.address.lower()}:{int(time.time())}"
    try:
        resp = fwd.sign_transaction(
            wallet=wallet,
            chain=chain_id,
            to=account.address,
            gas=21_000,
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=max_priority,
            value_wei=str(value_wei),
            data="0x",
            idempotency_key=idem,
        )
    except (FwdTerminalError, FwdRetryableError, FwdError) as exc:
        return TopUp(account.name, account.address, before, before, 0, "", "failed", f"fwd sign: {exc}")

    try:
        tx_hash = rpc.send_raw_transaction(resp.signed_raw_tx)
    except RpcError as exc:
        try:
            fwd.report_broadcast_result(resp.tx_id, resp.hash, "rejected_releaseable")
        except (FwdError, FwdRetryableError, FwdTerminalError):
            pass
        return TopUp(
            account.name, account.address, before, before, 0, resp.hash, "failed", f"broadcast: {exc}"
        )

    try:
        fwd.report_broadcast_result(resp.tx_id, tx_hash, "accepted")
    except (FwdError, FwdRetryableError, FwdTerminalError):
        pass

    receipt = rpc.poll_receipt(tx_hash, timeout=poll_timeout)
    block = int(str(receipt["blockNumber"]), 16) if receipt else 0
    if receipt is not None:
        try:
            fwd.report_receipt(resp.tx_id, tx_hash, "mined_success", block)
        except (FwdError, FwdRetryableError, FwdTerminalError):
            pass
    status_ok = receipt is not None and int(str(receipt["status"]), 16) == 1
    after = rpc.get_balance(account.address) / _WEI
    ok = status_ok and after > before  # mined ≠ success: verify the balance rose
    return TopUp(
        account.name,
        account.address,
        before,
        after,
        round(value_wei / _WEI, 4),
        tx_hash,
        "funded" if ok else "failed",
        "" if ok else "no balance rise / reverted",
    )


def run_funding(
    *,
    rpc: RpcClient,
    fwd: FwdClient,
    network: str,
    wallet: str,
    chain_id: int,
    poll_timeout: float = 120.0,
) -> FundingResult:
    """Enforcement pass: top up every account below its band's lower bound to its
    upper bound. Deterministic (reactive) — the daemon's default. Keyless."""
    res = FundingResult(network=network)
    try:
        max_fee, max_priority = rpc.suggest_fees()
    except RpcError as exc:
        res.error = f"fee read failed: {exc}"
        return res

    for a in ACCOUNTS[network]:
        try:
            before = rpc.get_balance(a.address) / _WEI
        except RpcError as exc:
            res.topups.append(TopUp(a.name, a.address, 0, 0, 0, "", "failed", f"balance read: {exc}"))
            continue
        if before >= a.band.lower:
            res.skipped_ok += 1
            continue
        value = int(round((a.band.upper - before) * _WEI))
        res.topups.append(
            _execute_topup(
                rpc=rpc, fwd=fwd, account=a, before=before, value_wei=value,
                network=network, wallet=wallet, chain_id=chain_id,
                max_fee=max_fee, max_priority=max_priority, poll_timeout=poll_timeout,
            )
        )
    return res


# --- the agent membrane: validate a proposed plan against HARD bounds ----------
#
# An untrusted decision-maker (a future AI agent, ADR-0006) proposes a plan; this
# deterministic membrane accepts/rejects each line BEFORE any execution. fwd's
# policy (recipient allowlist + per-tx cap + rate + daily aggregate) is the
# independent final gate — a line that slips a bug here still can't exceed fwd.


@dataclass
class PlanLine:
    account: str
    address: str = ""
    amount: float = 0.0  # native token, resolved
    accepted: bool = False
    reason: str = ""


def validate_plan(
    network: str,
    items: list[dict],
    balances: dict[str, float],
    funder_balance: float,
    *,
    per_tx_cap: float = 400.0,
) -> list[PlanLine]:
    """Validate a proposed funding plan against hard bounds. Each item is
    {"account": <name>, "amount": <native>} or {"account": <name>, "target": <native>}.
    Rejects (never raises): unknown account, non-positive amount, amount over the
    per-tx cap, a top-up that would push the account ABOVE its band upper, or a
    running total that exceeds the funder's balance. `balances` is keyed by
    lowercased address (the plan's accounts + read fresh by the caller)."""
    reg = {a.name.lower(): a for a in ACCOUNTS.get(network, [])}
    out: list[PlanLine] = []
    running = 0.0
    for it in items:
        name = str(it.get("account", "")).strip()
        a = reg.get(name.lower())
        if a is None:
            out.append(PlanLine(name, reason="unknown account (not in the network registry)"))
            continue
        bal = balances.get(a.address.lower())
        if bal is None:
            out.append(PlanLine(a.name, a.address, reason="no balance provided for account"))
            continue
        if "target" in it:
            target = float(it["target"])
            if target > a.band.upper + 1e-9:
                out.append(PlanLine(a.name, a.address, reason=f"target {target:g} > band upper {a.band.upper:g}"))
                continue
            amount = round(target - bal, 6)
        else:
            amount = round(float(it.get("amount", 0)), 6)
        if amount <= 0:
            out.append(PlanLine(a.name, a.address, amount, reason="amount ≤ 0 (already at/above target)"))
            continue
        if amount > per_tx_cap + 1e-9:
            out.append(PlanLine(a.name, a.address, amount, reason=f"amount {amount:g} > per-tx cap {per_tx_cap:g}"))
            continue
        if bal + amount > a.band.upper + 1e-9:
            out.append(PlanLine(a.name, a.address, amount, reason=f"would push {a.name} above band upper {a.band.upper:g}"))
            continue
        if running + amount > funder_balance + 1e-9:
            out.append(PlanLine(a.name, a.address, amount, reason=f"exceeds ap-funder balance ({funder_balance:g})"))
            continue
        running += amount
        out.append(PlanLine(a.name, a.address, amount, accepted=True))
    return out


def apply_plan(
    *,
    rpc: RpcClient,
    fwd: FwdClient,
    network: str,
    wallet: str,
    chain_id: int,
    lines: list[PlanLine],
    poll_timeout: float = 120.0,
) -> FundingResult:
    """Execute the ACCEPTED lines of a validated plan (rejected lines are never
    touched). Same keyless execute path as the auto pass."""
    res = FundingResult(network=network)
    accepted = [ln for ln in lines if ln.accepted]
    if not accepted:
        return res
    try:
        max_fee, max_priority = rpc.suggest_fees()
    except RpcError as exc:
        res.error = f"fee read failed: {exc}"
        return res
    reg = {a.name.lower(): a for a in ACCOUNTS[network]}
    for ln in accepted:
        a = reg[ln.account.lower()]
        try:
            before = rpc.get_balance(a.address) / _WEI
        except RpcError as exc:
            res.topups.append(TopUp(a.name, a.address, 0, 0, 0, "", "failed", f"balance read: {exc}"))
            continue
        # Re-check the bound at execution time (balances move): never overfund.
        value = int(round(min(ln.amount, max(0.0, a.band.upper - before)) * _WEI))
        if value <= 0:
            res.skipped_ok += 1
            continue
        res.topups.append(
            _execute_topup(
                rpc=rpc, fwd=fwd, account=a, before=before, value_wei=value,
                network=network, wallet=wallet, chain_id=chain_id,
                max_fee=max_fee, max_priority=max_priority, poll_timeout=poll_timeout,
            )
        )
    return res
