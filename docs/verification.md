# Verification & rehearsal (in-repo reference)

> What "confirmed working" means for clif, what is proven, and what blocks
> each remaining rung. Doctrine: **real-RPC verification is the validation —
> mocks lie.** A change to the signing path is not "done" until proven against
> a live fwd + chain.
>
> fwd is **zero-egress sign-only**: clif POSTs `/v1/sign-transaction`,
> **broadcasts the returned `signed_raw_tx` itself**, then reports the outcome
> back (`/v1/transactions/{tx_id}/broadcast-result` → poll
> `eth_getTransactionReceipt` → `/receipt`). fwd never broadcasts.

## Verification ladder

| rung | confirms | status (2026-05-18) | blocker |
|---|---|---|---|
| 0 | unit logic (encode, classify, selector) | ✅ done | — 38 tests, ruff clean, selector anchored `0x8e33aba5` |
| 1 | keyless discovery vs live chain, empty case | ✅ done | — `list`/`spec` ran vs live Flare |
| 2 | keyless discovery with a **real reward** (fsp-rewards fetch + real proofs + real calldata) | ⛔ unproven | timing/data: AP has nothing claimable now; needs a live claimable epoch |
| 3 | clif ↔ fwd transport (health/auth/sign/deny) | ⛔ unproven | env: no fwd reachable; + operator: fwd not provisioned |
| 4 | end-to-end on Coston2 (mined, `from`==fwd wallet) | ⛔ unproven | rung 3 + on-chain `setClaimExecutors` |
| 5 | real Flare claim | ⛔ unproven | all above + operator approval (Core inv #15) |
| 6 | daemon/Docker/healthcheck in situ | ⛔ unproven | env: no Docker host here |

None of the open rungs is a clif **code** defect. Blockers are: this
environment (no fwd, no Docker), the deliberate operator gate, and a
~3.5-day reward-epoch cadence we don't control.

## The one lever available without the operator gate

Rung 2: catch a window when AP actually has a claimable Flare epoch and run
`clif spec` — this exercises `reward_data` + real proof fetch + real calldata
build and fills `docs/fwd-integration-spec.md`'s pending real sample. Can be
poll-driven (e.g. `/loop`). Everything past rung 2 needs the operator + a host
running fwd + Docker.

## Rehearsal ladder (operator-gated; the binding control)

`clif rehearse` is the binding command: it builds the claim via the real
`build_claim_calldata` (real discovery first, empty *real* proofs if nothing
is genuinely claimable — the least hand-modeled valid shape) and POSTs it to
fwd with an explicit `gas` so fwd skips `eth_estimateGas` (a reverting
rehearsal claim would otherwise abort pre-broadcast). The mined tx's on-chain
`from` == the fwd-custodied wallet is the custody proof. Each `clif rehearse`
attempt uses a rehearsal-only idempotency discriminator (`-r<tag>`, default
unix ts) so fwd cannot replay a stale prior outcome when the reward epoch has
not rolled; the production claim/auto path keeps the deterministic key (D10).

Run with clif-**generated** calldata only (never a hand-built shape):

1. **Coston2** (cheap) → 2. **Songbird** (lower-stakes real) → 3. **Flare**
(production — explicit operator approval).

Per-rung pass criteria:

- `POST /v1/sign-transaction` → 200 (`{tx_id, hash, signed_raw_tx, nonce}`).
- clif **broadcasts `signed_raw_tx`** itself (`eth_sendRawTransaction`) and
  POSTs `/v1/transactions/{tx_id}/broadcast-result` (`accepted`).
- clif polls `eth_getTransactionReceipt` to mined, then POSTs
  `/v1/transactions/{tx_id}/receipt` (`mined_success`/`mined_reverted`).
- On-chain `eth_getTransactionReceipt`: `status=0x1`, `to` == the network's
  RewardManager, **on-chain `from` == the fwd-custodied wallet** (secp256k1
  recovery proves fwd signed — clif holds no key).
- Idempotency replay of the same key (same body) → **same `tx_id`**, no second
  sign.
- A policy-denied request → **403, not retried**.
- Operator runs `clifwd audit verify` inside the fwd container →
  `chain intact: N rows`, exit 0.

## Pre-flight traps

- **fwd `policy.example.yaml`** — was wrong for `claim`, now **fixed upstream**
  (fwd v1.0.0 fold-in; line 56 is canonical). Still write the policy from the
  signature in `docs/fwd-contract.md` / `docs/fwd-integration-spec.md §2`
  (which now matches the fwd example).
- **No `setClaimExecutors` yet** → a perfect clif→fwd→sign still reverts
  on-chain until the offline identity key authorizes the fwd wallet
  (`docs/onchain-migration.md` step 3).
- **fwd network name** → the `clif-auto` container must share fwd's Docker
  network (`FWD_NETWORK`, default `fwd_fwd-callers`) or `FWD_ENDPOINT` must be
  otherwise reachable.

## Local checks (always runnable, no fwd/Docker)

```
poetry install && poetry run pytest -q && poetry run ruff check .
clif health    # expects fwd; fails closed when unreachable (correct)
clif list      # live keyless discovery
clif spec      # regenerate the operator handshake artifact
clif status    # exit 3 with no daemon (correct)
clif rehearse  # rehearsal-ladder fwd-custody proof (needs fwd + caller token)
```

## FSP verification ladder (2026-05-19)

| rung | confirms | status | blocker |
|---|---|---|---|
| F0 | FSP unit logic: selectors, UPTIME_VOTE_HASH, calldata builders, cross-field validation (merkleRoot regex, n≥0), epoch-bind (`rdd.reward_epoch_id==signing_epoch`), two-caller per-leg mapping, oracle vector parse | ✅ done | — see suite (ruff clean, both selectors anchored `0xdc5a4225`/`0xc00a1a97`) |
| F1 | clif ↔ fwd FSP transport (Leg-1 `/v1/sign-fsp-message` with `FSP_SIGN_CALLER_TOKEN`; Leg-2 `/v1/sign-transaction` + client broadcast + tx poll with `FSP_SUBMIT_CALLER_TOKEN`) | ⛔ GATE-1 env-deferred | env: `FSP_SIGN_CALLER_TOKEN` / `FSP_SUBMIT_CALLER_TOKEN` / `FSP_SIGNING_WALLET_NAME` / `FSP_SENDER_WALLET_NAME` not provisioned; two fwd FSP callers not created; fwd `/v1/sign-fsp-message` endpoint not confirmed |
| F2 | end-to-end FSP on Coston2 (mined, correct `from`); live byte-match of oracle vectors | ⛔ GATE-1 env+operator | F1 + operator provisions fwd FSP policy + wallets; on-chain FSP registration |

F1/F2 are environment-deferred. No code defect blocks them. GATE-1 remains
(nothing here is claimed on-chain-proven). Operator items (see D15 MAJOR-2):
- Provision `FSP_SIGN_CALLER_TOKEN` (`clif-fsp-sign` caller → `fsp_permissions`
  block in fwd; authorizes Leg-1 `/v1/sign-fsp-message`)
- Provision `FSP_SUBMIT_CALLER_TOKEN` (`clif-fsp-submit` caller → `permissions`
  block for FlareSystemsManager; authorizes Leg-2 + per-caller-scoped tx poll)
- Create `FSP_SIGNING_WALLET_NAME` and `FSP_SENDER_WALLET_NAME` in fwd
- Add `FlareSystemsManager` ABI + FSP policy to fwd (clif never authors fwd policy)
- Register AP's signing wallet on-chain for the FSP role

Set `CLIF_FSP_LIVE_FWD=1` + env vars to run the live byte-match upgrade in
`test_fsp_integration_oracle.py::test_live_uptime_byte_match` (uses
`FSP_SIGN_CALLER_TOKEN` — the Leg-1 sign caller).
