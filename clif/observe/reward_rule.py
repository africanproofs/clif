"""FTSO v2 primary/secondary reward-band rule — ported from ftso/ml/aptrainer-prices.

Reproduces the ACTUAL on-chain reward eligibility (`calculateMedianRewardClaims`):

    iqr_hit (PRIMARY/inner) = (Q1 < v < Q3)
        OR ((v == Q1 OR v == Q3) AND random_select(feedId, votingRoundId, voter))
    pct_hit (SECONDARY/outer) = (M - band < v < M + band),
        band = (|M| * secondaryBandWidthPPM) // 1_000_000

All comparisons are in raw integer TICK space (value * 10**decimals) — float epsilon
breaks the exact-equality boundary check the IQR rule hinges on.

The pure functions (`classify_bands`, `classify`, `random_select`, `to_raw`) are ported
VERBATIM from the aptrainer `reward_rule.py` (verified against the reference TypeScript
2026-07-25). The RPC-dependent offer-param fetch is adapted to clif's keyless RpcClient.
`secondaryBandWidthPPM` + the feed list + per-feed decimals come from the per-epoch
`InflationRewardsOffered` event (emitted early in epoch N-1).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from clif._keccak import keccak256
from clif.rpc import RpcClient, RpcError

VRS_PER_REWARD_EPOCH = 3360

# FtsoRewardOffersManager per chain (aptrainer reward_rule.py; Songbird resolved live +
# pinned). Fallback to a live registry lookup if unset for a network.
FTSO_REWARD_OFFERS_MANAGER = {
    "flare": "0x244EA7f173895968128D5847Df2C75B1460ac685",
    "songbird": "0x5ab9cb258a342001c4663d9526a1c54cccf8c545",
}
_EPOCH_ZERO_START = {"flare": 1658430000, "songbird": 1658429955}
TOPIC0_INFLATION_REWARDS_OFFERED = (
    "0x01070f0e535c0d3077d9ca64b3122869a1897ff5c7711ba33b4db1fb9fd70cfc"
)


# --- pure rule (ported verbatim) ------------------------------------------------


def reward_epoch_id_for_vr(voting_round: int) -> int:
    return voting_round // VRS_PER_REWARD_EPOCH


def to_raw(value: str | float | Decimal, decimals: int) -> int:
    """Exact decimal-string/float → integer tick-space (Decimal + ROUND_HALF_UP)."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int(d.scaleb(decimals).to_integral_value(rounding=ROUND_HALF_UP))


def random_select(feed: str, voting_round_id: int, voter_address: str) -> bool:
    """keccak256(abi.encode(bytes feedId, uint256 votingRoundId, address voter)) % 2 == 1."""
    name_for_encoding = feed.replace("/", "_").encode("ascii")
    padded = name_for_encoding.ljust(20, b"\x00")
    feed_id_bytes = b"\x01" + padded  # category 1 = Crypto
    encoded = abi_encode(
        ["bytes", "uint256", "address"], [feed_id_bytes, voting_round_id, voter_address]
    )
    return (int.from_bytes(keccak256(encoded), "big") % 2) == 1


class BandClass:
    INSIDE = "inside"  # strictly Q1 < v < Q3 → always a primary hit
    BOUNDARY = "boundary"  # v == Q1 or v == Q3 → ~50% expected hit (coin flip)
    OUTSIDE = "outside"  # not in [Q1,Q3] → never a hit


def classify_bands(
    *, value_raw: int, q1_raw: int, q3_raw: int, median_raw: int, secondary_band_width_ppm: int
) -> dict:
    """Deterministic band classification (no voter/coin-flip). `expected_primary =
    inside + 0.5*boundary` is the closed-form expected primary-reward rate."""
    if q1_raw < value_raw < q3_raw:
        band_class = BandClass.INSIDE
    elif value_raw == q1_raw or value_raw == q3_raw:
        band_class = BandClass.BOUNDARY
    else:
        band_class = BandClass.OUTSIDE
    band = (abs(median_raw) * secondary_band_width_ppm) // 1_000_000
    pct_hit = (median_raw - band) < value_raw < (median_raw + band)
    return {"band_class": band_class, "band_ticks": q3_raw - q1_raw, "pct_hit": pct_hit}


# --- offer params (adapted to clif RpcClient) -----------------------------------


@dataclass
class OfferParams:
    reward_epoch_id: int
    network: str
    block: int
    primary_band_reward_share_ppm: int
    min_rewarded_turnout_bips: int
    feeds: list[str]  # feed order (== the submit2 .values order)
    decimals: dict[str, int]  # per-feed decimals (signed int8)
    secondary_band_width_ppm: dict[str, int]


def _decode_feed_name(chunk21: bytes) -> str:
    return chunk21[1:].rstrip(b"\x00").decode("ascii").replace("_", "/")


def _block_ts_retry(rpc: RpcClient, n: int, *, tries: int = 4) -> int:
    """block_timestamp with a short retry — an archive-node probe over old blocks can blip
    ('not found' / conn reset) under load; a transient miss shouldn't abort the whole scan."""
    last: RpcError | None = None
    for _ in range(tries):
        try:
            return rpc.block_timestamp(n)
        except RpcError as exc:
            last = exc
            time.sleep(1.0)
    raise last  # type: ignore[misc]


def _find_block_for_ts(rpc: RpcClient, target_ts: int) -> int:
    """First SERVABLE block with timestamp >= target_ts (binary search).

    Robust against a PRUNED node: our targets are always recent (an epoch-N−1 start, days
    back), so any block the node no longer serves is necessarily OLDER than the target — a
    'not found' probe therefore means 'search higher', converging to the first retained block
    ≥ target without a magic retention floor. Genuine transient blips are absorbed by
    `_block_ts_retry`; only a persistent 'not found' is treated as pruned.
    """
    hi = rpc.block_number()
    lo = 1
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            ts = _block_ts_retry(rpc, mid)
        except RpcError as exc:
            if "not found" in str(exc):
                lo = mid + 1  # pruned ⇒ older than our recent target ⇒ go higher
                continue
            raise
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _offers_manager(rpc: RpcClient, network: str) -> str:
    addr = FTSO_REWARD_OFFERS_MANAGER.get(network)
    if addr:
        return addr
    resolved = rpc.contract_address_by_name("FtsoRewardOffersManager")
    if not resolved or int(resolved, 16) == 0:
        raise RpcError(f"could not resolve FtsoRewardOffersManager for {network}")
    return resolved


def scan_offer_event(
    rpc: RpcClient, network: str, reward_epoch_id: int, *, max_blocks: int = 20_000, chunk: int = 30
) -> OfferParams:
    """Scan forward from the start of epoch N-1 for InflationRewardsOffered(epoch=N) —
    offers for epoch N are announced early in epoch N-1. Decodes feed ids + decimals +
    secondaryBandWidthPPM per feed + primaryBandRewardSharePPM."""
    addr = _offers_manager(rpc, network)
    epoch0 = _EPOCH_ZERO_START[network]
    prior_start_ts = epoch0 + (reward_epoch_id - 1) * VRS_PER_REWARD_EPOCH * 90
    start_block = _find_block_for_ts(rpc, prior_start_ts)
    topic_epoch = "0x" + reward_epoch_id.to_bytes(32, "big").hex()
    n = start_block
    end_block = start_block + max_blocks
    while n <= end_block:
        chunk_end = min(n + chunk - 1, end_block)
        try:
            logs = rpc.get_logs(addr, [TOPIC0_INFLATION_REWARDS_OFFERED, topic_epoch], n, chunk_end)
        except RpcError:
            n = chunk_end + 1
            continue
        if logs:
            data_bytes = bytes.fromhex(logs[0]["data"][2:])
            (
                feed_ids_bytes, decimals_bytes, _amount, min_turnout_bips,
                primary_band_share_ppm, secondary_ppms_bytes, _mode,
            ) = abi_decode(
                ["bytes", "bytes", "uint256", "uint16", "uint24", "bytes", "uint16"], data_bytes
            )
            n_feeds = len(feed_ids_bytes) // 21
            feeds: list[str] = []
            decimals: dict[str, int] = {}
            secondary: dict[str, int] = {}
            for i in range(n_feeds):
                name = _decode_feed_name(feed_ids_bytes[i * 21 : (i + 1) * 21])
                feeds.append(name)
                decimals[name] = int.from_bytes(decimals_bytes[i : i + 1], "big", signed=True)
                secondary[name] = int.from_bytes(secondary_ppms_bytes[i * 3 : (i + 1) * 3], "big")
            return OfferParams(
                reward_epoch_id=reward_epoch_id, network=network,
                block=int(logs[0]["blockNumber"], 16),
                primary_band_reward_share_ppm=int(primary_band_share_ppm),
                min_rewarded_turnout_bips=int(min_turnout_bips),
                feeds=feeds, decimals=decimals, secondary_band_width_ppm=secondary,
            )
        n = chunk_end + 1
    raise RpcError(
        f"no InflationRewardsOffered(epoch={reward_epoch_id}) for {network} within "
        f"{max_blocks} blocks of epoch {reward_epoch_id - 1} start (block {start_block})"
    )


def get_offer_params(
    rpc: RpcClient, network: str, reward_epoch_id: int, *, cache_dir: str | None = None
) -> OfferParams:
    """Per-(network, epoch) offer params with an optional disk cache (survives restarts)."""
    path = None
    if cache_dir:
        path = os.path.join(cache_dir, f"offer-params-{network}-{reward_epoch_id}.json")
        try:
            with open(path) as f:
                d = json.load(f)
            return OfferParams(**d)
        except (OSError, ValueError, TypeError):
            pass
    params = scan_offer_event(rpc, network, reward_epoch_id)
    if path:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "w") as f:
                json.dump(params.__dict__, f)
        except OSError:
            pass
    return params


def prune_offer_cache(cache_dir: str | None, network: str, keep_epoch: int) -> None:
    """Delete `offer-params-<network>-<epoch>.json` files older than `keep_epoch - 1` (keep the
    current + previous reward epoch). Best-effort — the cache is tiny, this just bounds the count
    over indefinite runtime. Network names have no '-', so the epoch is the last '-' field."""
    if not cache_dir:
        return
    prefix = f"offer-params-{network}-"
    try:
        for name in os.listdir(cache_dir):
            if not (name.startswith(prefix) and name.endswith(".json")):
                continue
            try:
                epoch = int(name[len(prefix):-len(".json")])
            except ValueError:
                continue
            if epoch < keep_epoch - 1:
                try:
                    os.remove(os.path.join(cache_dir, name))
                except OSError:
                    pass
    except OSError:
        pass
