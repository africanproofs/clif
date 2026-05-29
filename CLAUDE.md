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

## Knowledge base (authoritative, in-repo)

Read these before non-trivial work; they are the binding references:

| file | what |
|---|---|
| `docs/phase8b-spec.md` | **Binding spec** (vendored canonical prompt). Authoritative; decisions adjudicated. |
| `docs/decisions.md` | Settled decisions — **do not relitigate** (D1–D10). |
| `docs/fwd-contract.md` | Verified fwd HTTP + ABI contract; the policy block; the `policy.example.yaml` trap. |
| `docs/onchain-migration.md` | Networks/addresses, actors, the >50% trigger, the operator-gated rotation, the `setClaimExecutors` drift. |
| `docs/verification.md` | Verification ladder (proven vs blocked), rehearsal ladder, pre-flight traps, local checks. |
| `docs/fwd-integration-spec.md` | Deliverable 2 — the operator handshake artifact (regenerate with `clif spec`). |

## Status (2026-05-18)

Keyless half + Deliverable 2 shipped; AP-registered; on private
`github.com/africanproofs/clif`. Claim + automation **code complete**
(`claimer`/`autostate`, `claim`/`auto`/`status` CLI, Dockerfile + compose; 38
tests, ruff clean). Production Flare automation and the on-chain/`.env` steps
remain **operator-gated** (fwd must be provisioned and the new wallet
authorized on-chain as executor first). See `docs/verification.md` for the
exact rung-by-rung state.

## Status (2026-05-19)

Keyless FSP signing-tool added. `fsp_calldata`, `fsp`, `fsp_autostate` modules
code complete; `fsp uptime|rewards|status|auto` CLI commands added; 6 new test
files (124 tests total), ruff clean, v0.4.0. Production FSP signing and the
fwd provisioning steps remain **operator-gated** (FSP caller token, signing +
sender wallet names, FlareSystemsManager ABI + policy in fwd — see
`docs/verification.md` F1/F2 rungs and `docs/fsp-signing-tool-spec.md`).

**Corrective pass (2026-05-19, D15, v0.5.0):** Two MAJOR defects corrected
clif-side, surgically and additively (nothing committed — operator gates
commits). (a) **Epoch-bind (MAJOR-1):** `reward-distribution-data.json` carries
a top-level `rewardEpochId`; `RewardDistributionData` now requires it,
validates `merkleRoot` `^0x[0-9a-fA-F]{64}$` and `noOfWeightBasedClaims` ≥ 0,
and `run_sign_rewards` asserts `rdd.reward_epoch_id == reward_epoch_id` BEFORE
Leg-1 (FAILED_TERMINAL with no sign call on mismatch — stale cache / wrong
file). (b) **Two FSP caller tokens (MAJOR-2):** fwd forbids one `policy_path`
key in both `permissions` and `fsp_permissions`, so one caller cannot span
Leg-1 and Leg-2. `fsp_caller_token` replaced by `fsp_sign_caller_token`
(Leg-1, `fsp_permissions`) and `fsp_submit_caller_token` (Leg-2 + tx poll,
`permissions`). The orchestrator owns both clients; CLI no longer builds/passes
an FSP `FwdClient`. (c) **`FSP_AUTO_ENABLED` hard-off:** `clif fsp auto`
refuses loudly (exit 2, D15 message) unless `FSP_AUTO_ENABLED=true` — a valid
signature over wrong data is irreversible on-chain. GATE-1 (F1/F2) remains
environment-deferred; nothing here is claimed on-chain-proven. See D15 for the
full rationale and accepted guard stack.

## Status (2026-05-27, v0.5.1)

Reward-distribution **Merkle verification** added (`clif/merkle.py`). Builds +
verifies the Flare fsp-rewards tree — leaf `keccak256(abi.encode((uint24,bytes20,
uint120,uint8)))` (single keccak, not OZ double), sorted-pair internal nodes,
sorted+deduped leaves; byte-exact vs flare epochs 228/400. Wired in two places:
`run_sign_rewards` now **recomputes the root from the published claims and refuses
to sign** (FAILED_TERMINAL, no Leg-1 call) if it ≠ the file's `merkleRoot` — the
cryptographic upgrade of the "never sign an unverified rewardsHash" rule (was
epoch-bind only); `discovery.reward_claim_for` **verifies each claim's proof**
against the published root and refuses a claim whose proof doesn't verify (no
gas-wasting chain-rejected submit). Pure computation via `eth_abi` + the vendored
`clif/_keccak` — the keyless invariant is intact, no new crypto dep. 156 tests.

## Status (2026-05-27, v0.5.2) — zero-egress fwd migration

fwd v1.1.0a9+ is **sign-only** (it retired `/v1/sign-and-send` for
`/v1/sign-transaction` and no longer broadcasts). clif is migrated: it now asks
fwd to SIGN, then **broadcasts the returned `signed_raw_tx` itself** (via `rpc.py`
`eth_sendRawTransaction`) and **reports the outcome back** to fwd
(`/v1/transactions/{tx_id}/broadcast-result` → poll `eth_getTransactionReceipt` →
`/v1/transactions/{tx_id}/receipt`). clif computes its own gas + EIP-1559 fees
(`rpc.estimate_gas` ×1.25, `rpc.suggest_fees` baseFee×2+1gwei, sanity-capped under
fwd's `FWD_MAX_GAS`/`FWD_MAX_FEE_PER_GAS`). fwd allocates the nonce; a
`409 nonce_not_initialized` is terminal and means the (wallet, chain) needs a
one-time fwd admin `nonce-init` (operator setup). Both the reward-claim path
(`claimer`) and FSP Leg-2 (`fsp`, the FlareSystemsManager submit) are migrated;
**FSP Leg-1 (`/v1/sign-fsp-message`) is unchanged**. The mined-≠-success effect
rule (RewardClaimed event / `MINED_NOOP`) is unchanged. 176 tests, keyless intact
(broadcasting a fwd-signed blob is not signing). **502 is gone** (fwd no longer does
RPC; broadcast/RPC errors are clif's own). **Production claim/FSP on-chain
verification remains operator-gated** (the coordinated cutover) — code + mocked
tests done; live rehearsal against the running fwd is the operator's gate.

## Status (2026-05-27, v0.5.5) — epoch-400 live drill: FSP broadcast path fixed

The epoch-400 mainnet drill (Flare + Songbird, through the migrated zero-egress
stack) surfaced — and fixed — **two FSP defects invisible to the mocked tests**
(the "mocks lie" rule, in the wild): (1) the one-shot `clif fsp uptime/rewards`
commands and the `fsp auto` path called `run_sign_*` **without `rpc=`** → clif
signed but never broadcast (`no rpc — cannot broadcast`); (2) FSP Leg-2 called
`rpc.estimate_gas` with the **wallet NAME** (`fsp_sender_wallet_name`) as `from` —
clif holds names, not addresses — and `estimateGas` reverts on an already-signed
epoch anyway. Fixes: wire an `RpcClient` into all three FSP call sites; FSP submits
now use the **configured `fsp_submit_gas`** (no `estimate_gas`; fee market still via
`eth_feeHistory`, which needs no `from`). **Verified end-to-end on mainnet, all
expected:** fee claim → `nothing-claimable` (400 already claimed); FSP uptime →
broadcast `nonce too low` (live ftso automation co-manages the sender nonce); FSP
rewards → Merkle-root verified → mined → **reverted** (already signed) → reported
back → honest `failed-terminal` (the mined-≠-success rule held). 176 tests green.
(Pre-existing mypy debt: 7 errors — typer/rich stubs + `Optional[str]` config args
— predate this; a separate cleanup.)

## Status (2026-05-27, v0.5.4) — adopted the shared fwd-client library

clif's fwd transport now comes from the shared **`fwd-client`** package
(`gitlab.com/proofs.africa/fwd-client` v0.1.0, public, keyless): `FwdClient`, the
`FwdError`/`FwdTerminalError`/`FwdRetryableError` taxonomy, `raise_for_fwd_error`,
and the wire models (`SignTransaction*`, `BroadcastResult*`, `Receipt*`,
`SignFspMessageResponse`, `TxStatus`, `Health`) are imported from it. `clif/fwd_client.py`
is now a thin shim re-exporting that surface and keeping clif's **idempotency-key
composition** (`make_idempotency_key`, `make_fsp_idempotency_key`) which delegates
hashing to the lib's generic `make_idempotency_key`. clif's business models
(`RewardsData`, reward claims, Merkle) are unchanged. Dockerfile gained `git` (to
clone the HTTPS git-dep at build). **Keyless intact** — the lib is httpx+pydantic
only; no crypto/signing dep added. 176 tests green; `docker compose build clif` ok.
One canonical impl of the fwd contract now — future consumers depend on the same lib.

## fwd in one line

`POST /v1/sign-transaction` (Bearer caller token, deterministic `Idempotency-Key`)
→ `{tx_id, hash, signed_raw_tx, nonce}`; clif broadcasts + reports back
(`/v1/transactions/{tx_id}/broadcast-result`, `/receipt`). 401/403/404/400/409/503
are **terminal** (409 = nonce-not-initialized → operator runs `nonce-init`); there
is no 502 from fwd anymore. Require `/healthz` `master=="ok"`. Full contract:
`docs/fwd-contract.md` (note: that file still documents the retired sign-and-send
shape — update pending).

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
Flare/Songbird/Coston2. No raw-digest signing. The flare-foundation
signing-tool / `SIGNING_POLICY_PRIVATE_KEY` is out of scope — deferred to fwd
Phase 9 (a structured protocol-message signer), never a local key.

RESOLVED 2026-05-19: D7 ("signing-tool deferred") is resolved for the
`signUptimeVote` / `signRewards` FSP protocol messages via
`POST /v1/sign-fsp-message` (Leg-1) + `sign_and_send` to FlareSystemsManager
(Leg-2). Raw-digest signing and `SIGNING_POLICY_PRIVATE_KEY` as a local key
remain out of scope and forbidden.

## Origin (provenance — not a dependency)

clif was built as fwd's Phase 8b consumer. Historical external artifacts —
the fwd repo, the AP root `proofs.africa/CLAUDE.md`, the canonical prompt at
`~/.claude/plans/fwd-phase8b-consumer-agent-prompt.md` — informed this repo
but are **not required** to work here; their durable content is vendored into
`docs/`. If they conflict with `docs/`, `docs/` (verified in-repo) wins for
clif's purposes; re-verify against a live fwd before production.
