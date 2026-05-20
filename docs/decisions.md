# Adjudicated decisions (do not relitigate)

> These are settled. Changing one requires explicit operator direction and a
> matching update here + in code/docs (doctrine and code must not drift).

- **D1 — Keyless.** clif holds zero private keys. No `eth-account`,
  `eth-keys`, `pycryptodome`, `web3`, `argon2`. keccak-256 is vendored
  (`clif/_keccak.py`) only to derive the `claim` selector. `assert_keyless()`
  refuses to start if any `*PRIVATE_KEY*` env var is present. *Why: clif's
  whole reason to exist is to delete the `.env PRIVATE_KEY=` (fwd Core inv #7).*
- **D2 — FEE + DIRECT parity.** One engine, `--type fee|direct`; same
  keyless→fwd path, only the beneficiary arg differs. *Why: operator-chosen
  2026-05-18; parity with the TS source, no extra key/fwd surface. Caveat: a
  real DIRECT calldata sample needs AP to actually accrue signing-policy
  direct rewards — never fabricate it (spec constraint 3).*
- **D3 — Automation = both forms.** `clif auto` (resilient daemon, Docker,
  restart:unless-stopped) **and** `clif claim` one-shot (rehearsal + manual).
  *Why: operator-chosen 2026-05-18. A scheduled Claude agent is the wrong tool
  for an unattended money path.*
- **D4 — Escalation = loud log + degraded status.** No external notification
  dep. `clif status` exits non-zero on degraded OR a dead/stale daemon; Docker
  healthcheck scrapes it. *Why: unclaimed FTSO rewards expire — a silent
  failure is the real risk; the TS tool's log-and-exit was rejected.*
- **D5 — Rotation is `setClaimExecutors`, not `setClaimRecipient`.** The keyed
  entity is the executor; the recipient is a keyless arg. fwd roadmap wording
  is drift; clif does not fix fwd (`docs/onchain-migration.md`).
- **D6 — Selector derived-from-ABI, anchored.** `clif/calldata.py`
  reconstructs the canonical signature from the vendored ABI, computes the
  selector, and asserts it `== 0x8e33aba5` at import (fail-loud). *Why: spec
  constraint 3/4 — never hardcode; cross-check the verified anchor.*
- **D7 — signing-tool deferred to fwd Phase 9.** `SIGNING_POLICY_PRIVATE_KEY`
  is never a local clif key. One README paragraph; no scaffold.
- **D8 — Operator gates production.** The Flare claim and the `.env` deletion
  happen only under explicit operator approval (Core invariant #15, inherited).
- **D9 — Commit doctrine.** Single terse conventional line; **never** any
  Claude/AI co-author or "Generated with" trailer; operator sole author; no
  push if a remote block exists (ask).
- **D10 — Sync, simple substrate.** httpx sync (short sequential paths),
  Typer, eth-abi, Pydantic v2. Stateless re "already claimed" (on-chain
  `getNextClaimableRewardEpochId` + idempotency replay are the source of
  truth); the only persisted artifact is the status JSON (no secrets).
- **D11 — Keyless gate covers the .env *file*, not just live env.**
  `assert_keyless` scans both `os.environ` and the resolved `.env` source
  file for `*PRIVATE_KEY*` names (pydantic silently ignores unknown `.env`
  keys, so a file-resident key would otherwise run green). Strengthens D1;
  a clean env+file still passes. *Why: fwd v1.0.0 audit STOP-SHIP #1 — the
  headline "the `.env PRIVATE_KEY=` line is gone" must not be falsifiable.*
- **D12 — Production idempotency = deterministic + explicit retry knob.**
  `make_idempotency_key(..., retry=None)` is byte-identical to the legacy
  key (same logical attempt — incl. network-retry / crash-rerun — dedups;
  no double-claim). An operator-controlled `retry` (`clif claim --retry` /
  `IDEMPOTENCY_RETRY` for `auto`) yields a fresh key for a **deliberate**
  post-on-chain-failure re-attempt (fwd replay is status-blind by design —
  fwd D14). Never auto-randomised. The `rehearse` `-r<tag>` discriminator
  stays walled off from this path. *Why: fwd v1.0.0 audit STOP-SHIP #2.*
- **D13 — fwd taxonomy + transport resilience.** 400/401/403/404/422
  (`transaction_rejected`) terminal; 502/503/any httpx transport error
  retryable. Transport errors are converted to `FwdRetryableError` in the
  sign + status + health paths — a down/restarting fwd degrades `clif auto`,
  never crashes it (D4 reward-expiry monitoring must survive fwd downtime).
  *Why: fwd v1.0.0 audit STOP-SHIP #3; fwd is correct-as-designed (a down
  fwd cannot emit a status — resilience is the consumer's).*
- **D14 — FSP signing-tool mandate expansion (2026-05-19).** D7 ("signing-tool
  deferred to fwd Phase 9") is **RESOLVED** for `signUptimeVote` /
  `signRewards` via fwd's new `POST /v1/sign-fsp-message` endpoint (Leg-1) +
  the existing `sign_and_send` to `FlareSystemsManager` (Leg-2). Settled:
  (a) keccak scope extends to FSM selectors + `fakeVoteHash` (`keccak256(0x00*32)`
  = UPTIME_VOTE_HASH); (b) two distinct fwd wallets/callers — `fsp_signing_wallet_name`
  (Leg-1) and `fsp_sender_wallet_name` (Leg-2) separate from the claim wallet; (c) FSP
  automation (`clif fsp auto`) implements both signing forms including a resilient
  unattended daemon whose wrong-data guard is strict file validation +
  deterministic idempotency with NO human confirm (operator-accepted 2026-05-19);
  (d) `chain_id` for the `_noOfWeightBasedClaims` tuple is taken from the static
  network table (`net.chain_id`) — no dynamic lookup required (static-table
  chain_id simplification); (e) raw-digest signing and `SIGNING_POLICY_PRIVATE_KEY`
  as a local key remain out of scope (Core invariant #7, D1).
  *Why: AP expanded clif's mandate to cover FSP message signing; fwd Phase 9
  is satisfied for the reward-epoch signing messages.*
- **D15 — FSP corrective pass + unattended REWARDS auto-signer (2026-05-19).**
  Appends to (does not edit) D14; supersedes/clarifies D14(c)'s thin
  "operator-accepted 2026-05-19 / NO human confirm" sub-claim (D14 frozen).
  Two MAJOR defects in the code-complete FSP client were corrected clif-side,
  additively, nothing committed (operator gates commits).
  **(a) Epoch-bind (MAJOR-1).** `reward-distribution-data.json` carries a
  top-level `rewardEpochId` (== directory epoch; confirmed flare/230,
  songbird/200, coston2/3156); upstream `signing-tool@838b87f`
  `getRewardsData()` asserts `data.rewardEpochId === rewardEpochId`. clif now
  mirrors it: `RewardDistributionData.reward_epoch_id` is required, `merkleRoot`
  is regex-validated `^0x[0-9a-fA-F]{64}$`, `noOfWeightBasedClaims` is
  validated integer ≥ 0, and `run_sign_rewards` asserts `rdd.reward_epoch_id
  == reward_epoch_id` BEFORE Leg-1 — a stale cache / wrong-epoch / wrong
  operator file is FAILED_TERMINAL with no sign call. The prior
  `"fsp rdd verified"` log (which verified nothing about the epoch) is replaced
  with one stating exactly what was bound.
  **(b) Two FSP caller tokens (MAJOR-2).** fwd's policy loader forbids the
  same `policy_path` key in both `permissions` and `fsp_permissions`
  (cross-domain key reuse = fail-fast boot), so one caller authorizes EITHER
  `/v1/sign-fsp-message` OR `/v1/sign-and-send`, never both. clif replaces the
  single `fsp_caller_token` with `fsp_sign_caller_token` (Leg-1) and
  `fsp_submit_caller_token` (Leg-2 + the per-caller-scoped tx poll), both
  distinct from the fee-claimer `fwd_caller_token`. The two FSP wallets are
  unchanged. The orchestrator owns both clients (the per-leg→caller mapping is
  centralized; the CLI no longer builds/passes an FSP `FwdClient`). Operator
  provisions two fwd callers (`clif-fsp-sign` → `fsp_permissions`;
  `clif-fsp-submit` → `permissions` for FlareSystemsManager) — an operator
  task; clif never authors fwd policy nor mints credentials.
  **(c) Unattended REWARDS auto-signer — operator explicitly ACCEPTED
  2026-05-19.** §5 risk in plain terms: an unattended signer that signs over
  the WRONG data still produces a cryptographically valid signature, and that
  is irreversible on-chain — strictly worse than no signature at all. The
  guard stack accepted as sufficient: epoch-bound rdd (the `rewardEpochId`
  equality assert of (a), which the auto path inherits because the bind lives
  in `run_sign_rewards` — "no auto-sign of REWARDS over an unbound merkle
  root, ever"); strict file validation (merkleRoot regex + n≥0); deterministic
  idempotency (fwd dedups a re-run); fwd's on-chain already-signed revert; and
  OFF-BY-DEFAULT with an explicit `FSP_AUTO_ENABLED=true` enable (`clif fsp
  auto` refuses loudly and terminally otherwise). **clif-only /
  fwd-automation-agnostic boundary:** the auto path is entirely clif-side; it
  introduces/assumes/requests NO fwd-side automation, scheduling, auto-endpoint,
  or automation-aware policy (the implementation has zero fwd-automation
  footprint — kept and stated).
  *Why: AP/operator accepted (2026-05-19) the unattended REWARDS auto-signer
  under the above guard stack; the two MAJOR defects were corrected clif-side,
  surgically and additively, before any on-chain use (GATE-1 remains
  environment-deferred — nothing here is claimed on-chain-proven).*
