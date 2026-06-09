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

> **Epoch daemon (v0.5.16/0.5.17, the canonical `clif epoch run`).** The new
> epoch-anchored sign→claim state machine reuses the same claim/FSP transport rungs
> below — its on-chain gates are identical (rung 4 `setClaimExecutors` for claiming;
> F2 FSP acceptance for signing). What is **proven** for the daemon specifically: its
> on-chain READS + timing are live-validated on Songbird + Flare — `epoch_end_ts(N)`
> (derived from `firstRewardEpochStartTs`+`rewardEpochDurationSeconds`) equals the
> contract's own `currentRewardEpochExpectedEndTs()` exactly, and `voter_*_sign_info` /
> reward-publication reads decode correctly (cross-checked vs flarestack). What is
> **pending**: the end-to-end sign→finalize→claim execution at the next ended epoch
> (same gate as rungs 4 / F2). See `README.md` § Automation and
> `docs/decisions.md` D17.

## Verification ladder

| rung | confirms | status | blocker |
|---|---|---|---|
| 0 | unit logic (encode, classify, selector) | ✅ done | — ruff clean, selector anchored `0x8e33aba5` |
| 1 | keyless discovery vs live chain, empty case | ✅ done | — `list`/`spec` ran vs live Flare |
| 2 | keyless discovery with a **real reward** (fsp-rewards fetch + real proofs + real calldata) | ✅ **proven** | reward epoch 404: the automated claim fetched the real reward distribution, built real proofs + real calldata, and claimed on both chains |
| 3 | clif ↔ fwd transport (health/auth/sign/deny) | ✅ done | — proven in the epoch-400 mainnet drill |
| 4 | end-to-end claim, mined, on-chain `from` == fwd wallet | ✅ **proven** | reward epoch 404: claimed end-to-end, unattended, on both chains (executor `setClaimExecutors`-authorized) |
| 5 | real Flare production claim | ✅ **proven** | reward epoch 404: real production reward claim on both Flare + Songbird mainnets, automated |
| 6 | daemon/Docker/healthcheck in situ | ✅ done | — fwd daemon reached over its Docker network in the drill |

All rungs are now proven. Reward epoch 404 closed the last open ones end-to-end:
rung 2 (real reward fetch + real proofs + real calldata), rungs 4–5 (end-to-end +
real production claim, mined, on both Flare + Songbird mainnets) — all in one
unattended `clif epoch run` cycle (executor `setClaimExecutors`-authorized; the
Core-inv-#15 operator gate satisfied at the production cutover). The earlier
epoch-400/401 mainnet drills had already proved the transport, client-broadcast,
report-back, and daemon-in-situ paths against live fwd + chain.

## The one lever available without the operator gate

Rung 2: catch a window when AP actually has a claimable Flare epoch and run
`clif spec` — this exercises `reward_data` + real proof fetch + real calldata
build and fills the pending real sample in `docs/fwd-integration-spec.md §3`.
Can be poll-driven (e.g. `/loop`). Everything past rung 2 needs the operator
plus a host running fwd + Docker.

## Rehearsal ladder (operator-gated; the binding control)

`clif rehearse` is the binding command: it builds the claim via the real
`build_claim_calldata` (real discovery first, empty *real* proofs if nothing
is genuinely claimable — the least hand-modeled valid shape) and POSTs it to
fwd with an explicit `gas` (clif's fixed `fsp_submit_gas`) — clif is keyless and
broadcasts itself, so the explicit gas avoids a revert aborting pre-broadcast
(fwd itself never estimates gas; it is zero-egress). The mined tx's on-chain
`from` == the fwd-custodied wallet is the custody proof. Each `clif rehearse`
attempt uses a rehearsal-only idempotency discriminator (`-r<tag>`, default
unix ts) so fwd cannot replay a stale prior outcome when the reward epoch has
not rolled; the production claim/auto path keeps the deterministic key (D12).

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

- **`claim` policy signature** — write the policy from the canonical signature
  in `docs/fwd-contract.md` / `docs/fwd-integration-spec.md §2`; it matches
  `fwd/docs/policy.example.yaml`.
- **No `setClaimExecutors` yet** → a perfect clif→fwd→sign still reverts
  on-chain until the offline identity key authorizes the fwd wallet
  (`docs/onchain-migration.md` step 3).
- **fwd network name** → the `clif-epoch-<net>` container (canonical daemon; or the
  legacy `clif-auto`) must share fwd's Docker network (`FWD_NETWORK`, default
  `fwd_fwd-callers`) or `FWD_ENDPOINT` must be otherwise reachable.

## Local checks (always runnable, no fwd/Docker)

```
poetry install && poetry run pytest -q && poetry run ruff check .
clif health    # expects fwd; fails closed when unreachable (correct)
clif list      # live keyless discovery
clif spec      # regenerate the operator handshake artifact
clif epoch status  # canonical daemon health; exit 3 with no daemon (correct)
clif status        # legacy claim-loop health
clif rehearse  # rehearsal-ladder fwd-custody proof (needs fwd + caller token)
```

## FSP verification ladder

| rung | confirms | status | blocker |
|---|---|---|---|
| F0 | FSP unit logic: selectors, UPTIME_VOTE_HASH, calldata builders, cross-field validation (merkleRoot regex, n≥0), epoch-bind (`rdd.reward_epoch_id==signing_epoch`), two-caller per-leg mapping, oracle vector parse | ✅ done | — both selectors anchored `0xdc5a4225`/`0xc00a1a97` |
| F1 | clif ↔ fwd FSP transport (Leg-1 `/v1/sign-fsp-message` with `FSP_SIGN_CALLER_TOKEN`; Leg-2 `/v1/sign-transaction` + client broadcast + client-side `eth_getTransactionReceipt` poll + report-back, with `FSP_SUBMIT_CALLER_TOKEN`) | ✅ done | — both legs exercised in the epoch-400 mainnet drill (uptime + rewards signed, broadcast, reported back) |
| F2 | end-to-end FSP **accepted** on-chain by the `FlareSystemsManager` (mined, correct `from`, no revert, signature recovered to a registered voter) | ✅ **proven** | reward epoch 404 (2026-06): our signing-policy key's `RewardsSigned` events landed + the epoch finalized on Songbird (`0x8eb571…eab5`, 50.43%) and Flare (`0xff02f8…095dd`, 51.77%), then rewards claimed — all unattended; the imported key is an accepted registered voter |

The clif ↔ fwd ↔ chain **integration** is proven (F1) AND on-chain **protocol
acceptance** is now proven (F2): for reward epoch 404 the daemon signed
REWARD_DISTRIBUTION via fwd, broadcast the Leg-2 submission, and our signing-policy
key's `RewardsSigned` event landed and the epoch finalized on **both** Songbird and
Flare (leading hash == on-chain `rewardsHash`), then the rewards were claimed — all
in one unattended `clif epoch run` cycle. That a `signRewards` was accepted proves
the imported key (`0xfB021c…` SGB / `0x3FA07a…` FLR) is an accepted registered voter
(it passes the same signer-registration check `signUptimeVote` does). Evidence:
`clif epoch signing-progress --epoch 404` (our_signed = true both chains; leading
hash == on-chain `rewardsHash(404)`). Note: a live `signUptimeVote` still reverts on
the FSM **window guard** (`epoch not ended yet`) when submitted too early, and a
`rewards hash already signed` revert just means the epoch finalized before our sig
landed (benign) — neither bears on acceptance, which is now demonstrated by the
landed reward signature.

Historical operator items that gated F2 (now satisfied — kept for reference):
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
