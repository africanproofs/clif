# FSP signing-tool spec (in-repo reference)

> Binding §3/§4 facts, FSM addresses, verified selectors, oracle vectors,
> offline-oracle limitation, reward-distribution-data.json source rule,
> and the Phase-0/`838b87f` verdict. Vendored so clif needs no external repo.
> Verified 2026-05-19.

## §3 — FSP signing-tool role (clif's scope)

clif's FSP role is **thin**: it calls fwd's `POST /v1/sign-fsp-message`
(Leg-1, returns the signed message) and then submits the resulting calldata to
`FlareSystemsManager` via the existing `sign_and_send` (Leg-2). clif never
sees a key. The on-chain contracts (`flare-system-client`, `fdc-client`,
`fast-updates`) are out of scope — clif submits signing confirmations, not
consensus logic.

### Two-leg flow

```
Leg-1:  POST /v1/sign-fsp-message
        Body: { wallet, message_type: "UPTIME"|"REWARD_DISTRIBUTION",
                reward_epoch_id, [chain_id, no_of_weight_based_claims, rewards_hash] }
        Response: { message_hash, v, r, s, signature }
        Auth: FSP_SIGN_CALLER_TOKEN (Leg-1 only — fsp_permissions block in fwd)

Leg-2:  POST /v1/sign-and-send
        Body: { wallet: FSP_SENDER_WALLET_NAME, chain, to: FlareSystemsManager,
                data: <ABI calldata from Leg-1 v/r/s>, value_wei: "0", gas: 500_000 }
        Auth: FSP_SUBMIT_CALLER_TOKEN (Leg-2 + tx poll — permissions block in fwd)
```

### Two distinct fwd callers (cross-domain rule)

fwd's policy loader forbids the same `policy_path` key appearing in both
`permissions` and `fsp_permissions` (cross-domain key reuse = fail-fast boot).
One caller → one `policy_path` → one block. So one caller authorizes EITHER
`/v1/sign-fsp-message` (Leg-1, `fsp_permissions`) OR `/v1/sign-and-send`
(Leg-2, `permissions`) — never both. The tx poll `/v1/transactions/{id}` is
per-caller-scoped and MUST use the Leg-2 (submit) caller. See D15 MAJOR-2.

Operator provisions two fwd callers:
- **`clif-fsp-sign`** → `fsp_permissions` block (authorizes `/v1/sign-fsp-message`)
  → inject as `FSP_SIGN_CALLER_TOKEN`
- **`clif-fsp-submit`** → `permissions` block for FlareSystemsManager
  (authorizes `/v1/sign-and-send` + tx poll) → inject as `FSP_SUBMIT_CALLER_TOKEN`

Both are distinct from `FWD_CALLER_TOKEN` (the claim caller). clif never authors
fwd policy nor mints credentials.

Two distinct fwd wallets:
- **signing wallet** (`FSP_SIGNING_WALLET_NAME`) — holds the signing-policy
  key that produces the protocol-message signature (Leg-1).
- **sender wallet** (`FSP_SENDER_WALLET_NAME`) — the on-chain submitter that
  calls `FlareSystemsManager` (Leg-2); needs gas, not a signing-policy role.

## §4 — FlareSystemsManager addresses + verified selectors

### Contract addresses

| network | FlareSystemsManager |
|---|---|
| Flare (chain_id=14) | `0x89e50DC0380e597ecE79c8494bAAFD84537AD0D4` |
| Songbird (chain_id=19) | `0x421c69E22f48e14Fc2d2Ee3812c59bfb81c38516` |
| Coston2 (chain_id=114) | `0xbC1F76CEB521Eb5484b8943B5462D08ea96617A1` |

### Verified selectors (derived from vendored ABI + keccak at import, fail-loud)

| function | canonical signature | selector |
|---|---|---|
| `signUptimeVote` | `signUptimeVote(uint24,bytes32,(uint8,bytes32,bytes32))` | `0xdc5a4225` |
| `signRewards` | `signRewards(uint24,(uint256,uint256)[],bytes32,(uint8,bytes32,bytes32))` | `0xc00a1a97` |
| `getCurrentRewardEpochId` | `getCurrentRewardEpochId()` | `0x70562697` |

### UPTIME_VOTE_HASH (fakeVoteHash)

`keccak256(0x00 * 32)` = `0x290decd9548b62a8d60345a988386fc84ba6bc95484008f6362f93160ef3e563`

Derived at import in `fsp_calldata.py`; asserted at import (fail-loud). Used as
`_uptimeVoteHash` in `signUptimeVote`.

### §4 Oracle frozen test vectors

**Test signer:** `0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A` (key `0x11` × 32)

**UPTIME epoch 0:**
```
message_hash: 0xb7e97e6b4b2c7cd5fb9b51a86ad7eae441872b770b5953443024cb1e0bc6f67d
v:  27
r:  0x9938afc59dae94cb20e0c5982e00c6a88afc01f6ff8c058024f999857a32e785
s:  0x1e926390fbdece399aa1c56dbcbc66d128d43fba246b9459d5018d0c2de9b4b5
```

**REWARD_DISTRIBUTION epoch 3, chain_id 114, n=56, rewards_hash=0xab×32:**
```
message_hash: 0x3f2025e652f0c582e59f6c0f8c7f1fde4fbd80e6f02771d0ab961cbc6ed742c0
v:  27
r:  0x641235a188dac8467dc0e8f3a71073312c4f0dde0f91058db0aca10bee275d5e
s:  0x53c2acf6985b72a9657c57368d9b5f83858f9e988ef52190c0b21410a5acfa7a
```

These vectors are used in `test_fsp_integration_oracle.py` for offline
shape+parse verification. The live byte-match upgrade (POST to real fwd,
compare returned `message_hash`) is gated by `CLIF_FSP_LIVE_FWD` env var.

## Offline-oracle limitation

clif cannot regenerate the oracle message hashes without a live fwd instance,
because message hash computation is fwd-internal (it knows the signing-policy
epoch parameters). The offline tests verify calldata shape + selector prefix,
not the message_hash value. The live byte-match gate (`CLIF_FSP_LIVE_FWD`)
is the runtime verification path.

## reward-distribution-data.json source rule

For `REWARD_DISTRIBUTION` signing, clif fetches `reward-distribution-data.json`
(NOT the tuples variant `reward-distribution-data-tuples.json`). This file
contains `rewardEpochId`, `merkleRoot`, and `noOfWeightBasedClaims`. URL
derivation: `reward_distribution_url(epoch)` replaces
`reward-distribution-data-tuples.json` with `reward-distribution-data.json` in
the network URL template.

Local cache path: `rewards-data/{network}/{epoch}/reward-distribution-data.json`
(operator-supplied; checked before remote fetch).

**Epoch bind (MAJOR-1, D15):** `reward-distribution-data.json` carries a
top-level `rewardEpochId` (confirmed flare/230, songbird/200, coston2/3156).
Upstream `signing-tool@838b87f` `getRewardsData()` asserts
`data.rewardEpochId === rewardEpochId` and throws on mismatch. clif mirrors
this: `RewardDistributionData.reward_epoch_id` is required (no default),
`merkleRoot` is regex-validated `^0x[0-9a-fA-F]{64}$`, and
`noOfWeightBasedClaims` is validated integer ≥ 0. `run_sign_rewards` asserts
`rdd.reward_epoch_id == reward_epoch_id` BEFORE Leg-1 — a stale cache / wrong
operator file / wrong-epoch payload is `FAILED_TERMINAL` with no sign call.

**Guard:** if the file is unavailable, clif STOPS with
`FAILED_TERMINAL "reward-distribution-data unavailable — refusing to sign unverified rewardsHash"`.
It never signs an unverified rewardsHash.

## Phase-0 / `838b87f` verdict

Phase-0 of the FSP signing-tool (commit `838b87f`, if referenced) is
superseded by this implementation. The binding reference is this spec + the
code in `clif/fsp_calldata.py`, `clif/fsp.py`, `clif/fwd_client.py`
(`sign_fsp_message`). Any Phase-0 scaffold (no-op path, placeholder endpoint)
is not present in clif — the implementation is full Leg-1 + Leg-2.

## Operator provisioning (GATE-1: F1/F2 rungs — environment-deferred)

Required before any FSP signing can occur (never claimed as proven here).
See D15 for the full corrective-pass rationale.

1. **`FSP_SIGN_CALLER_TOKEN`** — fwd caller token for the Leg-1 sign caller
   (`clif-fsp-sign`). Authorized in fwd's `fsp_permissions` block only
   (cross-domain rule: cannot share `policy_path` with Leg-2). Distinct from
   `FWD_CALLER_TOKEN` (the claim caller).
2. **`FSP_SUBMIT_CALLER_TOKEN`** — fwd caller token for the Leg-2 submit caller
   (`clif-fsp-submit`). Authorized in fwd's `permissions` block for
   `FlareSystemsManager`. Also used for the per-caller-scoped tx poll. Distinct
   from `FSP_SIGN_CALLER_TOKEN` (fwd cross-domain rule, D15 MAJOR-2).
3. `FSP_SIGNING_WALLET_NAME` — the fwd wallet holding the signing-policy key
   (Leg-1). Created by fwd admin, not clif.
4. `FSP_SENDER_WALLET_NAME` — the fwd wallet that sends the on-chain tx (Leg-2).
   Needs gas on the target network.
5. fwd policy: `FlareSystemsManager` ABI + policy permitting `signUptimeVote`
   and `signRewards` for the Leg-2 submit caller (`clif-fsp-submit`).
   **clif never authors fwd policy.**
6. On-chain: the signing wallet address must be registered as the FSP provider
   for AP's identity address (chain-side setup — operator-only, offline key).
