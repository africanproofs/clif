"""Keyless reward discovery — port of the read half of `claimer.ts`.

No key is ever touched here: it is `eth_call` view reads plus the public
fsp-rewards files. The output (`RewardClaimWithProof` list) is exactly what
`calldata.build_claim_calldata` consumes to produce the real bytes fwd will
sign.
"""

from __future__ import annotations

import sys

from clif.config import ZERO_BYTES32, Settings
from clif.merkle import compute_leaf, verify_proof
from clif.models import RewardClaimBody, RewardClaimWithProof
from clif.reward_data import get_reward_calculation_data
from clif.rpc import RpcClient


def claimable_epoch_ids(rpc: RpcClient, settings: Settings, beneficiary: str) -> list[int]:
    """Epochs in [next_claimable, end] whose rewards hash has been signed."""
    net = settings.net
    start = rpc.next_claimable_reward_epoch_id(net.reward_manager, beneficiary)
    _, end = rpc.reward_epoch_id_range(net.reward_manager)
    if end < start:
        return []
    ids: list[int] = []
    for epoch in range(start, end + 1):
        if rpc.rewards_hash(net.flare_systems_manager, epoch) != ZERO_BYTES32:
            ids.append(epoch)
    return ids


def reward_claim_for(
    settings: Settings, epoch: int, beneficiary: str, claim_type: int
) -> RewardClaimWithProof | None:
    """The `(merkleProof, body)` for this beneficiary+claimType in one epoch.

    Returns ``None`` if the data file is unavailable, the beneficiary is not
    found, or the claim's Merkle proof fails to verify against the file's
    published ``merkleRoot``.  A failed proof is logged to stderr and refused —
    submitting an unverifiable proof wastes gas and is chain-rejected.
    """
    data = get_reward_calculation_data(settings, epoch)
    if data is None:
        return None
    for merkle_proof, (epoch_id, address, amount_str, c_type) in data.reward_claims:
        if address.lower() == beneficiary.lower() and c_type == claim_type:
            body = RewardClaimBody(
                reward_epoch_id=epoch_id,
                beneficiary=address,
                amount=int(amount_str),
                claim_type=c_type,
            )
            leaf = compute_leaf(body.reward_epoch_id, body.beneficiary, body.amount, body.claim_type)
            if not verify_proof(leaf, merkle_proof, data.merkle_root):
                print(
                    f"discovery: proof verification FAILED for epoch={epoch} "
                    f"beneficiary={beneficiary} claimType={claim_type} — "
                    "refusing: proof does not verify against published merkleRoot "
                    "(corrupted data file or wrong epoch)",
                    file=sys.stderr,
                )
                return None
            return RewardClaimWithProof(
                merkle_proof=merkle_proof,
                body=body,
            )
    return None


def collect_reward_claims(
    rpc: RpcClient,
    settings: Settings,
    beneficiary: str,
    claim_type: int,
    only_epoch: int | None = None,
) -> list[RewardClaimWithProof]:
    """All claimable `(proof, body)` structs for a beneficiary+claimType.

    `only_epoch` restricts to a single epoch (the `claim -e N` path); it is
    still range-checked against the on-chain claimable window.
    """
    if only_epoch is not None:
        # Gate the single-epoch path against the SAME claimable window the auto
        # path uses (claimable_epoch_ids): refuse an already-claimed, out-of-range
        # or not-yet-signed epoch, so we never build a silent no-op claim.
        # (next_claimable advances past an epoch once it is claimed.)
        start = rpc.next_claimable_reward_epoch_id(settings.net.reward_manager, beneficiary)
        _, end = rpc.reward_epoch_id_range(settings.net.reward_manager)
        if not (start <= only_epoch <= end):
            return []
        if rpc.rewards_hash(settings.net.flare_systems_manager, only_epoch) == ZERO_BYTES32:
            return []
        epochs = [only_epoch]
    else:
        epochs = claimable_epoch_ids(rpc, settings, beneficiary)
    out: list[RewardClaimWithProof] = []
    for epoch in epochs:
        rc = reward_claim_for(settings, epoch, beneficiary, claim_type)
        if rc is not None:
            out.append(rc)
    return out


def _reason_from_state(epoch: int, beneficiary: str, nxt: int, end: int, signed: bool) -> str | None:
    """Why `epoch` fails the on-chain claimability gates, or None if it passes them.

    Pure: takes the already-fetched chain reads. None means epoch is in the
    finalized, signed, not-yet-claimed window — i.e. claimable iff the
    beneficiary has an entry in the published merkle tree (a merkle lookup,
    not an on-chain gate). The three returned strings are the canonical
    reasons the `clif claim -e` pre-flight has always used; keep them stable
    (tests assert the substrings 'already claimed' / 'not in claimable range'
    / 'not yet signed').
    """
    if epoch < nxt:
        return f"already claimed for {beneficiary} (next claimable = {nxt})"
    if epoch > end:
        return f"not in claimable range (max signed-rewards epoch = {end})"
    if not signed:
        return "rewards not yet signed (>50% claim gate not reached)"
    return None


def unclaimable_reason(
    rpc: RpcClient, settings: Settings, beneficiary: str, epoch: int
) -> str | None:
    """Return why `epoch` is NOT claimable for `beneficiary`, or None if it passes
    the on-chain gates (→ claimable iff the beneficiary is in the merkle tree).

    Read-only: reuses the exact on-chain views the discovery window already uses
    (`getNextClaimableRewardEpochId`, `getRewardEpochIdsWithClaimableRewards`,
    `rewardsHash`). No new RPC. Distinguishes the DONE state (already claimed)
    from the PENDING states (not finalized / not signed) — the whole point.
    """
    net = settings.net
    nxt = rpc.next_claimable_reward_epoch_id(net.reward_manager, beneficiary)
    _, end = rpc.reward_epoch_id_range(net.reward_manager)
    signed = epoch <= end and rpc.rewards_hash(net.flare_systems_manager, epoch) != ZERO_BYTES32
    return _reason_from_state(epoch, beneficiary, nxt, end, signed)


def classify_claim_frontier(
    rpc: RpcClient, settings: Settings, beneficiary: str, claim_type: int
) -> list[tuple[int, str]]:
    """Per-epoch claim state across the frontier, for the empty-discovery report.

    Returns `[(epoch, reason), ...]` for the handful of epochs around the
    claim frontier — the last-claimed (`next_claimable-1`), the next claimable
    (`next_claimable`), the latest finalized (`end`) and the next-to-finalize
    (`end+1`). Each reason distinguishes already-claimed / not-finalized /
    not-signed / no-accrual / claimable, so `list`/`auto`/`claim` never report a
    bare 'nothing-claimable' that conflates DONE with PENDING. Bounded to ≤4
    epochs; reuses existing reads only.
    """
    net = settings.net
    nxt = rpc.next_claimable_reward_epoch_id(net.reward_manager, beneficiary)
    start, end = rpc.reward_epoch_id_range(net.reward_manager)
    candidates = sorted({e for e in (nxt - 1, nxt, end, end + 1) if e >= max(start, 0)})
    out: list[tuple[int, str]] = []
    for epoch in candidates:
        signed = epoch <= end and rpc.rewards_hash(net.flare_systems_manager, epoch) != ZERO_BYTES32
        reason = _reason_from_state(epoch, beneficiary, nxt, end, signed)
        if reason is None:
            # Gates pass → claimable iff in the merkle tree. A miss here (the
            # epoch is finalized + signed but the beneficiary is absent) is a
            # genuine no-accrual, not a pending state.
            rc = reward_claim_for(settings, epoch, beneficiary, claim_type)
            reason = (
                f"claimable: {rc.body.amount} wei"
                if rc is not None
                else "no rewards accrued for this beneficiary"
            )
        out.append((epoch, reason))
    return out
