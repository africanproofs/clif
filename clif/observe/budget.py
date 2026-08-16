"""Per-reward-epoch minimal-conditions budget — the "we cannot breach 20%" tracker.

Flare's minimal conditions are pass/fail PER reward epoch (breach → a strike → eventually removed),
and the authoritative data only publishes AFTER the epoch closes — so you'd learn of a breach too
late. This tracks the cumulative FTSO submission rate for the CURRENT epoch and frames the gap to
the floor as a **depleting miss-budget** with an at-current-pace projection, so a developing breach
is visible mid-epoch.

Thresholds (grounded vs published minimal-conditions.json): FTSO ≥80%, FDC ≥60%, staking uptime OK.
FTSO is the tight one and the one we can reconstruct for the WHOLE epoch cheaply: AP's Submit
address sends exactly submit1+submit2 per round and nothing else, so `nonce(now) − nonce(epoch-start
block)` ÷ 2 = rounds submitted — two RPC calls, no block scan, restart-proof. FDC/fast-updates/uptime
are surfaced from the observer's live per-epoch data + the P-chain uptime overlay.
"""

from __future__ import annotations

from clif.observe.reward_rule import VRS_PER_REWARD_EPOCH, _find_block_for_ts
from clif.rpc import RpcClient

# Minimal-conditions floors (fraction).
FTSO_MIN = 0.80
FDC_MIN = 0.60
UPTIME_MIN = 0.80


def budget_status(rate: float | None, elapsed: int, total: int, threshold: float) -> dict:
    """Frame a cumulative participation `rate` (0–1, over `elapsed`/`total` rounds) as a miss-budget
    vs `threshold`. Includes the at-current-pace projected final rate — the forward-looking breach
    signal. severity: CRIT already-breached or projected-to-breach; WARN < 40% budget left."""
    if rate is None or elapsed <= 0:
        return {"rate_pct": None, "severity": "unknown"}
    miss_budget = int(round((1 - threshold) * total))   # rounds we may miss all epoch (e.g. 672)
    missed = max(0, int(round((1 - rate) * elapsed)))    # rounds missed so far
    left = miss_budget - missed
    miss_rate = missed / elapsed
    projected_final = 1 - miss_rate                       # if this pace holds to epoch end
    eta_rounds = int(left / miss_rate) if miss_rate > 0 and left > 0 else None
    if left <= 0 or projected_final < threshold:
        sev = "CRIT"                                      # breached, or on track to breach
    elif left < 0.4 * miss_budget:
        sev = "WARN"                                      # burning the budget fast
    else:
        sev = "OK"
    return {
        "rate_pct": round(100 * rate, 2),
        "threshold_pct": round(100 * threshold),
        "miss_budget": miss_budget,
        "missed": missed,
        "budget_left": left,
        "budget_left_pct": round(100 * max(0, left) / miss_budget, 1) if miss_budget else None,
        "projected_final_pct": round(100 * projected_final, 2),
        "eta_rounds_to_breach": eta_rounds,
        "severity": sev,
    }


def read_ftso_budget(rpc: RpcClient, *, submit_addr: str, factory) -> dict:
    """FTSO submission budget for the CURRENT reward epoch, reconstructed for the WHOLE epoch via
    the Submit address nonce delta (submit1+submit2 = 2 tx/round). Restart-proof (a fresh on-chain
    read each call). Returns the round tallies + a `budget_status` block."""
    now_round = factory.now_id()
    epoch = now_round // VRS_PER_REWARD_EPOCH
    epoch_start_round = epoch * VRS_PER_REWARD_EPOCH
    elapsed = max(0, now_round - epoch_start_round)
    start_ts = factory.make_epoch(epoch_start_round).start_s
    start_block = _find_block_for_ts(rpc, start_ts)
    nonce_start = rpc.get_transaction_count(submit_addr, hex(start_block))
    nonce_now = rpc.get_transaction_count(submit_addr, "latest")
    submitted = max(0, (nonce_now - nonce_start) // 2)    # 2 tx (submit1+submit2) per round
    rate = (submitted / elapsed) if elapsed else 1.0
    out = {
        "epoch": epoch,
        "rounds_elapsed": elapsed,
        "rounds_total": VRS_PER_REWARD_EPOCH,
        "epoch_start_block": start_block,
        "ftso_submitted": submitted,
        **budget_status(rate, elapsed, VRS_PER_REWARD_EPOCH, FTSO_MIN),
    }
    return out
