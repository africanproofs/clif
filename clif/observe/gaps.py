"""Outage/backfill ledger — so a reader is NEVER unsure whether a report is live or replayed.

When the RPC is unreachable the observer pauses (its cursor holds); on reconnect it re-reads every
missed block from the archive node — a **backfill**. During that replay the rolling window reflects
the OUTAGE window, not real time, which is dangerously ambiguous. This records each outage as a
`Gap` (when it happened, how long, which blocks it spans) and lets the surface show `stream: LIVE`
vs `stream: CATCHING UP` + a tabulated outage list. A gap is `backfilled` once the stream's
last-processed block has advanced past its `to_block` — derived, never a stored flag that can drift.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

_RETAIN_SEC = 7 * 86_400  # keep a week of outages for the report's "outages (…)" line


@dataclass
class Gap:
    start: int          # wall/chain seconds the outage began (first failed poll)
    end: int            # seconds it recovered
    dur: int            # end - start
    fails: int          # consecutive failed polls during the outage
    from_block: int     # last block processed BEFORE the outage (backfill replays from here+1)
    to_block: int       # chain head at recovery (backfill target)

    def backfilled(self, last_block: int | None) -> bool:
        """True once the stream has replayed past this gap (last_block ≥ to_block)."""
        return last_block is not None and last_block >= self.to_block


def load_gaps(path: Path, *, now: int) -> list[Gap]:
    """Recent gaps (≤ 7 days). Never raises — a missing/corrupt ledger yields an empty list."""
    out: list[Gap] = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            g = Gap(**json.loads(ln))
        except (ValueError, TypeError):
            continue
        if now - g.end <= _RETAIN_SEC:
            out.append(g)
    return sorted(out, key=lambda g: g.start)


def append_gap(path: Path, g: Gap) -> None:
    """Append one outage (best-effort — a persistence hiccup never breaks the engine)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(asdict(g), separators=(",", ":")) + "\n")
    except OSError:
        pass


def prune_gaps(path: Path, *, now: int) -> None:
    """Rewrite the ledger keeping only the last 7 days — bounds the file (best-effort)."""
    kept = load_gaps(path, now=now)
    try:
        Path(path).write_text("".join(json.dumps(asdict(g), separators=(",", ":")) + "\n" for g in kept))
    except OSError:
        pass


def hms(seconds: int) -> str:
    """Compact human duration: 3h16m, 45m, 8s."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"
