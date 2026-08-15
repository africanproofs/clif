"""Independent-RPC cross-verification — turn "true if you trust our node" into "≥2 nodes agree".

The observer's per-block streaming is single-RPC (impractical to double, and its verdicts are
recomputable + final on Avalanche). But the LOW-FREQUENCY gating reads that back the report's
assertions — is-registered, the current reward epoch, the registered-voter set — are read once an
hour, so cross-checking each against a second, INDEPENDENT node is cheap. A mismatch is surfaced as
DISPUTED rather than silently trusted. Reads only; holds no key.
"""

from __future__ import annotations

from collections.abc import Callable

from clif.rpc import RpcClient, RpcError


def _short(x: object) -> str:
    """Compact repr for the status JSON (don't dump a 100-address list)."""
    if isinstance(x, (list, tuple, set)):
        return f"n={len(x)}"
    return str(x)[:48]


class CrossVerifier:
    """Wraps the verify (independent) RpcClient. `compare` runs the same read there and classifies
    the outcome vs the primary node's value: agree / dispute / unavailable (verify node down —
    NOT a dispute, we just don't know)."""

    def __init__(self, rpc: RpcClient, host: str) -> None:
        self.rpc = rpc
        self.host = host

    def compare(
        self, primary: object, reader: Callable[[RpcClient], object],
        *, key: Callable[[object], object] | None = None,
    ) -> dict:
        k = key or (lambda v: v)
        try:
            other = reader(self.rpc)
        except RpcError:
            return {"status": "unavailable"}
        if k(other) == k(primary):
            return {"status": "agree"}
        return {"status": "dispute", "primary": _short(primary), "verify": _short(other)}


def quorum_overall(results: dict) -> str:
    """Roll a {fact: {status,...}} map into one verdict: dispute > unavailable > agree > none.
    'dispute' if ANY fact disagrees; else 'agree' if any confirmed; 'unavailable' if the verify
    node answered nothing; 'off' when quorum isn't configured (empty map)."""
    if not results:
        return "off"
    statuses = {r.get("status") for r in results.values()}
    if "dispute" in statuses:
        return "dispute"
    if "agree" in statuses:
        return "agree"
    return "unavailable"
