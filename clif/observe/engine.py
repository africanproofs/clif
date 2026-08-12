"""The per-block streaming engine — the fsp-observer loop, clif-native + keyless.

For each block: pull it with full transactions, keep only txs TO the Submission contract FROM
one of AP's own submit/signatures addresses, decode the FTSO submission, and record it into its
voting round. Rounds finalize once their windows close; the FTSO checks run then. A rolling
window of finalized rounds feeds `ObserveHealth`, written to a status file each cycle.

Stateless across restarts: re-syncs from `head - lookback_blocks` (a couple of rounds back) so
a restart re-observes recent rounds. OBSERVE-only — reads, classifies, never signs or sends.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from clif.observe.decode import decode_submit
from clif.observe.health import build_status
from clif.observe.state import ObserverState
from clif.observe.timing import voting_factory
from clif.rpc import RpcClient, RpcError


def _blk_int(v) -> int:
    return int(str(v), 16)


def run_engine(
    *,
    rpc: RpcClient,
    network: str,
    submission_address: str,
    our_submit: str,
    our_sig: str,
    status_writer: Callable[[dict], None],
    lookback_blocks: int = 250,
    window_rounds: int = 40,
    poll_sec: float = 2.0,
    status_every_blocks: int = 100,
    log=None,
    _max_blocks: int | None = None,  # test hook: process at most N blocks then return
) -> None:
    """Run the streaming observer. Blocks forever (until KeyboardInterrupt) unless `_max_blocks`
    is set (tests). `status_writer` persists the status dict each catch-up + each idle poll."""
    factory = voting_factory(network)
    submission = submission_address.lower()
    head = rpc.block_number()
    cursor = max(1, head - lookback_blocks)
    # Anchor the boundary-round guard to the first block's timestamp (rounds that opened
    # before this are incomplete-by-construction and won't be counted).
    _first = rpc.get_block(cursor, full_transactions=False)
    start_ts = _blk_int(_first.get("timestamp", "0x0")) if _first else 0
    state = ObserverState(network, our_submit, our_sig, window_rounds=window_rounds, observe_start_ts=start_ts)
    if log:
        log.info(
            "observe start network=%s from block %s (head %s, lookback %s) submission=%s",
            network, cursor, head, lookback_blocks, submission,
        )
    # Write a status immediately so `observe status` shows "warming up", not a missing-file CRIT.
    state.last_block = cursor
    status_writer(build_status(state, network=network, enabled=True))
    processed = 0
    since_status = 0
    while True:
        try:
            head = rpc.block_number()
        except RpcError as exc:
            if log:
                log.error("\033[1;31m🔴 observe head read failed: %s\033[0m", exc)
            time.sleep(poll_sec)
            continue
        while cursor <= head:
            try:
                blk = rpc.get_block(cursor, full_transactions=True)
            except RpcError as exc:
                if log:
                    log.warning("observe block %s read failed: %s (retry)", cursor, exc)
                time.sleep(poll_sec)
                break
            if blk is None:
                break
            ts = _blk_int(blk.get("timestamp", "0x0"))
            for tx in blk.get("transactions", []):
                if (tx.get("to") or "").lower() != submission:
                    continue
                frm = (tx.get("from") or "").lower()
                if frm != our_submit.lower() and frm != our_sig.lower():
                    continue
                d = decode_submit(tx.get("input", "0x"))
                if d is not None:
                    state.record(d, frm, ts, factory)
            state.last_block = cursor
            state.last_ts = ts
            for rs in state.finalize_due(ts, factory):
                if rs.issues and log:
                    lvl = log.error if rs.reveal_offence else log.warning
                    lvl("\033[38;5;208m OBS\033[0m round %s: %s", rs.round_id, "; ".join(rs.issues))
            cursor += 1
            processed += 1
            since_status += 1
            # Keep the status fresh DURING a long catch-up so `observe status` isn't stale.
            if since_status >= status_every_blocks:
                status_writer(build_status(state, network=network, enabled=True))
                since_status = 0
            if _max_blocks is not None and processed >= _max_blocks:
                status_writer(build_status(state, network=network, enabled=True))
                return
        status_writer(build_status(state, network=network, enabled=True))
        since_status = 0
        try:
            time.sleep(poll_sec)
        except KeyboardInterrupt:
            if log:
                log.info("observe stopped")
            return
