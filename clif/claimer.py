"""Claim orchestration: discover → build calldata → fwd sign → clif broadcasts → confirm.

This is the only path that touches a value operation, and it does so without a
key: clif builds the `RewardManager.claim` calldata; fwd signs and returns a raw
tx blob; clif broadcasts via rpc.py and reports the outcome back to fwd. Every
fwd failure and every broadcast failure is classified so the caller (one-shot
`clif claim` or the `clif auto` daemon) can act correctly — terminal errors are
escalated, not hot-looped; retryable errors back off.

Node broadcast-error classification (for fwd broadcast-result report):
  "nonce too low" / "nonce too low" substring → "rejected_nonce_too_low"
  Any other deterministic node rejection (insufficient funds, gas limit, etc.)
    → "rejected_releaseable" (fwd releases the nonce reservation)
  Transport/RPC failure (httpx error, node down) → FAILED_RETRYABLE, no report
    (the nonce remains reserved; operator must inspect and cancel manually or
    wait for fwd's internal stuck-tx replacement).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from clif.calldata import build_claim_calldata
from clif.config import Settings
from clif.discovery import (
    classify_claim_frontier,
    collect_reward_claims,
    unclaimable_reason,
)
from clif.fwd_client import (
    FwdClient,
    FwdError,
    FwdRetryableError,
    FwdTerminalError,
    make_idempotency_key,
)
from clif.models import ClaimType
from clif.rpc import RpcClient, RpcError

# Substring match for "nonce too low" node rejection across node implementations.
_NONCE_TOO_LOW_MARKERS = ("nonce too low", "nonce too small", "tx nonce too low")

log = logging.getLogger("clif")


class OutcomeStatus(str, Enum):
    NOTHING_CLAIMABLE = "nothing-claimable"
    SUBMITTED_MINED = "submitted-mined"
    SUBMITTED_PENDING = "submitted-pending"
    MINED_NOOP = "mined-noop"  # tx mined (status 0x1) but claimed nothing — epoch already claimed
    ALREADY_FINALIZED = "already-finalized"  # FSP: epoch reached the >50% signing-weight threshold
    # + finalized before our signature landed — too late this round, not a fault
    FAILED_RETRYABLE = "failed-retryable"  # transient — retry next cycle
    FAILED_TERMINAL = "failed-terminal"  # operator action needed — escalate


_OK = {
    OutcomeStatus.NOTHING_CLAIMABLE,
    OutcomeStatus.SUBMITTED_MINED,
    OutcomeStatus.SUBMITTED_PENDING,
    OutcomeStatus.MINED_NOOP,
    OutcomeStatus.ALREADY_FINALIZED,
}


def _classify_broadcast_error(exc: RpcError) -> tuple[str, str]:
    """Map a node-rejection RpcError to a (fwd_outcome, error_class) pair.

    Returns:
      ("rejected_nonce_too_low", <class>) — nonce race/restart, fwd corrects.
      ("rejected_releaseable", <class>)   — deterministic rejection (insuff.
        funds, gas limit, etc.); fwd releases the nonce reservation.
    """
    msg = str(exc).lower()
    if any(marker in msg for marker in _NONCE_TOO_LOW_MARKERS):
        return "rejected_nonce_too_low", type(exc).__name__
    return "rejected_releaseable", type(exc).__name__


def _claim_took_effect(rpc: RpcClient, reward_manager: str, tx_hash: str) -> bool:
    """Did the mined claim actually transfer a reward?

    A real `RewardManager.claim` emits a `RewardClaimed` log from the reward
    manager; claiming an already-claimed `(rewardOwner, epoch)` is a SILENT
    status-0x1 no-op with NO such log (it does NOT revert, unlike
    `signUptimeVote`). So a mined receipt is verified by EFFECT (a log from the
    reward manager), never by status alone.

    On an RPC failure we return False (not confirmed) and log a warning — the
    caller should treat the claim as uncertain, not successful. Returning True
    on RPC failure previously caused silent missed claims when the receipt fetch
    failed; returning False surfaces the uncertainty so the operator can verify.
    """
    try:
        receipt = rpc.get_transaction_receipt(tx_hash)
    except RpcError as exc:
        log.warning(
            "_claim_took_effect: RPC failure fetching receipt for %s: %s — "
            "treating as not confirmed (claim effect uncertain)",
            tx_hash, exc,
        )
        return False
    if receipt is None:
        log.warning(
            "_claim_took_effect: receipt for %s is None — "
            "treating as not confirmed (claim effect uncertain)",
            tx_hash,
        )
        return False
    rm = reward_manager.lower()
    return any(
        str(lg.get("address", "")).lower() == rm
        for lg in (receipt.get("logs") or [])
    )


@dataclass
class ClaimOutcome:
    claim_type: int
    claim_type_name: str
    beneficiary: str
    status: OutcomeStatus
    detail: str
    epochs: list[int] = field(default_factory=list)
    last_epoch: int | None = None
    tx_id: str | None = None
    tx_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in _OK


def submit_claims(
    settings: Settings,
    fwd: FwdClient,
    claim_type: int,
    beneficiary: str,
    claims: list,
    wait: bool = True,
    wait_timeout: float = 600.0,
    retry: str | None = None,
    rpc: RpcClient | None = None,
) -> ClaimOutcome:
    """The post-discovery half: build calldata → fwd sign → clif broadcast → confirm.

    Split out from `run_claim` so the `clif auto` daemon can discover once
    (keyless), gate the terminal-error cooldown on the prospective last
    epoch, and only then submit — without a second discovery round-trip.

    `retry` (operator-controlled) overrides `settings.idempotency_retry` for
    this attempt; both default to None ⇒ the legacy deterministic key (a
    same-attempt network retry / crash-rerun still dedups). A new value is a
    deliberate post-on-chain-failure re-attempt → fresh key (fwd D14).

    fwd v1.1.0a9+: fwd signs and returns a raw tx blob; clif broadcasts via
    rpc.py. `rpc` is therefore required when `wait=True` or when broadcasting
    is needed. If rpc is None, we can only sign (no broadcast) and return
    SUBMITTED_PENDING.
    """
    name = ClaimType(claim_type).name

    def out(status: OutcomeStatus, detail: str, **kw: object) -> ClaimOutcome:
        return ClaimOutcome(
            claim_type=claim_type,
            claim_type_name=name,
            beneficiary=beneficiary,
            status=status,
            detail=detail,
            **kw,  # type: ignore[arg-type]
        )

    # Config preconditions are terminal (operator must fix; do not hot-loop).
    if not settings.fwd_wallet_name or not settings.fwd_caller_token:
        return out(OutcomeStatus.FAILED_TERMINAL, "FWD_WALLET_NAME / FWD_CALLER_TOKEN not set")
    if not settings.claim_recipient_address:
        return out(OutcomeStatus.FAILED_TERMINAL, "CLAIM_RECIPIENT_ADDRESS not set")

    if not claims:
        return out(OutcomeStatus.NOTHING_CLAIMABLE, "no claimable rewards", epochs=[])

    epochs = [c.body.reward_epoch_id for c in claims]
    last_epoch = epochs[-1]
    data = build_claim_calldata(
        beneficiary,
        settings.claim_recipient_address,
        last_epoch,
        settings.wrap_rewards,
        claims,
    )
    retry_token = retry if retry is not None else settings.idempotency_retry
    idem = make_idempotency_key(
        settings.network, claim_type, beneficiary, last_epoch, retry=retry_token
    )
    log.info(
        "submit %s beneficiary=%s epochs=%s idempotency-key=%s retry=%s",
        name, beneficiary, epochs, idem, retry_token or "<none>",
    )

    # Step 1: ask fwd to sign. fwd allocates the nonce; gas+fees supplied by
    # caller (or estimated via rpc below). When rpc is available, estimate gas
    # on-the-fly; otherwise use settings.fsp_submit_gas as a safe fallback
    # (non-FSP claim; the fallback is conservative but correct).
    if rpc is not None:
        try:
            # clif is keyless: it does NOT know the executor (fwd) wallet's
            # address, so it cannot supply a valid `from` for eth_estimateGas
            # (passing the wallet NAME made the node reject the call). Use a
            # fixed claim gas, mirroring the FSP Leg-2 fix; fees need no `from`.
            gas = settings.fsp_submit_gas
            max_fee, max_priority = rpc.suggest_fees()
        except RpcError as exc:
            return out(
                OutcomeStatus.FAILED_RETRYABLE,
                f"fee estimation rpc failure: {exc}",
                epochs=epochs, last_epoch=last_epoch,
            )
    else:
        # No rpc: use conservative defaults.
        gas = settings.fsp_submit_gas
        max_fee = 100_000_000_000  # 100 gwei
        max_priority = 1_000_000_000  # 1 gwei

    try:
        resp = fwd.sign_transaction(
            wallet=settings.fwd_wallet_name,
            chain=settings.net.chain_id,
            to=settings.net.reward_manager,
            data=data,
            value_wei="0",
            gas=gas,
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=max_priority,
            idempotency_key=idem,
        )
    except FwdTerminalError as exc:
        return out(
            OutcomeStatus.FAILED_TERMINAL, f"fwd denied/failed: {exc}",
            epochs=epochs, last_epoch=last_epoch,
        )
    except FwdRetryableError as exc:
        return out(
            OutcomeStatus.FAILED_RETRYABLE, f"fwd transient: {exc}",
            epochs=epochs, last_epoch=last_epoch,
        )

    # Step 2: broadcast (requires rpc).
    if rpc is None:
        # No rpc available — can't broadcast. Return pending so the caller
        # can decide (e.g. `wait=False` test paths).
        return out(
            OutcomeStatus.SUBMITTED_PENDING, "signed (no rpc — cannot broadcast)",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
        )

    if not wait:
        # Caller requested no-wait. We have the signed blob but skip broadcast+poll.
        return out(
            OutcomeStatus.SUBMITTED_PENDING, "submitted (no wait)",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
        )

    try:
        broadcast_hash = rpc.send_raw_transaction(resp.signed_raw_tx)
    except RpcError as exc:
        # Classify the node rejection and report back to fwd.
        fwd_outcome, err_class = _classify_broadcast_error(exc)
        report_hash = resp.hash  # use fwd's locally-computed hash
        try:
            fwd.report_broadcast_result(resp.tx_id, report_hash, fwd_outcome, err_class)
        except (FwdRetryableError, FwdTerminalError, FwdError):
            pass  # best-effort; the broadcast already failed
        if fwd_outcome == "rejected_nonce_too_low":
            # Nonce race — transient, operator should retry.
            return out(
                OutcomeStatus.FAILED_RETRYABLE,
                f"broadcast rejected (nonce too low): {exc}",
                epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
            )
        # Deterministic node rejection — terminal.
        return out(
            OutcomeStatus.FAILED_TERMINAL,
            f"broadcast rejected ({err_class}): {exc}",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
        )

    # Step 3: report accepted broadcast to fwd.
    try:
        fwd.report_broadcast_result(resp.tx_id, broadcast_hash, "accepted")
    except (FwdRetryableError, FwdTerminalError, FwdError) as exc:
        log.warning("fwd broadcast-result report failed (non-fatal): %s", exc)

    log.info(
        "submit %s broadcasted tx_id=%s hash=%s",
        name, resp.tx_id, broadcast_hash,
    )

    # Step 4: poll for receipt.
    receipt = rpc.poll_receipt(broadcast_hash, timeout=wait_timeout)
    if receipt is None:
        return out(
            OutcomeStatus.SUBMITTED_PENDING, "submitted; receipt poll timed out",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=broadcast_hash,
        )

    block_number = int(str(receipt.get("blockNumber", "0x0")), 16)
    status_hex = str(receipt.get("status", "0x0"))
    mined_ok = int(status_hex, 16) == 1

    # Step 5: report receipt to fwd.
    receipt_outcome = "mined_success" if mined_ok else "mined_reverted"
    try:
        fwd.report_receipt(resp.tx_id, broadcast_hash, receipt_outcome, block_number)
    except (FwdRetryableError, FwdTerminalError, FwdError) as exc:
        log.warning("fwd receipt report failed (non-fatal): %s", exc)

    if not mined_ok:
        return out(
            OutcomeStatus.FAILED_TERMINAL, "tx reverted on-chain",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=broadcast_hash,
        )

    # Step 6: effect verification — a mined receipt with NO RewardManager log
    # claimed nothing (the epoch was already claimed — a silent status-0x1 no-op).
    # Never report such a tx as a successful claim.
    if not _claim_took_effect(rpc, settings.net.reward_manager, broadcast_hash):
        return out(
            OutcomeStatus.MINED_NOOP,
            "mined but claimed nothing — epoch already claimed "
            "(no RewardManager event in receipt)",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=broadcast_hash,
        )

    return out(
        OutcomeStatus.SUBMITTED_MINED, "mined",
        epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=broadcast_hash,
    )


def run_claim(
    settings: Settings,
    rpc: RpcClient,
    fwd: FwdClient,
    claim_type: int,
    beneficiary: str,
    only_epoch: int | None = None,
    wait: bool = True,
    wait_timeout: float = 600.0,
    retry: str | None = None,
) -> ClaimOutcome:
    """Discover (keyless) then submit. The one-shot `clif claim` entry point.

    For the explicit `-e <epoch>` path, a pre-flight refuses — with a precise
    reason — an epoch that is already claimed / out of range / not yet signed,
    so clif never submits a silent status-0x1 no-op nor reports one as a claim.
    The `submit_claims` post-flight effect-check (RewardManager event in the
    mined receipt) is the safety net for the claimed-in-the-race case.
    """
    name = ClaimType(claim_type).name

    def _out(status: OutcomeStatus, detail: str) -> ClaimOutcome:
        return ClaimOutcome(
            claim_type=claim_type, claim_type_name=name, beneficiary=beneficiary,
            status=status, detail=detail, epochs=[], last_epoch=only_epoch,
        )

    if only_epoch is not None:
        # Pre-flight gate (reused by the shared classifier): refuse — with the
        # precise reason — an already-claimed / out-of-range / not-yet-signed
        # epoch, so clif never builds a silent no-op claim.
        try:
            reason = unclaimable_reason(rpc, settings, beneficiary, only_epoch)
        except RpcError as exc:
            return _out(OutcomeStatus.FAILED_RETRYABLE, f"discovery rpc failure: {exc}")
        if reason is not None:
            return _out(OutcomeStatus.NOTHING_CLAIMABLE, f"epoch {only_epoch} {reason}")

    try:
        claims = collect_reward_claims(rpc, settings, beneficiary, claim_type, only_epoch)
    except RpcError as exc:
        return _out(OutcomeStatus.FAILED_RETRYABLE, f"discovery rpc failure: {exc}")
    if not claims:
        # Never report a bare 'nothing-claimable' that conflates a DONE state
        # (already claimed / no accrual) with a PENDING one (not finalized /
        # not signed). Classify the real reason from the on-chain frontier.
        if only_epoch is not None:
            # Passed the on-chain gates above but absent from the merkle tree.
            return _out(
                OutcomeStatus.NOTHING_CLAIMABLE,
                f"epoch {only_epoch} no rewards accrued for this beneficiary",
            )
        try:
            frontier = classify_claim_frontier(rpc, settings, beneficiary, claim_type)
        except RpcError as exc:
            return _out(OutcomeStatus.FAILED_RETRYABLE, f"discovery rpc failure: {exc}")
        detail = (
            "nothing pending — " + "; ".join(f"epoch {e}: {r}" for e, r in frontier)
            if frontier
            else "no claimable rewards"
        )
        return _out(OutcomeStatus.NOTHING_CLAIMABLE, detail)
    return submit_claims(
        settings, fwd, claim_type, beneficiary, claims,
        wait=wait, wait_timeout=wait_timeout, retry=retry, rpc=rpc,
    )
