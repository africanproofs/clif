# clif — keyless FTSO reward claimer + FSP signer

`clif` is a keyless client for two FTSO v2 operations on Flare, Songbird, and
Coston2:

- **reward claiming** — `RewardManager.claim(...)` for FEE and DIRECT rewards;
- **FSP signing** — the protocol messages `signUptimeVote` and `signRewards`.

It holds **no private keys.** Every key operation is delegated to the
[**fwd**](https://github.com/africanproofs/fwd) signing daemon: clif builds the
intent, fwd gates it by policy, signs it, and allocates the nonce; **clif
broadcasts the signed payload itself and reports the outcome back** (fwd is
zero-egress and never broadcasts). All epoch and Merkle-proof discovery is
plain keyless RPC + HTTP.

clif is the Python successor to the TypeScript `ftso-fee-claimer`, and the
first consumer in the fwd program to delete its `.env PRIVATE_KEY=` line.

## The keyless guarantee

clif carries no `.env PRIVATE_KEY=` and no local-signing dependency
(`eth-account`, `eth-keys`, `pycryptodome`, `web3`, `argon2`). keccak-256 is
vendored (`clif/_keccak.py`) to derive ABI function selectors (the `claim` and
FSP `signUptimeVote`/`signRewards` selectors) and to hash Merkle leaves/nodes for
client-side reward-proof verification — it is not a signing primitive. clif refuses to start if any
`*PRIVATE_KEY*` variable is present in its environment.

## Install

clif is built from source from the public repository
(<https://github.com/africanproofs/fwd> ships the one-command installer that
clones and builds clif alongside fwd; see *Run your own provider stack* below).
To build clif on its own:

```
git clone https://github.com/africanproofs/clif.git
cd clif
poetry install
```

## Commands

Read-only (no fwd provisioning needed):

```
clif health                       # probe fwd /healthz (require master == "ok")
clif list                         # enumerate claimable FEE/DIRECT epochs + amounts
clif spec                         # emit docs/fwd-integration-spec.md from real captured calldata
clif chain nonce --address 0x...  # read an address's on-chain nonce (used by fwd onboarding)
```

Claim + automation (needs fwd provisioned and the signing wallet authorized
on-chain as executor — **operator-gated for production**):

```
clif rehearse                                       # prove the on-chain `from` is your fwd wallet
clif claim [-t fee|direct] [-e EPOCH] [--no-wait]   # one-shot claim
clif auto  [--interval SECONDS]                     # resilient daemon
clif status                                         # scrapable health
```

## How automation works

A reward epoch becomes claimable when providers' reward-signing weight crosses
**>50%** — observable on-chain as `FlareSystemsManager.rewardsHash(epoch)`
flipping non-zero, which clif's keyless discovery detects. `clif auto` polls
(~15 min default; epochs run ~3.5 days, so this is ample), and when an epoch is
claimable it builds the `claim` calldata, has fwd sign it, then broadcasts and
reports back (idempotency-keyed so retries never double-broadcast; stuck txs are
resubmitted via fwd's replacement path).

It never exits on error: a transient failure retries next cycle; a **terminal**
failure (policy denial / on-chain revert / fwd down) enters a cooldown (no
denial spam) and marks clif **degraded**. Because unclaimed FTSO rewards
eventually expire, a claimable epoch left unclaimed past `stale_after` (24h
default) also goes degraded. Degraded state is loud in the logs and exposed by
`clif status` (non-zero exit; also detects a dead daemon) for the Docker
healthcheck and monitoring.

**A mined tx is not a successful claim.** clif never reports success from a
`status == 0x1` receipt, a "mined" line, or a balance delta. Proof of a claim
is the effect of that exact tx — a `RewardClaimed` log with amount > 0.
`RewardManager.claim` *silently no-ops* on an already-claimed `(owner, epoch)`
(mined, no event); clif reports that as a distinct `MINED_NOOP` outcome, never
as success, and pre-flights the `-e` path to refuse already-claimed,
out-of-range, or not-yet-signed epochs without submitting.

Deploy: `docker compose up -d clif-auto` (see `docker-compose.yml` for the fwd
network note). One-shot: `docker compose run --rm clif claim -t fee`.
Multichain (Flare ∥ Songbird ∥ Coston2 on one shared fwd): `docker compose
--profile multichain up -d` — one keyless claim + FSP daemon per network, each
with its own gitignored `.env.<network>` and `CLIF_STATE_DIR`; status files are
network-scoped. See the `docker-compose.yml` multichain header.

## Configuration

Copy `.env.example` to `.env`. `NETWORK`, the beneficiary addresses
(`IDENTITY_ADDRESS` for FEE, `SIGNING_POLICY_ADDRESS` for DIRECT),
`CLAIM_RECIPIENT_ADDRESS`, and the fwd connection (`FWD_ENDPOINT`,
`FWD_WALLET_NAME`, `FWD_CALLER_TOKEN`) are all keyless.

## Run your own provider stack

clif + fwd are not AP-specific. Any FTSO provider can self-host the same
keyless stack — clif claims/signs, **their own** fwd custodies the keys.

1. **Stand up fwd.** Follow fwd's
   [`docs/one-command-install.md`](https://github.com/africanproofs/fwd) — the
   `curl | sh` installer clones and builds fwd from source and brings up an
   inert default-deny daemon, and `--with-clif` overlays the clif claim/FSP
   layer. On the fwd side the operator runs `fwd onboard rewards` (the canonical
   onboarding wizard; `clifwd onboard` is a compat alias), which provisions the
   policy, wallets, caller tokens, and nonces for the reward classes —
   `--import-existing` imports operator-supplied keys. fwd's `clifwd` admin CLI
   (`clifwd policy init` / `validate`, `clifwd nonce init`) is available for
   manual provisioning. clif never authors fwd policy.
2. **Configure clif.** `cp .env.example .env` (or a per-network
   `.env.<network>` for multichain). Set **your** beneficiary addresses —
   `IDENTITY_ADDRESS` (FEE), `SIGNING_POLICY_ADDRESS` (DIRECT, if any),
   `CLAIM_RECIPIENT_ADDRESS`, `WRAP_REWARDS` — and the fwd integration:
   `FWD_ENDPOINT`, `FWD_WALLET_NAME`, `FWD_CALLER_TOKEN`, plus the FSP `FSP_*`
   vars (`FSP_SIGN_CALLER_TOKEN`, `FSP_SUBMIT_CALLER_TOKEN`,
   `FSP_SIGNING_WALLET_NAME`, `FSP_SENDER_WALLET_NAME`) if you sign FSP
   messages. All operator-provisioned on **your** fwd; the per-var detail is in
   `.env.example`.
3. **Verify keyless, then rehearse.** `clif health` (require `master == "ok"`)
   → `clif list` (your claimable epochs) → `clif rehearse` on **Coston2**
   first, then Songbird, then Flare. The rehearsal proves the on-chain `from`
   is your fwd-custodied wallet (clif holds no key). Only then go live.

## Knowledge base

This repo is self-contained. `CLAUDE.md` is the entry point; the binding
references are:

- `docs/phase8b-spec.md` — binding spec (authoritative)
- `docs/decisions.md` — settled decisions D1–D16
- `docs/fwd-contract.md` — verified fwd HTTP + ABI contract, and the policy trap
- `docs/onchain-migration.md` — networks, actors, the >50% trigger, rotation
- `docs/verification.md` — what's proven vs blocked; the rehearsal ladder
- `docs/fwd-integration-spec.md` — operator handshake (generated by `clif spec`)

## FSP signing (keyless)

`clif fsp` signs the FTSO FSP protocol messages (`signUptimeVote`,
`signRewards`) via fwd — clif holds zero keys. Two-leg flow:

- **Leg-1** — `POST /v1/sign-fsp-message`: fwd reconstructs the protocol-message
  hash from typed fields and returns its v/r/s signature.
- **Leg-2** — `POST /v1/sign-transaction`: fwd signs the built
  `FlareSystemsManager` calldata; clif broadcasts and reports back.

For `REWARD_DISTRIBUTION`, clif first fetches and validates
`reward-distribution-data.json` — it never signs an unverified `rewardsHash`.
The file's `rewardEpochId` must equal the signing epoch before Leg-1; on
mismatch clif fails terminally with no sign call, because a valid signature over
the wrong data is irreversible on-chain.

```
clif fsp uptime   --epoch EPOCH [--no-wait] [--yes/-y] [--retry STR]
clif fsp rewards  --epoch EPOCH [--no-wait] [--yes/-y] [--retry STR]
clif fsp status                # scrapable health + current epoch from chain
clif fsp auto    [--interval SEC] [--from-epoch EPOCH]  # resilient daemon
```

**Two distinct fwd caller tokens are required.** fwd's policy loader forbids
the same `policy_path` key appearing in both `permissions` and
`fsp_permissions` (cross-domain key reuse fails the boot), so one caller cannot
span both legs:

- `FSP_SIGN_CALLER_TOKEN` — Leg-1 `/v1/sign-fsp-message`; operator provisions
  the `clif-fsp-sign` caller in fwd's `fsp_permissions` block.
- `FSP_SUBMIT_CALLER_TOKEN` — Leg-2 `/v1/sign-transaction` plus client broadcast
  and per-caller-scoped tx poll; operator provisions the `clif-fsp-submit`
  caller in fwd's `permissions` block for `FlareSystemsManager`.

`clif fsp auto` is **hard-disabled by default**. Set `FSP_AUTO_ENABLED=true`
explicitly to enable the unattended REWARDS auto-signer; without it, `clif fsp
auto` exits 2.

See `docs/fsp-signing-tool-spec.md` and `docs/decisions.md` (D15) for full
provisioning details.

## On `SIGNING_POLICY_PRIVATE_KEY`

The FSP signing-policy key is **never a local key for clif to hold.** clif
signs `signUptimeVote` and `signRewards` through fwd's structured
`/v1/sign-fsp-message` endpoint, which reconstructs the protocol-message hash
from typed fields and never accepts a caller-supplied raw digest. A free
raw-ECDSA `eth_sign`-style path, and putting `SIGNING_POLICY_PRIVATE_KEY` into a
local `.env`, remain out of scope and forbidden — that would re-introduce the
exact anti-pattern fwd exists to kill.

## License

MIT.
