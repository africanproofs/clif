"""Per-reward-epoch minimal-conditions ledger — the EXACT, gap-free performance record.

At the start of each reward epoch a fresh tracker accumulates AP's performance for the whole
epoch, per condition, so the panel shows the exact picture (not a rolling ~1h window) and it
survives restarts AND network disconnections. Each of the four conditions is tracked:

  • FTSO   — already exact full-epoch via the Submit nonce delta (`budget.py`); NOT in this ledger.
  • uptime — already exact from the P-chain's own cumulative measure; NOT in this ledger.
  • FDC    — bitvotes ride inside submit2 (no nonce shortcut), so it MUST be observed per round.
  • FastUpd— per-block sortition submissions, attributed to their voting round.

This module is the gap-free substrate for the two that can only be *observed*: it persists one
compact record per finalized voting round (FDC expected/participated + fast-update count),
reloads it on start, and — because the engine no longer skips a long backfill — every round of
the epoch is eventually recorded even across a multi-hour outage. Aggregation is per reward epoch
(`rid // VRS_PER_REWARD_EPOCH`), so the tracker is implicitly reset at each epoch boundary.

OBSERVE-only; holds nothing sensitive.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from clif.observe.reward_rule import VRS_PER_REWARD_EPOCH

# A reveal offence (commit via submit1, then no reveal) is penalised 30× the provider's expected
# reward share for that round, deducted from the epoch's total reward and BURNED, capped at the
# provider's whole-epoch reward (Flare FTSO Scaling; FIP.06). So each offence forfeits ~30 rounds'
# reward ≈ REVEAL_OFFENCE_PENALTY_ROUNDS / VRS of the epoch's earnings.
REVEAL_OFFENCE_PENALTY_ROUNDS = 30


@dataclass
class RoundRecord:
    rid: int       # voting round id (→ reward epoch = rid // VRS_PER_REWARD_EPOCH)
    s1: int = 0    # submit1 (commit) seen — 0/1
    s2: int = 0    # submit2 (reveal) seen — 0/1 (the FTSO minimal condition is on the reveal)
    cl: int = 0    # fully clean round — 0/1
    fexp: int = 0  # FDC expected this round (had attestation requests) — 0/1
    fok: int = 0   # FDC participated (expected & AP bitvoted) — 0/1
    fu: int = 0    # fast-update submissions attributed to this round
    ro: int = 0    # reveal offence (committed via submit1 but never revealed) — 0/1; penalty-bearing


def epoch_of(rid: int) -> int:
    return rid // VRS_PER_REWARD_EPOCH


def from_round(rs) -> RoundRecord:
    """Build the compact record from a finalized RoundState."""
    return RoundRecord(
        rid=rs.round_id,
        s1=1 if rs.submit1_seen else 0,
        s2=1 if rs.submit2_seen else 0,
        cl=1 if rs.clean else 0,
        fexp=1 if rs.fdc_expected else 0,
        fok=1 if (rs.fdc_expected and rs.fdc_bitvote_seen and not rs.fdc_gap) else 0,
        fu=int(getattr(rs, "fu_count", 0) or 0),
        ro=1 if getattr(rs, "reveal_offence", False) else 0,
    )


def load_history(path: Path, *, reward_epoch: int | None) -> dict[int, RoundRecord]:
    """Load retained records as {rid: RoundRecord}, deduped by rid (a restart re-finalizes recent
    rounds → last wins). Retains the current + prior reward epoch (prior covers the boundary).
    Never raises — a missing/corrupt log yields an empty map (the tracker rebuilds forward)."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return {}
    keep_from = None if reward_epoch is None else reward_epoch - 1
    out: dict[int, RoundRecord] = {}
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = RoundRecord(**json.loads(ln))
        except (ValueError, TypeError):
            continue
        if keep_from is None or epoch_of(r.rid) >= keep_from:
            out[r.rid] = r
    return out


def append_record(path: Path, rec: RoundRecord) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(asdict(rec), separators=(",", ":")) + "\n")
    except OSError:
        pass


def prune_history(path: Path, *, reward_epoch: int | None) -> None:
    """Rewrite the log keeping only the current + prior epoch (also collapses duplicate lines)."""
    if reward_epoch is None:
        return
    kept = load_history(path, reward_epoch=reward_epoch)
    try:
        Path(path).write_text(
            "".join(json.dumps(asdict(r), separators=(",", ":")) + "\n" for r in sorted(kept.values(), key=lambda r: r.rid))
        )
    except OSError:
        pass


def epoch_tally(records: dict[int, RoundRecord] | list[RoundRecord], *, epoch: int) -> dict:
    """Exact full-epoch FDC + fast-update totals for `epoch`, from the persisted records.
    `fdc_pct` is None until the epoch has had at least one FDC-request round.

    Reveal offences are counted from the durable `s1=1 ∧ s2=0` signal (commit seen, reveal not
    seen) — NOT the `ro` flag. `ro` derives from the byte-level reveal-offence detection which the
    engine zeroes for any round finalized while `catching_up` (pruned-node tx bodies are unreliable
    on backfill), so an offence around an outage lost its `ro` and the count silently read 0. The
    `s1=1 ∧ s2=0` signal is intrinsically pruned-immune: a backfilled round can never decode the
    commit tx body, so it always records `s1=0` — `s1=1` therefore proves the commit was observed
    LIVE, and a live commit with no reveal is a genuine reveal offence. The residual blind spot is
    rounds ENTIRELY absent from the ledger (`missing_in_span`) — a commit-without-reveal there is
    invisible — so the count is reported as a LOWER BOUND whenever the span has such a hole."""
    vals = records.values() if isinstance(records, dict) else records
    ep = [r for r in vals if epoch_of(r.rid) == epoch]
    n = fdc_exp = fdc_ok = ftso_rev = ftso_clean = fu = reveal_offences = 0
    first_rid = last_rid = None
    for r in ep:  # single pass — also tracks the observed span for gap accounting
        n += 1
        fdc_exp += r.fexp
        fdc_ok += r.fok
        ftso_rev += r.s2
        ftso_clean += r.cl
        fu += r.fu
        if r.s1 and not r.s2:  # commit observed live, no reveal — pruned-immune reveal-offence signal
            reveal_offences += 1
        if first_rid is None or r.rid < first_rid:
            first_rid = r.rid
        if last_rid is None or r.rid > last_rid:
            last_rid = r.rid
    span = (last_rid - first_rid + 1) if n else 0
    # Humility: rounds present between the first and last recorded round but NOT in the ledger —
    # a hole the never-skip backfill should have filled. Distinct from partial coverage (rounds
    # before/after the observed span). Any non-zero value while LIVE is a real integrity flag.
    missing_in_span = max(0, span - n)
    return {
        "epoch": epoch,
        "rounds_recorded": n,
        "rounds_expected": VRS_PER_REWARD_EPOCH,
        "coverage_pct": (round(100.0 * n / VRS_PER_REWARD_EPOCH, 1) if n else 0.0),
        "first_rid": first_rid,
        "last_rid": last_rid,
        "span": span,
        "missing_in_span": missing_in_span,
        "ftso_revealed": ftso_rev,
        "ftso_clean": ftso_clean,
        "ftso_pct": (round(100.0 * ftso_rev / n, 1) if n else None),
        "fdc_expected": fdc_exp,
        "fdc_participated": fdc_ok,
        "fdc_pct": (round(100.0 * fdc_ok / fdc_exp, 1) if fdc_exp else None),
        "fu_updates": fu,
        # Reveal offences from the pruned-immune `s1=1 ∧ s2=0` signal. A LOWER BOUND when
        # `missing_in_span > 0` (a ledger hole could hide a further commit-without-reveal).
        "reveal_offences": reveal_offences,
        # Estimated reveal-offence penalty (30 reward-rounds each, burned; capped at the whole epoch).
        "penalty_reward_rounds": min(VRS_PER_REWARD_EPOCH, reveal_offences * REVEAL_OFFENCE_PENALTY_ROUNDS),
        "penalty_pct_of_epoch": round(
            min(100.0, 100.0 * reveal_offences * REVEAL_OFFENCE_PENALTY_ROUNDS / VRS_PER_REWARD_EPOCH), 2
        ),
    }


def format_penalty(offences: int, *, per_round_reward_flr: float = 0.0, lower_bound: bool = False) -> str:
    """The accumulated reveal-offence cost as a string: reward-rounds burned + % of the epoch's
    reward, and (if the operator has set a per-round reward) the estimated FLR figure. Each offence
    burns 30 reward-rounds, capped at the whole-epoch reward. `lower_bound` prefixes `≥` and appends
    a note when the ledger has undecoded-commit rounds that could hide a further offence."""
    rounds = min(VRS_PER_REWARD_EPOCH, offences * REVEAL_OFFENCE_PENALTY_ROUNDS)
    pct = round(min(100.0, 100.0 * rounds / VRS_PER_REWARD_EPOCH), 2)
    flr = f" ≈ {rounds * per_round_reward_flr:,.1f} FLR" if per_round_reward_flr > 0 else ""
    ge = "≥" if lower_bound else ""
    tail = " — lower bound, chain-audit to confirm" if lower_bound else ""
    return f"{ge}{offences} offence(s) · −{ge}{rounds} reward-rounds{flr} burned (~{ge}{pct}% of epoch FTSO reward){tail}"


def epoch_gap_ranges(
    records: dict[int, RoundRecord] | list[RoundRecord], *, epoch: int, limit: int = 6
) -> tuple[list[tuple[int, int]], int]:
    """The missing round ranges within the observed span for `epoch` — `([(lo, hi), …], total)`.
    Empty when coverage is contiguous. `total` is the full count even if the list is capped at
    `limit` (for the close-out display). O(n log n); called only at the epoch boundary, not per cycle."""
    vals = records.values() if isinstance(records, dict) else records
    rids = sorted(r.rid for r in vals if epoch_of(r.rid) == epoch)
    ranges: list[tuple[int, int]] = []
    for i in range(1, len(rids)):
        if rids[i] > rids[i - 1] + 1:
            ranges.append((rids[i - 1] + 1, rids[i] - 1))
    return ranges[:limit], len(ranges)
