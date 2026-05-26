"""Claim orchestration: discover → build calldata → fwd sign-and-send → confirm.

This is the only path that touches a value operation, and it does so without a
key: clif builds the `RewardManager.claim` calldata and fwd signs+broadcasts.
Every fwd failure is classified so the caller (one-shot `clif claim` or the
`clif auto` daemon) can act correctly — terminal errors are escalated, not
hot-looped; retryable errors back off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from clif.calldata import build_claim_calldata
from clif.config import ZERO_BYTES32, Settings
from clif.discovery import collect_reward_claims
from clif.fwd_client import (
    FwdClient,
    FwdError,
    FwdRetryableError,
    FwdTerminalError,
    make_idempotency_key,
)
from clif.models import ClaimType
from clif.rpc import RpcClient, RpcError

log = logging.getLogger("clif")


class OutcomeStatus(str, Enum):
    NOTHING_CLAIMABLE = "nothing-claimable"
    SUBMITTED_MINED = "submitted-mined"
    SUBMITTED_PENDING = "submitted-pending"
    MINED_NOOP = "mined-noop"  # tx mined (status 0x1) but claimed nothing — epoch already claimed
    FAILED_RETRYABLE = "failed-retryable"  # transient — retry next cycle
    FAILED_TERMINAL = "failed-terminal"  # operator action needed — escalate


_OK = {
    OutcomeStatus.NOTHING_CLAIMABLE,
    OutcomeStatus.SUBMITTED_MINED,
    OutcomeStatus.SUBMITTED_PENDING,
    OutcomeStatus.MINED_NOOP,
}


def _claim_took_effect(rpc: RpcClient, reward_manager: str, tx_hash: str) -> bool:
    """Did the mined claim actually transfer a reward?

    A real `RewardManager.claim` emits a `RewardClaimed` log from the reward
    manager; claiming an already-claimed `(rewardOwner, epoch)` is a SILENT
    status-0x1 no-op with NO such log (it does NOT revert, unlike
    `signUptimeVote`). So a mined receipt is verified by EFFECT (a log from the
    reward manager), never by status alone. On an RPC failure we cannot disprove
    the claim, so we do not assert a false no-op (return True).
    """
    try:
        receipt = rpc.get_transaction_receipt(tx_hash)
    except RpcError:
        return True
    if receipt is None:
        return True
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
    """The post-discovery half: build calldata → fwd sign-and-send → confirm.

    Split out from `run_claim` so the `clif auto` daemon can discover once
    (keyless), gate the terminal-error cooldown on the prospective last
    epoch, and only then submit — without a second discovery round-trip.

    `retry` (operator-controlled) overrides `settings.idempotency_retry` for
    this attempt; both default to None ⇒ the legacy deterministic key (a
    same-attempt network retry / crash-rerun still dedups). A new value is a
    deliberate post-on-chain-failure re-attempt → fresh key (fwd D14).
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

    try:
        resp = fwd.sign_and_send(
            wallet=settings.fwd_wallet_name,
            chain=settings.net.chain_id,
            to=settings.net.reward_manager,
            data=data,
            value_wei="0",
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

    if not wait:
        return out(
            OutcomeStatus.SUBMITTED_PENDING, "submitted (no wait)",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
        )

    try:
        st = fwd.wait_until_mined(resp.tx_id, timeout=wait_timeout)
    except (FwdRetryableError, TimeoutError) as exc:
        return out(
            OutcomeStatus.SUBMITTED_PENDING, f"submitted; not yet mined: {exc}",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
        )
    except FwdError as exc:  # e.g. tx_id 404 on status poll
        return out(
            OutcomeStatus.FAILED_TERMINAL, f"submitted; status poll terminal: {exc}",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
        )

    if st.status == "mined":
        # Effect-check: a mined receipt with NO RewardManager log claimed nothing
        # (the epoch was already claimed — a silent status-0x1 no-op). Never report
        # such a tx as a successful claim. Only runs when a keyless rpc is supplied.
        if rpc is not None and resp.hash and not _claim_took_effect(
            rpc, settings.net.reward_manager, resp.hash
        ):
            return out(
                OutcomeStatus.MINED_NOOP,
                "mined but claimed nothing — epoch already claimed "
                "(no RewardManager event in receipt)",
                epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
            )
        return out(
            OutcomeStatus.SUBMITTED_MINED, "mined",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
        )
    if st.status == "replaced":
        return out(
            OutcomeStatus.SUBMITTED_PENDING, "fwd replacing (gas bump) — still pending",
            epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
        )
    # failed / dropped — an on-chain revert of a legitimate claim is serious.
    return out(
        OutcomeStatus.FAILED_TERMINAL, f"tx terminal on-chain status={st.status}",
        epochs=epochs, last_epoch=last_epoch, tx_id=resp.tx_id, tx_hash=resp.hash,
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
        net = settings.net
        try:
            nxt = rpc.next_claimable_reward_epoch_id(net.reward_manager, beneficiary)
            _, end = rpc.reward_epoch_id_range(net.reward_manager)
            signed = rpc.rewards_hash(net.flare_systems_manager, only_epoch) != ZERO_BYTES32
        except RpcError as exc:
            return _out(OutcomeStatus.FAILED_RETRYABLE, f"discovery rpc failure: {exc}")
        if only_epoch < nxt:
            return _out(
                OutcomeStatus.NOTHING_CLAIMABLE,
                f"epoch {only_epoch} already claimed for {beneficiary} "
                f"(next claimable = {nxt})",
            )
        if only_epoch > end:
            return _out(
                OutcomeStatus.NOTHING_CLAIMABLE,
                f"epoch {only_epoch} not in claimable range "
                f"(max signed-rewards epoch = {end})",
            )
        if not signed:
            return _out(
                OutcomeStatus.NOTHING_CLAIMABLE,
                f"epoch {only_epoch} rewards not yet signed (>50% claim gate not reached)",
            )

    try:
        claims = collect_reward_claims(rpc, settings, beneficiary, claim_type, only_epoch)
    except RpcError as exc:
        return _out(OutcomeStatus.FAILED_RETRYABLE, f"discovery rpc failure: {exc}")
    return submit_claims(
        settings, fwd, claim_type, beneficiary, claims,
        wait=wait, wait_timeout=wait_timeout, retry=retry, rpc=rpc,
    )
