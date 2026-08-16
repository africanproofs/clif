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
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from py_flare_common.fsp.messaging import parse_submit2_tx

from clif.observe.decode import decode_submit
from clif.observe.health import (
    build_status,
    observe_health_from_dict,
    render_protocol_report,
)
from clif.observe.budget import read_ftso_budget
from clif.observe.delegation import read_delegation
from clif.observe.gaps import Gap, append_gap, hms, load_gaps, prune_gaps
from clif.observe.iqr import build_voter_weight_map
from clif.observe.iqr_history import append_tally, load_history, prune_history
from clif.observe.reward_rule import VRS_PER_REWARD_EPOCH, get_offer_params, prune_offer_cache
from clif.observe.state import ObserverState
from clif.observe.timing import voting_factory
from clif.observe.verify import CrossVerifier
from clif.rpc import RpcClient, RpcError


def _blk_int(v) -> int:
    return int(str(v), 16)


# keccak256("AttestationRequest(bytes,uint256)") — FdcHub's per-request event topic0.
_ATTESTATION_REQUEST_TOPIC = "0x251377668af6553101c9bb094ba89c0c536783e005e203625e6cd57345918cc9"
_SUBMIT2_SELECTOR = "9d00c9fd"
# keccak256("FastUpdateFeedsSubmitted(uint32,address)") — FastUpdater's per-submission event;
# topic[2] = the submitter's signingPolicyAddress (indexed), so AP's updates filter directly.
_FAST_UPDATE_TOPIC = "0x63db91b14b3d088c677f046180aefcea7a236649704d90ce810cde455d38d936"


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
    confirmations: int = 0,  # stay this many blocks behind the tip (finality defense-in-depth)
    live_lag_blocks: int = 8,  # ≤ this behind head ⇒ LIVE; more ⇒ CATCHING UP (backfilling an outage)
    gaps_file: str | None = None,  # persist the outage/backfill ledger (survives restart)
    status_log_sec: float = 3600.0,  # emit a rendered OBS status line to the log this often (+once seeded)
    fdc_hub: str | None = None,  # FdcHub address — enables FDC participation tracking when set
    ap_signing_policy: str | None = None,  # AP's signingPolicyAddress — enables fast-update (255) tracking
    validator_node_id: str | None = None,  # AP's P-chain NodeID — enables the validator uptime check
    delegation_addr: str | None = None,  # AP's FTSO delegation address — enables the FTSO delegation read
    entity_manager: str | None = None,  # with voter_registry + FSM ⇒ enables IQR reward-band scoring
    verify_rpc_url: str | None = None,  # independent RPC for cross-verifying gating reads (quorum)
    quorum_crit: bool = False,  # a DISPUTED gating fact ⇒ CRIT severity (else WARN)
    iqr_cache_dir: str | None = None,  # per-epoch offer-params disk cache (default: no disk cache)
    iqr_enabled: bool = True,  # gate the (per-block all-voter reveal decode) IQR scoring
    iqr_history_file: str | None = None,  # persist per-round IQR tallies (multi-horizon; survives restart)
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
    state = ObserverState(
        network, our_submit, our_sig, window_rounds=window_rounds,
        observe_start_ts=start_ts, factory=factory,
    )
    if log:
        log.info(
            "observe start network=%s from block %s (head %s, lookback %s) submission=%s",
            network, cursor, head, lookback_blocks, submission,
        )
    # Independent-RPC quorum: cross-check the low-frequency gating reads (registration, reward
    # epoch, registered-voter set) against a second, INDEPENDENT node. A mismatch → DISPUTED in the
    # report. Its own long-lived client; closed at the (test/KeyboardInterrupt) exits.
    vrpc = RpcClient(verify_rpc_url) if verify_rpc_url else None
    verifier = CrossVerifier(vrpc, urlparse(verify_rpc_url).hostname or verify_rpc_url) if vrpc else None
    quorum: dict = {}
    if verifier and log:
        log.info("\033[38;5;208m OBS\033[0m quorum on — cross-verifying gating reads vs %s", verifier.host)

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
            if verifier:  # cross-verify the two gating facts against the independent node
                quorum["reward_epoch"] = verifier.compare(
                    ep, lambda r: r.get_current_reward_epoch_id(flare_systems_manager)
                )
                quorum["registration"] = verifier.compare(
                    reg["registered"], lambda r: r.is_voter_registered(voter_registry, identity, ep)
                )
        except RpcError:
            pass  # keep the last known value

    # Fast-updates (protocol 255): FastUpdater emits FastUpdateFeedsSubmitted per submission; we
    # filter AP's by its signingPolicyAddress (indexed topic[2]). Resolve the contract once.
    fast_updater = None
    if ap_signing_policy:
        try:
            fu = rpc.contract_address_by_name("FastUpdater")
            fast_updater = fu if fu and int(fu, 16) != 0 else None
        except RpcError:
            fast_updater = None
    spa_topic = "0x" + "0" * 24 + ap_signing_policy[2:].lower() if ap_signing_policy else None

    # Validator uptime (P-chain) — hourly poll of platform.getCurrentValidators (Flare-only; AP
    # runs no Songbird validator). Best-effort: a probe failure keeps the last value.
    up = {"pct": None, "connected": None, "checked": 0.0}

    # Per-epoch minimal-conditions budget (FTSO 80% via Submit nonce-delta) + live delegation
    # (validator + FTSO) — both slow-changing, refreshed hourly. Best-effort; a failure keeps last.
    bud: dict = {"data": None, "checked": 0.0}
    deleg: dict = {"data": None, "checked": 0.0}

    def _refresh_budget(now: float) -> None:
        if bud["checked"] and now - bud["checked"] < registration_refresh_sec:
            return
        try:  # best-effort — an overlay read must NEVER crash the streaming engine
            bud["data"] = read_ftso_budget(rpc, submit_addr=our_submit, factory=factory)
            bud["checked"] = now
        except Exception:  # noqa: BLE001
            pass

    def _refresh_delegation(now: float) -> None:
        if not (validator_node_id or delegation_addr):
            return
        if deleg["checked"] and now - deleg["checked"] < registration_refresh_sec:
            return
        try:  # best-effort — never crash the engine
            deleg["data"] = read_delegation(
                rpc, network=network, node_id=validator_node_id, delegation_addr=delegation_addr
            )
            deleg["checked"] = now
        except Exception:  # noqa: BLE001
            pass

    def _refresh_uptime(now: float) -> None:
        if not validator_node_id:
            return
        if up["checked"] and now - up["checked"] < registration_refresh_sec:
            return
        try:
            res = rpc.validator_uptime(validator_node_id)
            if res is not None:
                up["pct"], up["connected"] = res
            up["checked"] = now
            if verifier:  # uptime is NODE-SUBJECTIVE — report the verify node's view too, don't "agree"
                try:
                    up["verify"] = verifier.rpc.validator_uptime(validator_node_id)
                except RpcError:
                    up["verify"] = None
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
            if verifier:  # cross-verify the registered-voter set that the IQR median is built from
                quorum["voter_set"] = verifier.compare(
                    rpc.get_registered_voters(voter_registry, ep),
                    lambda r: r.get_registered_voters(voter_registry, ep),
                    key=lambda v: sorted(a.lower() for a in v),
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

    # FDC + fast-update events are scanned in 30-block CHUNKS (get_logs' cap), not per block —
    # a per-block get_logs made startup catch-up crawl (900 blocks × 2 extra calls). Each event
    # is attributed via its own block's timestamp (from the per-block `ts_map` we already build).
    def _flush_events(lo: int, hi: int, ts_map: dict[int, int]) -> None:
        if lo > hi:
            return
        if fdc_hub:
            try:
                for lv in rpc.get_logs(fdc_hub, [_ATTESTATION_REQUEST_TOPIC], lo, hi):
                    bts = ts_map.get(_blk_int(lv.get("blockNumber", "0x0")))
                    if bts is not None:
                        state.record_fdc_request(factory.from_timestamp(bts).id)
            except RpcError:
                pass
        if fast_updater and spa_topic:
            try:
                for lv in rpc.get_logs(fast_updater, [_FAST_UPDATE_TOPIC, None, spa_topic], lo, hi):
                    bts = ts_map.get(_blk_int(lv.get("blockNumber", "0x0")))
                    if bts is not None:
                        state.record_fast_update(bts)
            except RpcError:
                pass

    def _status() -> dict:
        return build_status(
            state, network=network, enabled=True,
            registered=reg["registered"], reward_epoch=reg["epoch"],
            uptime_pct=up["pct"], uptime_connected=up["connected"], validator_node=validator_node_id,
            quorum=quorum or None, verify_host=(verifier.host if verifier else None),
            uptime_verify=up.get("verify"), quorum_crit=quorum_crit,
            gaps=[asdict(g) for g in gap_list[-8:]], live_lag_blocks=live_lag_blocks,
            budget=bud["data"], delegation=deleg["data"],
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
        # The explicit, per-protocol FSP health report (registration, FTSO commit/reveal/sigs,
        # FDC, fast-updates, uptime, IQR) — logged at the overall-severity level.
        lvl = log.error if h.severity == "CRIT" else (log.warning if h.severity == "WARN" else log.info)
        for ln in render_protocol_report(h):
            lvl(ln)
        ep_hist = iqr["epoch"] if iqr["epoch"] is not None else reg["epoch"]
        if iqr_history_file:  # bound the persisted log on the same (hourly) cadence
            prune_history(
                Path(iqr_history_file), now_ts=state.last_ts or int(now),
                reward_epoch=ep_hist, vrs_per_epoch=VRS_PER_REWARD_EPOCH,
            )
        if iqr_cache_dir and ep_hist is not None:  # bound the offer-params cache file count
            prune_offer_cache(iqr_cache_dir, network, ep_hist)
        if gaps_file:  # bound the outage ledger to ~7 days
            prune_gaps(Path(gaps_file), now=int(now))

    # Write a status immediately so `observe status` shows "warming up", not a missing-file CRIT.
    state.last_block = cursor
    _refresh_registration(time.time())
    _refresh_iqr(time.time())
    _refresh_uptime(time.time())
    _refresh_budget(time.time())
    _refresh_delegation(time.time())
    # Seed the multi-horizon IQR history from the persisted log (so 24h / since-epoch survive a
    # restart). Scope to the now-known reward epoch; deduped on load + against re-finalized rounds.
    if iqr_history_file:
        ep_hist = iqr["epoch"] if iqr["epoch"] is not None else reg["epoch"]
        state.seed_iqr_history(
            load_history(
                Path(iqr_history_file), now_ts=start_ts, reward_epoch=ep_hist,
                vrs_per_epoch=VRS_PER_REWARD_EPOCH,
            )
        )
        if log and state.iqr_history:
            log.info(
                "\033[38;5;208m OBS\033[0m IQR history seeded: %d rounds from %s",
                len(state.iqr_history), iqr_history_file,
            )
    # Outage/backfill ledger — records each outage so the surface can say LIVE vs CATCHING UP
    # unambiguously and tabulate what was replayed. `catching_up` gates the backfill-progress logs.
    # MUST be bound before the first `_status()` call below (the closure reads gap_list).
    gap_list = load_gaps(Path(gaps_file), now=int(time.time())) if gaps_file else []
    catching_up = False
    status_writer(_status())
    processed = 0
    since_status = 0
    seeded = False
    scan_lo = cursor  # first block whose FDC/FU events haven't been flushed yet
    ts_map: dict[int, int] = {}
    # Temperance on a sustained RPC outage: log the FIRST head-read failure, then at most once a
    # minute (with a running count), and a single recovery line — not every poll (~2s).
    hf = {"since": 0.0, "count": 0, "last_log": 0.0}
    while True:
        try:
            head = rpc.block_number()
        except RpcError as exc:
            now = time.time()
            hf["count"] += 1
            if not hf["since"]:
                hf["since"] = now
            if log and (hf["count"] == 1 or now - hf["last_log"] >= 60):
                hf["last_log"] = now
                log.error(
                    "\033[1;31m🔴 observe RPC unreachable — %d fail(s) over %ds: %s\033[0m",
                    hf["count"], int(now - hf["since"]), exc,
                )
            time.sleep(poll_sec)
            continue
        if hf["since"]:  # recovered from an outage streak → record the gap + open a backfill
            rec_now = int(time.time())
            g = Gap(
                start=int(hf["since"]), end=rec_now, dur=rec_now - int(hf["since"]),
                fails=int(hf["count"]), from_block=(state.last_block or cursor), to_block=head,
            )
            gap_list.append(g)
            if gaps_file:
                append_gap(Path(gaps_file), g)
            catching_up = head - (state.last_block or cursor) > live_lag_blocks
            if log:
                log.info(
                    "\033[32m✓ observe RPC recovered after %s (%d fail(s)) — backfilling blocks "
                    "%d→%d (%d blocks, ~%d rounds)\033[0m",
                    hms(g.dur), g.fails, g.from_block + 1, g.to_block,
                    g.to_block - g.from_block, g.dur // 90 or 1,
                )
            hf = {"since": 0.0, "count": 0, "last_log": 0.0}
        state.head = head
        # Confirmation lag — on Avalanche `latest` is already the accepted (final) block, so this
        # is belt-and-suspenders: never observe/attribute a block newer than head-confirmations.
        safe_head = head - confirmations
        while cursor <= safe_head:
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
            ts_map[cursor] = ts
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
            # FDC (200) + fast-updates (255) events — flushed in ≤30-block chunks (get_logs cap),
            # well within the ~180s finalize lag so a round's FDC requests are always recorded
            # before it finalizes.
            if cursor - scan_lo + 1 >= 30:
                _flush_events(scan_lo, cursor, ts_map)
                scan_lo = cursor + 1
                ts_map = {}
            state.last_block = cursor
            state.last_ts = ts
            for rs in state.finalize_due(ts, factory):
                if rs.issues and log:
                    lvl = log.error if rs.reveal_offence else log.warning
                    lvl("\033[38;5;208m OBS\033[0m round %s: %s", rs.round_id, "; ".join(rs.issues))
                if iqr_history_file and rs.iqr_tally is not None and rs.iqr_tally_new:
                    append_tally(Path(iqr_history_file), rs.iqr_tally)
            cursor += 1
            processed += 1
            since_status += 1
            # Keep the status fresh DURING a long catch-up so `observe status` isn't stale.
            if since_status >= status_every_blocks:
                status_writer(_status())
                since_status = 0
                if catching_up and log:  # unambiguous: we're REPLAYING, not live
                    lag_b = (state.head or cursor) - cursor
                    behind = int(time.time() - (state.last_ts or time.time()))
                    log.info(
                        "\033[38;5;208m OBS\033[0m ⏳ backfill: %d blocks behind (~%s), replaying to head",
                        lag_b, hms(behind),
                    )
            if _max_blocks is not None and processed >= _max_blocks:
                _flush_events(scan_lo, cursor - 1, ts_map)
                status_writer(_status())
                if vrpc:
                    vrpc.close()
                return
        # Caught up (or paused on a read error) — flush the trailing partial chunk so recent
        # FDC/FU events aren't withheld until 30 blocks accrue.
        if cursor - 1 >= scan_lo:
            _flush_events(scan_lo, cursor - 1, ts_map)
            scan_lo = cursor
            ts_map = {}
        # Backfill complete → LIVE. Log the transition ONCE so the reader knows the window is
        # real-time again, not still replaying an outage.
        if catching_up and state.last_block is not None and head - state.last_block <= live_lag_blocks:
            catching_up = False
            if log:
                log.info("\033[32m✓ observe caught up — LIVE at head (backfill complete)\033[0m")
        _refresh_registration(time.time())
        _refresh_iqr(time.time())
        _refresh_uptime(time.time())
        _refresh_budget(time.time())
        _refresh_delegation(time.time())
        status_writer(_status())
        since_status = 0
        # First time we're fully caught up, the lookback window is seeded (rounds finalized +
        # IQR scored) — emit the opening status line; thereafter it self-reports hourly.
        if not seeded and cursor > safe_head:
            seeded = True
            _maybe_log_status(time.time(), force=True)
        else:
            _maybe_log_status(time.time())
        try:
            time.sleep(poll_sec)
        except KeyboardInterrupt:
            if log:
                log.info("observe stopped")
            if vrpc:
                vrpc.close()
            return
