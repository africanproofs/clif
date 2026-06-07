# fwd integration contract (in-repo reference)

> Verified against the fwd **v1.1.0a69** contract (zero-egress sign-only) on
> **2026-06-04**. Vendored so clif needs no access to the fwd repo. fwd code is
> the ultimate source of truth; before relying in production, re-confirm against
> the live daemon (`clif health` + a Coston2 rehearsal — mocks lie).

fwd is **zero-egress and sign-only**: it signs, allocates the nonce, and makes
**no outbound connection** — it never broadcasts. The flow is **sign →
client-broadcast → report-back**: clif POSTs the intent, fwd returns a signed
raw tx, clif broadcasts it itself (`clif/rpc.py`, `eth_sendRawTransaction`),
then reports the outcome back so fwd can confirm or release the nonce.

clif talks to fwd through the shared **fwd-client** library
(`github.com/africanproofs/fwd-client`, Python in `subdirectory=python`, tag
**v0.1.1**) — `from fwd_client import …`, re-exported via `clif/fwd_client.py`.
Error classification is **class-based** (`FwdRetryableError` / `FwdTerminalError`,
HTTP-status-driven — never `error_code`; see § Error taxonomy). clif composes
idempotency keys via the lib's generic `make_idempotency_key`, **byte-identical to
the Go port's `MakeIdempotencyKey`** — never reimplement the hashing.

## Auth

`Authorization: Bearer fwd_live_<…>` — a per-caller token minted **once** by
fwd's admin endpoint and given to clif by the operator (clif consumes it via
`FWD_CALLER_TOKEN`; clif never mints it). Missing/invalid/revoked → **401**.

## POST /v1/sign-transaction

The single key operation for `RewardManager.claim` and the FSP Leg-2
FlareSystemsManager submit. fwd ABI-decodes the calldata, policy-gates it,
signs, and allocates the nonce — **fwd does not broadcast**.

Request JSON:

| field | type | notes |
|---|---|---|
| `wallet` | str(1..64) | fwd wallet **name** (not address); `FWD_WALLET_NAME` |
| `chain` | int ≥ 1 | 14 Flare / 19 Songbird / 114 Coston2 |
| `to` | str | `0x` + 40 hex (the RewardManager / FlareSystemsManager) |
| `value_wei` | str | decimal string, non-negative; default `"0"` |
| `data` | str | `0x` or `0x`+even-hex; the claim/submit calldata |
| `gas` | int | a FIXED gas limit clif supplies (`fsp_submit_gas`, default 500000) — clif is keyless and cannot supply a `from` for `eth_estimateGas`, so it sends a fixed value for both the claim and the FSP Leg-2 submit |
| `max_fee_per_gas` | str | decimal-string EIP-1559 cap clif computed |
| `max_priority_fee_per_gas` | str | decimal-string tip clif computed |

Optional header `Idempotency-Key` (≤128 chars). Replay of the same key with the
**same body** returns the **same `tx_id`** (no second sign) — safe across
retries and restarts; the same key with a *different* body is a **409
`idempotency_conflict`**. **fwd replay is status-blind by design:** a cached tx
is replayed for its `(caller, key)` regardless of on-chain outcome — including a
tx that broadcast then reverted, dropped, or failed. That is correct
at-least-once dedup, and exactly what stops a double-claim on a client retry.
The consumer owns the consequence: a key bound only to logical identity pins a
failed claim forever. clif's production key is therefore deterministic by
default (same logical attempt ⇒ same key ⇒ fwd dedups) **plus** an explicit
operator-controlled discriminator (`clif claim --retry …` / `IDEMPOTENCY_RETRY`
for `auto`) for a **deliberate** re-attempt after an on-chain failure. clif
never auto-randomises (that would reintroduce double-claim risk). The `rehearse`
harness uses its own separate `-r<tag>` discriminator, walled off from the
production money path.

Success **200**: `{ "tx_id": str, "hash": str, "signed_raw_tx": str, "nonce": int }`.
clif broadcasts `signed_raw_tx` via `eth_sendRawTransaction`.

## POST /v1/sign-fsp-message — FSP Leg-1

The FSP protocol-message signer (`signUptimeVote` / `signRewards` preimage).
EIP-191 `personal_sign` over a messageHash fwd **reconstructs** from the typed
fields — the caller supplies **no** digest. **No nonce / no broadcast / no
receipt** on this endpoint (it returns a detached signature, not a tx).

This is a **different caller token** than the Leg-2 submit caller — fwd's
policy loader forbids the same `policy_path` key in both `permissions` and
`fsp_permissions`, so one caller cannot span both legs (clif uses
`FSP_SIGN_CALLER_TOKEN` here, `FSP_SUBMIT_CALLER_TOKEN` for Leg-2).

Request JSON:

| field | type | notes |
|---|---|---|
| `wallet` | str | fwd FSP signing wallet **name**; `FSP_SIGNING_WALLET_NAME` |
| `message_type` | str | `"UPTIME"` or `"REWARD_DISTRIBUTION"` |
| `reward_epoch_id` | int | the signing epoch |
| `chain_id` | int? | optional |
| `no_of_weight_based_claims` | int? | `REWARD_DISTRIBUTION` only |
| `rewards_hash` | str? | `REWARD_DISTRIBUTION` only |

Success **200**: `{ "message_hash": str, "v": int, "r": str, "s": str, "signature": str }`.

## POST /v1/transactions/{tx_id}/broadcast-result

clif reports the broadcast outcome so fwd can confirm or release the nonce.

Body: `{ "tx_hash": str, "outcome": "accepted"|"rejected_releaseable"|"rejected_nonce_too_low", "error_class": str? }`.

- `accepted` → fwd holds the nonce, awaits the receipt.
- `rejected_releaseable` → fwd releases the (tail-only) nonce.
- `rejected_nonce_too_low` → fwd keeps the nonce (chain is ahead → operator
  `nonce-sync`).

## POST /v1/transactions/{tx_id}/receipt

clif reports the mined receipt; this confirms the nonce.

Body: `{ "tx_hash": str, "outcome": "mined_success"|"mined_reverted", "block_number": int }`.
Accepts any hash fwd recorded for the tx (including a replacement hash).

## GET /v1/transactions/{tx_id}

Caller-token-gated. Polls tx status (status + recorded hashes). Cross-caller
access returns **404** (not 403) by design.

## POST /v1/sign-replacement

Re-signs a **stuck** tx at the **same nonce** with a bumped tip (the same
recorded intent — fwd never hands a reserved nonce to a different intent). clif
then broadcasts the replacement and reports its receipt via the normal
`/receipt` call.

## GET /healthz

`{ "master": "ok"|"unavailable", "fwd": "ok" }`. The `rpc` field is **retired**
(zero-egress — fwd makes no RPC call). Require `master == "ok"` before relying
on signing (`clif health`).

## Error taxonomy

Envelope (FastAPI-nested): `{ "detail": { "error": str, "message": str } }`.

> **Known gap (fwd-client Python ≤ v0.1.1):** `raise_for_fwd_error` reads
> top-level `body["error"]`, not `body["detail"]["error"]`, so
> `FwdError.error_code` is `"unknown"` for daemon errors. **Harmless for clif** —
> classification is by exception **class** (`FwdRetryableError` vs
> `FwdTerminalError`), which is **HTTP-status-driven**; clif never branches on
> `error_code`, and must not start.

| HTTP | examples | class |
|---|---|---|
| 400 | bad request / `bad_idempotency_key` / `chain_not_allowed` | **terminal** |
| 401 | unauthorized | **terminal** |
| 403 | `policy_denied` | **terminal** |
| 404 | `wallet_not_found` / cross-caller tx | **terminal** |
| 409 | `nonce_not_initialized` (operator runs admin `nonce-init`) / `idempotency_conflict` / `tx_hash_mismatch` / `illegal_transition` — every code **except** `idempotent_replay` (see note) | **terminal** |
| 422 | `/v1/sign-fsp-message` body cannot be reconstructed into a messageHash (bad / unknown FSP fields) | **terminal** |
| 503 | `vault_unavailable` (sealed master not loaded) | **retryable** |
| — | any httpx transport error (ConnectError/ReadTimeout/PoolTimeout/…) — a down/restarting fwd | **retryable** |

**409 is split by error code in fwd-client:** `idempotent_replay` → **retryable**
(a defensive fail-safe), every other 409 code → **terminal**. In practice the fwd
**daemon never sends clif a 409 `idempotent_replay`** — a replayed idempotency key
returns the **cached 200 result** — so **every 409 clif actually receives is terminal**.

**There is no `502` from fwd anymore** — fwd does no RPC, so broadcast/RPC
errors are clif's **own** (raised by `clif/rpc.py`, not by fwd). Rule:
**400/401/403/404/409/422 (and any unmapped status — fail closed) → do not retry
(escalate); 503/transport-error → backoff + retry.** A down fwd MUST degrade the clif
daemon (`clif epoch run`, or the legacy `clif auto`/`clif fsp auto`), never crash it
(clif converts transport errors to `FwdRetryableError` via the shared `fwd-client`
lib, never propagated raw).

## RewardManager ABI (what fwd decodes)

Canonical signature (reconstructed from the registered ABI; clif asserts this
at import):

```
claim(address,address,uint24,bool,(bytes32[],(uint24,bytes20,uint120,uint8))[])
```

Selector **`0x8e33aba5`**. fwd's decoder projects only the scalar args into the
gateable set: `_rewardOwner` (address), `_recipient` (address), `_rewardEpochId`
(uint24), `_wrap` (bool). `_proofs` is decoded but **not** predicable (tuple
array). So fwd policy bounds this method via `max_value_wei: "0"` + a
`_recipient` arg-predicate + rate — **not** a predicate on the proof.
`setClaimRecipient` is **absent** from fwd's `reward_manager.json` (see
`docs/onchain-migration.md` for why that's correct).

## Operator-side fwd policy notes (clif never authors fwd policy)

These are fwd-side operator config (the operator runs `clifwd policy init` /
`validate` on the fwd side); clif only consumes the resulting caller tokens:

- Write the policy from the canonical `claim` signature above — it matches
  `fwd/docs/policy.example.yaml` and the live one in
  `docs/fwd-integration-spec.md §2`.
- fwd requires `chains: [...]` on every contract rule and
  `allow_unconstrained_args: true` on methods with array/tuple args (`claim`,
  `signUptimeVote`, `signRewards`).

## The policy block clif's caller needs (operator writes this in fwd)

Shape (operator fills addresses from `docs/fwd-integration-spec.md`):

```yaml
callers:
  clif-claimer: { policy_path: perm/clif-claim }
permissions:
  perm/clif-claim:
    contracts:
      "0x<RewardManager>":
        abi: reward_manager
        chains: [ 14 ]
        methods:
          "claim(address,address,uint24,bool,(bytes32[],(uint24,bytes20,uint120,uint8))[])":
            max_value_wei: "0"
            allow_unconstrained_args: true
            arg_predicates: { _recipient: "0x<CLAIM_RECIPIENT>" }
    wallet_allowlist: [ claim-recipient ]
    rate: { per_hour: 4, per_day: 8 }
```
