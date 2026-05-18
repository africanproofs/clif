# Verification & rehearsal (in-repo reference)

> What "confirmed working" means for clif, what is proven, and what blocks
> each remaining rung. Doctrine: **real-RPC verification is the validation —
> mocks lie.** A change to the signing path is not "done" until proven against
> a live fwd + chain.

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

- `POST /v1/sign-and-send` → 200.
- `GET /v1/transactions/{tx_id}` polls to `status=mined`.
- On-chain `eth_getTransactionReceipt`: `status=0x1`, `to` == the network's
  RewardManager, **on-chain `from` == the fwd-custodied wallet** (secp256k1
  recovery proves fwd signed — clif holds no key).
- Idempotency replay of the same key → **same `tx_id`**, no second broadcast.
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
