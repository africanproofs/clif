"""Delegation history + 24h / reward-epoch deltas — is stake flowing in or out?

`getCurrentValidators` / `votePowerOf` are current-only (no historical read), so to show *change*
we persist a timestamped snapshot each refresh and diff the current value against (a) the newest
snapshot ≥24h old and (b) the earliest snapshot of the current reward epoch. Survives restarts, so
the epoch delta reflects change since we first observed this epoch. OBSERVE-only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

_RETAIN_SEC = 8 * 86_400  # ≥ 24h + a ~3.5-day reward epoch + margin


@dataclass
class DelegSnap:
    ts: int
    epoch: int
    val_delegated: float   # FLR delegated to the validator
    val_dels: int          # validator delegator count
    ftso_vp: float         # FTSO WNat vote power


def load_snaps(path: Path, *, now: int) -> list[DelegSnap]:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    out: list[DelegSnap] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            s = DelegSnap(**json.loads(ln))
        except (ValueError, TypeError):
            continue
        if now - s.ts <= _RETAIN_SEC:
            out.append(s)
    return sorted(out, key=lambda s: s.ts)


def append_snap(path: Path, s: DelegSnap) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(asdict(s), separators=(",", ":")) + "\n")
    except OSError:
        pass


def prune_snaps(path: Path, *, now: int) -> None:
    kept = load_snaps(path, now=now)
    try:
        Path(path).write_text("".join(json.dumps(asdict(s), separators=(",", ":")) + "\n" for s in kept))
    except OSError:
        pass


def compute_deltas(history: list[DelegSnap], *, now: int, epoch: int, current: DelegSnap) -> dict:
    """Δ vs ~24h ago and vs the epoch's first snapshot, per tracked field. None ⇒ no baseline yet.
    `history` should NOT include `current`."""
    older24 = [s for s in history if s.ts <= now - 86_400]
    base24 = older24[-1] if older24 else None  # newest snapshot at least 24h old
    this_epoch = [s for s in history if s.epoch == epoch]
    base_ep = this_epoch[0] if this_epoch else None  # earliest snapshot this epoch

    def _d(base: DelegSnap | None, field: str):
        return None if base is None else round(getattr(current, field) - getattr(base, field), 4)

    return {
        f: {"h24": _d(base24, f), "epoch": _d(base_ep, f)}
        for f in ("val_delegated", "val_dels", "ftso_vp")
    }
