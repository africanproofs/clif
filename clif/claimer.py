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
from clif.config import Settings
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
    FAILED_RETRYABLE = "failed-retryable"  # transient — retry next cycle
    FAILED_TERMINAL = "failed-terminal"  # operator action needed — escalate


_OK = {
    OutcomeStatus.NOTHING_CLAIMABLE,
    OutcomeStatus.SUBMITTED_MINED,
    OutcomeStatus.SUBMITTED_PENDING,
}


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
    """Discover (keyless) then submit. The one-shot `clif claim` entry point."""
    try:
        claims = collect_reward_claims(rpc, settings, beneficiary, claim_type, only_epoch)
    except RpcError as exc:
        return ClaimOutcome(
            claim_type=claim_type,
            claim_type_name=ClaimType(claim_type).name,
            beneficiary=beneficiary,
            status=OutcomeStatus.FAILED_RETRYABLE,
            detail=f"discovery rpc failure: {exc}",
        )
    return submit_claims(
        settings, fwd, claim_type, beneficiary, claims,
        wait=wait, wait_timeout=wait_timeout, retry=retry,
    )
