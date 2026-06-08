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
| `docs/decisions.md` | Settled decisions — **do not relitigate** (D1–D16). |
| `docs/fwd-contract.md` | Verified fwd HTTP + ABI contract; the policy block; the `policy.example.yaml` trap. |
| `docs/onchain-migration.md` | Networks/addresses, actors, the >50% trigger, the operator-gated rotation, the `setClaimExecutors` drift. |
| `docs/verification.md` | Verification ladder (proven vs blocked), rehearsal ladder, pre-flight traps, local checks. |
| `docs/fwd-integration-spec.md` | The operator handshake artifact (regenerate with `clif spec`). |

## Status — current: v0.5.17

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
the returned `signed_raw_tx` itself** and **reports the outcome back** to fwd. 216
tests green. Build via the shared `fwd-client` lib
(`github.com/africanproofs/fwd-client`, `subdirectory=python`, tag **v0.1.1**); fwd
error classification is **class-based** (`FwdRetryableError`/`FwdTerminalError`, never
`error_code`) — see `docs/fwd-contract.md` § Error taxonomy and `docs/decisions.md` D18.

**Changelog (condensed):**

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
