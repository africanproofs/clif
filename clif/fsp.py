"""FSP signing-tool orchestration: Leg-1 (fwd sign-fsp-message) + Leg-2 (fwd sign-and-send).

clif holds zero private keys. Leg-1 calls fwd's /v1/sign-fsp-message using the
SIGN caller token (fsp_permissions block); fwd signs the protocol message
(UPTIME or REWARD_DISTRIBUTION) and returns (message_hash, v, r, s). Leg-2 uses
the SUBMIT caller token (permissions block) and the existing sign_and_send to
broadcast the resulting signUptimeVote / signRewards calldata to
FlareSystemsManager. The tx poll (/v1/transactions/{id}) is per-caller-scoped in
fwd and MUST use the Leg-2 submit caller.

fwd cross-domain rule: the same policy_path key cannot appear in both
`permissions` and `fsp_permissions` (fwd fail-fast boot). One caller → one
block. So the orchestrator owns two distinct FwdClient instances — one per leg.

For REWARD_DISTRIBUTION, clif first fetches reward-distribution-data.json to
verify the rewardsHash it is about to request signing for. If unavailable,
clif STOPS — never signs an unverified rewardsHash. The file's rewardEpochId
is asserted == the signing epoch before Leg-1 (MAJOR-1 epoch-bind, D15).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from clif.claimer import OutcomeStatus, _OK
from clif.config import Settings
from clif.fsp_calldata import build_sign_rewards_calldata, build_sign_uptime_calldata
from clif.fwd_client import (
    FwdClient,
    FwdError,
    FwdRetryableError,
    FwdTerminalError,
    make_fsp_idempotency_key,
)
from clif.models import SignFspMessageResponse
from clif.reward_data import get_reward_distribution_data

log = logging.getLogger("clif")

# fwd cross-domain rule (D15 MAJOR-2): the same policy_path key cannot appear
# in both `permissions` and `fsp_permissions`. Operator provisions TWO callers:
#   clif-fsp-sign  → fsp_permissions  (authorizes /v1/sign-fsp-message, Leg-1)
#   clif-fsp-submit→ permissions      (authorizes /v1/sign-and-send to
#                                      FlareSystemsManager, Leg-2 + tx poll)
# clif never authors fwd policy nor mints credentials.
_OPERATOR_PROVISION_HINT = (
    "operator must provision TWO fwd FSP callers (fwd cross-domain policy_path "
    "rule: one caller cannot span both fsp_permissions and permissions): "
    "clif-fsp-sign → fsp_permissions (Leg-1 /v1/sign-fsp-message); "
    "clif-fsp-submit → permissions for FlareSystemsManager (Leg-2 + tx poll). "
    "Also: FSP wallets, FlareSystemsManager ABI+policy — clif never authors fwd policy"
)


@dataclass
class FspOutcome:
    message_type: str
    reward_epoch_id: int
    status: OutcomeStatus
    detail: str
    message_hash: str | None = None
    leg1_sig: tuple[int, str, str] | None = None
    tx_id: str | None = None
    tx_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in _OK


def _out(
    message_type: str,
    reward_epoch_id: int,
    status: OutcomeStatus,
    detail: str,
    **kw: object,
) -> FspOutcome:
    return FspOutcome(
        message_type=message_type,
        reward_epoch_id=reward_epoch_id,
        status=status,
        detail=detail,
        **kw,  # type: ignore[arg-type]
    )


def _check_fsp_config(settings: Settings, message_type: str, epoch: int) -> FspOutcome | None:
    """Return a FAILED_TERMINAL outcome if required FSP config is missing, else None."""
    missing = [
        n
        for n, v in (
            ("FSP_SIGN_CALLER_TOKEN", settings.fsp_sign_caller_token),
            ("FSP_SUBMIT_CALLER_TOKEN", settings.fsp_submit_caller_token),
            ("FSP_SIGNING_WALLET_NAME", settings.fsp_signing_wallet_name),
            ("FSP_SENDER_WALLET_NAME", settings.fsp_sender_wallet_name),
        )
        if not v
    ]
    if missing:
        return _out(
            message_type, epoch,
            OutcomeStatus.FAILED_TERMINAL,
            f"missing FSP config: {', '.join(missing)} — {_OPERATOR_PROVISION_HINT}",
        )
    return None


def run_sign_uptime(
    settings: Settings,
    reward_epoch_id: int,
    *,
    wait: bool = True,
    wait_timeout: float = 600.0,
    retry: str | None = None,
) -> FspOutcome:
    """Orchestrate keyless UPTIME signing: Leg-1 (SIGN caller) + Leg-2 (SUBMIT caller).

    Leg-1 uses fsp_sign_caller_token (fsp_permissions block in fwd — only
    /v1/sign-fsp-message). Leg-2 and the tx poll use fsp_submit_caller_token
    (permissions block — /v1/sign-and-send + /v1/transactions/{id}). The
    per-caller-scoped tx poll mandates the split (fwd cross-domain rule, D15
    MAJOR-2). The orchestrator owns both clients; the CLI no longer builds or
    passes an FSP FwdClient.
    """
    mt = "UPTIME"
    net = settings.network

    cfg_err = _check_fsp_config(settings, mt, reward_epoch_id)
    if cfg_err is not None:
        return cfg_err

    retry_token = retry if retry is not None else settings.fsp_idempotency_retry

    with (
        FwdClient(settings.fwd_endpoint, settings.fsp_sign_caller_token) as sign_fwd,
        FwdClient(settings.fwd_endpoint, settings.fsp_submit_caller_token) as submit_fwd,
    ):
        # Leg 1: request the FSP message signature from fwd (SIGN caller).
        leg1_key = make_fsp_idempotency_key(net, mt, reward_epoch_id, "sign", retry_token)
        log.info(
            "fsp leg-1 %s epoch=%s wallet=%s idempotency-key=%s",
            mt, reward_epoch_id, settings.fsp_signing_wallet_name, leg1_key,
        )
        try:
            sig = sign_fwd.sign_fsp_message(
                wallet=settings.fsp_signing_wallet_name,  # type: ignore[arg-type]
                message_type=mt,
                reward_epoch_id=reward_epoch_id,
                idempotency_key=leg1_key,
            )
        except FwdTerminalError as exc:
            hint = f" — {_OPERATOR_PROVISION_HINT}" if exc.status in (403, 404) else ""
            return _out(mt, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL, f"fwd leg-1 denied: {exc}{hint}")
        except FwdRetryableError as exc:
            return _out(mt, reward_epoch_id, OutcomeStatus.FAILED_RETRYABLE, f"fwd leg-1 transient: {exc}")

        log.info(
            "fsp leg-1 OK %s epoch=%s message_hash=%s v=%s",
            mt, reward_epoch_id, sig.message_hash, sig.v,
        )

        # Leg 2: build calldata and broadcast via fwd sign-and-send (SUBMIT caller).
        data = build_sign_uptime_calldata(reward_epoch_id, sig.v, sig.r, sig.s)
        leg2_key = make_fsp_idempotency_key(net, mt, reward_epoch_id, "submit", retry_token)
        log.info(
            "fsp leg-2 %s epoch=%s sender=%s to=%s idempotency-key=%s",
            mt, reward_epoch_id, settings.fsp_sender_wallet_name,
            settings.net.flare_systems_manager, leg2_key,
        )

        try:
            resp = submit_fwd.sign_and_send(
                wallet=settings.fsp_sender_wallet_name,  # type: ignore[arg-type]
                chain=settings.net.chain_id,
                to=settings.net.flare_systems_manager,
                data=data,
                value_wei="0",
                gas=settings.fsp_submit_gas,
                idempotency_key=leg2_key,
            )
        except FwdTerminalError as exc:
            hint = f" — {_OPERATOR_PROVISION_HINT}" if exc.status in (403, 404) else ""
            return _out(
                mt, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL,
                f"fwd leg-2 denied: {exc}{hint}",
                message_hash=sig.message_hash,
                leg1_sig=(sig.v, sig.r, sig.s),
            )
        except FwdRetryableError as exc:
            return _out(
                mt, reward_epoch_id, OutcomeStatus.FAILED_RETRYABLE,
                f"fwd leg-2 transient: {exc}",
                message_hash=sig.message_hash,
                leg1_sig=(sig.v, sig.r, sig.s),
            )

        if not wait:
            return _out(
                mt, reward_epoch_id, OutcomeStatus.SUBMITTED_PENDING, "submitted (no wait)",
                message_hash=sig.message_hash, leg1_sig=(sig.v, sig.r, sig.s),
                tx_id=resp.tx_id, tx_hash=resp.hash,
            )

        return _wait_for_tx(submit_fwd, resp.tx_id, resp.hash, mt, reward_epoch_id, sig, wait_timeout)


def run_sign_rewards(
    settings: Settings,
    reward_epoch_id: int,
    *,
    wait: bool = True,
    wait_timeout: float = 600.0,
    retry: str | None = None,
) -> FspOutcome:
    """Orchestrate keyless REWARD_DISTRIBUTION signing: fetch rdd → Leg-1 (SIGN) + Leg-2 (SUBMIT).

    Leg-1 uses fsp_sign_caller_token; Leg-2 and the tx poll use
    fsp_submit_caller_token. The per-caller-scoped tx poll mandates the split
    (fwd cross-domain rule, D15 MAJOR-2).

    The file's rewardEpochId is asserted == reward_epoch_id BEFORE Leg-1
    (D15 MAJOR-1 epoch-bind). A mismatch is FAILED_TERMINAL with no sign call —
    a stale cache / wrong operator file / wrong-epoch payload is strictly worse
    than no signature (irreversible on-chain once submitted).
    """
    mt = "REWARD_DISTRIBUTION"
    net = settings.network

    cfg_err = _check_fsp_config(settings, mt, reward_epoch_id)
    if cfg_err is not None:
        return cfg_err

    # Guard: fetch and validate reward-distribution-data BEFORE requesting signing.
    # Never sign an unverified rewardsHash.
    rdd = get_reward_distribution_data(settings, reward_epoch_id)
    if rdd is None:
        return _out(
            mt, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL,
            "reward-distribution-data unavailable — refusing to sign unverified rewardsHash",
        )

    # MAJOR-1 epoch-bind (D15): assert the file's rewardEpochId == signing epoch.
    # Mirrors upstream signing-tool@838b87f getRewardsData() which throws on mismatch.
    if rdd.reward_epoch_id != reward_epoch_id:
        return _out(
            mt, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL,
            f"reward-distribution-data epoch mismatch: file rewardEpochId="
            f"{rdd.reward_epoch_id} != signing epoch {reward_epoch_id} — "
            "refusing to sign a rewardsHash bound to a different epoch "
            "(stale cache / wrong operator file / wrong-epoch payload)",
        )

    log.info(
        "fsp rdd epoch-bound epoch=%s (file rewardEpochId==signing epoch) "
        "merkle_root=%s n=%s",
        reward_epoch_id, rdd.merkle_root, rdd.no_of_weight_based_claims,
    )

    retry_token = retry if retry is not None else settings.fsp_idempotency_retry

    with (
        FwdClient(settings.fwd_endpoint, settings.fsp_sign_caller_token) as sign_fwd,
        FwdClient(settings.fwd_endpoint, settings.fsp_submit_caller_token) as submit_fwd,
    ):
        leg1_key = make_fsp_idempotency_key(net, mt, reward_epoch_id, "sign", retry_token)
        log.info(
            "fsp leg-1 %s epoch=%s wallet=%s idempotency-key=%s",
            mt, reward_epoch_id, settings.fsp_signing_wallet_name, leg1_key,
        )
        try:
            sig = sign_fwd.sign_fsp_message(
                wallet=settings.fsp_signing_wallet_name,  # type: ignore[arg-type]
                message_type=mt,
                reward_epoch_id=reward_epoch_id,
                chain_id=settings.net.chain_id,
                no_of_weight_based_claims=rdd.no_of_weight_based_claims,
                rewards_hash=rdd.merkle_root,
                idempotency_key=leg1_key,
            )
        except FwdTerminalError as exc:
            hint = f" — {_OPERATOR_PROVISION_HINT}" if exc.status in (403, 404) else ""
            return _out(mt, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL, f"fwd leg-1 denied: {exc}{hint}")
        except FwdRetryableError as exc:
            return _out(mt, reward_epoch_id, OutcomeStatus.FAILED_RETRYABLE, f"fwd leg-1 transient: {exc}")

        log.info(
            "fsp leg-1 OK %s epoch=%s message_hash=%s v=%s",
            mt, reward_epoch_id, sig.message_hash, sig.v,
        )

        # Leg 2 (SUBMIT caller).
        data = build_sign_rewards_calldata(
            reward_epoch_id,
            settings.net.chain_id,
            rdd.no_of_weight_based_claims,
            rdd.merkle_root,
            sig.v, sig.r, sig.s,
        )
        leg2_key = make_fsp_idempotency_key(net, mt, reward_epoch_id, "submit", retry_token)
        log.info(
            "fsp leg-2 %s epoch=%s sender=%s to=%s idempotency-key=%s",
            mt, reward_epoch_id, settings.fsp_sender_wallet_name,
            settings.net.flare_systems_manager, leg2_key,
        )

        try:
            resp = submit_fwd.sign_and_send(
                wallet=settings.fsp_sender_wallet_name,  # type: ignore[arg-type]
                chain=settings.net.chain_id,
                to=settings.net.flare_systems_manager,
                data=data,
                value_wei="0",
                gas=settings.fsp_submit_gas,
                idempotency_key=leg2_key,
            )
        except FwdTerminalError as exc:
            hint = f" — {_OPERATOR_PROVISION_HINT}" if exc.status in (403, 404) else ""
            return _out(
                mt, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL,
                f"fwd leg-2 denied: {exc}{hint}",
                message_hash=sig.message_hash,
                leg1_sig=(sig.v, sig.r, sig.s),
            )
        except FwdRetryableError as exc:
            return _out(
                mt, reward_epoch_id, OutcomeStatus.FAILED_RETRYABLE,
                f"fwd leg-2 transient: {exc}",
                message_hash=sig.message_hash,
                leg1_sig=(sig.v, sig.r, sig.s),
            )

        if not wait:
            return _out(
                mt, reward_epoch_id, OutcomeStatus.SUBMITTED_PENDING, "submitted (no wait)",
                message_hash=sig.message_hash, leg1_sig=(sig.v, sig.r, sig.s),
                tx_id=resp.tx_id, tx_hash=resp.hash,
            )

        return _wait_for_tx(submit_fwd, resp.tx_id, resp.hash, mt, reward_epoch_id, sig, wait_timeout)


def _wait_for_tx(
    submit_fwd: FwdClient,
    tx_id: str,
    tx_hash: str,
    message_type: str,
    reward_epoch_id: int,
    sig: SignFspMessageResponse,
    wait_timeout: float = 600.0,
) -> FspOutcome:
    """Poll fwd until the tx is terminal; map to FspOutcome.

    Polls via the SUBMIT caller `submit_fwd` (fwd's /v1/transactions/{id} is
    per-caller-scoped — the submit and the poll must share one caller). The
    orchestrator (run_sign_uptime / run_sign_rewards) passes submit_fwd here;
    the SIGN caller is closed at this point (its context block has exited).
    Leg-1=SIGN caller; Leg-2 + poll=SUBMIT caller. The per-caller-scoped poll
    mandates the split (fwd cross-domain rule, D15 MAJOR-2).
    """
    mh = sig.message_hash
    l1s = (sig.v, sig.r, sig.s)
    try:
        st = submit_fwd.wait_until_mined(tx_id, timeout=wait_timeout)
    except (FwdRetryableError, TimeoutError) as exc:
        return _out(
            message_type, reward_epoch_id, OutcomeStatus.SUBMITTED_PENDING,
            f"submitted; not yet mined: {exc}",
            message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=tx_hash,
        )
    except FwdError as exc:
        return _out(
            message_type, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL,
            f"submitted; status poll terminal: {exc}",
            message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=tx_hash,
        )

    if st.status == "mined":
        return _out(
            message_type, reward_epoch_id, OutcomeStatus.SUBMITTED_MINED, "mined",
            message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=tx_hash,
        )
    if st.status == "replaced":
        return _out(
            message_type, reward_epoch_id, OutcomeStatus.SUBMITTED_PENDING,
            "fwd replacing (gas bump) — still pending",
            message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=tx_hash,
        )
    return _out(
        message_type, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL,
        f"tx terminal on-chain status={st.status}",
        message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=tx_hash,
    )
