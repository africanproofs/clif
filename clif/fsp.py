"""FSP signing-tool orchestration: Leg-1 (fwd sign-fsp-message) + Leg-2 (fwd sign-transaction).

clif holds zero private keys. Leg-1 calls fwd's /v1/sign-fsp-message using the
SIGN caller token (fsp_permissions block); fwd signs the protocol message
(UPTIME or REWARD_DISTRIBUTION) and returns (message_hash, v, r, s). Leg-2 uses
the SUBMIT caller token (permissions block): fwd signs the tx via
/v1/sign-transaction; clif broadcasts via rpc.py and reports back via
/v1/transactions/{id}/broadcast-result + /v1/transactions/{id}/receipt.

fwd v1.1.0a9+: /v1/sign-and-send is retired. Leg-2 uses the new sign-only
flow (sign-transaction + clif-side broadcast + report-back). The
/v1/transactions/{id} poll is gone (fwd no longer manages broadcast/receipt);
clif uses rpc.py to poll eth_getTransactionReceipt instead.

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

from clif.claimer import OutcomeStatus, _OK, _classify_broadcast_error
from clif.config import ZERO_BYTES32, Settings
from clif.fsp_calldata import build_sign_rewards_calldata, build_sign_uptime_calldata
from clif.fwd_client import (
    FwdClient,
    FwdError,
    FwdRetryableError,
    FwdTerminalError,
    make_fsp_idempotency_key,
)
from clif.merkle import build_reward_merkle_root
from clif.models import SignFspMessageResponse
from clif.reward_data import get_reward_distribution_data
from clif.rpc import RpcClient, RpcError

log = logging.getLogger("clif")

# Known FSP finalization-guard revert strings.  These are benign: the network
# finalized the epoch (>50% signing-weight threshold) before our signature landed.
# REWARD_DISTRIBUTION: confirmed live on Songbird epoch 402 (2026-06-01) by
#   replaying reverted tx 0x097d48c4… via eth_call.
# UPTIME: "uptime vote hash already signed" is the expected string by analogy with
#   the REWARD_DISTRIBUTION path; NOT yet live-confirmed.
#   TODO: confirm the exact uptime revert string against a live revert or the
#   FlareSystemsManager source before treating this as confirmed.
_FSP_FINALIZATION_REVERTS: dict[str, str] = {
    "REWARD_DISTRIBUTION": "rewards hash already signed",
    "UPTIME": "uptime vote hash already signed",  # best-effort — see TODO above
}


def _is_finalization_revert(message_type: str, reason: str | None) -> bool:
    """Return True iff the revert reason matches the known FSP finalization guard.

    Matches the specific known string only — any unmatched revert stays
    FAILED_TERMINAL (safe default: escalate the unknown).
    """
    if reason is None:
        return False
    expected = _FSP_FINALIZATION_REVERTS.get(message_type)
    if expected is None:
        return False
    return expected.lower() in reason.lower()


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
    rpc: RpcClient | None = None,
) -> FspOutcome:
    """Orchestrate keyless UPTIME signing: Leg-1 (SIGN caller) + Leg-2 (SUBMIT caller).

    Leg-1 uses fsp_sign_caller_token (fsp_permissions block in fwd — only
    /v1/sign-fsp-message). Leg-2 uses fsp_submit_caller_token (permissions
    block — /v1/sign-transaction); clif broadcasts via rpc.py and reports back
    via broadcast-result + receipt endpoints. The per-caller split is required
    by the fwd cross-domain policy_path rule (D15 MAJOR-2). The orchestrator
    owns both clients; the CLI no longer builds or passes an FSP FwdClient.
    """
    mt = "UPTIME"
    net = settings.network

    cfg_err = _check_fsp_config(settings, mt, reward_epoch_id)
    if cfg_err is not None:
        return cfg_err

    # Pre-flight finalization skip (mirrors the claim-side unclaimable_reason gate).
    # If the uptime vote for this epoch has already finalized on-chain (the >50%
    # signing-weight threshold was reached), skip leg-1/leg-2 entirely — a doomed
    # attempt would revert on-chain (consuming the nonce + the idempotency key) with
    # no benefit.
    # uptimeVoteHash(epoch) != ZERO_BYTES32 means finalized; == ZERO_BYTES32 = not yet.
    # TODO: verify the analogous FSM revert string for signUptimeVote finalization
    # guard (post-revert classification below uses best-effort "uptime vote hash already
    # signed" — not live-confirmed unlike the REWARD_DISTRIBUTION string).
    if rpc is not None:
        try:
            uv_hash = rpc.uptime_vote_hash(settings.net.flare_systems_manager, reward_epoch_id)
            if uv_hash != ZERO_BYTES32:
                return _out(
                    mt, reward_epoch_id, OutcomeStatus.ALREADY_FINALIZED,
                    f"epoch {reward_epoch_id} uptime already finalized by the network "
                    "(>threshold) — nothing to sign this round",
                )
        except RpcError as exc:
            log.warning(
                "fsp uptime pre-flight finalization check failed (proceeding): %s", exc
            )

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

        # Leg 2: build calldata and sign via fwd /v1/sign-transaction (SUBMIT caller).
        data = build_sign_uptime_calldata(reward_epoch_id, sig.v, sig.r, sig.s)
        leg2_key = make_fsp_idempotency_key(net, mt, reward_epoch_id, "submit", retry_token)
        log.info(
            "fsp leg-2 %s epoch=%s sender=%s to=%s idempotency-key=%s",
            mt, reward_epoch_id, settings.fsp_sender_wallet_name,
            settings.net.flare_systems_manager, leg2_key,
        )

        # FSP submit gas is fixed (config): clif holds wallet NAMES, not addresses,
        # so it cannot eth_estimateGas with a `from`; and estimateGas would revert
        # on an already-signed epoch anyway. Estimate only the fee market
        # (eth_feeHistory needs no `from`).
        gas = settings.fsp_submit_gas
        if rpc is not None:
            try:
                max_fee, max_priority = rpc.suggest_fees()
            except RpcError as exc:
                return _out(
                    mt, reward_epoch_id, OutcomeStatus.FAILED_RETRYABLE,
                    f"fee suggestion rpc failure: {exc}",
                    message_hash=sig.message_hash, leg1_sig=(sig.v, sig.r, sig.s),
                )
        else:
            max_fee = 100_000_000_000  # 100 gwei
            max_priority = 1_000_000_000  # 1 gwei

        try:
            resp = submit_fwd.sign_transaction(
                wallet=settings.fsp_sender_wallet_name,  # type: ignore[arg-type]
                chain=settings.net.chain_id,
                to=settings.net.flare_systems_manager,
                data=data,
                value_wei="0",
                gas=gas,
                max_fee_per_gas=max_fee,
                max_priority_fee_per_gas=max_priority,
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

        return _broadcast_and_confirm(
            submit_fwd, rpc, resp.tx_id, resp.hash, resp.signed_raw_tx,
            mt, reward_epoch_id, sig, wait_timeout,
        )


def run_sign_rewards(
    settings: Settings,
    reward_epoch_id: int,
    *,
    wait: bool = True,
    wait_timeout: float = 600.0,
    retry: str | None = None,
    rpc: RpcClient | None = None,
) -> FspOutcome:
    """Orchestrate keyless REWARD_DISTRIBUTION signing: fetch rdd → Leg-1 (SIGN) + Leg-2 (SUBMIT).

    Leg-1 uses fsp_sign_caller_token; Leg-2 uses fsp_submit_caller_token with
    the new sign-transaction + clif-side broadcast + report-back flow. The
    per-caller split is required by the fwd cross-domain policy_path rule (D15
    MAJOR-2).

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

    # Cryptographic Merkle-root verification: recompute the root from the
    # published claims and assert it equals the file's merkleRoot (= the
    # rewardsHash we are about to send to fwd for signing). A mismatch means
    # the file is internally inconsistent — corrupted, tampered, or from a
    # different epoch family — and we must not sign it (irreversible on-chain).
    if rdd.reward_claims:
        recomputed_root = build_reward_merkle_root(c.body for c in rdd.reward_claims)
        if recomputed_root.lower() != rdd.merkle_root.lower():
            return _out(
                mt, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL,
                f"recomputed merkle root != published merkleRoot — refusing to sign: "
                f"recomputed={recomputed_root} published={rdd.merkle_root}",
            )
        log.info(
            "fsp merkle-root verified epoch=%s recomputed=%s == published=%s",
            reward_epoch_id, recomputed_root, rdd.merkle_root,
        )

    log.info(
        "fsp rdd epoch-bound epoch=%s (file rewardEpochId==signing epoch) "
        "merkle_root=%s n=%s",
        reward_epoch_id, rdd.merkle_root, rdd.no_of_weight_based_claims,
    )

    # Pre-flight finalization skip (mirrors the claim-side unclaimable_reason gate).
    # rewardsHash(epoch) != ZERO_BYTES32 means the >50% signing-weight threshold
    # was already reached and the epoch's rewards are finalized on-chain.  Signing
    # a finalized epoch produces the on-chain "rewards hash already signed" revert,
    # consumes the nonce, and leaves a cached idempotency key — both are avoided by
    # skipping leg-1/leg-2 entirely here.
    if rpc is not None:
        try:
            rh = rpc.rewards_hash(settings.net.flare_systems_manager, reward_epoch_id)
            if rh != ZERO_BYTES32:
                return _out(
                    mt, reward_epoch_id, OutcomeStatus.ALREADY_FINALIZED,
                    f"epoch {reward_epoch_id} rewards already finalized by the network "
                    "(>threshold) — nothing to sign this round",
                )
        except RpcError as exc:
            log.warning(
                "fsp rewards pre-flight finalization check failed (proceeding): %s", exc
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

        # Leg 2 (SUBMIT caller): sign-transaction + clif-side broadcast + report-back.
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

        # FSP submit gas is fixed (config): clif holds wallet NAMES, not addresses,
        # so it cannot eth_estimateGas with a `from`; and estimateGas would revert
        # on an already-signed epoch anyway. Estimate only the fee market
        # (eth_feeHistory needs no `from`).
        gas = settings.fsp_submit_gas
        if rpc is not None:
            try:
                max_fee, max_priority = rpc.suggest_fees()
            except RpcError as exc:
                return _out(
                    mt, reward_epoch_id, OutcomeStatus.FAILED_RETRYABLE,
                    f"fee suggestion rpc failure: {exc}",
                    message_hash=sig.message_hash, leg1_sig=(sig.v, sig.r, sig.s),
                )
        else:
            max_fee = 100_000_000_000  # 100 gwei
            max_priority = 1_000_000_000  # 1 gwei

        try:
            resp = submit_fwd.sign_transaction(
                wallet=settings.fsp_sender_wallet_name,  # type: ignore[arg-type]
                chain=settings.net.chain_id,
                to=settings.net.flare_systems_manager,
                data=data,
                value_wei="0",
                gas=gas,
                max_fee_per_gas=max_fee,
                max_priority_fee_per_gas=max_priority,
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

        return _broadcast_and_confirm(
            submit_fwd, rpc, resp.tx_id, resp.hash, resp.signed_raw_tx,
            mt, reward_epoch_id, sig, wait_timeout,
        )


def _broadcast_and_confirm(
    submit_fwd: FwdClient,
    rpc: RpcClient | None,
    tx_id: str,
    fwd_hash: str,
    signed_raw_tx: str,
    message_type: str,
    reward_epoch_id: int,
    sig: SignFspMessageResponse,
    wait_timeout: float = 600.0,
) -> FspOutcome:
    """Broadcast the fwd-signed FSP tx; report-back + poll receipt; map to FspOutcome.

    fwd signed the tx and returned signed_raw_tx; clif broadcasts via rpc,
    reports the broadcast result to fwd, polls eth_getTransactionReceipt, and
    reports the receipt. If rpc is None (test/no-wait paths), return pending.
    """
    mh = sig.message_hash
    l1s = (sig.v, sig.r, sig.s)

    if rpc is None:
        # No rpc available — can't broadcast.
        return _out(
            message_type, reward_epoch_id, OutcomeStatus.SUBMITTED_PENDING,
            "signed (no rpc — cannot broadcast)",
            message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=fwd_hash,
        )

    # Broadcast the signed blob.
    try:
        broadcast_hash = rpc.send_raw_transaction(signed_raw_tx)
    except RpcError as exc:
        fwd_outcome, err_class = _classify_broadcast_error(exc)
        try:
            submit_fwd.report_broadcast_result(tx_id, fwd_hash, fwd_outcome, err_class)
        except (FwdRetryableError, FwdTerminalError, FwdError):
            pass
        if fwd_outcome == "rejected_nonce_too_low":
            return _out(
                message_type, reward_epoch_id, OutcomeStatus.FAILED_RETRYABLE,
                f"broadcast rejected (nonce too low): {exc}",
                message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=fwd_hash,
            )
        return _out(
            message_type, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL,
            f"broadcast rejected ({err_class}): {exc}",
            message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=fwd_hash,
        )

    # Report accepted broadcast to fwd.
    try:
        submit_fwd.report_broadcast_result(tx_id, broadcast_hash, "accepted")
    except (FwdRetryableError, FwdTerminalError, FwdError) as exc:
        log.warning("fwd broadcast-result report failed (non-fatal): %s", exc)

    log.info("fsp leg-2 broadcasted tx_id=%s hash=%s", tx_id, broadcast_hash)

    # Poll for receipt.
    receipt = rpc.poll_receipt(broadcast_hash, timeout=wait_timeout)
    if receipt is None:
        return _out(
            message_type, reward_epoch_id, OutcomeStatus.SUBMITTED_PENDING,
            "submitted; receipt poll timed out",
            message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=broadcast_hash,
        )

    block_number = int(str(receipt.get("blockNumber", "0x0")), 16)
    status_hex = str(receipt.get("status", "0x0"))
    mined_ok = int(status_hex, 16) == 1

    # Report receipt to fwd.
    receipt_outcome = "mined_success" if mined_ok else "mined_reverted"
    try:
        submit_fwd.report_receipt(tx_id, broadcast_hash, receipt_outcome, block_number)
    except (FwdRetryableError, FwdTerminalError, FwdError) as exc:
        log.warning("fwd receipt report failed (non-fatal): %s", exc)

    if not mined_ok:
        # Attempt to decode the revert reason and classify benign finalization reverts.
        reason = rpc.get_revert_reason(broadcast_hash)
        if _is_finalization_revert(message_type, reason):
            # The network finalized the epoch (>50% threshold reached) before our
            # signature landed — too late this round, not a fault.
            if message_type == "REWARD_DISTRIBUTION":
                detail = (
                    f"epoch {reward_epoch_id} rewards finalized by the network "
                    "(>threshold) before our signature — too late this round (not a fault)"
                )
            else:
                # UPTIME — best-effort string match (see TODO in run_sign_uptime pre-flight).
                detail = (
                    f"epoch {reward_epoch_id} uptime finalized by the network "
                    "(>threshold) before our signature — too late this round (not a fault)"
                )
            return _out(
                message_type, reward_epoch_id, OutcomeStatus.ALREADY_FINALIZED, detail,
                message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=broadcast_hash,
            )
        # Unknown revert or a real fault — escalate with the decoded reason appended.
        return _out(
            message_type, reward_epoch_id, OutcomeStatus.FAILED_TERMINAL,
            f"tx reverted on-chain: {reason or 'unknown'}",
            message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=broadcast_hash,
        )

    return _out(
        message_type, reward_epoch_id, OutcomeStatus.SUBMITTED_MINED, "mined",
        message_hash=mh, leg1_sig=l1s, tx_id=tx_id, tx_hash=broadcast_hash,
    )
