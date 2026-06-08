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

# Initial eth_getLogs chunk — fine for a full/archive node (a few-hour window is a
# handful of requests). If the node caps the range ("maximum is set to N", e.g. the
# public RPC's 30), we auto-detect N and chunk to it for the rest of the scan.
_CHUNK_BLOCKS = 5000
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
    kind: str = "rewards"  # "rewards" | "uptime"
    # The leading candidate message hash (most accumulated weight) — the one
    # heading to / past finalization; "" if no signatures yet.
    message_hash: str = ""
    # complete=False ⇒ the getLogs budget was exhausted before the whole signing
    # window was scanned (small-cap RPC); signed_pct is then a FLOOR. scanned_from
    # is the lowest block actually scanned.
    complete: bool = True
    scanned_from_block: int = 0
    signers: list[SignerEntry] = field(default_factory=list)


def _merge(by_hash: dict, logs: list) -> None:
    # by_hash: message_hash -> {signing_policy_address: threshold_reached}
    for entry in logs:
        by_hash.setdefault(entry.message_hash, {})[entry.signing_policy_address] = (
            entry.threshold_reached
        )


def _scan_window(
    rpc: RpcClient, fsm: str, epoch: int, kind: str, latest: int, epoch_end_ts: float
) -> tuple[dict, int, bool]:
    """Backward chunked getLogs from `latest`, stopping once a chunk predates
    epoch-end (the signing-window start) — block-time-independent. Cap-adaptive;
    budget-bounded (small-cap RPC → complete=False, a recent-window floor).
    Returns (by_hash, scanned_from_block, complete)."""
    target = int(epoch_end_ts)
    by_hash: dict = {}
    chunk = _CHUNK_BLOCKS
    reqs = 0
    hi = latest
    scanned_from = latest + 1
    complete = False
    while hi >= 0 and reqs < _MAX_REQUESTS:
        lo = max(0, hi - chunk + 1)
        try:
            logs = rpc.signed_logs(fsm, epoch, lo, hi, kind=kind)
        except RpcError as exc:
            cap = _parse_block_cap(str(exc))
            if cap and cap < chunk:
                chunk = cap  # node range cap discovered → retry this slice smaller
                continue
            raise
        reqs += 1
        _merge(by_hash, logs)
        scanned_from = lo
        if lo == 0 or rpc.block_timestamp(lo) < target:
            complete = True
            break
        hi = lo - 1
    return by_hash, scanned_from, complete


def _scan_forward(rpc: RpcClient, fsm: str, epoch: int, kind: str, lo: int, hi: int, by_hash: dict) -> None:
    """Chunked getLogs over a bounded NEW range [lo, hi], merging signers into
    `by_hash`. Cap-adaptive; no timestamp stop (the whole range is in-window).
    Used for the incremental refresh — events are append-only, so only blocks
    above the last-scanned high-water mark can contain new signatures."""
    if lo > hi:
        return
    chunk = _CHUNK_BLOCKS
    reqs = 0
    cur = lo
    while cur <= hi and reqs < _MAX_REQUESTS:
        top = min(hi, cur + chunk - 1)
        try:
            logs = rpc.signed_logs(fsm, epoch, cur, top, kind=kind)
        except RpcError as exc:
            cap = _parse_block_cap(str(exc))
            if cap and cap < chunk:
                chunk = cap
                continue
            raise
        reqs += 1
        _merge(by_hash, logs)
        cur = top + 1


def _aggregate(
    epoch: int,
    kind: str,
    by_hash: dict,
    weight_of: dict,
    voter_of: dict,
    total_weight: int,
    ppm: int,
    our_spa: str | None,
    complete: bool,
    scanned_from: int,
) -> SigningProgress:
    """Reduce accumulated signers → SigningProgress. The >threshold finalization is
    per-messageHash, so progress is the LEADING hash's weight (matches the contract
    + the Explorer's per-hash view), not the sum across competing hashes."""
    threshold_weight = math.ceil(total_weight * ppm / 1_000_000)
    threshold_pct = ppm / 1_000_000 * 100

    def _hash_weight(spas: dict) -> int:
        return sum(weight_of[s] for s in spas)

    if by_hash:
        message_hash = max(by_hash, key=lambda h: _hash_weight(by_hash[h]))
        lead = by_hash[message_hash]
    else:
        message_hash, lead = "", {}

    signed_weight = _hash_weight(lead)
    entries = sorted(
        (SignerEntry(spa, voter_of[spa], weight_of[spa]) for spa in lead),
        key=lambda e: e.weight,
        reverse=True,
    )
    signed_pct = (100.0 * signed_weight / total_weight) if total_weight else 0.0
    finalized = any(lead.values()) or signed_weight > threshold_weight
    our = our_spa.lower() if our_spa else None
    return SigningProgress(
        epoch=epoch,
        signed_weight=signed_weight,
        total_weight=total_weight,
        threshold_weight=threshold_weight,
        signed_pct=signed_pct,
        threshold_pct=threshold_pct,
        finalized=finalized,
        our_signed=bool(our and our in lead),
        signer_count=len(lead),
        kind=kind,
        message_hash=message_hash,
        complete=complete,
        scanned_from_block=scanned_from,
        signers=entries,
    )


def _lookup_weights(rpc: RpcClient, vr: str, epoch: int, by_hash: dict, weight_of: dict, voter_of: dict) -> None:
    """Fill weight_of/voter_of for any signer not already cached (weights are
    IMMUTABLE per epoch, so a cached signer is never re-fetched)."""
    seen = {spa for m in by_hash.values() for spa in m}
    for spa in seen - weight_of.keys():
        v, w = rpc.voter_normalised_weight(vr, epoch, spa)
        weight_of[spa] = w
        voter_of[spa] = v.lower()


def compute_signing_progress(
    rpc: RpcClient,
    net: NetworkConfig,
    epoch: int,
    our_spa: str | None,
    *,
    epoch_end_ts: float,
    kind: str = "rewards",
) -> SigningProgress:
    """Aggregate signing-event logs for `epoch` into a weight-weighted progress %.

    Stateless one-shot (used by the `epoch signing-progress` command). `kind`
    selects the event: "rewards" (RewardsSigned) or "uptime" (UptimeVoteSigned) —
    both use the same VoterRegistry normalised weights + signing-policy threshold.
    The daemon uses `refresh_signing_progress` (cached/incremental) instead. Raises
    RpcError on transport / node error; ValueError if no VoterRegistry.
    """
    vr = net.voter_registry
    if not vr:
        raise ValueError(f"VoterRegistry not configured for network {net.name}")
    fsm = net.flare_systems_manager
    latest = rpc.block_number()
    by_hash, scanned_from, complete = _scan_window(rpc, fsm, epoch, kind, latest, epoch_end_ts)
    total_weight = rpc.weights_sums(vr, epoch)[1]
    ppm = rpc.signing_policy_threshold_ppm(fsm)
    weight_of: dict = {}
    voter_of: dict = {}
    _lookup_weights(rpc, vr, epoch, by_hash, weight_of, voter_of)
    return _aggregate(
        epoch, kind, by_hash, weight_of, voter_of, total_weight, ppm, our_spa, complete, scanned_from
    )


def refresh_signing_progress(
    cache: dict,
    rpc: RpcClient,
    net: NetworkConfig,
    epoch: int,
    our_spa: str | None,
    *,
    epoch_end_ts: float,
    kind: str = "rewards",
) -> SigningProgress:
    """Cached/incremental signing-progress for the daemon (called every cycle).

    `cache` is a dict the caller persists across cycles. The immutable per-epoch
    facts (total weight, threshold, per-signer normalised weight) are fetched ONCE
    and reused; each subsequent refresh scans ONLY blocks above the last high-water
    mark (events are append-only) and looks up weights ONLY for newly-seen signers.
    Steady state (no new signers) ≈ 2 calls/cycle vs ~95 for a full scan. Falls back
    to a full scan if the initial backward scan was incomplete (capped-RPC only).
    Raises RpcError on transport / node error; ValueError if no VoterRegistry.
    """
    vr = net.voter_registry
    if not vr:
        raise ValueError(f"VoterRegistry not configured for network {net.name}")
    fsm = net.flare_systems_manager
    key = (net.name, epoch, kind)
    st = cache.get(key)
    latest = rpc.block_number()

    if st is None or not st["base_complete"]:
        by_hash, scanned_from, complete = _scan_window(rpc, fsm, epoch, kind, latest, epoch_end_ts)
        st = {
            "total": rpc.weights_sums(vr, epoch)[1],
            "ppm": rpc.signing_policy_threshold_ppm(fsm),
            "weight_of": {},
            "voter_of": {},
            "by_hash": by_hash,
            "scanned_hi": latest,
            "scanned_from": scanned_from,
            "base_complete": complete,
        }
        cache[key] = st
        # Bound cache growth on a long-running daemon (epochs accrue ~every 3.5d).
        for k in [k for k in cache if k[0] == net.name and k[1] < epoch - 4]:
            del cache[k]
    else:
        # Incremental: only blocks ABOVE the last high-water mark can hold new sigs.
        _scan_forward(rpc, fsm, epoch, kind, st["scanned_hi"] + 1, latest, st["by_hash"])
        st["scanned_hi"] = latest

    _lookup_weights(rpc, vr, epoch, st["by_hash"], st["weight_of"], st["voter_of"])
    return _aggregate(
        epoch, kind, st["by_hash"], st["weight_of"], st["voter_of"],
        st["total"], st["ppm"], our_spa, st["base_complete"], st["scanned_from"],
    )
