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

## fwd in one line

`POST /v1/sign-and-send` (Bearer caller token, deterministic
`Idempotency-Key`); 401/403/404/400/503 are **terminal** (escalate, do not
retry), 502 is **retryable**; require `/healthz` `master=="ok"`. Full,
verified contract: `docs/fwd-contract.md`.

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
