"""Keyless reward discovery — port of the read half of `claimer.ts`.

No key is ever touched here: it is `eth_call` view reads plus the public
fsp-rewards files. The output (`RewardClaimWithProof` list) is exactly what
`calldata.build_claim_calldata` consumes to produce the real bytes fwd will
sign.
"""

from __future__ import annotations

from clif.config import ZERO_BYTES32, Settings
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
    """The `(merkleProof, body)` for this beneficiary+claimType in one epoch."""
    data = get_reward_calculation_data(settings, epoch)
    if data is None:
        return None
    for merkle_proof, (epoch_id, address, amount_str, c_type) in data.reward_claims:
        if address.lower() == beneficiary.lower() and c_type == claim_type:
            return RewardClaimWithProof(
                merkle_proof=merkle_proof,
                body=RewardClaimBody(
                    reward_epoch_id=epoch_id,
                    beneficiary=address,
                    amount=int(amount_str),
                    claim_type=c_type,
                ),
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
