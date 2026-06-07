# clif

Keyless FTSO v2 reward claimer and FSP signing client for Flare, Songbird, and
Coston2.

clif builds reward-claim and FSP signing requests, sends them to the
`fwd` signing daemon, broadcasts the resulting signed transactions itself,
and reports the results back to fwd. clif holds no private
keys, and it refuses to start if any `*PRIVATE_KEY*` variable is present in the
environment or `.env` file.

## Requirements

- Python 3.12
- Poetry
- A reachable [fwd](https://github.com/africanproofs/fwd) daemon for signing commands
- Docker Compose, if you run the daemon containers

Read-only commands use public RPC and do not need fwd credentials.

## Install

### Full reward-provider stack (fwd + clif)

For a full FTSO reward-provider stack, install fwd with the optional clif
claim/FSP layer:

```sh
curl -sfL https://get.proofs.africa/fwd | sudo sh -s -- --with-clif
```

Until `get.proofs.africa` hosting is live, run the public source installer
directly:

```sh
git clone https://github.com/africanproofs/fwd.git
sudo sh fwd/install/install.sh --with-clif
```

The installer builds from source, creates `/opt/fwd`, starts only fwd and
litestream, and leaves the stack inert: empty default-deny policy, zero wallets,
no signable custody.

### clif only (from source)

Build clif on its own — for development, or to run against an existing fwd:

```sh
git clone https://github.com/africanproofs/clif.git
cd clif
poetry install
poetry run clif version
```

## Onboard rewards

Reward custody is a separate opt-in step. Start with the Songbird canary, prove
it, then add Flare:

```sh
sudo fwd onboard rewards \
  --identity 0xYOUR_OFFLINE_IDENTITY_ADDRESS \
  --recipient 0xYOUR_CLAIM_RECIPIENT_ADDRESS \
  --networks songbird
```

The wizard creates or imports the reward wallets, writes the policy, mints
caller tokens into clif's env files, reads sender nonces from chain truth
through keyless clif, and prints the on-chain authorizations you must perform
from the offline identity key (`setClaimExecutors` + allowed recipients — see
`docs/onchain-migration.md`). It is compact by default; add `--guided` for the
full walk-through.

Migrating an existing provider uses the same wizard with `--import-existing`.
Stop the old claimer/submitter before fwd takes over those keys, or the two
systems will collide on nonces.

## Configure

The onboarding wizard writes clif's env files for the full-stack install. For a
clif-only deployment (or to review/adjust), copy the example environment file
and fill in the network, beneficiary, claim recipient, and fwd settings:

```sh
cp .env.example .env
```

Core variables:

| variable | purpose |
|---|---|
| `NETWORK` | `flare`, `songbird`, or `coston2` |
| `IDENTITY_ADDRESS` | FEE reward owner |
| `SIGNING_POLICY_ADDRESS` | DIRECT reward owner; primary FSP voter (EntityManager-resolved if unset) |
| `CLAIM_RECIPIENT_ADDRESS` | recipient allowed on-chain by the operator |
| `WRAP_REWARDS` | wrap claimed rewards when `true` |
| `FWD_ENDPOINT` | fwd HTTP endpoint, usually `http://fwd:8080` in Compose |
| `FWD_WALLET_NAME` | fwd wallet used for reward claims |
| `FWD_CALLER_TOKEN` | fwd caller token for reward claims |

FSP signing needs separate fwd callers and wallets:

| variable | purpose |
|---|---|
| `FSP_SIGN_CALLER_TOKEN` | Leg 1, `/v1/sign-fsp-message` |
| `FSP_SUBMIT_CALLER_TOKEN` | Leg 2, `/v1/sign-transaction` |
| `FSP_SIGNING_WALLET_NAME` | fwd wallet holding the FSP signing-policy key |
| `FSP_SENDER_WALLET_NAME` | fwd wallet that submits `FlareSystemsManager` txs |

`FSP_AUTO_ENABLED=false` by default. `clif epoch run` signs, so it exits until
the operator explicitly sets `FSP_AUTO_ENABLED=true`. Uptime signing is also
gated by `UPTIME_AUTO_ENABLED`, which defaults to false.

## Commands

Read-only:

```sh
poetry run clif list
poetry run clif preflight --identity 0x... --recipient 0x...
poetry run clif chain nonce --address 0x...
poetry run clif spec
```

fwd-backed one-shots:

```sh
poetry run clif health
poetry run clif rehearse
poetry run clif claim --type fee
poetry run clif claim --type direct --epoch <epoch>
poetry run clif fsp uptime --epoch <epoch>
poetry run clif fsp rewards --epoch <epoch>
```

Daemons and health checks:

```sh
poetry run clif epoch run
poetry run clif epoch status
```

Legacy loops still exist for manual/backward-compatible operation, but
`clif epoch run` is the daemon entrypoint:

```sh
poetry run clif auto
poetry run clif status
poetry run clif fsp auto
poetry run clif fsp status
```

## Automation

`clif epoch run` runs one state machine per network. For reward epoch `N`, it:

1. optionally signs uptime after the epoch closes;
2. waits for reward data publication;
3. verifies reward data and signs rewards;
4. waits for `FlareSystemsManager.rewardsHash(N)` to finalize;
5. claims only epoch `N`;
6. idles until the next epoch window.

The daemon re-derives state from chain reads, so restarts resume safely. A
terminal fwd error, on-chain revert, stale unclaimed epoch, or dead daemon is
reported as degraded by `clif epoch status`.

clif never treats a mined transaction as success by itself. A reward claim is
successful only when the exact transaction emits a `RewardClaimed` event with
amount greater than zero; an already-claimed epoch can mine as a no-op and is
reported separately.

## Docker

Single-network daemon:

```sh
docker compose up -d clif-epoch
docker compose run --rm clif claim --type fee
```

`clif-epoch` signs, so it stays up only when `FSP_AUTO_ENABLED=true` (see
Configure); otherwise it exits by design.

Multichain daemon:

```sh
docker compose --profile multichain up -d
```

Each multichain service reads its own `.env.<network>` file. Set
`CLIF_STATE_DIR` per network, and set `FWD_NETWORK` if fwd's Docker network is
not named `fwd_fwd-callers`.

## Documentation

Current operating references:

- `docs/fwd-contract.md` - fwd HTTP contract and policy shape.
- `docs/onchain-migration.md` - executor and recipient authorization flow.
- `docs/verification.md` - what is proven, what is blocked, and rehearsal steps.
- `docs/decisions.md` - settled design decisions, including the epoch daemon.
- `docs/fwd-integration-spec.md` - generated operator handshake; regenerate with
  `clif spec` for the active environment.

Historical specs:

- `docs/phase8b-spec.md`
- `docs/fsp-signing-tool-spec.md`

Those historical files preserve the original `/v1/sign-and-send` design record.
The current fwd contract is `/v1/sign-transaction` plus client broadcast and
report-back.

## Development Checks

```sh
poetry run pytest -q
poetry run ruff check .
```

## License

MIT
