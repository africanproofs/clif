# On-chain migration mechanics (in-repo reference)

> Vendored so clif needs neither the AP root constitution nor the fwd repo.
> All addresses here are **public** (FTSO identity/recipient/contract
> addresses). No private keys exist anywhere in clif.

## Networks

| network | chain_id | RewardManager | FlareSystemsManager | ClaimSetupManager |
|---|---|---|---|---|
| flare | 14 | `0xC8f55c5aA2C752eE285Bd872855C749f4ee6239B` | `0x89e50DC0380e597ecE79c8494bAAFD84537AD0D4` | `0xD56c0Ea37B848939B59e6F5Cda119b3fA473b5eB` |
| songbird | 19 | `0xE26AD68b17224951b5740F33926Cc438764eB9a7` | `0x421c69E22f48e14Fc2d2Ee3812c59bfb81c38516` | `0xDD138B38d87b0F95F6c3e13e78FFDF2588F1732d` |
| coston2 | 114 | `0xB4f43E342c5c77e6fe060c0481Fe313Ff2503454` | `0xbC1F76CEB521Eb5484b8943B5462D08ea96617A1` | _unknown — operator to confirm (not in source)_ |

Code source of truth for RewardManager/FSM/RPC: `clif/config.py`. Reward data:
`flare-foundation/fsp-rewards` (flare/songbird), `timivesel/ftsov2-testnet-
rewards` (coston2), with a local `rewards-data/{net}/{epoch}/…` cache fallback.

## The actors

| role | who | keyed? |
|---|---|---|
| **Identity** (FEE beneficiary) | AP Flare identity `0x26534aC74153E3257dDD3471f96faA33D5D3B575` | offline hardware wallet — never in fwd/clif |
| **Signing-policy** (DIRECT beneficiary) | AP signing-policy address | offline; keyless arg to `claim` |
| **Claim recipient** | AP Flare recipient `0x7c3579aB3E647395c96a1EfC98aF9A31C5Ecc294` | **keyless** `claim` arg; allow-listed via `ClaimSetupManager.setAllowedClaimRecipient` |
| **Executor** | the new **fwd-custodied wallet** | the ONLY key — held by fwd, never by clif |

## The trigger: ">50% reward-signing weight"

A reward epoch becomes claimable when providers' accumulated reward-signing
weight crosses **>50%** of the signing-policy weight. On-chain this manifests
as `FlareSystemsManager.rewardsHash(epochId)` flipping from zero to the signed
Merkle root. clif's keyless discovery (`clif/discovery.py`) already gates on
exactly this (`rewards_hash(...) != ZERO_BYTES32`). **Automation is a poll of
this flip + submit via fwd — no new chain logic.**

## Doctrine drift to know (do not "fix" — fwd-side)

The fwd roadmap phrases Phase 8 rotation as "on-chain via `setClaimRecipient`".
The producing code shows the **keyed entity is the executor**
(`CLAIM_EXECUTOR_PRIVATE_KEY` in the TS tool), authorized by the
identity/signing-policy address via **`ClaimSetupManager.setClaimExecutors`**.
The recipient is a keyless arg, separately allow-listed via
`setAllowedClaimRecipient`. So the real rotation authorizes fwd's new wallet as
**executor**, not a recipient swap. Surfaced for the operator/Reviewer;
clif does not edit fwd (`docs/phase8b-spec.md` constraint 2).

## Operator-gated rotation sequence (Core invariant #15)

1. Operator reviews `docs/fwd-integration-spec.md`.
2. Operator provisions fwd: least-privilege `policy.yaml`
   (`docs/fwd-contract.md` — use the CORRECT signature, not fwd's example),
   `POST /v1/admin/wallets` → new executor wallet (note its address),
   `POST /v1/admin/callers` → clif caller token.
3. **Irreversible-ish:** operator calls
   `ClaimSetupManager.setClaimExecutors([newFwdWalletAddress])` **signed by
   the offline identity key** (and signing-policy key for DIRECT). fwd cannot
   do this (does not custody identity keys); clif cannot do this. Until done,
   any real claim **reverts on-chain** even if clif→fwd→sign is perfect.
4. Rehearsal ladder (`docs/verification.md`): Coston2 → Songbird → Flare.
5. **Irreversible:** delete the `.env PRIVATE_KEY=` line from the fee-claimer
   path — the Phase-8b deliverable; lifts fwd's doctrine-ship freeze.

clif builds + rehearses freely; it never performs steps 2–3, 5 and never the
production Flare claim without explicit operator approval.

## FSP actors (2026-05-19)

| role | who | keyed? |
|---|---|---|
| **FSP signing wallet** | fwd-custodied wallet holding the signing-policy key for `signUptimeVote` / `signRewards` (Leg-1) | held by fwd; fwd admin provisions (`POST /v1/admin/wallets`); never in clif |
| **FSP sender wallet** | fwd-custodied wallet that sends the on-chain `FlareSystemsManager` tx (Leg-2) | held by fwd; needs gas; distinct from signing wallet |
| **FSP sign caller** (`clif-fsp-sign`) | clif's Leg-1 identity in fwd; receives `FSP_SIGN_CALLER_TOKEN` | `FSP_SIGN_CALLER_TOKEN` is keyless in clif (a Bearer token, not a signing key); authorized in fwd's `fsp_permissions` block only |
| **FSP submit caller** (`clif-fsp-submit`) | clif's Leg-2 + tx-poll identity in fwd; receives `FSP_SUBMIT_CALLER_TOKEN` | `FSP_SUBMIT_CALLER_TOKEN` is keyless in clif; authorized in fwd's `permissions` block for FlareSystemsManager; used for `/v1/sign-transaction` (clif then broadcasts + reports back) and the per-caller-scoped `/v1/transactions/{id}` poll |

**Two distinct callers are required (D15 MAJOR-2):** fwd's policy loader forbids
the same `policy_path` key appearing in both `permissions` and `fsp_permissions`
(cross-domain key reuse = fail-fast boot). One caller → one policy block. The
Leg-1 sign caller and the Leg-2 submit caller must be different. Both are distinct
from `FWD_CALLER_TOKEN` (the claim caller). clif never authors fwd policy.

The FSP signing wallet address must be registered on-chain as the FSP provider
for AP's identity address (chain-side operator step — offline key; clif does
not perform this registration). Until done, Leg-2 may revert on-chain even if
Leg-1 is successful.

**Operator provisioning items (all GATE-1 — not yet done; see D15):**
- Provision `FSP_SIGN_CALLER_TOKEN`: create `clif-fsp-sign` caller in fwd admin,
  authorize in `fsp_permissions` block (Leg-1 `/v1/sign-fsp-message`)
- Provision `FSP_SUBMIT_CALLER_TOKEN`: create `clif-fsp-submit` caller in fwd
  admin, authorize in `permissions` block for FlareSystemsManager (Leg-2 +
  per-caller-scoped tx poll)
- Create `FSP_SIGNING_WALLET_NAME` in fwd with the signing-policy key
- Create `FSP_SENDER_WALLET_NAME` in fwd with gas
- Add `FlareSystemsManager` ABI + FSP policy to fwd (clif never authors this)
- Register the FSP signing wallet address on-chain for AP's FSP role
