# fwd integration contract (verified — in-repo reference)

> Verified against fwd source (`fwd/src/fwd/api/sign.py`) on 2026-05-18 during
> the clif build. Vendored so clif needs no access to the fwd repo. fwd code
> is the ultimate source of truth; before relying in production, re-confirm
> against the live daemon (`clif health` + a Coston2 rehearsal — mocks lie).

## Auth

`Authorization: Bearer fwd_live_<…>` — a per-caller token minted **once** by
fwd's admin endpoint and given to clif by the operator (clif consumes it via
`FWD_CALLER_TOKEN`; clif never mints it). Missing/invalid/revoked → **401**.

## POST /v1/sign-and-send

Request JSON:

| field | type | notes |
|---|---|---|
| `wallet` | str(1..64) | fwd wallet **name** (not address); `FWD_WALLET_NAME` |
| `chain` | int ≥ 1 | 14 Flare / 19 Songbird / 114 Coston2 |
| `to` | str | `0x` + 40 hex (the RewardManager) |
| `value_wei` | str | decimal string, non-negative; default `"0"` |
| `data` | str | `0x` or `0x`+even-hex; the claim calldata |
| `gas` | int \| null | ≥ 21000 if set; omit to let fwd estimate |

Optional header `Idempotency-Key` (≤128 chars). Replay of the same key returns
the **same `tx_id`** with **no second broadcast** — safe across retries/restarts.
**fwd replay is status-blind by design (fwd D14):** a cached tx is replayed
for its `(caller, key)` regardless of on-chain outcome — including a tx that
broadcast then reverted/dropped/failed. That is correct at-least-once dedup
(it is exactly what stops a double-claim on a client retry). The consumer owns
the consequence: a key bound only to logical identity pins a failed claim
forever. clif's production key is therefore deterministic by default (same
logical attempt ⇒ same key ⇒ fwd dedups) **plus** an explicit
operator-controlled discriminator (`clif claim --retry …` / `IDEMPOTENCY_RETRY`
for `auto`) for a **deliberate** post-on-chain-failure re-attempt. clif never
auto-randomises (that would reintroduce double-claim risk). The `rehearse`
harness uses its own separate `-r<tag>` discriminator — walled off from the
production money path.

Success **200**: `{ "tx_id": str, "hash": str, "nonce": int }`.

Errors — envelope `{ "error": str, "message": str }`:

| HTTP | error | class |
|---|---|---|
| 400 | bad request / `bad_idempotency_key` / `chain_not_allowed` | **terminal** |
| 401 | unauthorized | **terminal** |
| 403 | `policy_denied` | **terminal** |
| 404 | `wallet_not_found` | **terminal** |
| 422 | `transaction_rejected` (node deterministically refused — e.g. insufficient funds) | **terminal** |
| 502 | `rpc_unreachable` | **retryable** |
| 503 | `vault_unavailable` (sealed master) | **retryable** |
| — | any httpx transport error (ConnectError/ReadTimeout/PoolTimeout/…) — a down/restarting fwd | **retryable** |

Rule (fwd v1.0.0 taxonomy): **400/401/403/404/422 → do not retry (escalate);
502/503/transport-error → backoff + retry.** A down fwd MUST degrade
`clif auto`, never crash it. clif implements this in `clif/fwd_client.py`
(transport errors are converted to `FwdRetryableError`, never propagated raw).

## GET /v1/transactions/{tx_id}

Caller-token-gated. Returns `status`, `hashes: [{sequence_num, hash_hex,
submitted_at}]`, `confirmed_at`. Cross-caller access returns **404** (not 403)
by design. Terminal statuses: `mined`, `failed`, `replaced`, `dropped`.

## GET /healthz

`{ "master": "ok"|"unavailable", "rpc": …, "fwd": "ok" }`. Require
`master == "ok"` before relying on signing (`clif health`).

## RewardManager ABI (what fwd decodes)

Canonical signature (reconstructed from the registered ABI; clif asserts this
at import):

```
claim(address,address,uint24,bool,(bytes32[],(uint24,bytes20,uint120,uint8))[])
```

Selector **`0x8e33aba5`**. fwd's decoder **B1-projects only the scalar args**
into the gateable set: `_rewardOwner` (address), `_recipient` (address),
`_rewardEpochId` (uint24), `_wrap` (bool). `_proofs` is decoded but **not**
predicable (tuple array). So fwd policy bounds this method via
`max_value_wei: "0"` + a `_recipient` arg-predicate + rate — **not** a
predicate on the proof. `setClaimRecipient` is **absent** from fwd's
`reward_manager.json` (see `docs/onchain-migration.md` for why that's correct).

## Policy example: FIXED upstream (was a trap, now safe)

Historical note (resolved): earlier `fwd/docs/policy.example.yaml` showed an
incorrect tuple ordering for `claim`, which would have 403'd every claim if
copied verbatim. **fwd has corrected it** — `fwd/docs/policy.example.yaml:56`
now carries the canonical
`claim(address,address,uint24,bool,(bytes32[],(uint24,bytes20,uint120,uint8))[])`.
The operator may rely on the fwd example; it matches the signature above and
the live one emitted in `docs/fwd-integration-spec.md §2`. (Verified by the
fwd Reviewer, fwd v1.0.0 fold-in.)

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
        methods:
          "claim(address,address,uint24,bool,(bytes32[],(uint24,bytes20,uint120,uint8))[])":
            max_value_wei: "0"
            arg_predicates: { _recipient: "0x<CLAIM_RECIPIENT>" }
    wallet_allowlist: [ claim-recipient ]
    rate: { per_hour: 4, per_day: 8 }
```
