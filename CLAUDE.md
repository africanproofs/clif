# clif — keyless FTSO reward claimer

> Python successor to `ftso-fee-claimer`. Claims AP's FTSO v2 rewards (FEE +
> DIRECT) on Flare/Songbird/Coston2 by calling the `fwd` signing daemon —
> **clif holds zero private keys**. This is **Phase 8b** of the fwd program:
> the first deleted `.env PRIVATE_KEY=` line, the event that lifts fwd's
> doctrine-ship freeze.

## Identity

`clif` does all reward discovery (claimable epochs, Merkle proofs from
fsp-rewards) keylessly — `eth_call` view reads + public HTTP. The single key
operation, `RewardManager.claim(...)`, is performed by building the calldata
locally and calling fwd `POST /v1/sign-and-send`. fwd holds the key, gates the
call by policy, signs, broadcasts. The `.env` private key the TS tool held was
`CLAIM_EXECUTOR_PRIVATE_KEY` (the *executor*); under clif that key lives only
in fwd's sealed master, and the new fwd wallet is authorized on-chain as
executor via `ClaimSetupManager.setClaimExecutors` (operator, offline identity
key — not fwd, not clif).

The binding spec for this project is the Phase 8b canonical prompt at
`~/.claude/plans/fwd-phase8b-consumer-agent-prompt.md` (authoritative — "do
not relitigate"). fwd at `/home/l/working/gitlab.com/proofs.africa/fwd` is
**read-only reference**; clif never modifies it or authors its `policy.yaml`.

## Scope / status

- **v0.1.0 (keyless half — shipped):** project scaffold; keyless discovery
  (`config`, `models`, `rpc`, `reward_data`, `discovery`, `calldata` with the
  ABI-derived selector anchored to `0x8e33aba5`); fwd transport (`fwd_client`);
  CLI `version` / `health` / `list` / `spec`; 21 unit tests; vendored
  keccak-256 (no `eth-hash` backend). `docs/fwd-integration-spec.md`
  (Deliverable 2) generated from the live keyless path.
- **Next (operator-gated — plan step 4):** `claimer.py` orchestration + `claim`
  / `auto` CLI + Dockerfile/compose. Lands only **after** the operator reviews
  `docs/fwd-integration-spec.md` and provisions the matching fwd policy +
  wallet + caller token.

## Core invariant (clif-specific)

**clif holds zero private keys.** No `.env PRIVATE_KEY=`; no local-signing
dependency (`eth-account`, `eth-keys`, `pycryptodome`, `web3`, `argon2`).
`clif.config.assert_keyless()` makes clif refuse to start if any
`*PRIVATE_KEY*` env var is present (fwd Core invariant #7, operationalised).
keccak-256 is vendored solely to derive the `claim` selector from the ABI.

## fwd integration contract (verified — `fwd/src/fwd/api/sign.py`)

`POST /v1/sign-and-send` body `{wallet, chain, to, value_wei, data, gas}`,
header `Idempotency-Key`, auth `Authorization: Bearer fwd_live_…`. Success
**200** `{tx_id, hash, nonce}`. Errors `{error,message}`: 400/401/403/404/503
**terminal** (never retry), 502 **retryable**. Status: `GET
/v1/transactions/{tx_id}`. Readiness: `GET /healthz` (require `master=="ok"`).

## Stack & layout

Python 3.12, Poetry, Typer+rich, httpx (sync — short sequential paths),
`eth-abi` for calldata, Pydantic v2 / pydantic-settings. `clif/` package:
`config` (network table + keyless settings), `models`, `rpc`, `reward_data`,
`discovery`, `calldata`, `fwd_client`, `cli`; `clif/abi/` vendored ABIs;
`tests/`; `docs/fwd-integration-spec.md`.

## Workflow & safety (inherited from the AP constitution + fwd doctrine)

- Operator gates the production Flare claim and the `.env` deletion (fwd Core
  invariant #15). Build/rehearse freely; never cross those gates unprompted.
- Rehearsal ladder: Coston2 → Songbird → Flare; clif-**generated** calldata
  only; verify on-chain `from` == the fwd-custodied wallet.
- Commits: a single terse conventional line (`feat: update`, …). **Never** add
  a Claude/AI co-author or "Generated with" trailer — strip it if a tool adds
  one. Operator is sole author. Do not push if the GitLab account block is in
  effect — ask the operator.
- If clif integration reveals fwd needs any change (ABI/policy/endpoint/the
  `docs/policy.example.yaml` wrong-signature defect), **STOP and report to the
  operator** — do not edit fwd.

## What clif is NOT

Not a signer, not a key store, not a wallet. Not multi-chain beyond
Flare/Songbird/Coston2. No raw-digest signing. The flare-foundation
signing-tool / `SIGNING_POLICY_PRIVATE_KEY` is **out of scope** — deferred to
fwd Phase 9 (a structured protocol-message signer), never a local key.
