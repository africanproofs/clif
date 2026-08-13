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

from py_flare_common.fsp.messaging import parse_submit2_tx

from clif.observe.decode import decode_submit
from clif.observe.health import build_status, observe_health_from_dict, render_observe
from clif.observe.iqr import build_voter_weight_map
from clif.observe.reward_rule import get_offer_params
from clif.observe.state import ObserverState
from clif.observe.timing import voting_factory
from clif.rpc import RpcClient, RpcError


def _blk_int(v) -> int:
    return int(str(v), 16)


# keccak256("AttestationRequest(bytes,uint256)") — FdcHub's per-request event topic0.
_ATTESTATION_REQUEST_TOPIC = "0x251377668af6553101c9bb094ba89c0c536783e005e203625e6cd57345918cc9"
_SUBMIT2_SELECTOR = "9d00c9fd"


def run_engine(
    *,
    rpc: RpcClient,
    network: str,
    submission_address: str,
    our_submit: str,
    our_sig: str,
    status_writer: Callable[[dict], None],
    # Lookback must exceed ~270s + N*90s to SEED N rounds at startup: the boundary guard
    # drops rounds begun before we started, and finalize lags a round's start by ~180s, so
    # the effective seed window = span − 180s − one boundary round. ~900 blocks (~15min on
    # Songbird) seeds ~8-10 rounds; the window then fills forward at ~1/90s to window_rounds.
    lookback_blocks: int = 900,
    window_rounds: int = 40,
    poll_sec: float = 2.0,
    status_every_blocks: int = 100,
    voter_registry: str | None = None,
    flare_systems_manager: str | None = None,
    identity: str | None = None,
    registration_refresh_sec: float = 3600.0,  # registration changes per reward epoch (~3.5d)
    status_log_sec: float = 3600.0,  # emit a rendered OBS status line to the log this often (+once seeded)
    fdc_hub: str | None = None,  # FdcHub address — enables FDC participation tracking when set
    entity_manager: str | None = None,  # with voter_registry + FSM ⇒ enables IQR reward-band scoring
    iqr_cache_dir: str | None = None,  # per-epoch offer-params disk cache (default: no disk cache)
    iqr_enabled: bool = True,  # gate the (per-block all-voter reveal decode) IQR scoring
    log=None,
    _max_blocks: int | None = None,  # test hook: process at most N blocks then return
) -> None:
    """Run the streaming observer. Blocks forever (until KeyboardInterrupt) unless `_max_blocks`
    is set (tests). `status_writer` persists the status dict each catch-up + each idle poll."""
    factory = voting_factory(network)
    submission = submission_address.lower()
    our_submit_lc = our_submit.lower()
    our_sig_lc = our_sig.lower()
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
    # Registration overlay — hourly (registration only changes per reward epoch). We ARE
    # submitting every round; if AP is NOT in the registered voter set for the current
    # reward epoch, all those clean submissions earn ZERO (the RE423 blind spot). Best-effort:
    # a probe failure leaves the last value (never breaks the engine).
    reg = {"registered": None, "epoch": None, "checked": 0.0}

    def _refresh_registration(now: float) -> None:
        if not (voter_registry and flare_systems_manager and identity):
            return
        if reg["epoch"] is not None and now - reg["checked"] < registration_refresh_sec:
            return
        try:
            ep = rpc.get_current_reward_epoch_id(flare_systems_manager)
            reg["registered"] = rpc.is_voter_registered(voter_registry, identity, ep)
            reg["epoch"] = ep
            reg["checked"] = now
        except RpcError:
            pass  # keep the last known value

    # IQR reward-band scoring overlay — per reward epoch (offer band params + voter→weight map,
    # both disk-cached), then AP's inner/outer band hit rates are scored natively at finalize
    # (median from ALL registered voters' reveals). Best-effort: if the offer/weight can't be
    # resolved we keep observing without IQR (state.iqr_offer stays None ⇒ scoring simply off).
    iqr_on = bool(iqr_enabled and voter_registry and entity_manager and flare_systems_manager)
    iqr = {"epoch": None, "checked": 0.0, "ready": False}

    def _refresh_iqr(now: float) -> None:
        if not iqr_on:
            return
        if iqr["epoch"] is not None and now - iqr["checked"] < registration_refresh_sec:
            return
        try:
            ep = rpc.get_current_reward_epoch_id(flare_systems_manager)
            if ep == iqr["epoch"] and state.iqr_offer is not None:
                iqr["checked"] = now
                return
            offer = get_offer_params(rpc, network, ep, cache_dir=iqr_cache_dir)
            wmap = build_voter_weight_map(
                rpc, voter_registry=voter_registry, entity_manager=entity_manager, epoch=ep
            )
            state.set_iqr_context(offer, wmap)
            iqr.update(epoch=ep, checked=now, ready=True)
            if log:
                log.info(
                    "\033[38;5;208m OBS\033[0m IQR scoring on for %s epoch %s (%d feeds, %d voters)",
                    network, ep, len(offer.feeds), len(wmap),
                )
        except Exception as exc:  # noqa: BLE001 — offer scan / weight map are best-effort
            if log and not iqr["ready"]:
                log.warning("IQR context unavailable (%s) — band scoring off this cycle", exc)

    def _status() -> dict:
        return build_status(
            state, network=network, enabled=True,
            registered=reg["registered"], reward_epoch=reg["epoch"],
        )

    # Periodic self-report: the observer otherwise only logs issues, so its participation +
    # (would-be) IQR quality never showed up in `clifctl logs` alongside REG/FUND/EPCH. Emit
    # the rendered OBS line once the window is seeded, then every `status_log_sec` — at the
    # severity-appropriate level so a healthy line is INFO and an excluded/degraded one stands out.
    slog = {"last": 0.0}

    def _maybe_log_status(now: float, *, force: bool = False) -> None:
        if not log or (not force and now - slog["last"] < status_log_sec):
            return
        slog["last"] = now
        h = observe_health_from_dict(_status())
        line = render_observe(h, active=(h.severity == "CRIT"))
        lvl = log.error if h.severity == "CRIT" else (log.warning if h.severity == "WARN" else log.info)
        lvl(line)

    # Write a status immediately so `observe status` shows "warming up", not a missing-file CRIT.
    state.last_block = cursor
    _refresh_registration(time.time())
    _refresh_iqr(time.time())
    status_writer(_status())
    processed = 0
    since_status = 0
    seeded = False
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
                inp = tx.get("input", "0x")
                if frm == our_submit_lc or frm == our_sig_lc:
                    d = decode_submit(inp)
                    if d is not None:
                        state.record(d, frm, ts, factory)
                # IQR: fold AP's + every registered voter's submit2 reveal into the round's
                # weighted median inputs (scored at finalize). Best-effort per-tx parse.
                if (
                    state.iqr_offer is not None and inp[2:10] == _SUBMIT2_SELECTOR
                    and (frm == our_submit_lc or frm in state.iqr_weight_map)
                ):
                    try:
                        pm = parse_submit2_tx(inp[10:])
                        if pm.ftso is not None:
                            state.record_reveal_values(
                                pm.ftso.voting_round_id, frm, pm.ftso.payload.values
                            )
                    except Exception:  # noqa: BLE001
                        pass
            # FDC: count this block's AttestationRequests into the block's voting round
            # (best-effort — an FDC-scan hiccup must never break the FTSO path).
            if fdc_hub:
                try:
                    logs = rpc.get_logs(fdc_hub, [_ATTESTATION_REQUEST_TOPIC], cursor, cursor)
                    if logs:
                        rid = factory.from_timestamp(ts).id
                        for _ in logs:
                            state.record_fdc_request(rid)
                except RpcError:
                    pass
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
                status_writer(_status())
                since_status = 0
            if _max_blocks is not None and processed >= _max_blocks:
                status_writer(_status())
                return
        _refresh_registration(time.time())
        _refresh_iqr(time.time())
        status_writer(_status())
        since_status = 0
        # First time we're fully caught up, the lookback window is seeded (rounds finalized +
        # IQR scored) — emit the opening status line; thereafter it self-reports hourly.
        if not seeded and cursor > head:
            seeded = True
            _maybe_log_status(time.time(), force=True)
        else:
            _maybe_log_status(time.time())
        try:
            time.sleep(poll_sec)
        except KeyboardInterrupt:
            if log:
                log.info("observe stopped")
            return
