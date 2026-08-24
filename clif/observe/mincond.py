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


@dataclass
class RoundRecord:
    rid: int       # voting round id (→ reward epoch = rid // VRS_PER_REWARD_EPOCH)
    s1: int = 0    # submit1 (commit) seen — 0/1
    s2: int = 0    # submit2 (reveal) seen — 0/1 (the FTSO minimal condition is on the reveal)
    cl: int = 0    # fully clean round — 0/1
    fexp: int = 0  # FDC expected this round (had attestation requests) — 0/1
    fok: int = 0   # FDC participated (expected & AP bitvoted) — 0/1
    fu: int = 0    # fast-update submissions attributed to this round


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
    `fdc_pct` is None until the epoch has had at least one FDC-request round."""
    vals = records.values() if isinstance(records, dict) else records
    ep = [r for r in vals if epoch_of(r.rid) == epoch]
    n = len(ep)
    fdc_exp = sum(r.fexp for r in ep)
    fdc_ok = sum(r.fok for r in ep)
    ftso_rev = sum(r.s2 for r in ep)
    ftso_clean = sum(r.cl for r in ep)
    return {
        "epoch": epoch,
        "rounds_recorded": n,
        "rounds_expected": VRS_PER_REWARD_EPOCH,
        "coverage_pct": (round(100.0 * n / VRS_PER_REWARD_EPOCH, 1) if n else 0.0),
        "ftso_revealed": ftso_rev,
        "ftso_clean": ftso_clean,
        "ftso_pct": (round(100.0 * ftso_rev / n, 1) if n else None),
        "fdc_expected": fdc_exp,
        "fdc_participated": fdc_ok,
        "fdc_pct": (round(100.0 * fdc_ok / fdc_exp, 1) if fdc_exp else None),
        "fu_updates": sum(r.fu for r in ep),
    }
