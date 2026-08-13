"""Per-round IQR tallies + their disk persistence — the substrate for multi-horizon IQR rates.

The rolling in-memory window (`ObserverState.finalized`) only spans ~40 rounds (~1 h), so it
can't answer "IQR over 6 h / 24 h / since the reward epoch began". Each finalized, band-scored
round contributes one compact `IqrTally` (round id + finalize timestamp + the aggregate band
counts) to an append-only JSONL log; the engine reloads it on start so long horizons — including
since-epoch (up to ~3.5 d) — survive restarts. OBSERVE-only; holds nothing sensitive.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

# Retain enough to answer 24h even across a reward-epoch boundary (a reward epoch is ~3.5d, so
# the last 24h can straddle two epochs) plus the whole current epoch for "since epoch".
_RETAIN_AGE_SEC = 90_000  # ~25 h


@dataclass
class IqrTally:
    rid: int  # voting round id
    ts: int   # finalize timestamp (chain seconds) — used for the time-window horizons
    fr: int   # feed-rounds scored this round
    ins: int  # inside the IQR (primary band)
    bnd: int  # on Q1/Q3 (coin-flip → ½ toward inner)
    pct: int  # inside the secondary (PCT) band
    cap: int  # feed-rounds with Q3−Q1 ≤ 1 tick (structurally capped)


def _retain(t: IqrTally, *, now_ts: int, reward_epoch: int | None, vrs_per_epoch: int) -> bool:
    if now_ts and t.ts >= now_ts - _RETAIN_AGE_SEC:
        return True
    return reward_epoch is not None and t.rid // vrs_per_epoch == reward_epoch


def load_history(
    path: Path, *, now_ts: int, reward_epoch: int | None, vrs_per_epoch: int
) -> list[IqrTally]:
    """Load retained tallies (recent ≤25h OR in the current reward epoch). Never raises — a
    missing/corrupt log yields an empty history (the horizons just rebuild forward)."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    by_rid: dict[int, IqrTally] = {}  # dedup: a restart re-finalizes recent rounds → last wins
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            t = IqrTally(**json.loads(ln))
        except (ValueError, TypeError):
            continue
        if _retain(t, now_ts=now_ts, reward_epoch=reward_epoch, vrs_per_epoch=vrs_per_epoch):
            by_rid[t.rid] = t
    return sorted(by_rid.values(), key=lambda t: t.rid)


def append_tally(path: Path, t: IqrTally) -> None:
    """Append one tally as a JSONL line (best-effort — a persistence hiccup never breaks the engine)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(asdict(t), separators=(",", ":")) + "\n")
    except OSError:
        pass


def prune_history(
    path: Path, *, now_ts: int, reward_epoch: int | None, vrs_per_epoch: int
) -> None:
    """Rewrite the log keeping only retained tallies — bounds the file (best-effort)."""
    kept = load_history(path, now_ts=now_ts, reward_epoch=reward_epoch, vrs_per_epoch=vrs_per_epoch)
    try:
        Path(path).write_text(
            "".join(json.dumps(asdict(t), separators=(",", ":")) + "\n" for t in kept)
        )
    except OSError:
        pass
