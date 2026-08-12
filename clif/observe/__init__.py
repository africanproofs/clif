"""clif observe — per-block protocol-participation observer (the fsp-observer native port).

Closes the gap RE423 generalised: clif watched only the ~3.5-day reward-epoch layer, blind
to the per-90s-voting-round FTSO submissions that actually earn rewards. This package streams
blocks and tracks whether AP's own submit / signatures addresses are participating on-chain
each round — on-time, and (for reveals) matching their commit — surfacing the result in the
clif idiom (never-green-on-error severity + module badge + status/run + --json + MCP).

Byte-level parsing + epoch timing come from `py-flare-common` (the Foundation's own library,
the exact one fsp-observer uses) — see memory `py-flare-common-fsp-parser`. This package writes
only the streaming engine, per-round state + checks, and the clif surface. OBSERVE-only: it
reads and classifies, never signs or sends.

Phase 1 (this build): FTSO submit1/submit2/submitSignatures participation + timing + reveal
offence (commit-hash mismatch). Phase 2+ (marked TODO): minimal-conditions value checks
(need all-voter medians), signature-vs-finalization recovery, FDC, fast-updates, uptime.
"""

from clif.observe.health import ObserveHealth, read_observe_status, render_observe

__all__ = ["ObserveHealth", "read_observe_status", "render_observe"]
