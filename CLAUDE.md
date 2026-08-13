# clif — keyless FTSO reward claimer + keyless FSP signing-tool

> Python successor to `ftso-fee-claimer`. Claims AP's FTSO v2 rewards (FEE +
> DIRECT) on Flare/Songbird/Coston2 by calling the **fwd** signing daemon —
> **clif holds zero private keys**. This is **Phase 8b** of the fwd program:
> the first deleted `.env PRIVATE_KEY=` line, the event that lifts fwd's
> doctrine-ship freeze.

**This repo stands on its own.** Everything needed to work here is in-repo
(`docs/` + this file). You do **not** need the fwd repo, the AP root
constitution, or any `~/.claude/*` file. Those are provenance, not
dependencies (see §Origin).

## THE Core invariant — clif holds zero private keys

Inviolable. There is no `.env PRIVATE_KEY=` anywhere in clif and no
local-signing dependency (`eth-account`, `eth-keys`, `pycryptodome`, `web3`,
`argon2`). `clif.config.assert_keyless()` refuses to start if any
`*PRIVATE_KEY*` env var is present. keccak-256 is vendored
(`clif/_keccak.py`) solely to derive the `claim` selector and FSM selectors
(`signUptimeVote`, `signRewards`) + the `fakeVoteHash` / `UPTIME_VOTE_HASH`
(`keccak256(0x00*32)`) — not a signing primitive. The key operations
(`RewardManager.claim`, `FlareSystemsManager.signUptimeVote`,
`FlareSystemsManager.signRewards`) are built locally as calldata and signed by
**fwd**; clif never sees a key. Any change that re-introduces a key is a
regression — STOP.

## Hard rule — a mined tx is not a successful operation (verify the effect, not the status)

Inviolable, like the keyless rule. **Never report or record an on-chain write
(claim, FSP submission, transfer, registration) as successful from its mined
receipt / `status == 0x1`, a tool's "submitted/mined" line, or a balance compared
against a stale baseline.** Success is proven ONLY by the intended effect of *that
exact transaction* — its emitted event and/or resulting state change
(`RewardManager.claim` ⇒ a `RewardClaimed` log with amount > 0; per-tx, never
aggregate). Contract behaviour is not uniform: `signUptimeVote` / `signRewards`
**revert** when already done, but `RewardManager.claim` **silently no-ops with
`status 0x1` and no event** on an already-claimed epoch. clif enforces this in
`clif/claimer.py` / `clif/discovery.py`: (a) **pre-flight** refuses an
already-claimed / out-of-range / not-yet-signed `-e` epoch with the precise reason
(no no-op submitted); (b) **post-flight** reports a mined claim with no
`RewardManager` event as the distinct `MINED_NOOP` outcome, never `SUBMITTED_MINED`.
A no-op is never reported as success. See `docs/decisions.md` D16. *Codified
2026-05-26 after a mined no-op (already-claimed epoch) was briefly mis-reported as a
successful claim — caught by the operator claiming the epoch manually first.*

## Hard rule — an empty discovery has a reason; classify it, never assume

Companion to the mined-≠-success rule, at the *discovery* level. **Never report (or
let an agent/operator read) an empty reward discovery as "not yet claimable" without
determining why on-chain.** `clif list` / `claim` / `auto` finding no claims has at
least three distinct causes that must NOT be conflated:

- **already-claimed** (DONE) — `getNextClaimableRewardEpochId(owner)` has advanced past
  a finalized epoch;
- **not-yet-claimable** (PENDING) — `rewardsHash(epoch) == 0x00…` or the epoch is beyond
  `getRewardEpochIdsWithClaimableRewards()`'s end;
- **no-accrual** (DONE) — the on-chain gates pass but the beneficiary is absent from the
  published merkle tree.

`clif/discovery.py::unclaimable_reason` / `classify_claim_frontier` compute this from the
view reads already in use — **no new RPC** (`getUnclaimedRewardState` resets to
`(False,0,0)` after a claim and cannot discriminate claimed-vs-no-accrual; the reliable
signal is `next_claimable`). The `-e` path always classified; the auto / `list` / `claim`
paths now do too (v0.5.6). *Codified 2026-05-29 after an empty Flare discovery (epoch 401
already claimed manually by the operator) was mis-reported as "not yet claimable" — the
same done-vs-pending conflation as the rule above, made right after invoking it.*

## Hard rule — `NETWORK` selects the chain; unset defaults *silently* to flare

Companion to the mined-≠-success rule, at the *configuration* level. clif resolves the
active chain as `net = network or os.environ.get("NETWORK") or "flare"`
(`clif/cli.py:715`) — so an `.env.<network>` (or a v2 handoff bundle `config`) that
**omits `NETWORK`** makes clif **silently run on flare**: `preflight` AND the `epoch run`
daemon, with no error. A songbird deployment missing `NETWORK` would sign/claim on the
**wrong chain**. **Every `.env.<network>` MUST carry `NETWORK`** (the v2 bundle `config`
supplies it — allowlisted via `Settings.network`, D20), or the command must pass
`--network`. The trap: the omission is invisible on flare (= the default) — only a
non-default (songbird) canary exposes it (`clifctl nonce-sync` is *not* affected — the
wrapper maps net→chain itself; only the in-container clif command reads `NETWORK`). See
`docs/decisions.md` D20. *Codified 2026-06-12 after a fresh v2-bundle onboard dropped
`NETWORK` and `clifctl run songbird preflight` printed `Preflight — flare (chain 14)`.*

## Automation — `clif epoch run` (canonical reward-lifecycle daemon)

The canonical automation is **`clif epoch run`** (`clif/epoch_auto.py`; decisions D17) — ONE
epoch-anchored sign→claim state machine per network that **replaces** the older always-on
`clif auto` (claim) + `clif fsp auto` (sign) 15-min pollers as the daemon entrypoint (those
survive for manual one-shots only). Per reward epoch N, once it closes:

(optional) sign uptime → wait until `epoch_end + EPOCH_REWARD_INITIAL_DELAY_SEC` (1h) →
poll every `EPOCH_POLL_INTERVAL_SEC` (30m) for reward publication
(`get_reward_distribution_data`) → sign rewards (Merkle-verified) → wait for the >threshold
`rewardsHash` finalization → **claim ONLY epoch N** (`run_claim(only_epoch=N)`) → idle until
the next epoch.

- **Timing (apgateway model):** read `firstRewardEpochStartTs()` + `rewardEpochDurationSeconds()`
  from FlareSystemsManager ONCE, then `epoch_end_ts(N) = first + (N+1)·dur` — pure math for any
  epoch incl. the current/next not-yet-closed one. Mirrors
  `ftso/apgateway/apgateway/indexer/epoch_cache.py::get_timing`; **apgateway is the reference for
  FTSO reward-epoch timing** (it does not model the FSP finalization phases — clif does).
  `next_sleep_seconds` sleeps precisely (next-window when idle / `wait_until` when too-early /
  `poll_interval` while waiting), never a flat poll.
- **Idempotency is chain-derived** (no durable phase state): `getVoterRewardsSignInfo` /
  `getVoterUptimeVoteSignInfo` (ts≠0 ⇒ we signed), `rewardsHash != 0` (finalized), `run_claim`
  pre-flight + `MINED_NOOP`. A restart re-derives each epoch's phase and resumes.
- **Gates:** signs only when `FSP_AUTO_ENABLED=true` (hard-off, D15); the uptime phase is
  additionally gated by `UPTIME_AUTO_ENABLED` (default false). Claim scope = the signed epoch only.
- **Deploy:** clif is its OWN compose project (`clif`) — separate from the zero-egress fwd
  signer. Service `clif-epoch-<net>` (`command: ["epoch","run"]`, healthcheck `clif epoch
  status`), brought up by `clifctl up <net>` (clif's own host wrapper, `install/clifctl`).
  clif joins fwd's `${FWD_NETWORK:-fwd_fwd-callers}` network (external) + its own `egress`
  bridge; fwd never launches it (fwd a92 dropped the bundled overlay + `fwd start <net>`).
- **No live signing-weight %** is on-chain (only the binary finalized flip); a live-% readout
  would self-index `RewardsSigned` events + the Relay signing policy (deferred Phase-2).

The reads + timing are **live-validated** on Songbird+Flare (`epoch_end_ts(N)` == the contract's
own `currentRewardEpochExpectedEndTs()`, exact). The end-to-end sign→finalize→claim execution +
FSP on-chain acceptance remain the operator's standing live drill at the next ended epoch. Deeper
rationale: agent memory `clif-epoch-daemon-and-apgateway-timing.md` + `validating-keyless-chain-reads.md`.

## Knowledge base (authoritative, in-repo)

Read these before non-trivial work; they are the binding references:

| file | what |
|---|---|
| `docs/phase8b-spec.md` | **Binding spec** (vendored canonical prompt). Authoritative; decisions adjudicated. |
| `docs/decisions.md` | Settled decisions — **do not relitigate** (D1–D18). |
| `docs/fwd-contract.md` | Verified fwd HTTP + ABI contract; the policy block; the `policy.example.yaml` trap. |
| `docs/onchain-migration.md` | Networks/addresses, actors, the >50% trigger, the operator-gated rotation, the `setClaimExecutors` drift. |
| `docs/verification.md` | Verification ladder (proven vs blocked), rehearsal ladder, pre-flight traps, local checks. |
| `docs/fwd-integration-spec.md` | The operator handshake artifact (regenerate with `clif spec`). |

## Status — current: v0.5.45

clif is on the public `github.com/africanproofs/clif` (build-from-source). The
reward-claim and FSP signing paths are code complete and keyless. The clif ↔ fwd ↔
chain **integration** is proven on Songbird mainnet: fwd signs both legs, clif
broadcasts the signed payload and reports the outcome back, and the nonce confirms
on a mined receipt / releases on a revert. End-to-end on-chain claim execution and
FSP on-chain protocol acceptance by the `FlareSystemsManager` remain **deferred** and
**operator-gated**: the claim path needs the new wallet authorized on-chain as
executor (`setClaimExecutors`) and a claimable epoch; FSP acceptance needs a clean
ended-but-not-yet-signed epoch to submit into (the last live submit hit the FSM
window guard, which fires before the signer-registration check, so acceptance is
inferred via the registered `0xfB021c…` voter key, not demonstrated). See
`docs/verification.md` for the rung-by-rung state.

Current contract: clif asks fwd to SIGN (`/v1/sign-transaction`), then **broadcasts
the returned `signed_raw_tx` itself** and **reports the outcome back** to fwd. 299
tests green. Build via the shared `fwd-client` lib
(`github.com/africanproofs/fwd-client`, `subdirectory=python`, tag **v0.1.2**);
fwd error classification is **class-based** (`FwdRetryableError` /
`FwdTerminalError`) with one deliberate code-specific recovery for a Leg-2
`409 idempotency_conflict` restart path — see `docs/fwd-contract.md` § Error
taxonomy and `docs/decisions.md` D18.

**Changelog (condensed):**

- **v0.5.61 (2026-08-13) — observer self-reports its status line (participation + would-be IQR) to the log.** The observe daemon previously logged only issues/errors + `observe start`/`IQR scoring on`, so in `clifctl logs` it was silent between problems and its IQR quality never showed alongside REG/FUND/EPCH. Now it renders the OBS status line (via shared `observe_health_from_dict` + `render_observe`) **once the lookback window is seeded** (opening line) and then **every `status_log_sec` (default 1 h)**, at the severity-appropriate level (healthy→INFO, excluded/degraded→ERROR/WARN). Same one-liner as `observe status` — e.g. `🔴 submitting N/N rounds … earn ZERO · would-be IQR in P% / out P%`.
- **v0.5.60 (2026-08-13) — IQR offer-scan works on a PRUNED node (Flare fix).** The Flare observe RPC (`ap-ftso-01`) only serves ~the last 1M blocks (63M null, 66.3M+ served), so `_find_block_for_ts`'s binary-search-from-block-1 hit "block not found" on its very first probe (the ~33.6M chain midpoint) and turned Flare IQR off every cycle. Fix: since every target is RECENT (an epoch-N−1 start), a pruned/"not found" probe is necessarily OLDER than the target ⇒ treat it as "search higher" — the search converges to the first *retained* block ≥ target with no magic retention floor, on any node. Live-verified: Flare epoch 423 offer resolves (63 feeds, block 66,802,305) ⇒ Flare IQR scores.
- **v0.5.59 (2026-08-13) — observer startup no longer crashes with a full traceback on a transient RPC blip.** `observe run`'s startup `contract_address_by_name("Submission")` was outside the engine's retry loop, so a `Connection reset by peer` at boot dumped a ~200-line Rich traceback and Docker restart-looped. Now it retries (12× backoff) with a one-line warning. Also `_find_block_for_ts` (the IQR offer-scan binary search) retries each block probe 4× — a single archive-node hiccup during the ~26 probes over old blocks no longer aborts the scan and turns Flare IQR off ("block N not found" was a transient miss, not a pruned block).
- **v0.5.58 (2026-08-13) — `observe status` shows WOULD-BE IQR while AP is excluded.** The `registered is False` render branch returned before the IQR clause, so the one-line status hid band quality on exactly the nets where AP is currently excluded (RE423). Now it appends `· would-be IQR in P% / out P%` after the "earn ZERO" headline (the JSON/MCP already carried it). Deployed Songbird: inner ~47% / outer ~95% over the rolling window.
- **v0.5.57 (2026-08-13) — IQR scoring folded into the CONTINUOUS observer (live, per-round).** The one-shot `observe iqr` is now also computed inline by the streaming `clif-observe-<net>` daemon: per reward epoch it caches the offer band params + the voter→weight map (`_refresh_iqr`, hourly-gated, disk-cached), decodes **every registered voter's** submit2 reveal per round (not just AP's), and at finalize scores AP's values against the native all-voter median — rolling `iqr_inner_pct`/`iqr_outer_pct` over the finalized window. Surfaced in `observe status` (`· IQR in P% / out P%`), the status JSON (`iqr_inner_pct`/`iqr_outer_pct`/`iqr_scored_rounds`/`iqr_feed_rounds`/`iqr_capped`), and thus the `observe_status` MCP tool. Per-round vote buffers dropped at finalize (bounded memory); **informational only — never affects `clean`/severity**. Gated by `OBSERVE_IQR` (default true; needs VoterRegistry+EntityManager+FSM). **Live-verified Songbird epoch 423: inner ~50% / outer 94.6% over the rolling window — outer matches the one-shot path exactly (independent cross-check).** New `state.record_reveal_values`/`set_iqr_context`, `engine` all-voter decode. 397 tests green (+2 streaming-integration).
- **v0.5.56 (2026-08-13) — observer IQR scoring: `clif observe iqr` (AP's inner/outer reward-band hit rates).** Scores AP's *reward quality* — how often its on-chain submitted values land in the primary/IQR band (inner) and secondary/PCT band (outer). NATIVE + keyless: `clif/observe/iqr.py` builds the consensus median/quartiles per feed from ALL registered voters' reveals (`py-flare-common calculate_median` = the Foundation's reference algorithm; `getRegisteredVoters`→`getVoterAddresses`→`getVoterRegistrationWeight` for the voter→weight map), then `clif/observe/reward_rule.py` (ported from `ftso/ml/aptrainer-prices/scripts/reward_rule.py`, verified vs the on-chain `calculateMedianRewardClaims`) classifies AP's value: inner = INSIDE/BOUNDARY/OUTSIDE of `[Q1,Q3]` (boundary = keccak coin-flip → expected = inside+0.5·boundary), outer = within `M±(|M|·secondaryBandWidthPPM)`. Band params (feed order + decimals + secondaryBandWidthPPM) from the per-epoch `InflationRewardsOffered` offer event (`get_offer_params`, cached). `parse_submit2_tx` values are already raw ticks (no float conversion). **`clif observe iqr [--rounds N] [--json]`** — one-shot: scans the recent reveal windows, prints per-feed + overall inner%/outer% (capped feeds — Q3−Q1≤1 tick, ~50% structural ceiling — flagged). Works even while AP is EXCLUDED (RE423) — scores AP's values vs the registered consensus (would-be quality). **Live-verified Songbird epoch 423: overall inner 43.2% / outer 94.6% over 5 rounds, 60 voters.** New `rpc.get_registered_voters` + `rpc.get_logs`. Deferred: continuous per-round IQR in the streaming daemon (this is the on-demand command). 395 tests green (+17).
- **v0.5.55 (2026-08-13) — observer Phase 2a: FDC participation.** The observer now tracks AP's FDC participation alongside FTSO, on a SEPARATE axis (FDC is its own protocol — not folded into FTSO `clean`). The engine scans FdcHub `AttestationRequest` events (topic `0x2513…8cc9`) per block, attributing each to the block's voting round, so a round is "FDC-expected" only when it actually had requests; then it checks whether AP's submit2 carried an FDC bitvote (`parse_submit2_tx(...).fdc`). Robust-signal-only: we do NOT compare bitvote length to our request count (our per-block count is ±1 at round boundaries — that produced false mismatches; AP's own bitvote uses the protocol's exact request set). Aggregates: `fdc_request_rounds` / `fdc_participated` / `fdc_missing` + `fdc_participation_pct`; severity CRIT when sustained (`≥10 request-rounds` and `<80%` — the FDC minimal condition), WARN on isolated gaps. `render_observe` appends `· FDC N/M (pct%)`. New `rpc.get_logs` + `engine` `fdc_hub` param (resolved via the registry; optional — degrades off). Per-block getLogs roughly doubles catch-up RPC (fine for the background daemon). **Deferred to a fuller FDC pass:** exact request-round attribution, consensus-bitvote domination, and the FDC signature check (all need all-voter aggregation). 385 tests green (+5).
- **v0.5.54 (2026-08-13) — observer registration overlay (closes the RE423 blind spot AT the observer).** The Phase-1 observer measured raw submission participation, so during the RE423 exclusion it showed `✓ 40/40 clean 100%` — green while AP earned ZERO (the FTSO client submits every round regardless of registration; "submitting successfully toward nothing"). The observer now cross-references registration: the engine probes `isVoterRegistered(Identity, current_reward_epoch)` **hourly** (registration changes only per ~3.5-day epoch; best-effort, keeps last value on RPC error), stores `registered`/`reward_epoch` in the status, and when `registered is False` forces severity **CRIT** and overrides the render to `🔴🔴 submitting N rounds — but NOT REGISTERED for RE<epoch>: these submissions earn ZERO`. `registered=None` (unprobed) falls through (no false alarm). So neither the observer nor registration daemon can independently show green while AP loses rewards. Deployed to BOTH networks. 380 tests green (+4).
- **v0.5.51 (2026-08-12) — the `observe` module (Phase 1): per-block FTSO participation observer (fsp-observer native port).** Closes the gap RE423 generalised — clif watched only the ~3.5-day reward-epoch layer, blind to the per-90s-voting-round FTSO submissions that actually earn rewards. New `clif/observe/` package streams blocks (keyless) and tracks whether AP's OWN submit/submitSignatures addresses participate each round: submit1 (commit) + submit2 (reveal) on-time, and — the byte-critical check — the reveal reconstructs the commit hash (reveal-offence detection). **Depends on `py-flare-common==0.1.10`** (the Foundation's own FSP parser/timing library, the exact one fsp-observer uses — see memory `py-flare-common-fsp-parser`) for all byte-level parsing + epoch timing, so we glue rather than hand-port. `ObserveHealth` rolls a window of finalized rounds into a severity (CRIT = reveal offence / sustained non-participation <90% / stale engine / read error; WARN = isolated miss or warming up; OK = all clean) — never green on error/stale. `clif observe status [--json]` (exit 0/1/2), `clif observe run` (the `clif-observe-<net>` engine daemon, hard-off `OBSERVE_ENABLED`, points at an RPC that keeps up per-block, restart re-syncs from `head-lookback`, drops boundary rounds so startup doesn't false-alarm). `_BADGE_OBS` (orange). **Live-verified on Songbird: decoded AP's real submissions, finalized a round clean (commit-hash reconstruction matched — no false offence).** 376 tests green (+17). Config: OBSERVE_ENABLED / OBSERVE_RPC / OBSERVE_LOOKBACK_BLOCKS / OBSERVE_WINDOW_ROUNDS / OBSERVE_POLL_SEC. **Phase 2+ (deferred, marked TODO):** minimal-conditions value checks (need all-voter medians), signature-vs-finalization recovery (eth-keys), FDC, fast-updates, uptime; MCP `observe_status` tool + epoch-daemon line + doctor field + Flare rollout.
- **v0.5.50 (2026-08-12) — per-module badges in the daemon log.** The epoch daemon interleaves three module lines every cycle (epoch schedule · funding · registration) and funding+registration shared the green/yellow/red severity palette, so two CRITs looked identical. Each module now carries a FIXED-hue badge prefix — `EPCH` (cyan) · `FUND` (magenta) · `REG` (blue) — while the message body keeps its severity color. `render_health`/`render_readiness` prepend their badge (one wrapper each, `_BADGE_*` in funding.py); the epoch schedule line gets the cyan `EPCH` tag. Module identity at a glance + severity alarm preserved.
- **v0.5.49 (2026-08-12) — registration-readiness review fixes (self-review of v0.5.48).** Three defects found + fixed: (1) `doctor` text output printed `register : … read error: None` when healthy — the guard tested key presence (`"error" not in registration`) but `to_dict()` always includes an `error` key; now checks the value. (2) `render_readiness` had dead logic (`"🔴🔴" if (active or True)`) — a live exclusion now always gets the loud double-marker. (3) The `registration run` daemon tightened cadence only by a 2h time heuristic; it now tightens whenever the on-chain registration window is actually OPEN (`next_window_enabled`) — robust if the window opens earlier than the heuristic guesses — while deliberately NOT tight-polling a persistent, unfixable current-epoch exclusion (avoids days of 2-min log spam). 359 tests green.
- **v0.5.48 (2026-08-12) — registration-readiness tracker: the RE423 detector (`clif registration status`/`run`).** RE423 (2026-08-10) lost a whole ~3.5-day epoch because `registerVoter` reverted `insufficient funds` (the Submit gas account drained) and the node silently fell out of the registered voter set — invisible behind green submit metrics, caught only by an EXTERNAL dashboard. This adds the missing POSITIVE signal. New `clif/registration.py` `read_readiness()` (never-raises → CRIT on read error) reads the on-chain registered set + window directly: `isVoterRegistered(Identity, epoch)` (current membership — the RE423 blind spot), `getVoterRegistrationData(next)` (window open?), `getVoterRegistrationWeight` (will we register with weight>0?), plus gas (Submit ≥ `REGISTRATION_GAS_FLOOR`=10, independent of the 250 funding band) + entity (`getVoterAddresses` non-zero). Severity: **CRIT** = NOT registered for the current epoch (live exclusion) / a prereq that will fail the next registerVoter / any read error; **WARN** = next window open + not yet registered (client retrying); **OK** = registered + prereqs green. New rpc reads are revert-tolerant (`voter not registered` / `... not supported` ⇒ not-registered/window-closed, not error). `clif registration status [--json]` (exit 0/1/2), `clif registration run` (the boundary-aware `clif-registration-<net>` daemon — tightens cadence within `REGISTRATION_TIGHT_WINDOW_SEC`=2h of the epoch boundary where the window opens for ~6.7 min; hard-off `REGISTRATION_ENABLED`; OBSERVE-only, never signs). Surfaced color-coded in the epoch daemon each cycle + as a `registration` field in `doctor --json`. **Live-validated: it immediately flags the current RE423 exclusion on both mainnets** (🔴🔴 NOT REGISTERED — RE423 LIVE EXCLUSION). 359 tests green (+21). Config: REGISTRATION_ENABLED / REGISTRATION_POLL_INTERVAL_SEC / REGISTRATION_TIGHT_INTERVAL_SEC / REGISTRATION_TIGHT_WINDOW_SEC / REGISTRATION_GAS_FLOOR / REGISTRATION_SENDER_ACCOUNT.
- **v0.5.47 (2026-08-12) — `fund apply --json` (completes the ADR-0006 machine-readable ACT surface).** `clif fund apply` now takes `--json` and emits the `FundingResult` as typed JSON — `{network, ok, error, skipped_ok, rejected[], funded[{account,address,before,after,sent,tx_hash}], failed[{account,detail}]}` — so the `flaresystems-mcp` layer (Phase 3) wraps the ACT tool with the same structured contract as the OBSERVE reads. Exit unchanged (0 ok / 2 on any failure or error). The human log path is unchanged when `--json` is absent. 338 tests green.
- **v0.5.46 (2026-08-12) — the funding membrane (ADR-0006 Phase 1+2): `fund propose` / `fund apply` + `--json`.** The agent-interface front door for the ACT tier — an untrusted decision-maker *proposes* a funding plan; clif's deterministic membrane *validates + executes* the accepted subset, keyless; fwd's policy is the independent final gate. `validate_plan()` (in `funding.py`) rejects — never raises — any line that is an unknown account, a non-positive/`≤0` amount, over the per-tx cap (400), a top-up that would push the account **above its band upper**, or one that overruns the ap-funder runway (running total across the plan). Plan JSON is `[{"account","amount"}]` or `{"account","target"}` (target resolves to the gap); accepts a bare list or `{"topups":[…]}`. `clif fund propose --plan <json> [--json]` validates + shows accept/reject per line, executes NOTHING (the human-approve dry-run). `clif fund apply --plan <json>` validates then executes the accepted lines via the same keyless `_execute_topup` path as the auto pass (re-checks the bound at execution time — never overfund), requires FUNDING_CALLER_TOKEN/FUNDING_WALLET_NAME. `clif fund health --json` emits the machine-readable health (the MCP scrape surface). No new authority: this is ADR-0001's proposer/executor membrane restated at the interface layer. 338 tests green (+8).
- **v0.5.45 (2026-08-11) — keyless gas-funding (Part 2): `clif fund` + color-coded funding health surfaced during reward-signing.** RE423 (2026-08-10) lost a whole epoch to a Submit account that drained below the registerVoter gas cost, unseen behind green submit metrics. New `clif/funding.py` keeps every FSP account within a balance band (operator bands: gas-payers 250→400, Identity/Delegation 150→200) by topping up any breach to the upper bound via fwd's `native-transfer` capability from the `ap-funder` wallet — keyless (fwd signs, clif broadcasts + reports back + verifies the balance rose; mined≠success). `clif fund health` (read-only, exit 0/1/2), `clif fund once [--dry-run]`, `clif fund run` (the `clif-fund-<net>` daemon, hard-off `FUNDING_ENABLED`, own 15m cadence). **The epoch daemon surfaces funding health EVERY cycle, COLOR-CODED** (green ✓ / yellow ⚠ / bold-red 🔴🔴 + ERROR level, louder when a reward-signing epoch is active) so a gas-starved account or an exhausted funder is impossible to miss while the operator watches signing. `read_health` never raises — a read failure is CRIT, never green (the RE423 blind-spot). Config: FUNDING_ENABLED/FUNDING_CALLER_TOKEN/FUNDING_WALLET_NAME. 330 tests green (+9).
- **v0.5.44 (2026-07-24) — SECURITY: `clif doctor` leaked caller bearer tokens via a rich traceback.** Two coupled defects, both found running `clif doctor` after v0.5.43. (1) doctor's human-readable branch read `c['clif']` while `_compat()` emits `claim` — so EVERY text-mode `clif doctor` died with `KeyError`. (2) Typer defaults to `pretty_exceptions_show_locals=True`, so that crash printed the doctor frame's locals — including `token_by_role`, the live fwd **caller bearer tokens** — to the terminal. Fixed the key, and set `pretty_exceptions_show_locals=False` on all four Typer apps so no uncaught error can ever dump secrets again. The exposed tokens were rotated. Tests pin both. 321 tests green (+2).
- **v0.5.43 (2026-07-24) — `voter already signed` is benign, not terminal; stale VoterRegistry pin refreshed.** Flare epoch 417 wedged: the Foundation re-deployed the **VoterRegistry** on BOTH mainnets (flare `0x2580…Fce83`→`0xA480…9Af7`, songbird `0x31B9…dC8D`→`0xd23F…4310`), so `getWeightsSums`/`getVoterWithNormalisedWeight` reverted `reward epoch id not supported`. That blinded v0.5.31's `our_signed_fn` guard, so the daemon re-signed every cycle; the duplicate reverted **`voter already signed`** — a string the benign-revert map did not carry — so it was classified `FAILED_TERMINAL` → DEGRADED + cooldown → **the claim was skipped every cycle**. Two fixes: (1) the contract's per-voter guard now maps to a NEW `OutcomeStatus.ALREADY_SIGNED` (in `_OK`), kept **distinct** from `ALREADY_FINALIZED` because our vote being on-chain does NOT mean the epoch finalized — 417's `rewardsHash` was still zero, so falling through to the claim would have been wrong; the daemon keeps awaiting finalization. (2) both VoterRegistry pins refreshed. Guard strings are now verified against `FlareSystemsManager.sol` (resolving the long-standing UPTIME TODO — both message types share `voter already signed`). `clif doctor` gains a **contract-drift check** against the chain's own FlareContractRegistry (best-effort, never fails doctor) so the next migration surfaces as a warning instead of a stuck epoch. An unrecognised revert still escalates TERMINAL. 319 tests green (+7).
- **v0.5.42 (2026-07-10) — per-network fee cap; fail fast instead of broadcasting an underpriced tx.** Songbird raised its minimum base fee from 1 wei to 500 gwei at 2026-07-07 12:00:00 UTC, so clif's hard-coded 300 gwei `_MAX_FEE_CAP_WEI` made every Songbird broadcast fail `transaction underpriced` (epoch 413 REWARD_DISTRIBUTION leg-2 wedged terminal). The cap is now `CLIF_MAX_FEE_PER_GAS_WEI` (per-daemon env, default 300 gwei), and `suggest_fees` raises `RpcError` naming the required value when the cap cannot cover `baseFee + tip` — rather than emitting a transaction the chain is certain to reject. Requires fwd's `FWD_MAX_FEE_PER_GAS` (raised to 2000 gwei) to be >= the cap. Flare is expected to take the same floor at its 2026-07-14 hardfork.
- **v0.5.41 (2026-06-30) — idle schedule line no longer mislabels the poll-start as "reward window opens" (cosmetic).** The v0.5.40 idle line read `current open epoch 411 — its reward window opens <end+3600s> after it closes`, implying 411's rewards are available at `epoch_end + 3600s`. They are NOT: `epoch_end_ts(N)` (the epoch END / next-epoch START) is exact + voting-aligned, but `end+3600s` is only clif's poll-START heuristic — the reward data publishes later (during epoch N+1, off-chain, unpredictable; e.g. epoch 410's data was not ready at `end+1h`, clif kept polling). `schedule_line`'s idle branch now narrates the two distinctly: `epoch N (open) ends <end> (in …); clif then polls for its rewards from <end+3600s> and signs once published`. No change to `epoch_end_ts`/`next_sleep_seconds`/epoch math/poll cadence/signing/nonce. Tests updated.
- **v0.5.40 (2026-06-29) — daemon schedule-line clarity + explicit `UTC` (cosmetic; no logic/timing change).** The `epoch run` idle line read `idle — caught up; next reward window (epoch 410 end +3600s) opens …Z` — mathematically exact (`first+(N+1)·dur` == on-chain `currentRewardEpochExpectedEndTs`, 0 s drift) but it *looked* behind: it named the current OPEN epoch clif is waiting to close while a later chain glance shows the next id, and `…Z` invited a UTC-vs-local misread. `schedule_line` now takes `last_done` and states status explicitly — idle: `caught up (signed through epoch N); current open epoch M — its reward window opens … UTC … after it closes`; active: `epoch X (closed; chain at M) <phase> — …`. `_fmt_ts` and the log formatter spell ` UTC` (was `Z`; `Formatter.converter=gmtime` already made it UTC). Pure log wording + timestamp suffix — no change to timing/epoch math, signing, nonce, or the dedicated-sender path. Tests updated.
- **v0.5.39 (2026-06-19) — reward-sign self-heals a wedged idempotency key (durable nonce-too-low fix).** A leg-2 `REWARD_DISTRIBUTION` submit that failed `rejected_nonce_too_low` (fwd's local nonce for the SHARED sender `0x7c3579` drifted behind chain when the co-using legacy FTSO automation sent) left the epoch PERMANENTLY wedged: clif's per-epoch idempotency key is constant, so every 30-min retry replayed the cached stale-nonce tx (`sign-transaction-duplicate` → nonce-too-low) or was denied `idempotency_key_body_mismatch` → AP's reward signature never landed (`our vote on-chain: no`), live on epoch 407 both chains. Fix: `epoch_auto` now threads an in-memory `{epoch: retry-count}` map (`drive_epoch`/`run_cycle`/the `epoch run` loop); on a RETRYABLE reward-sign it bumps the count and passes `_sign_retry_token(base, count)` as the leg-2 (+leg-1) discriminator, so the NEXT cycle re-signs under a FRESH fwd idempotency key (fwd re-signs at its corrected nonce instead of replaying the dead tx). Chain truth (`our_signed_fn`/`getVoterRewardsSignInfo`) stays the double-submit guard, so the in-memory reset on restart can't double-sign. Bounded at `_MAX_REWARD_SIGN_RETRIES=3`: a PERSISTENT fwd-nonce drift (which a fresh key can't fix — clif is keyless, can't admin fwd's nonce) surfaces TERMINAL with a `clifctl nonce-sync <net>` hint instead of looping silently. clif does NOT modify fwd. 302 tests green (+3). The manual unwedge that proved this live (fwd `nonce init --force` + `FSP_IDEMPOTENCY_RETRY` bump + restart) is the same mechanism, now automatic.

- **v0.5.38 (2026-06-10) — `import-credentials` consumes the v2 COMPLETE bundle (tokens + wallet-envs + config); ADR-0003 Unit 4b clif lockstep.** The fwd credential handoff bundle becomes **v2 — the complete onboard handoff** (consumer-contract-v1 §4): clif's ENTIRE `.env.<network>` is now sourced from the bundle, so **fwd no longer reads or writes clif's env** (closes the cross-project Invariant #5; the `--clif-env-dir` env-write is retired on the fwd side). `import_credentials` now writes, per capability, the bearer caller TOKEN **and** the fwd WALLET NAME (the wallet-env NAME is clif's own — `config.capabilities()[cid].wallet_env`; the bundle supplies only the value), plus a top-level **`config`** section for the rest of the env. The config keys are validated against an **allowlist derived from clif's own `Settings` fields** (`config.config_env_allowlist`) so it stays in sync — an unknown key is rejected (no arbitrary-env injection); the per-cap **token env-vars are excluded** from the config allowlist (secrets travel only via the guarded per-cap path) as is any `*PRIVATE_KEY*` name/value (D1). The env-injection guard (no control/newline chars) applies to config values + wallet names; config values must be strings. Config keys **overwrite in place** (the bundle is authoritative; same idempotent `upsert_env_var` collapse as token rotation; written in sorted order for diff determinism). **v1 (tokens-only) bundles still import** for back-compat. Output reports NAMES only — never a token value. `BUNDLE_VERSION → 2`; `SUPPORTED_VERSIONS = {1, 2}`. **The real-fwd v2 e2e (Songbird canary) is BLOCKED-pending-fwd** — fwd still emits v1 (`bundle_emit.BUNDLE_VERSION == 1`); Unit 4b is the lockstep half. `credentials.py` keeps its e2e NOTE PENDING until the canary proves it. See `docs/decisions.md` D19. No daemon/epoch/signing-path change. 299 tests green.
- **v0.5.33 (2026-06-09) — FSP acceptance + claim PROVEN (epoch 404); verification ladder all-green; fwd-client v0.1.3.** Reward epoch 404 closed every open verification rung end-to-end, unattended, on BOTH chains: REWARD_DISTRIBUTION signed + accepted on-chain (our `RewardsSigned` events landed + the epoch finalized — Songbird `0x8eb571…eab5` 50.43%, Flare `0xff02f8…095dd` 51.77%) and the rewards claimed. `docs/verification.md` reconciled: claim rungs 2/4/5 and FSP rung **F2** go from ⛔ to ✅ **proven** (was the dominant deferred item across two readiness audits). Also: bumped the `fwd-client` dep to **v0.1.3** (`health()` 503 now raises `FwdRetryableError`, not a raw httpx error) + relocked; added a **rewards-leg** `409 idempotency_conflict`→retryable test (prior round only covered the uptime leg). No signing/timing logic change.
- **v0.5.32 (2026-06-09) — deployment-readiness audit fixes (defense-in-depth on the restart/409 path + CI + observability).** Five confirmed audit findings: (1) **arch-02** — a leg-2 `409 idempotency_conflict` (we already submitted this epoch's sign) now maps to `FAILED_RETRYABLE`, not `FAILED_TERMINAL`, so a restart-before-finalization can't wedge the epoch in a false terminal + cooldown (complements 0.5.31's event-based prevention; relies on the now-reliable `error_code` from **fwd-client v0.1.2**). (2) **fwd-client v0.1.2** consumed (`tag` bump + `poetry update`): its Python error parser now reads the nested `detail.error`, so `FwdError.error_code` is accurate — `docs/fwd-contract.md` § Error taxonomy updated (clif now branches on `error_code` for that one recoverable case). (3) **arch-06** — `<NET>_LOGS_RPC` added to `.env.example` + a startup WARNING when it's unset (the 0.5.31 fix + live signing-% are otherwise silently inert). (4) **FSP silent-miss alarm** — when an epoch finalizes WITHOUT our vote for a kind we sign, a WARNING fires (a missed window = lost reward is no longer silent). (5) **CI** — GitHub Actions (`.github/workflows/ci.yml`: ruff + pytest + docker build); clif is github-hosted so a `.gitlab-ci.yml` would be inert. No signing/timing logic change beyond the 409 reclassification.
- **v0.5.31 (2026-06-08) — no false-TERMINAL re-sign on restart (event-based "already signed"); % logs unblocked.** A restart before an epoch finalizes made the daemon re-attempt the reward sign — `getVoterRewardsSignInfo` reverts pre-finalization, so the revert branch set `signed_rewards=False` even though we'd already signed → leg-2 hit fwd's `idempotency_conflict` (same deterministic key, fee-drifted body) → `FAILED_TERMINAL` → `DEGRADED`+cooldown, and the 0.5.29 skip-terminal filter then hid the % lines. Fix: `drive_epoch`/`run_cycle` take `our_signed_fn`; on the pre-finalization revert the daemon consults the **RewardsSigned events** (chain truth, via `refresh_signing_progress(...).our_signed` on the archive RPC) — if we already signed → `CLAIM_WAIT`, never re-attempting (no conflict, no false TERMINAL). NOT a fwd-error-taxonomy change (clif must not branch on `error_code`). Also dropped the skip-terminal narration filter (cheap with the 0.5.30 cache) so % shows for every non-done epoch. Safe: `our_signed=true` only comes from an on-chain event, so it can never skip a sign we haven't done. No event check (logs RPC unset) ⇒ prior behaviour.
- **v0.5.30 (2026-06-08) — signing-progress: cached + incremental scan (daemon RPC volume ~95→~2 calls/cycle).** The per-signer normalised weight, the epoch total, and the threshold are IMMUTABLE for an epoch, yet 0.5.28's full scan re-fetched all ~65 weight reads (of ~95 calls) every 30-min cycle. New `refresh_signing_progress(cache, …)` (daemon) persists a per-(epoch,kind) cache across cycles: immutable facts fetched ONCE; each cycle scans only blocks ABOVE the last high-water mark (events are append-only) and looks up weights ONLY for newly-seen signers. Steady state (no new signers) ≈ 2 calls/cycle. The stateless `compute_signing_progress` (the one-shot `epoch signing-progress` command) is unchanged; both now share `_scan_window`/`_scan_forward`/`_aggregate`. Falls back to a full scan if the initial backward scan was incomplete (capped-RPC only). No signing/timing logic change.
- **v0.5.29 (2026-06-08) — quiet the daemon log; don't re-scan stuck epochs.** The 0.5.28 signing-progress scan makes ~100 RPC calls/cycle (one `eth_call` per signer for normalised weight × both kinds), and httpx/httpcore log one INFO `HTTP Request: …` line per call → the daemon log was flooded and clif's own lines drowned. Silenced `httpx`/`httpcore` to WARNING (clif logs every meaningful outcome itself). Also: the per-cycle % narration now skips `terminal`/cooldown (and done) epochs — a stuck epoch (e.g. a reward re-sign that hit fwd's `idempotency_conflict` after a restart) no longer triggers the full ~100-call scan every cycle. No signing/timing logic change.
- **v0.5.28 (2026-06-08) — signing-progress adds uptime %, logs both %s every daemon cycle, logs the recipient.** Generalised the 0.5.27 aggregator to both signing events (one code path): `RewardsSigned` and **`UptimeVoteSigned`** (`UptimeVoteSigned(uint24,address,address,bytes32,uint64,bool)`, topic0 `0x5506…e797`, data `(bytes32,uint64,bool)` — no claims array; same VoterRegistry normalised weights + same `signingPolicyThresholdPPM` + strict `accumulated>threshold`; finalization getter `uptimeVoteHash`). `rpc.signed_logs(…, kind=)` + generic `SignedLog`/`message_hash`; `compute_signing_progress(…, kind="rewards"|"uptime")`. `epoch signing-progress` now shows BOTH uptime and reward progress + the **claim recipient** (nested JSON `{recipient, our_voter, uptime:{…}, rewards:{…}}` — supersedes 0.5.27's flat shape, no consumers). The `epoch run` daemon logs the recipient (startup + each active cycle) and, **every cycle for each active epoch, both uptime% and reward%** — gated on a configured `<NET>_LOGS_RPC` (the public RPC's 30-block getLogs cap + uptime events sitting near epoch-end make a public-RPC scan wrong/expensive; without it the daemon logs the recipient + a one-line "set `<NET>_LOGS_RPC`" notice). Live-verified both chains epoch 404: uptime leading hash `0x290de…3e563` == `uptimeVoteHash(404)`. Topic0 anchors pinned. No signing/timing logic change.
- **v0.5.27 (2026-06-08) — `clif epoch signing-progress`: live reward-signing % (the Explorer's "Reward Signed" figure, off-chain).** The FlareSystemsManager exposes no view getter for *intermediate* reward-signing progress (`rewardsHash`/`getVoterRewardsSignInfo` revert until finalization), so clif now reproduces what the Flare Systems Explorer does: aggregate the FSM `RewardsSigned` event (epoch/spa/voter all indexed → precise `eth_getLogs` topic filter) and sum each signer's **normalised** signing-policy weight (`VoterRegistry.getVoterWithNormalisedWeight`) over the epoch total (`getWeightsSums[1]`); threshold = `signingPolicyThresholdPPM` (50%); finalized = strictly `accumulated > threshold` (matches the contract + flare-system-client). New keyless reads in `rpc.py` (`block_number`, `reward_signed_logs`, `voter_normalised_weight`, `weights_sums`, `signing_policy_threshold_ppm`), a `signing_progress.py` aggregator (chunked getLogs over the post-epoch-end window), the `epoch signing-progress [--epoch][--network][--json]` command, and a per-cycle daemon narration line while awaiting finalization. Pure httpx + eth-abi + vendored keccak (stays keyless). VoterRegistry: flare `0x2580…Fce83`, songbird `0x31B9…dC8D`. Live-verified both chains (epoch 404); `topic0` pinned. No signing/timing logic change.
- **v0.5.24–0.5.26 (2026-06-08) — daemon signs the just-closed epoch on a fresh start; FSM pre-signing reverts handled.** (1) Songbird/Flare FSM revert BOTH `rewardsHash(epoch)` AND `getVoterRewardsSignInfo(epoch,voter)` with "rewards hash not signed yet" before an epoch enters active signing — both calls are now wrapped (treat as not-finalized/not-signed-by-us, fall through to publication+sign). (2) Fresh start (`last_done=None`) set `last_done=current-1` → `range(current,current)` skipped the just-closed epoch; now `current-2` so the daemon self-discovers and signs it (no `FROM_EPOCH` needed). Both live-confirmed (SGB+FLR epoch 404 signed). `fwd install/onboard` writes `FSP_AUTO_ENABLED=true` by default (the onboard is the gate).
- **v0.5.23 (2026-06-08) — `clifctl restart` reloads env (force-recreate) + `FROM_EPOCH` env for the daemon's backfill point.** Two gaps surfaced bringing up the Songbird canary. (1) **`clifctl restart` didn't pick up an env change:** it ran `docker compose restart` (restarts the SAME container with the env captured at creation), so flipping `FSP_AUTO_ENABLED=true` in `.env.<net>` had no effect — the daemon still logged DISABLED. Now `clifctl restart` = `docker compose up -d --force-recreate` → re-reads `.env.<net>` (and the rebuilt image). (2) **The daemon couldn't be pointed at the just-closed epoch:** it runs via a fixed compose command (`epoch run`), so `--from-epoch` couldn't be passed; a fresh start (`last_done=None`) sets `last_done=current-1` and SKIPS the just-closed epoch — e.g. with epoch 405 open it idled 3d8h waiting for 405 instead of polling for **404**'s reward publication. Added `envvar="FROM_EPOCH"` to `--from-epoch` (verified: `[env var: FROM_EPOCH]`), so `FROM_EPOCH=N` in `.env.<net>` backfills the daemon from N. Both live-confirmed.
- **v0.5.22 (2026-06-08) — epoch daemon logs: timestamps + a clear "what to expect and when" narrative.**
  Three fixes to `clif epoch run` (logging/UX only — no signing/timing logic change). (1) **Disabled
  state no longer restart-loop-spams:** when `FSP_AUTO_ENABLED!=true` the daemon logged via
  `err.print()` (no timestamp) + `typer.Exit(2)` → `restart: unless-stopped` re-ran it forever. Now it
  logs ONE timestamped line, writes a `disabled` status, and idles (hourly heartbeat) — `clif epoch
  status` reports healthy-disabled (exit 0), reboot-resilience preserved. (2) **UTC ISO-8601
  timestamps** on every line (`%(asctime)sZ` + `Formatter.converter=gmtime`) — matches on-chain/epoch
  times. (3) **Narrative:** new `schedule_line()`/`_fmt_ts`/`_fmt_dur` in epoch_auto; per cycle the
  daemon logs what each active epoch is waiting for + the ABSOLUTE next-action time + countdown, and
  the sleep line is "sleeping <dur> (until <ts>)" not raw seconds. `autostate.status_exit_code` treats
  `disabled` as healthy; `clifctl up` pre-warns if the daemon would idle. Live-verified (one disabled
  line, healthy-disabled status, exit 0) + 6 unit tests; 223 pass.
- **v0.5.21 (2026-06-08) — `clifctl nonce-sync` (automated chain-truth nonce seeding).**
  Restores the no-hand-typing nonce seed the fwd de-intermingling regressed (onboard no longer
  reads chain, since fwd is zero-egress). `clifctl nonce-sync [<net>]` reads each imported
  tx-wallet's (claimer + FSP sender) on-chain tx count via clif (egress) and writes fwd's nonce
  via the `clifwd` host wrapper (admin) — fwd never touches the chain. Idempotent (skips seeded
  wallets via `clifwd nonce get` rc). clif's `install.sh` runs it automatically (step 5,
  best-effort, non-fatal). Components live-verified: address resolve from `clifwd wallets list`,
  idempotent skip (rc=4), JSON `latest` parse.
- **v0.5.20 (2026-06-08) — `install.sh` clone-into-place fix.** `fwd onboard --clif-env-dir
  /opt/clif` writes `.env.<net>` into `/opt/clif` BEFORE clif is installed, so the installer's
  empty-dir clone check failed → "no configuration file provided." Fixed: `install.sh` now
  `git init`+`fetch`+`checkout -f`s the source INTO a non-empty `/opt/clif`, preserving the
  gitignored `.env.<net>` (branches on `docker-compose.yml` presence for build-in-place).
  Verified offline (source laid down + `.env.songbird` preserved). Installer-only — no image
  or runtime change.
- **v0.5.19 (2026-06-08) — build-from-source network resilience (Dockerfile only).** Mirrors
  fwd a93 after the operator's fresh build hit an intermittent external link: runtime `apt`
  gains `Acquire::Retries=5`, and the runtime `pip install -r requirements.txt` + clif-wheel
  install (incl. the `fwd-client` git dep) are wrapped in a 3× retry at `--timeout 300`, so a
  transient unreachable/timeout on one package doesn't fail the image build. Build images
  SEQUENTIALLY (parallel fwd+clif builds saturate a constrained link). No code change; validated
  `docker compose build` → exit 0, `clif version` → `clif 0.5.19`.
- **v0.5.18 (2026-06-08) — clif deployed standalone (`clifctl`); de-intermingled from fwd.**
  fwd a92 made its installer fwd-only and dropped the bundled `docker-compose.clif.yml` overlay +
  `fwd start <net>`. clif now ships its OWN deployment: `install/clifctl` (up/down/restart/status/
  logs/run; project `clif`; joins fwd's `${FWD_NETWORK:-fwd_fwd-callers}` network external + its own
  `egress`) + `install/install.sh` (clone `/opt/clif` → build → install `clifctl`). No daemon code
  change — `docker-compose.yml` already declared `fwd-net` external + its own `egress`. The epoch
  daemon is launched by `clifctl up <net>`; manual ops via `clifctl run <net> …`. fwd's onboarding
  still provisions clif's `.env.<net>` (`fwd onboard … --clif-env-dir /opt/clif`).
- **v0.5.16–0.5.17 (2026-06-06) — epoch-anchored sign→claim daemon.** New `clif epoch run`
  (`clif/epoch_auto.py`, D17) replaces `clif auto` + `clif fsp auto` as the daemon: one
  per-network state machine sequencing uptime?→reward-sign→claim per reward epoch (§ Automation).
  0.5.17 adopts apgateway's timing model — FSM constants (`firstRewardEpochStartTs` +
  `rewardEpochDurationSeconds`) read once → `epoch_end_ts(N)` math + `next_sleep_seconds` precise
  idle/poll scheduling. Reads live-validated SGB+FLR (cross-checked vs `currentRewardEpochExpectedEndTs`).
  fwd install wiring shipped fwd a88; fwd a92 then de-intermingled clif into its OWN
  deployment, so the daemon is launched by `clifctl up <net>`, not `fwd start <net>`.
- **v0.5.8 (2026-05-31)** — docs-only professionalization (cross-repo pass with fwd):
  corrected "What clif is NOT" to the present (FSP signing is live + keyless via
  fwd's `/v1/sign-fsp-message` + `/v1/sign-transaction` — not "deferred"; dropped the
  retired `sign_and_send` wording), fixed the `decisions.md` range (D1–D16), and
  professionalized README + `docs/*` (current, consistent, github canonical-public).
- **v0.5.7 (2026-05-31)** — docs-only: retired stale `/v1/sign-and-send` references
  in the current-reference docs, aligned to the zero-egress `/v1/sign-transaction` +
  client-broadcast + report-back contract (`docs/fwd-contract.md`, `docs/verification.md`,
  `docs/onchain-migration.md`); historical binding specs `docs/phase8b-spec.md` and
  `docs/fsp-signing-tool-spec.md` carry a SUPERSEDED banner (body preserved). Added a
  "Run your own provider stack" section to `README.md` for third-party FTSO providers.
  No `*.py` logic changed.
- **v0.5.5 (2026-05-27) — epoch-400 live drill, FSP broadcast path fixed.** The Flare +
  Songbird mainnet drill surfaced two FSP defects invisible to the mocked tests ("mocks
  lie"): (1) the one-shot `clif fsp uptime/rewards` and `fsp auto` paths called `run_sign_*`
  **without `rpc=`** → clif signed but never broadcast; (2) FSP Leg-2 called `rpc.estimate_gas`
  with the **wallet NAME** as `from` (clif holds names, not addresses). Fixes: wire an
  `RpcClient` into all three FSP call sites; FSP submits use the **configured `fsp_submit_gas`**
  (no `estimate_gas`; fee market via `eth_feeHistory`, which needs no `from`). Verified
  end-to-end on mainnet: fee claim → `nothing-claimable`; FSP uptime → `nonce too low` (live
  ftso automation co-manages the sender nonce); FSP rewards → Merkle-root verified → mined →
  **reverted** (already signed) → honest `failed-terminal` (the mined-≠-success rule held).
- **v0.5.4 (2026-05-27) — adopted the shared `fwd-client` library.** clif's fwd transport now
  comes from the public, keyless `fwd-client` package: `FwdClient`, the
  `FwdError`/`FwdTerminalError`/`FwdRetryableError` taxonomy, `raise_for_fwd_error`, and the wire
  models. `clif/fwd_client.py` is a thin shim re-exporting that surface and keeping clif's
  **idempotency-key composition** (`make_idempotency_key`, `make_fsp_idempotency_key`). Keyless
  intact — the lib is httpx+pydantic only. One canonical impl of the fwd contract; future
  consumers depend on the same lib.
- **v0.5.2 (2026-05-27) — zero-egress fwd migration.** fwd is now **sign-only** (retired
  `/v1/sign-and-send` for `/v1/sign-transaction`; no longer broadcasts). clif asks fwd to SIGN,
  **broadcasts the returned `signed_raw_tx` itself** (`rpc.py` `eth_sendRawTransaction`), and
  **reports the outcome back** (`/v1/transactions/{tx_id}/broadcast-result` → poll
  `eth_getTransactionReceipt` → `/receipt`). clif computes its own gas + EIP-1559 fees
  (`rpc.estimate_gas` ×1.25, `rpc.suggest_fees` baseFee×2+1gwei, sanity-capped under fwd's
  `FWD_MAX_GAS`/`FWD_MAX_FEE_PER_GAS`). fwd allocates the nonce; `409 nonce_not_initialized` is
  terminal and means the (wallet, chain) needs a one-time fwd admin `nonce-init`. Both the
  reward-claim path (`claimer`) and FSP Leg-2 are migrated; **FSP Leg-1 (`/v1/sign-fsp-message`)
  is unchanged**. **502 is gone** (broadcast/RPC errors are clif's own). Keyless intact
  (broadcasting a fwd-signed blob is not signing).
- **v0.5.1 (2026-05-27) — reward-distribution Merkle verification** (`clif/merkle.py`). Builds +
  verifies the Flare fsp-rewards tree — leaf `keccak256(abi.encode((uint24,bytes20,uint120,uint8)))`
  (single keccak, not OZ double), sorted-pair internal nodes, sorted+deduped leaves; byte-exact vs
  flare epochs 228/400. Wired twice: `run_sign_rewards` **recomputes the root from the published
  claims and refuses to sign** if it ≠ the file's `merkleRoot` (FAILED_TERMINAL, no Leg-1 call) —
  the cryptographic upgrade of "never sign an unverified rewardsHash"; `discovery.reward_claim_for`
  **verifies each claim's proof** against the published root and refuses a claim whose proof doesn't
  verify. Pure computation via `eth_abi` + vendored `clif/_keccak`; keyless intact, no new crypto dep.
- **v0.5.0 (2026-05-19, D15) — corrective pass, two MAJOR defects.** (a) **Epoch-bind:**
  `reward-distribution-data.json` carries a top-level `rewardEpochId`; `RewardDistributionData` now
  requires it, validates `merkleRoot` `^0x[0-9a-fA-F]{64}$` and `noOfWeightBasedClaims` ≥ 0, and
  `run_sign_rewards` asserts `rdd.reward_epoch_id == reward_epoch_id` BEFORE Leg-1 (FAILED_TERMINAL,
  no sign call on mismatch). (b) **Two FSP caller tokens:** fwd forbids one `policy_path` key in both
  `permissions` and `fsp_permissions`, so one caller cannot span Leg-1 and Leg-2. `fsp_caller_token`
  replaced by `fsp_sign_caller_token` (Leg-1, `fsp_permissions`) and `fsp_submit_caller_token` (Leg-2 +
  tx poll, `permissions`); the orchestrator owns both clients. (c) **`FSP_AUTO_ENABLED` hard-off:**
  `clif fsp auto` refuses loudly (exit 2) unless `FSP_AUTO_ENABLED=true` — a valid signature over wrong
  data is irreversible on-chain. See D15 for the full rationale.
- **v0.4.0 (2026-05-19) — keyless FSP signing-tool added.** `fsp_calldata`, `fsp`, `fsp_autostate`
  modules; `fsp uptime|rewards|status|auto` CLI commands. Production FSP signing remains operator-gated
  (FSP caller token, signing + sender wallet names, FlareSystemsManager ABI + policy in fwd — see
  `docs/verification.md` F1/F2 and `docs/fsp-signing-tool-spec.md`).
- **2026-05-18 — keyless reward-claim half + Deliverable 2 shipped; AP-registered.** Claim + automation
  code complete (`claimer`/`autostate`, `claim`/`auto`/`status` CLI, Dockerfile + compose). Production
  Flare automation and the on-chain/`.env` steps operator-gated (fwd provisioned, new wallet authorized
  on-chain as executor first).

## fwd in one line

`POST /v1/sign-transaction` (Bearer caller token, deterministic `Idempotency-Key`)
→ `{tx_id, hash, signed_raw_tx, nonce}`; clif broadcasts + reports back
(`/v1/transactions/{tx_id}/broadcast-result`, `/receipt`). 401/403/404/400/409/503
are **terminal** (409 = nonce-not-initialized → operator runs `nonce-init`); there
is no 502 from fwd anymore. Require `/healthz` `master=="ok"`. Full contract:
`docs/fwd-contract.md`.

## Stack & layout

Python 3.12 · Poetry · Typer+rich · httpx (sync) · eth-abi · Pydantic v2.
`clif/`: `config` (network table + keyless settings + `assert_keyless`),
`models`, `rpc` (keyless view reads), `reward_data` (fsp-rewards + reward
distribution data), `discovery` (the >50% `rewardsHash` trigger), `calldata`
(ABI-derived, anchored selector), `fsp_calldata` (FSM selectors + UPTIME_VOTE_HASH
+ calldata builders), `fwd_client` (transport + terminal/retry classes +
`sign_fsp_message`), `claimer` (discover→submit), `fsp` (FSP Leg-1/Leg-2
orchestrator), `autostate` (degraded eval + status file), `fsp_autostate` (FSP
stream keys + build_fsp_report), `cli`; `clif/abi/` vendored ABIs; `tests/`;
`docs/`.

## Working in this repo

- **Surgical changes.** Touch only what the task needs; match existing style;
  every changed line traces to a task or a surfaced legitimate deviation.
- **Real-RPC verification is the validation — mocks lie.** A signing-path
  change is not done until proven against a live fwd + chain
  (`docs/verification.md`).
- **Operator gates production** (the Flare claim, the on-chain
  `setClaimExecutors`, the `.env` deletion). Build and rehearse freely;
  never cross those gates without explicit approval. Surface every deviation.
- **Never modify fwd or author fwd's `policy.yaml`.** If fwd needs a change
  (missing ABI, the `policy.example.yaml` defect, an endpoint gap), STOP and
  report to the operator — do not edit fwd.
- **Do not relitigate `docs/decisions.md`** without operator direction; keep
  doctrine and code aligned (update both or neither).
- Linear-forward version in `pyproject.toml` + `clif/__init__.py` on each ship.

## Commits

A single terse conventional line (`feat: update`, `fix: update`,
`docs: update`, …) — no body, no specifics. **Never** add a
`Co-Authored-By: Claude`, an AI co-author, or a "Generated with" line to any
commit, PR, tag, or release — strip it if a tool adds one. Operator is the
sole author. Do not push if a remote block exists — ask the operator.

## What clif is NOT

Not a signer, key store, or wallet. Not multi-chain beyond
Flare/Songbird/Coston2. No raw-digest signing. clif never holds a
`SIGNING_POLICY_PRIVATE_KEY` or any local key.

FSP protocol signing is live and keyless: `signUptimeVote` / `signRewards` are
signed via fwd's structured `POST /v1/sign-fsp-message` (Leg-1), then submitted
to `FlareSystemsManager` via `POST /v1/sign-transaction` + client broadcast
(Leg-2). Raw-digest signing and a local `SIGNING_POLICY_PRIVATE_KEY` remain out
of scope and forbidden — the signing key lives only in fwd's sealed master.

## Origin (provenance — not a dependency)

clif was built as fwd's Phase 8b consumer. Historical external artifacts —
the fwd repo, the AP root `proofs.africa/CLAUDE.md`, the canonical prompt at
`~/.claude/plans/fwd-phase8b-consumer-agent-prompt.md` — informed this repo
but are **not required** to work here; their durable content is vendored into
`docs/`. If they conflict with `docs/`, `docs/` (verified in-repo) wins for
clif's purposes; re-verify against a live fwd before production.
