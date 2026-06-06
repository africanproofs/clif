"""FSP automation state — thin wrapper around autostate primitives.

autostate.py's AutoState / write_status_atomic / read_status / status_exit_code
are generic (no claim-specific wording). The only claim-specific helper is
stream_key() and build_report(). This module provides FSP equivalents so the
existing autostate.py is untouched (per the prompt constraint).
"""

from __future__ import annotations

import time

from clif.autostate import AutoState, _DEAD_INTERVALS, EXIT_DEGRADED, EXIT_HEALTHY, EXIT_NO_STATE, _ts_iso


def fsp_stream_key(network: str, message_type: str) -> str:
    """FSP stream key: fsp:{network}:{message_type}."""
    return f"fsp:{network}:{message_type}"


def build_fsp_report(
    state: AutoState,
    network: str,
    poll_interval_sec: int,
    stale_after_sec: int,
    now: float,
) -> dict:
    """Parallel to autostate.build_report but uses FSP stream keys."""
    degraded, reasons = state.evaluate(now, stale_after_sec)
    streams = []
    for key, s in state.streams.items():
        streams.append(
            {
                "stream": key,
                "pending_epochs": sorted(s.first_seen),
                # OBS-007: ISO8601 timestamps for human/tooling readability.
                "last_success_ts": _ts_iso(s.last_success_ts),
                "last_attempt_ts": _ts_iso(s.last_attempt_ts),
                "last_outcome": s.last_outcome,
            }
        )
    return {
        # OBS-007: ISO8601 timestamp for updated_at.
        "updated_at": _ts_iso(now),
        "updated_at_ts": now,  # retain raw float for staleness arithmetic
        "network": network,
        "poll_interval_sec": poll_interval_sec,
        "stale_after_sec": stale_after_sec,
        "degraded": degraded,
        "reasons": reasons,
        "streams": streams,
    }


def fsp_status_exit_code(report: dict | None, now: float | None = None) -> tuple[int, str]:
    """Map an FSP status report to (exit_code, human_line) for `clif fsp status`."""
    if report is None:
        return EXIT_NO_STATE, "no FSP daemon status found (clif fsp auto has not run)"
    now = time.time() if now is None else now
    interval = int(report.get("poll_interval_sec", 900))
    # Support both the new updated_at_ts (float) and old updated_at (float) fields;
    # updated_at is now an ISO8601 string (OBS-007) so fall back to updated_at_ts.
    raw_ts = report.get("updated_at_ts") or report.get("updated_at", 0.0)
    try:
        age = now - float(raw_ts)
    except (TypeError, ValueError):
        age = float("inf")
    if age > _DEAD_INTERVALS * interval:
        return (
            EXIT_DEGRADED,
            f"FSP daemon status is stale ({int(age)}s old > "
            f"{_DEAD_INTERVALS}x{interval}s) — clif fsp auto is dead or stuck",
        )
    if report.get("degraded"):
        return EXIT_DEGRADED, "DEGRADED: " + "; ".join(report.get("reasons", []))
    return EXIT_HEALTHY, "healthy"
