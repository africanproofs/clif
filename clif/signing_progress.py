"""Live reward-signing progress for a reward epoch (off-chain aggregation).

The FlareSystemsManager exposes no view getter for *intermediate* reward-signing
progress — `rewardsHash(epoch)` and `getVoterRewardsSignInfo` both revert until
the epoch finalizes (>50% of signing weight). The protocol (and the Flare
Systems Explorer) derive live progress by aggregating the per-signer
`RewardsSigned` event and summing each signer's NORMALISED signing-policy
weight, divided by the epoch's total normalised weight. This module reproduces
that, keyless.

Weights come from VoterRegistry (`getWeightsSums` → denominator,
`getVoterWithNormalisedWeight` → per-signer); the threshold (50%) from the
FlareSystemsManager `signingPolicyThresholdPPM`. Finalization is strictly
`accumulated_weight > threshold_weight` — matches the contract
(`FlareSystemsManager.signRewards`) and `flare-system-client`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from clif.config import NetworkConfig
from clif.rpc import RpcClient, RpcError

# Initial eth_getLogs chunk — fine for a full/archive node (a 30k window is ~6
# requests). If the node caps the range ("maximum is set to N", e.g. the public
# RPC's 30), we auto-detect N and chunk to it for the rest of the scan.
_CHUNK_BLOCKS = 5000
# Over-scan the estimated signing window so we never miss the first signatures.
# Over-scan is harmless: the epoch-id topic filter makes the query idempotent.
_OVERSCAN = 1.5
_MARGIN_BLOCKS = 500
# Reward signing completes within ~2h of epoch end; cap the backward scan well
# above that (~15h at 1.8s) so a long-finalized epoch can't trigger a runaway scan.
_MAX_WINDOW_BLOCKS = 30_000
# Request budget — bounds the scan on a small-cap node (e.g. the public RPC at 30
# blocks/request) so the call can't hang for thousands of requests. On a full node
# the window is covered in a few requests; on a capped node we cover the MOST
# RECENT _MAX_REQUESTS×cap blocks and report complete=False (a floor, not exact).
_MAX_REQUESTS = 240


def _parse_block_cap(msg: str) -> int | None:
    """Extract N from a node's "...maximum is set to N" getLogs range-cap error."""
    m = re.search(r"maximum is set to (\d+)", msg)
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class SignerEntry:
    signing_policy_address: str
    voter: str
    weight: int


@dataclass(frozen=True)
class SigningProgress:
    epoch: int
    signed_weight: int
    total_weight: int
    threshold_weight: int
    signed_pct: float
    threshold_pct: float
    finalized: bool
    our_signed: bool
    signer_count: int
    # The leading candidate rewards hash (most accumulated weight) — the one
    # heading to / past finalization; "" if no signatures yet.
    rewards_hash: str = ""
    # complete=False ⇒ the getLogs budget was exhausted before the whole signing
    # window was scanned (small-cap RPC); signed_pct is then a FLOOR. scanned_from
    # is the lowest block actually scanned.
    complete: bool = True
    scanned_from_block: int = 0
    signers: list[SignerEntry] = field(default_factory=list)


def _scan_from_block(net: NetworkConfig, latest: int, now: float, epoch_end_ts: float) -> int:
    """Earliest block to scan: cover (now − epoch_end) with over-scan + margin, capped.

    Reward signing can only happen after epoch end, so the signing window starts
    at epoch_end. We bias the estimate EARLY (over-scan) to never miss the first
    signatures; the cap bounds a long-finalized epoch.
    """
    seconds_since_end = max(0.0, now - float(epoch_end_ts))
    est = int(seconds_since_end / net.block_time_sec * _OVERSCAN) + _MARGIN_BLOCKS
    window = min(est, _MAX_WINDOW_BLOCKS)
    return max(0, latest - window)


def compute_signing_progress(
    rpc: RpcClient,
    net: NetworkConfig,
    epoch: int,
    our_spa: str | None,
    *,
    now: float,
    epoch_end_ts: float,
) -> SigningProgress:
    """Aggregate `RewardsSigned` logs for `epoch` into a weight-weighted progress %.

    `our_spa` is the caller's FSP signing-policy address (used to set
    `our_signed`); None if unresolved. Raises RpcError on transport / node error;
    raises ValueError if the network has no VoterRegistry configured.
    """
    fsm = net.flare_systems_manager
    vr = net.voter_registry
    if not vr:
        raise ValueError(f"VoterRegistry not configured for network {net.name}")

    latest = rpc.block_number()
    from_block = _scan_from_block(net, latest, now, epoch_end_ts)

    # Chunked getLogs over the signing window, scanning RECENT→older so a budget
    # cutoff (small-cap RPC) still covers the most-relevant recent signatures.
    # Auto-adapt the chunk to the node's range cap on a "too many blocks" error.
    # Group signers by the rewards hash they signed: the >threshold finalization
    # is per-messageHash, so progress is the LEADING hash's weight, not the sum
    # across competing hashes (matches the contract + the Explorer's per-hash view).
    by_hash: dict[str, dict[str, bool]] = {}  # rewards_hash -> {spa: threshold_reached}
    chunk = _CHUNK_BLOCKS
    reqs = 0
    hi = latest
    scanned_from = latest + 1
    while hi >= from_block and reqs < _MAX_REQUESTS:
        lo = max(from_block, hi - chunk + 1)
        try:
            logs = rpc.reward_signed_logs(fsm, epoch, lo, hi)
        except RpcError as exc:
            cap = _parse_block_cap(str(exc))
            if cap and cap < chunk:
                chunk = cap  # node range cap discovered → retry this slice smaller
                continue
            raise
        reqs += 1
        for entry in logs:
            by_hash.setdefault(entry.rewards_hash, {})[entry.signing_policy_address] = (
                entry.threshold_reached
            )
        scanned_from = lo
        hi = lo - 1
    complete = hi < from_block

    total_weight = rpc.weights_sums(vr, epoch)[1]
    ppm = rpc.signing_policy_threshold_ppm(fsm)
    threshold_weight = math.ceil(total_weight * ppm / 1_000_000)
    threshold_pct = ppm / 1_000_000 * 100

    # One weight lookup per distinct signer (across all hashes — usually one hash).
    all_spas = {spa for m in by_hash.values() for spa in m}
    weight_of: dict[str, int] = {}
    voter_of: dict[str, str] = {}
    for spa in all_spas:
        v, w = rpc.voter_normalised_weight(vr, epoch, spa)
        weight_of[spa] = w
        voter_of[spa] = v.lower()

    # Leading hash = greatest accumulated weight (the one finalizing / finalized).
    def _hash_weight(spas: dict[str, bool]) -> int:
        return sum(weight_of[s] for s in spas)

    if by_hash:
        rewards_hash = max(by_hash, key=lambda h: _hash_weight(by_hash[h]))
        lead = by_hash[rewards_hash]
    else:
        rewards_hash, lead = "", {}

    signed_weight = _hash_weight(lead)
    entries = sorted(
        (SignerEntry(spa, voter_of[spa], weight_of[spa]) for spa in lead),
        key=lambda e: e.weight,
        reverse=True,
    )
    signed_pct = (100.0 * signed_weight / total_weight) if total_weight else 0.0
    finalized = any(lead.values()) or signed_weight > threshold_weight
    our = our_spa.lower() if our_spa else None
    our_signed = bool(our and our in lead)

    return SigningProgress(
        epoch=epoch,
        signed_weight=signed_weight,
        total_weight=total_weight,
        threshold_weight=threshold_weight,
        signed_pct=signed_pct,
        threshold_pct=threshold_pct,
        finalized=finalized,
        our_signed=our_signed,
        signer_count=len(lead),
        rewards_hash=rewards_hash,
        complete=complete,
        scanned_from_block=scanned_from,
        signers=entries,
    )
