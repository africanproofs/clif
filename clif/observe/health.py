"""ObserveHealth — the rolling participation snapshot + severity + color render.

The engine writes a status dict each cycle (`build_status`); `observe status` and the epoch
daemon read it back into an `ObserveHealth` that computes severity + staleness at read time.
Never green on a read error or a stale engine (the RE423 doctrine: silence is never green).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from clif.funding import _BADGE_OBS, _GREEN, _RED, _RESET, _YELLOW

_STALE_AFTER_SEC = 300  # no fresh status write in 5 min ⇒ the engine is dead → CRIT
_PARTICIPATION_CRIT_PCT = 90.0  # sustained on-time completeness below this ⇒ CRIT
_FDC_CRIT_PCT = 80.0  # the FDC minimal condition; sustained below this ⇒ CRIT


def build_status(
    state, *, network: str, enabled: bool,
    registered: bool | None = None, reward_epoch: int | None = None,
) -> dict:
    """The JSON the engine writes each cycle. `registered` = is AP in the registered voter
    set for the current reward epoch (None = not probed); when False, all the clean
    submissions below earn ZERO (the RE423 blind spot) and severity is forced CRIT."""
    agg = state.aggregates()
    return {
        "network": network,
        "enabled": enabled,
        "written_at": int(time.time()),
        "last_block": state.last_block,
        "last_ts": state.last_ts,
        "last_round_finalized": state.last_round_finalized,
        "registered": registered,
        "reward_epoch": reward_epoch,
        **agg,
    }


@dataclass
class ObserveHealth:
    network: str
    enabled: bool
    written_at: int | None = None
    last_block: int | None = None
    last_ts: int | None = None
    last_round_finalized: int | None = None
    window_rounds: int = 0
    complete: int = 0
    missing_submit1: int = 0
    missing_submit2: int = 0
    reveal_offences: int = 0
    off_window: int = 0
    fdc_request_rounds: int = 0  # rounds in the window that had FDC attestation requests
    fdc_participated: int = 0  # of those, rounds AP bitvoted (clean)
    fdc_missing: int = 0  # request-rounds AP failed to bitvote
    # IQR reward-band scoring (quality, not liveness — informational, never drives severity).
    iqr_scored_rounds: int = 0  # finalized rounds where AP's values were band-scored
    iqr_feed_rounds: int = 0  # feed×round observations scored
    iqr_inside: int = 0  # inside the IQR (primary band)
    iqr_boundary: int = 0  # on Q1/Q3 (coin-flip → counts ½ toward inner)
    iqr_pct_hit: int = 0  # inside the secondary (PCT) band
    iqr_capped: int = 0  # feed-rounds where Q3−Q1 ≤ 1 tick (structural ~50% inner ceiling)
    recent_issues: list[str] | None = None
    registered: bool | None = None  # in the registered voter set for the current reward epoch?
    reward_epoch: int | None = None
    error: str | None = None

    @property
    def age_sec(self) -> float | None:
        return None if self.written_at is None else max(0.0, time.time() - self.written_at)

    @property
    def stale(self) -> bool:
        a = self.age_sec
        return a is not None and a > _STALE_AFTER_SEC

    @property
    def participation_pct(self) -> float | None:
        if self.window_rounds <= 0:
            return None
        return round(100.0 * self.complete / self.window_rounds, 1)

    @property
    def fdc_participation_pct(self) -> float | None:
        if self.fdc_request_rounds <= 0:
            return None
        return round(100.0 * self.fdc_participated / self.fdc_request_rounds, 1)

    @property
    def iqr_inner_pct(self) -> float | None:
        """Expected primary-band hit rate: (inside + ½·boundary) / feed-rounds."""
        if self.iqr_feed_rounds <= 0:
            return None
        return round(100.0 * (self.iqr_inside + 0.5 * self.iqr_boundary) / self.iqr_feed_rounds, 1)

    @property
    def iqr_outer_pct(self) -> float | None:
        if self.iqr_feed_rounds <= 0:
            return None
        return round(100.0 * self.iqr_pct_hit / self.iqr_feed_rounds, 1)

    @property
    def severity(self) -> str:
        if self.error is not None:
            return "CRIT"  # unknown = treat as bad
        if not self.enabled:
            return "OK"  # explicitly off — nothing to assert
        if self.stale:
            return "CRIT"  # engine stopped writing → not observing
        if self.registered is False:
            return "CRIT"  # submitting but NOT in the registered set ⇒ these rounds earn ZERO
        if self.window_rounds == 0:
            return "WARN"  # warming up / no finalized round yet
        if self.reveal_offences > 0:
            return "CRIT"  # a reveal offence loses rewards + risks a strike
        pct = self.participation_pct
        if pct is not None and pct < _PARTICIPATION_CRIT_PCT:
            return "CRIT"  # sustained non-participation — the RE423-family "not on-chain" signal
        fpct = self.fdc_participation_pct
        if self.fdc_request_rounds >= 10 and fpct is not None and fpct < _FDC_CRIT_PCT:
            return "CRIT"  # sustained FDC non-participation — the FDC minimal condition (80%) at risk
        if self.missing_submit1 or self.missing_submit2 or self.off_window or self.fdc_missing:
            return "WARN"  # isolated miss / off-window / FDC gap — worth a look, not yet systemic
        return "OK"

    def to_dict(self) -> dict:
        return {
            "network": self.network,
            "enabled": self.enabled,
            "severity": self.severity,
            "written_at": self.written_at,
            "age_sec": (int(self.age_sec) if self.age_sec is not None else None),
            "stale": self.stale,
            "last_block": self.last_block,
            "last_round_finalized": self.last_round_finalized,
            "window_rounds": self.window_rounds,
            "complete": self.complete,
            "participation_pct": self.participation_pct,
            "missing_submit1": self.missing_submit1,
            "missing_submit2": self.missing_submit2,
            "reveal_offences": self.reveal_offences,
            "off_window": self.off_window,
            "fdc_request_rounds": self.fdc_request_rounds,
            "fdc_participated": self.fdc_participated,
            "fdc_missing": self.fdc_missing,
            "fdc_participation_pct": self.fdc_participation_pct,
            "iqr_scored_rounds": self.iqr_scored_rounds,
            "iqr_feed_rounds": self.iqr_feed_rounds,
            "iqr_inner_pct": self.iqr_inner_pct,
            "iqr_outer_pct": self.iqr_outer_pct,
            "iqr_capped": self.iqr_capped,
            "registered": self.registered,
            "reward_epoch": self.reward_epoch,
            "recent_issues": self.recent_issues or [],
            "error": self.error,
        }


def read_observe_status(path: Path, *, enabled: bool) -> ObserveHealth:
    """Read the engine's status file into an ObserveHealth. Never raises — a missing/corrupt
    file with the observer ENABLED is CRIT (it should be writing); disabled ⇒ a benign OK."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        if not enabled:
            return ObserveHealth(network="?", enabled=False)
        return ObserveHealth(network="?", enabled=True, error=f"no observer status ({exc})")
    return ObserveHealth(
        network=d.get("network", "?"),
        enabled=bool(d.get("enabled", enabled)),
        written_at=d.get("written_at"),
        last_block=d.get("last_block"),
        last_ts=d.get("last_ts"),
        last_round_finalized=d.get("last_round_finalized"),
        window_rounds=d.get("window_rounds", 0),
        complete=d.get("complete", 0),
        missing_submit1=d.get("missing_submit1", 0),
        missing_submit2=d.get("missing_submit2", 0),
        reveal_offences=d.get("reveal_offences", 0),
        off_window=d.get("off_window", 0),
        fdc_request_rounds=d.get("fdc_request_rounds", 0),
        fdc_participated=d.get("fdc_participated", 0),
        fdc_missing=d.get("fdc_missing", 0),
        iqr_scored_rounds=d.get("iqr_scored_rounds", 0),
        iqr_feed_rounds=d.get("iqr_feed_rounds", 0),
        iqr_inside=d.get("iqr_inside", 0),
        iqr_boundary=d.get("iqr_boundary", 0),
        iqr_pct_hit=d.get("iqr_pct_hit", 0),
        iqr_capped=d.get("iqr_capped", 0),
        registered=d.get("registered"),
        reward_epoch=d.get("reward_epoch"),
        recent_issues=d.get("recent_issues", []),
    )


def render_observe(h: ObserveHealth, *, active: bool) -> str:
    """One color-coded line with the OBS badge (fixed orange) + severity-colored body."""
    sev = h.severity
    if not h.enabled:
        return f"{_BADGE_OBS} \033[2mFTSO observer disabled on {h.network}{_RESET}"
    if h.error is not None:
        return f"{_BADGE_OBS} {_RED}🔴 observer STATE UNKNOWN on {h.network} ({h.error}){_RESET}"
    if h.stale:
        age = int(h.age_sec or 0)
        return f"{_BADGE_OBS} {_RED}🔴 observer STALE on {h.network} — no update for {age}s (engine down?){_RESET}"
    if h.registered is False:
        # The RE423 blind spot made explicit: we ARE submitting, but it earns nothing.
        subs = f"{h.complete}/{h.window_rounds}" if h.window_rounds else "0"
        loud = "🔴🔴" if active else "🔴"
        # Still surface the WOULD-BE IQR quality — it's scored vs the registered consensus, so it
        # stays meaningful (and visible) even while AP is excluded.
        wb = f" · would-be IQR in {h.iqr_inner_pct}% / out {h.iqr_outer_pct}%" if h.iqr_feed_rounds else ""
        return (
            f"{_BADGE_OBS} {_RED}{loud} submitting {subs} rounds on {h.network} — but NOT REGISTERED "
            f"for RE{h.reward_epoch}: these submissions earn ZERO{_RESET}{wb}"
        )
    if h.window_rounds == 0:
        return f"{_BADGE_OBS} {_YELLOW}⚠ warming up on {h.network} (no finalized round yet, block {h.last_block}){_RESET}"
    pct = h.participation_pct
    body = f"FTSO {h.network} {h.complete}/{h.window_rounds} rounds clean ({pct}%)"
    # FDC clause — only meaningful when the window had request-rounds.
    if h.fdc_request_rounds:
        body += f" · FDC {h.fdc_participated}/{h.fdc_request_rounds} ({h.fdc_participation_pct}%)"
    # IQR reward-band quality clause — only once a round has been band-scored.
    if h.iqr_feed_rounds:
        body += f" · IQR in {h.iqr_inner_pct}% / out {h.iqr_outer_pct}%"
    if sev == "OK":
        return f"{_BADGE_OBS} {_GREEN}✓ {body}{_RESET}"
    problems = []
    if h.reveal_offences:
        problems.append(f"{h.reveal_offences} REVEAL OFFENCE")
    if h.missing_submit1:
        problems.append(f"{h.missing_submit1} missing commit")
    if h.missing_submit2:
        problems.append(f"{h.missing_submit2} missing reveal")
    if h.off_window:
        problems.append(f"{h.off_window} off-window")
    if h.fdc_missing:
        problems.append(f"{h.fdc_missing} FDC gap")
    color = _RED if sev == "CRIT" else _YELLOW
    mark = "🔴" if sev == "CRIT" else "⚠"
    loud = "🔴🔴" if (sev == "CRIT" and active) else mark
    return f"{_BADGE_OBS} {color}{loud} {body} — {', '.join(problems)}{_RESET}"
