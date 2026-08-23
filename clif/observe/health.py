"""ObserveHealth — the rolling participation snapshot + severity + color render.

The engine writes a status dict each cycle (`build_status`); `observe status` and the epoch
daemon read it back into an `ObserveHealth` that computes severity + staleness at read time.
Never green on a read error or a stale engine (the RE423 doctrine: silence is never green).
"""

from __future__ import annotations

import json
import resource
import time
from dataclasses import dataclass
from pathlib import Path

from clif.funding import _BADGE_OBS, _GREEN, _RED, _RESET, _YELLOW
from clif.observe.gaps import hms

_STALE_AFTER_SEC = 300  # no fresh status write in 5 min ⇒ the engine is dead → CRIT
_PARTICIPATION_CRIT_PCT = 90.0  # sustained on-time completeness below this ⇒ CRIT
_FDC_CRIT_PCT = 80.0  # sustained FDC participation below this ⇒ CRIT (a margin over the 60% floor)
_RECOVERED_CLEAN_ROUNDS = 3  # this many consecutive clean rounds ⇒ an isolated-miss WARN is recovering
# Reward-eligibility floors for the minimal-conditions panel (the published minimal-conditions.json).
_FTSO_MIN_PCT = 80.0
_FDC_MIN_PCT = 60.0
_UPTIME_MIN_PCT = 80.0


def build_status(
    state, *, network: str, enabled: bool,
    registered: bool | None = None, reward_epoch: int | None = None,
    uptime_pct: float | None = None, uptime_connected: bool | None = None,
    validator_node: str | None = None,
    quorum: dict | None = None, verify_host: str | None = None,
    uptime_verify: tuple | None = None, quorum_crit: bool = False,
    gaps: list | None = None, live_lag_blocks: int = 8,
    budget: dict | None = None, delegation: dict | None = None,
    mincond: dict | None = None,
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
        "head": state.head,
        "lag_sec": (int(time.time() - state.last_ts) if state.last_ts else None),
        "gaps": gaps or [],
        "live_lag_blocks": live_lag_blocks,
        "budget": budget,
        "delegation": delegation,
        "mincond": mincond,  # per-epoch exact FDC + fast-updates (gap-free ledger)
        "last_round_finalized": state.last_round_finalized,
        "registered": registered,
        "reward_epoch": reward_epoch,
        "iqr_windows": state.windowed_iqr(state.last_ts or int(time.time()), reward_epoch),
        "fu_windows": state.windowed_fastupdates(state.last_ts or int(time.time())),
        "uptime_pct": uptime_pct,
        "uptime_connected": uptime_connected,
        "validator_node": validator_node,
        "quorum": quorum,  # {fact: {status: agree|dispute|unavailable, [primary, verify]}}
        "verify_host": verify_host,
        "uptime_verify": list(uptime_verify) if uptime_verify else None,
        "quorum_crit": quorum_crit,
        # Resource gauge — bounded-collection sizes + process RSS, so a slow leak shows as a
        # trend in the hourly report long before it could OOM (ru_maxrss is KiB on Linux).
        "resources": {
            "in_flight_rounds": len(state.rounds),
            "iqr_hist": len(state.iqr_history),
            "fu_events": len(state.fu_events),
            "rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        },
        **agg,
    }


@dataclass
class ObserveHealth:
    network: str
    enabled: bool
    written_at: int | None = None
    last_block: int | None = None
    last_ts: int | None = None
    head: int | None = None  # latest chain head — for the lag / LIVE-vs-CATCHING-UP signal
    lag_sec: int | None = None  # how far behind chain time the last-processed block is
    gaps: list | None = None  # recorded RPC outages [{start,end,dur,fails,from_block,to_block}]
    live_lag_blocks: int = 8  # ≤ this behind head ⇒ LIVE; more ⇒ CATCHING UP
    budget: dict | None = None  # per-epoch FTSO miss-budget vs the 80% floor (read_ftso_budget)
    delegation: dict | None = None  # live validator + FTSO delegation snapshot
    mincond: dict | None = None  # per-epoch exact FDC + fast-updates (gap-free ledger)
    last_round_finalized: int | None = None
    window_rounds: int = 0
    trailing_clean: int = 0  # consecutive clean rounds at the newest end of the window (recovery signal)
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
    iqr_windows: dict | None = None  # {1h,6h,24h,epoch} → {inner_pct,outer_pct,feed_rounds,rounds,capped}
    signatures_seen: int = 0  # submitSignatures seen in the rolling window
    fu_windows: dict | None = None  # fast-updates (255): {1h,6h,24h} AP counts + total_tracked
    uptime_pct: float | None = None  # P-chain validator uptime %
    uptime_connected: bool | None = None
    validator_node: str | None = None
    quorum: dict | None = None  # independent-RPC cross-check {fact: {status, [primary, verify]}}
    verify_host: str | None = None
    uptime_verify: list | None = None  # [pct, connected] from the verify node (uptime is subjective)
    quorum_crit: bool = False  # a DISPUTED gating fact ⇒ CRIT (else WARN)
    resources: dict | None = None  # {in_flight_rounds, iqr_hist, fu_events, rss_mib} — leak gauge
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
    def lag_blocks(self) -> int | None:
        if self.head is None or self.last_block is None:
            return None
        return max(0, self.head - self.last_block)

    @property
    def stream_state(self) -> str:
        """`live` = at head (real-time); `catching_up` = replaying an outage backfill; `unknown`
        until the first head is seen. The load-bearing signal: is the report LIVE or REPLAYED?"""
        lb = self.lag_blocks
        if lb is None:
            return "unknown"
        return "live" if lb <= self.live_lag_blocks else "catching_up"

    @property
    def open_gaps(self) -> list:
        """Recorded outages not yet fully backfilled (last_block < to_block). A `skipped` gap is
        resolved-by-skip (deliberately not replayed), so it's not 'open'."""
        return [
            g for g in (self.gaps or [])
            if not g.get("skipped") and (self.last_block or 0) < g.get("to_block", 0)
        ]

    @property
    def quorum_status(self) -> str:
        """Overall independent-RPC verdict: agree / dispute / unavailable / off."""
        from clif.observe.verify import quorum_overall

        return quorum_overall(self.quorum or {})

    @property
    def disputed_facts(self) -> list[str]:
        """Gating facts where the independent node DISAGREED with ours (data may be untrustworthy)."""
        return [k for k, v in (self.quorum or {}).items() if (v or {}).get("status") == "dispute"]

    @property
    def severity(self) -> str:
        if self.error is not None:
            return "CRIT"  # unknown = treat as bad
        if not self.enabled:
            return "OK"  # explicitly off — nothing to assert
        if self.disputed_facts and self.quorum_crit:
            return "CRIT"  # an independent node disagrees on a gating fact — trust the disagreement
        if self.stale:
            return "CRIT"  # engine stopped writing → not observing
        if self.registered is False:
            return "CRIT"  # submitting but NOT in the registered set ⇒ these rounds earn ZERO
        if (self.budget or {}).get("severity") == "CRIT":
            return "CRIT"  # minimal-conditions budget breached or on track to breach (the 20% floor)
        if self.window_rounds == 0:
            return "WARN"  # warming up / no finalized round yet
        if self.reveal_offences > 0:
            return "CRIT"  # a reveal offence loses rewards + risks a strike
        pct = self.participation_pct
        if pct is not None and pct < _PARTICIPATION_CRIT_PCT:
            return "CRIT"  # sustained non-participation — the RE423-family "not on-chain" signal
        fpct = self.fdc_participation_pct
        if self.fdc_request_rounds >= 10 and fpct is not None and fpct < _FDC_CRIT_PCT:
            return "CRIT"  # sustained FDC non-participation (rolling window) — early warning at 80%
        mc = self.mincond or {}
        if (mc.get("fdc_expected") or 0) >= 10 and mc.get("fdc_pct") is not None and mc["fdc_pct"] < _FDC_MIN_PCT:
            return "CRIT"  # EXACT full-epoch FDC has breached the 60% minimal-condition floor
        if self.validator_node and self.uptime_pct is not None and self.uptime_pct < _UPTIME_MIN_PCT:
            return "CRIT"  # validator uptime below the 80% minimal-condition floor — staking reward risk
        if self.missing_submit1 or self.missing_submit2 or self.off_window or self.fdc_missing:
            return "WARN"  # isolated miss / off-window / FDC gap — worth a look, not yet systemic
        if self.disputed_facts:
            return "WARN"  # independent node disagrees — surface it (CRIT only if quorum_crit)
        return "OK"

    @property
    def recovering(self) -> bool:
        """A WARN that is a STALE isolated miss aging out of the rolling window — the miss is still
        counted, but the most recent rounds are all clean, so the problem is effectively resolved
        (the window just hasn't slid past it). Lets the report relax its cadence back to hourly
        instead of re-shouting a degradation every few minutes for the ~1h window lifetime. NEVER
        for CRIT (a real, current problem), a still-warming window, or a disputed-quorum WARN."""
        if self.severity != "WARN" or self.window_rounds == 0:
            return False
        isolated_miss = bool(
            self.missing_submit1 or self.missing_submit2 or self.off_window or self.fdc_missing
        )
        return isolated_miss and self.trailing_clean >= _RECOVERED_CLEAN_ROUNDS

    def verdict(self) -> tuple[str, str, list[str]]:
        """The bottom line — overall health as ONE proclamation plus a concrete call to action
        when something needs attention. Returns `(level, headline, actions)`: `level` mirrors
        `severity` (OK/WARN/CRIT); `actions` is empty when healthy. This is the single line an
        operator (or agent) can read and act on without parsing the per-protocol block above;
        each reason is paired with the specific fix, most-severe first."""
        if not self.enabled:
            return ("OK", f"observer disabled on {self.network} — nothing asserted", [])

        net = self.network
        sev = self.severity
        reasons: list[str] = []
        actions: list[str] = []

        # CRIT causes — each plain reason paired with the fix. (When sev is OK/WARN these are all
        # false by construction of `severity`, so nothing is added.)
        if self.error is not None:
            reasons.append(f"observer read error ({self.error})")
            actions.append(f"check the observe RPC, then restart clif-observe-{net} — monitoring is BLIND")
        if self.stale:
            reasons.append(f"observer STALE — no status write for {int(self.age_sec or 0)}s")
            actions.append(f"restart clif-observe-{net}; the engine stopped writing (flying blind)")
        if self.disputed_facts:
            reasons.append("independent node DISPUTES: " + ", ".join(self.disputed_facts))
            if self.quorum_crit:
                actions.append("reconcile against a third source before trusting these numbers")
        if self.registered is False:
            reasons.append(f"NOT REGISTERED for RE{self.reward_epoch} — every submission earns ZERO")
            actions.append(f"check Submit gas + registerVoter NOW (REG line / clif-registration-{net})")
        b = self.budget or {}
        if b.get("severity") == "CRIT":
            eta = f", ~{b['eta_rounds_to_breach']} rounds to breach" if b.get("eta_rounds_to_breach") else ""
            reasons.append(f"minimal-conditions budget at risk — FTSO {b.get('rate_pct')}%{eta}")
            actions.append("investigate missed rounds immediately — the 20% floor is in play")
        if self.reveal_offences > 0:
            reasons.append(f"REVEAL OFFENCE ×{self.reveal_offences} — lost reward + strike risk")
            actions.append("investigate the value-provider / submit pipeline")
        pct = self.participation_pct
        if pct is not None and pct < _PARTICIPATION_CRIT_PCT:
            reasons.append(f"FTSO participation {pct}% (< {_PARTICIPATION_CRIT_PCT:g}%)")
            actions.append(f"confirm the voter is submitting on {net}")
        fpct = self.fdc_participation_pct
        if self.fdc_request_rounds >= 10 and fpct is not None and fpct < _FDC_CRIT_PCT:
            reasons.append(f"FDC participation {fpct}% (< {_FDC_CRIT_PCT:g}%)")
            actions.append("check the FDC bitvote path")

        # WARN causes — only when nothing above already fired.
        if sev == "WARN":
            if self.window_rounds == 0:
                reasons.append("warming up — no finalized round yet")
            misses = []
            if self.missing_submit1:
                misses.append(f"submit1×{self.missing_submit1}")
            if self.missing_submit2:
                misses.append(f"submit2×{self.missing_submit2}")
            if self.off_window:
                misses.append(f"off-window×{self.off_window}")
            if self.fdc_missing:
                misses.append(f"FDC×{self.fdc_missing}")
            if misses:
                reasons.append("isolated miss — " + " ".join(misses))
                actions.append("watch the next few rounds — not yet systemic")

        if sev == "OK":
            if self.stream_state == "catching_up":
                return ("OK", f"⏳ CATCHING UP on {net} — verdict pending (data is REPLAYED, not live)", [])
            return ("OK", f"✅ SYSTEM HEALTHY — all FSP protocols nominal on {net}, no action needed", [])
        if self.recovering:
            # A stale isolated miss aging out of the window; recent rounds are clean → no action.
            joined = "; ".join(reasons) if reasons else "isolated miss"
            return (
                sev,
                f"✓ RECOVERING on {net} — {joined}; last {self.trailing_clean} rounds clean, "
                f"aging out of the ~1h window (self-clearing)",
                [],
            )
        label, mark = ("CRITICAL", "🔴") if sev == "CRIT" else ("DEGRADED", "⚠")
        joined = "; ".join(reasons) if reasons else "see the report above"
        return (sev, f"{mark} SYSTEM {label} on {net} — {joined}", actions)

    def to_dict(self) -> dict:
        _level, _headline, _actions = self.verdict()
        return {
            "network": self.network,
            "enabled": self.enabled,
            "severity": self.severity,
            "verdict": {"level": _level, "headline": _headline, "actions": _actions},
            "written_at": self.written_at,
            "age_sec": (int(self.age_sec) if self.age_sec is not None else None),
            "stale": self.stale,
            "last_block": self.last_block,
            "head": self.head,
            "stream_state": self.stream_state,
            "lag_blocks": self.lag_blocks,
            "lag_sec": self.lag_sec,
            "gaps": self.gaps or [],
            "open_gaps": len(self.open_gaps),
            "budget": self.budget,
            "delegation": self.delegation,
            "mincond": self.mincond,
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
            "iqr_windows": self.iqr_windows,
            "signatures_seen": self.signatures_seen,
            "fu_windows": self.fu_windows,
            "uptime_pct": self.uptime_pct,
            "uptime_connected": self.uptime_connected,
            "validator_node": self.validator_node,
            "quorum": self.quorum,
            "verify_host": self.verify_host,
            "uptime_verify": self.uptime_verify,
            "quorum_status": self.quorum_status,
            "resources": self.resources,
            "registered": self.registered,
            "reward_epoch": self.reward_epoch,
            "recent_issues": self.recent_issues or [],
            "error": self.error,
        }


def observe_health_from_dict(d: dict, *, enabled_default: bool = True) -> ObserveHealth:
    """Map a status dict (from `build_status`) into an ObserveHealth. Shared by the file reader
    and the engine's own periodic status-log render."""
    return ObserveHealth(
        network=d.get("network", "?"),
        enabled=bool(d.get("enabled", enabled_default)),
        written_at=d.get("written_at"),
        last_block=d.get("last_block"),
        last_ts=d.get("last_ts"),
        head=d.get("head"),
        lag_sec=d.get("lag_sec"),
        gaps=d.get("gaps", []),
        live_lag_blocks=d.get("live_lag_blocks", 8),
        budget=d.get("budget"),
        delegation=d.get("delegation"),
        mincond=d.get("mincond"),
        last_round_finalized=d.get("last_round_finalized"),
        window_rounds=d.get("window_rounds", 0),
        trailing_clean=d.get("trailing_clean", 0),
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
        iqr_windows=d.get("iqr_windows"),
        signatures_seen=d.get("signatures_seen", 0),
        fu_windows=d.get("fu_windows"),
        uptime_pct=d.get("uptime_pct"),
        uptime_connected=d.get("uptime_connected"),
        validator_node=d.get("validator_node"),
        quorum=d.get("quorum"),
        verify_host=d.get("verify_host"),
        uptime_verify=d.get("uptime_verify"),
        quorum_crit=d.get("quorum_crit", False),
        resources=d.get("resources"),
        registered=d.get("registered"),
        reward_epoch=d.get("reward_epoch"),
        recent_issues=d.get("recent_issues", []),
    )


def read_observe_status(path: Path, *, enabled: bool) -> ObserveHealth:
    """Read the engine's status file into an ObserveHealth. Never raises — a missing/corrupt
    file with the observer ENABLED is CRIT (it should be writing); disabled ⇒ a benign OK."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        if not enabled:
            return ObserveHealth(network="?", enabled=False)
        return ObserveHealth(network="?", enabled=True, error=f"no observer status ({exc})")
    return observe_health_from_dict(d, enabled_default=enabled)


_IQR_HORIZONS = (("1h", "1h"), ("6h", "6h"), ("24h", "24h"), ("epoch", "ep"))


def _flr(x: float | None) -> str:
    """Compact token amount: 140.2M, 3.4K, 812."""
    if x is None:
        return "?"
    if x >= 1e6:
        return f"{x / 1e6:.1f}M"
    if x >= 1e3:
        return f"{x / 1e3:.1f}K"
    return f"{x:.0f}"


def _win_pair(w: dict | None) -> str:
    if not w or not w.get("feed_rounds"):
        return "—"
    return f"{w['inner_pct']:g}/{w['outer_pct']:g}"


def render_iqr_windows_compact(h: ObserveHealth) -> str | None:
    """One-line multi-horizon IQR (inner/outer) for the periodic log — None if nothing scored yet."""
    win = h.iqr_windows or {}
    if not any((win.get(k) or {}).get("feed_rounds") for k, _ in _IQR_HORIZONS):
        return None
    parts = " · ".join(f"{lbl} {_win_pair(win.get(k))}" for k, lbl in _IQR_HORIZONS)
    tag = "would-be IQR" if h.registered is False else "IQR"
    return f"{_BADGE_OBS} {tag}[in/out] {parts}"


def render_iqr_windows(h: ObserveHealth) -> list[str]:
    """Detailed per-horizon block for `observe status` (empty list if nothing scored yet)."""
    win = h.iqr_windows or {}
    if not any((win.get(k) or {}).get("feed_rounds") for k, _ in _IQR_HORIZONS):
        return []
    tag = "would-be IQR" if h.registered is False else "IQR"
    lines = [f"  {tag} (inner = primary/IQR band, outer = secondary/PCT band):"]
    for k, lbl in _IQR_HORIZONS:
        w = win.get(k) or {}
        if not w.get("feed_rounds"):
            lines.append(f"    {lbl:>5}  —")
            continue
        lines.append(
            f"    {lbl:>5}  inner {w['inner_pct']:>5}% · outer {w['outer_pct']:>5}%"
            f"  ({w['rounds']} rounds, {w['feed_rounds']} feed-rounds)"
        )
    return lines


def render_protocol_report(h: ObserveHealth) -> list[str]:
    """Explicit per-protocol FSP health block — the hourly report. One line per protocol, from
    the data the observer already tracks. Colour by the overall severity headline."""
    w = h.window_rounds or 0
    ep = f"RE{h.reward_epoch}" if h.reward_epoch is not None else "RE?"
    head_c = _RED if h.severity == "CRIT" else (_YELLOW if h.severity == "WARN" else _GREEN)
    lines = [f"{_BADGE_OBS} {head_c}══ FSP protocol health — {h.network} {ep} (rolling {w} rounds ≈1h) ══{_RESET}"]

    # STREAM state — the load-bearing qualifier: is this report LIVE, or REPLAYING an outage?
    st = h.stream_state
    if st == "catching_up":
        behind = f" / ~{hms(h.lag_sec)} behind" if h.lag_sec else ""
        opn = f" — replaying {len(h.open_gaps)} outage(s)" if h.open_gaps else ""
        lines.append(f"  stream       : {_YELLOW}⏳ CATCHING UP — {h.lag_blocks} blk{behind}{opn} (data is REPLAYED, not live){_RESET}")
    elif st == "live":
        lines.append(f"  stream       : {_GREEN}✓ LIVE{_RESET} (at head, lag {h.lag_blocks} blk)")
    else:
        lines.append("  stream       : · (starting)")

    # registration
    if h.registered is False:
        reg = f"{_RED}✗ NOT REGISTERED — submissions earn ZERO{_RESET}"
    elif h.registered is True:
        reg = f"{_GREEN}✓ registered{_RESET}"
    else:
        reg = "· (unknown)"
    lines.append(f"  registration : {reg}")

    # FTSO (100) — commit / reveal / signatures, broken out
    if w:
        c_seen = w - h.missing_submit1
        r_seen = max(0, w - h.missing_submit1 - h.missing_submit2)
        off = f", {h.off_window} off-window" if h.off_window else ""
        offc = f"{_RED} · {h.reveal_offences} REVEAL OFFENCE{_RESET}" if h.reveal_offences else ""
        lines.append(
            f"  FTSO (100)   : commit {c_seen}/{w} · reveal {r_seen}/{w} · sigs {h.signatures_seen}/{w}"
            f" · clean {h.complete}/{w} ({h.participation_pct}%){off}{offc}"
        )
    else:
        lines.append("  FTSO (100)   : · (warming up)")

    # FDC (200)
    if h.fdc_request_rounds:
        fdc = f"{h.fdc_participated}/{h.fdc_request_rounds} bitvoted ({h.fdc_participation_pct}%)"
        if h.fdc_missing:
            fdc += f"{_YELLOW} · {h.fdc_missing} gap{_RESET}"
    else:
        fdc = "· no attestation requests this window"
    lines.append(f"  FDC (200)    : {fdc}")

    # Fast updates (255)
    fw = h.fu_windows or {}
    if fw.get("total_tracked"):
        fu = f"1h {fw.get('1h', 0)} · 6h {fw.get('6h', 0)} · 24h {fw.get('24h', 0)} updates"
    elif h.registered:
        fu = f"{_YELLOW}0 updates (registered — expected some; sortition-weighted){_RESET}"
    else:
        fu = "0 updates (not registered ⇒ no sortition weight)"
    lines.append(f"  FastUpd (255): {fu}")

    # Validator uptime (P-chain) — Flare-only. NODE-SUBJECTIVE: show both nodes' views, don't "agree".
    if h.validator_node:
        if h.uptime_pct is None and h.uptime_connected is None:
            upl = "· (not yet probed)"
        else:
            conn = f"{_GREEN}connected{_RESET}" if h.uptime_connected else f"{_RED}DISCONNECTED{_RESET}"
            upl = f"our-node {h.uptime_pct:g}% · {conn}"
            if h.uptime_verify and h.uptime_verify[0] is not None:
                upl += f" · verify-node {h.uptime_verify[0]:g}%"
    else:
        upl = "· n/a (no validator on this net)"
    lines.append(f"  uptime       : {upl}")

    # IQR quality horizons (would-be while excluded)
    win = h.iqr_windows or {}
    if any((win.get(k) or {}).get("feed_rounds") for k, _ in _IQR_HORIZONS):
        tag = "would-be IQR" if h.registered is False else "IQR quality"
        seg = " · ".join(f"{lbl} {_win_pair(win.get(k))}" for k, lbl in _IQR_HORIZONS)
        lines.append(f"  {tag} : {seg}  [in/out %]")
    else:
        lines.append("  IQR quality  : · (warming up)")

    # Trust: independent-RPC quorum on the gating reads (registration, epoch, voter-set).
    q = h.quorum or {}
    qs = h.quorum_status
    if qs == "agree":
        n = sum(1 for v in q.values() if (v or {}).get("status") == "agree")
        lines.append(f"  quorum       : {_GREEN}✓ {n} gating facts agree{_RESET} (verify: {h.verify_host})")
    elif qs == "dispute":
        det = "; ".join(
            f"{k} our={v.get('primary')} verify={v.get('verify')}"
            for k, v in q.items() if (v or {}).get("status") == "dispute"
        )
        lines.append(f"  quorum       : {_RED}⚠ DISPUTED — {det}{_RESET} (verify: {h.verify_host})")
    elif qs == "unavailable":
        lines.append(f"  quorum       : {_YELLOW}· verify node unavailable{_RESET} ({h.verify_host})")

    # Minimal-conditions panel — the reward-eligibility floors this epoch, one line: FTSO ≥80 (the
    # "cannot breach 20%" tracker, full-epoch via the Submit nonce delta — restart-proof), FDC ≥60
    # (observed window — bitvotes ride inside submit2, not nonce-countable, so it is NOT full-epoch),
    # uptime ≥80 (P-chain point-in-time). Each metric coloured by its own standing. Fast-updates is
    # NOT a minimal condition (a separate reward stream) — it keeps its own line above.
    b = h.budget or {}
    conds = []
    if b.get("rate_pct") is not None:
        bc = _RED if b["severity"] == "CRIT" else (_YELLOW if b["severity"] == "WARN" else _GREEN)
        eta = f" · ETA ~{b['eta_rounds_to_breach']}r" if b.get("eta_rounds_to_breach") else ""
        conds.append(
            f"{bc}FTSO {b['rate_pct']}% (≥{b['threshold_pct']} · {b['budget_left']}/{b['miss_budget']} budget"
            f" · proj {b['projected_final_pct']}%{eta}){_RESET}"
        )
    mc = h.mincond or {}
    fdc_ep = mc.get("fdc_pct")
    if fdc_ep is not None:  # EXACT full-epoch, gap-free (the per-epoch ledger)
        fc = _GREEN if fdc_ep >= _FDC_CRIT_PCT else (_YELLOW if fdc_ep >= _FDC_MIN_PCT else _RED)
        conds.append(f"{fc}FDC {fdc_ep}% (≥{_FDC_MIN_PCT:g} · {mc.get('fdc_participated')}/{mc.get('fdc_expected')} epoch){_RESET}")
    elif h.fdc_request_rounds:  # fall back to the rolling window until the ledger has data
        fp = h.fdc_participation_pct or 0.0
        fc = _GREEN if fp >= _FDC_CRIT_PCT else (_YELLOW if fp >= _FDC_MIN_PCT else _RED)
        conds.append(f"{fc}FDC {h.fdc_participation_pct}% (≥{_FDC_MIN_PCT:g} · 1h obs){_RESET}")
    if h.validator_node and h.uptime_pct is not None:
        uc = _GREEN if h.uptime_pct >= _UPTIME_MIN_PCT else _RED
        conds.append(f"{uc}uptime {h.uptime_pct:g}% (≥{_UPTIME_MIN_PCT:g}){_RESET}")
    if mc.get("fu_updates") is not None:  # cumulative epoch fast-updates (volume, not a hard floor)
        conds.append(f"\033[2mFU {mc['fu_updates']} (epoch){_RESET}")
    if conds:
        prog = f"  [ep {b['rounds_elapsed']}/{b['rounds_total']}]" if b.get("rounds_total") else ""
        lines.append(f"  min-cond     : {' · '.join(conds)}{prog}")

    # Live delegation — validator (P-chain) + FTSO (WNat vote power).
    d = h.delegation or {}
    if d:
        segs = []
        v = d.get("validator")
        if v:
            segs.append(
                f"validator {_flr(v['total'])} ({_flr(v['self_bond'])} self + {_flr(v['delegated'])} "
                f"by {v['delegators']} dels, {v['fee_pct']:g}% fee)"
            )
        f = d.get("ftso")
        if f:
            segs.append(f"FTSO {_flr(f['vote_power'])} vote power")
        if segs:
            lines.append(f"  delegation   : {' · '.join(segs)}")
        # 24h / reward-epoch deltas — is stake flowing IN (green) or OUT (red)?
        # Colour-coded by direction so an outflow is impossible to skim past; flat
        # or no-baseline-yet reads are dimmed (not an event).
        dl = d.get("deltas") or {}
        if dl:
            _DIM = "\033[2m"

            def _col(x) -> str:
                if x is None:
                    return _DIM
                return _GREEN if x > 0 else (_RED if x < 0 else _DIM)

            def _amt(x):
                if x is None:
                    return f"{_DIM}n/a{_RESET}"
                s = f"+{_flr(x)}" if x >= 0 else f"-{_flr(-x)}"
                return f"{_col(x)}{s}{_RESET}"

            def _cnt(x):
                return f"{_DIM}n/a{_RESET}" if x is None else f"{_col(x)}{x:+d}{_RESET}"

            for horizon, lbl in (("h24", "24h"), ("epoch", "epoch")):
                vd = (dl.get("val_delegated") or {}).get(horizon)
                vc = (dl.get("val_dels") or {}).get(horizon)
                fv = (dl.get("ftso_vp") or {}).get(horizon)
                if vd is None and fv is None:
                    continue
                lines.append(
                    f"  Δ {lbl:<9}: validator {_amt(vd)} ({_cnt(vc)} dels) · FTSO {_amt(fv)}"
                )

    # Outage/backfill ledger — every recorded RPC outage + whether it's been backfilled, so a gap
    # in coverage is never silent. `backfilled ✓` = the observer replayed those blocks from chain.
    gaps = h.gaps or []
    if gaps:
        parts = []
        for g in gaps[-3:]:
            rng = f"{time.strftime('%m-%d %H:%M', time.gmtime(g['start']))}–{time.strftime('%H:%M', time.gmtime(g['end']))}"
            if g.get("skipped"):
                mk = "⏭ skipped"
            elif (h.last_block or 0) >= g.get("to_block", 0):
                mk = "✓"
            else:
                mk = "⏳"
            parts.append(f"{rng} UTC ({hms(g['dur'])}) {mk}")
        tail = "" if len(gaps) <= 3 else f" (+{len(gaps) - 3} older)"
        c = _GREEN if not h.open_gaps else _YELLOW
        lines.append(f"  outages (7d) : {c}{len(gaps)} — {'; '.join(parts)}{tail}{_RESET}")

    # Resource gauge — bounded-collection sizes + RSS; a leak shows as a trend here first.
    r = h.resources or {}
    if r:
        lines.append(
            f"  resources    : in-flight-rounds {r.get('in_flight_rounds', '?')} · "
            f"iqr-hist {r.get('iqr_hist', '?')} · fu-events {r.get('fu_events', '?')} · "
            f"rss {r.get('rss_mib', '?')} MiB"
        )

    # ── Bottom line — the proclamation, and a call to action when needed. The one line to read.
    level, headline, actions = h.verdict()
    vc = _RED if level == "CRIT" else (_YELLOW if level == "WARN" else _GREEN)
    lines.append(f"  {'─' * 64}")
    lines.append(f"  VERDICT      : {vc}{headline}{_RESET}")
    for a in actions:
        lines.append(f"  → ACTION     : {vc}{a}{_RESET}")
    return lines


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
