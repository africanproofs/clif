"""Automation state + degraded evaluation + the scrapable status file.

Pure, testable logic separated from the `clif auto` loop shell. The escalation
contract (operator-chosen 2026-05-18): a claimable epoch that stays unclaimed
past `stale_after`, or any recent terminal fwd failure, makes clif **degraded**
— surfaced loudly in logs and via `clif status`' exit code. Unclaimed FTSO
rewards eventually expire; a silent failure is the real risk.

"Claimable" here means *we actually have a reward to claim* (an epoch present
in `collect_reward_claims`), not merely that `rewardsHash` is set — so clif
never goes degraded for epochs in which AP has no reward.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# clif status exit codes (Docker healthcheck / monitoring scrape these).
EXIT_HEALTHY = 0
EXIT_DEGRADED = 2
EXIT_NO_STATE = 3  # daemon never wrote a report

# A report older than this multiple of the poll interval ⇒ daemon dead/stuck.
_DEAD_INTERVALS = 3


def stream_key(network: str, claim_type: int, beneficiary: str) -> str:
    return f"{network}:{claim_type}:{beneficiary.lower()}"


@dataclass
class _Stream:
    first_seen: dict[int, float] = field(default_factory=dict)  # epoch -> ts
    cooldown_until: dict[int, float] = field(default_factory=dict)  # epoch -> ts
    last_success_ts: float | None = None
    last_attempt_ts: float | None = None
    last_outcome: str = "init"


@dataclass
class AutoState:
    """In-memory cross-cycle memory for the daemon (not persisted itself)."""

    streams: dict[str, _Stream] = field(default_factory=dict)

    def _s(self, key: str) -> _Stream:
        return self.streams.setdefault(key, _Stream())

    def observe(self, key: str, claim_epochs: list[int], now: float) -> list[int]:
        """Record newly-claimable epochs; forget ones that are gone.

        Returns the epochs that *left* the claimable set since last cycle —
        in a non-blocking daemon that is the authoritative "this epoch got
        claimed" signal (`getNextClaimableRewardEpochId` advanced once the
        tx mined). (An epoch can also leave by *expiring* unclaimed, but the
        staleness guard would already have fired loudly before then.)
        """
        s = self._s(key)
        for e in claim_epochs:
            s.first_seen.setdefault(e, now)
        gone = [e for e in list(s.first_seen) if e not in claim_epochs]
        for e in gone:
            s.first_seen.pop(e, None)
            s.cooldown_until.pop(e, None)
        return gone

    def record_attempt(self, key: str, now: float, outcome: str) -> None:
        s = self._s(key)
        s.last_attempt_ts = now
        s.last_outcome = outcome

    def record_success(self, key: str, now: float) -> None:
        self._s(key).last_success_ts = now

    def record_terminal(self, key: str, epoch: int, now: float, cooldown_sec: int) -> None:
        self._s(key).cooldown_until[epoch] = now + cooldown_sec

    def in_cooldown(self, key: str, epoch: int, now: float) -> bool:
        return self._s(key).cooldown_until.get(epoch, 0.0) > now

    def evaluate(self, now: float, stale_after_sec: int) -> tuple[bool, list[str]]:
        """Degraded if any epoch is claimable too long, or in terminal cooldown."""
        reasons: list[str] = []
        for key, s in self.streams.items():
            for epoch, seen in s.first_seen.items():
                age = now - seen
                if age > stale_after_sec:
                    reasons.append(
                        f"{key}: epoch {epoch} claimable for "
                        f"{int(age)}s (> {stale_after_sec}s) without a "
                        f"successful claim"
                    )
                if s.cooldown_until.get(epoch, 0.0) > now:
                    reasons.append(
                        f"{key}: epoch {epoch} had a terminal fwd failure "
                        f"(in cooldown) — operator action likely needed"
                    )
        return (bool(reasons), reasons)


def build_report(
    state: AutoState,
    network: str,
    poll_interval_sec: int,
    stale_after_sec: int,
    now: float,
) -> dict:
    degraded, reasons = state.evaluate(now, stale_after_sec)
    streams = []
    for key, s in state.streams.items():
        streams.append(
            {
                "stream": key,
                "claimable_epochs": sorted(s.first_seen),
                "last_success_ts": s.last_success_ts,
                "last_attempt_ts": s.last_attempt_ts,
                "last_outcome": s.last_outcome,
            }
        )
    return {
        "updated_at": now,
        "network": network,
        "poll_interval_sec": poll_interval_sec,
        "stale_after_sec": stale_after_sec,
        "degraded": degraded,
        "reasons": reasons,
        "streams": streams,
    }


def write_status_atomic(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".status-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(report, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_status(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def status_exit_code(report: dict | None, now: float | None = None) -> tuple[int, str]:
    """Map a status report to (exit_code, human_line) for `clif status`."""
    if report is None:
        return EXIT_NO_STATE, "no daemon status found (clif auto has not run)"
    now = time.time() if now is None else now
    interval = int(report.get("poll_interval_sec", 900))
    age = now - float(report.get("updated_at", 0.0))
    if age > _DEAD_INTERVALS * interval:
        return (
            EXIT_DEGRADED,
            f"daemon status is stale ({int(age)}s old > "
            f"{_DEAD_INTERVALS}x{interval}s) — clif auto is dead or stuck",
        )
    if report.get("degraded"):
        return EXIT_DEGRADED, "DEGRADED: " + "; ".join(report.get("reasons", []))
    return EXIT_HEALTHY, "healthy"
