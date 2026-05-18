# clif — keyless FTSO reward claimer

`clif` claims African Proofs' FTSO v2 rewards (FEE and DIRECT) on Flare,
Songbird, and Coston2. It is the Python successor to the TypeScript
`ftso-fee-claimer`, with one decisive difference: **clif holds no private
keys.** All epoch and Merkle-proof discovery is keyless RPC + HTTP; the single
key operation — `RewardManager.claim(...)` — is performed by calling the
**fwd** signing daemon's `POST /v1/sign-and-send`. fwd holds the key, gates the
call by policy, signs, and broadcasts. clif never sees a key. This is **Phase
8b** of the fwd program: the first deleted `.env PRIVATE_KEY=` line.

## What clif does not hold

There is no `.env PRIVATE_KEY=` anywhere in clif and no local-signing
dependency (`eth-account`, `eth-keys`, `pycryptodome`, `web3`, `argon2`).
keccak-256 is vendored (`clif/_keccak.py`) purely to derive the `claim`
selector from the ABI at runtime; it is not a signing primitive. clif refuses
to start if any `*PRIVATE_KEY*` variable is present in its environment.

## Install

```
poetry install
```

## Commands

Keyless (no fwd provisioning needed):

```
clif health      # probe fwd /healthz (require master == "ok")
clif list        # enumerate AP's claimable FEE/DIRECT epochs + amounts
clif spec        # emit docs/fwd-integration-spec.md from REAL captured calldata
```

Claim + automation (needs fwd provisioned + the new wallet authorized on-chain
as executor — **operator-gated for production**, Core invariant #15):

```
clif claim [-t fee|direct] [-e EPOCH] [--no-wait]   # one-shot (rehearsal/ops)
clif auto  [--interval SECONDS]                     # resilient daemon
clif status                                         # scrapable health
```

## How automation works

A reward epoch becomes claimable when providers' reward-signing weight crosses
**>50%** — observable on-chain as `FlareSystemsManager.rewardsHash(epoch)`
flipping non-zero, which `clif`'s keyless discovery already detects. `clif
auto` polls (~15 min default; epochs are ~3.5 days so this is ample), and when
an epoch is claimable it builds the `claim` calldata and submits via fwd
(non-blocking; fwd mines/replaces; idempotency-keyed so retries never double
broadcast). It never exits on error: a transient failure retries next cycle; a
**terminal** failure (policy denial / on-chain revert / fwd down) enters a
cooldown (no fwd-denial spam) and marks clif **degraded**. Because unclaimed
FTSO rewards eventually expire, a claimable epoch left unclaimed past
`stale_after` (24h default) also goes degraded. Degraded state is loud in the
logs and exposed by `clif status` (non-zero exit; also detects a dead daemon)
for the Docker healthcheck / monitoring.

Deploy: `docker compose up -d clif-auto` (see `docker-compose.yml` for the fwd
network note). One-shot: `docker compose run --rm clif claim -t fee`.

## Configuration

Copy `.env.example` to `.env`. `NETWORK`, the beneficiary addresses
(`IDENTITY_ADDRESS` for FEE, `SIGNING_POLICY_ADDRESS` for DIRECT),
`CLAIM_RECIPIENT_ADDRESS`, and the fwd connection (`FWD_ENDPOINT`,
`FWD_WALLET_NAME`, `FWD_CALLER_TOKEN`) are all keyless.

## Knowledge base

This repo is self-contained — no external repo or constitution needed.
`CLAUDE.md` is the entry point; the binding references are:

- `docs/phase8b-spec.md` — binding spec (authoritative; do not relitigate)
- `docs/decisions.md` — settled decisions D1–D10
- `docs/fwd-contract.md` — verified fwd HTTP + ABI contract + the policy trap
- `docs/onchain-migration.md` — networks, actors, the >50% trigger, rotation
- `docs/verification.md` — what's proven vs blocked; rehearsal ladder
- `docs/fwd-integration-spec.md` — Deliverable 2 (operator handshake)

## On the signing-tool / `SIGNING_POLICY_PRIVATE_KEY`

The flare-foundation signing-tool's `SIGNING_POLICY_PRIVATE_KEY` (a raw ECDSA
signature over a protocol-message hash, used by the flare-system-client) is
**out of scope for clif and is not a local key for clif to hold**. It is
deferred to **fwd Phase 9** — a structured protocol-message signer (structured,
decodable intent; not raw `eth_sign`; not a Core invariant #3 violation). clif
neither ports it nor scaffolds a disabled path for it; putting that key in a
local `.env` would re-introduce the exact anti-pattern fwd exists to kill.

## License

MIT.
