"""Decode a Submission transaction into a normalized FTSO record (py-flare-common does the bytes).

Dispatch is by the 4-byte function selector (grounded live on Songbird), mirroring
fsp-observer/observer.py:1000-1065. OBSERVE-only pure decode; never raises (a malformed or
non-FTSO tx → None), so the streaming engine can't be killed by one odd transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from py_flare_common.fsp.messaging import (
    parse_generic_tx,
    parse_submit1_tx,
    parse_submit2_tx,
    parse_submit_signature_tx,
)
from py_flare_common.fsp.messaging.byte_parser import ByteParser

# Submission function selectors (keccak[:4]) — grounded on Songbird 2026-08-12.
SUBMIT1 = "6c532fae"
SUBMIT2 = "9d00c9fd"
SUBMITSIG = "57eed580"
_KIND = {SUBMIT1: "submit1", SUBMIT2: "submit2", SUBMITSIG: "signatures"}


@dataclass(frozen=True)
class Decoded:
    kind: str  # "submit1" | "submit2" | "signatures"
    round_id: int  # the FTSO voting_round_id from the payload
    commit_hash: bytes | None = None  # submit1
    reveal_random: int | None = None  # submit2 — for commit-hash reconstruction
    reveal_feed_bytes: bytes | None = None  # submit2 — raw feed values (bytes) for commit_hash
    reveal_value_count: int | None = None  # submit2 — number of parsed feed values


def decode_submit(tx_input: str) -> Decoded | None:
    """Decode a Submission tx input. Returns None unless it is a submit1/2/signatures carrying
    an FTSO (protocol 100) payload. The reveal (submit2) additionally extracts the raw
    random + feed bytes needed to reconstruct the commit hash (fsp-observer ftso.py:143-147)."""
    inp = tx_input[2:] if tx_input.startswith("0x") else tx_input
    if len(inp) < 8:
        return None
    sel = inp[:8].lower()
    kind = _KIND.get(sel)
    if kind is None:
        return None
    body = inp[8:]
    try:
        if kind == "submit1":
            pm = parse_submit1_tx(body)
            if pm.ftso is None:
                return None
            return Decoded("submit1", pm.ftso.voting_round_id, commit_hash=pm.ftso.payload.commit_hash)
        if kind == "submit2":
            pm = parse_submit2_tx(body)
            if pm.ftso is None:
                return None
            # Raw random + feed bytes from the generic envelope (commit_hash wants bytes,
            # not the parsed .values list) — exactly as fsp-observer reconstructs it.
            g = parse_generic_tx("0x" + sel + body)
            bp = ByteParser(g.ftso.payload)
            rnd = bp.uint256()
            feed_v = bp.drain()
            return Decoded(
                "submit2", pm.ftso.voting_round_id,
                reveal_random=rnd, reveal_feed_bytes=feed_v,
                reveal_value_count=len(pm.ftso.payload.values),
            )
        if kind == "signatures":
            pm = parse_submit_signature_tx(body)
            if pm.ftso is None:  # FDC-only signatures ignored in Phase 1 (FTSO focus)
                return None
            return Decoded("signatures", pm.ftso.voting_round_id)
    except Exception:  # noqa: BLE001 — a malformed tx is not our problem; skip it
        return None
    return None
