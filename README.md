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

clif is the FTSO reward AUTOMATION (the epoch sign→claim daemon + manual ops),
deployed **separately** from the fwd signer. fwd and clif are two independent
compose projects: fwd is zero-egress (signer only); clif has its own egress and
joins fwd's internal callers network to reach `fwd:8080`.

### 1. Install fwd (the signer) first

```sh
git clone https://github.com/africanproofs/fwd.git
sudo sh fwd/install/install.sh      # fwd-only; builds /opt/fwd, starts inert
```

When `get.proofs.africa` hosting is live, the equivalent fwd install is
`curl -sfL https://get.proofs.africa/fwd | sudo sh -`.

### 2. Install clif (this deployment)

```sh
git clone https://github.com/africanproofs/clif.git
sudo sh clif/install/install.sh     # clones /opt/clif, builds, installs `clifctl`
```

This builds the clif image from source and installs the `clifctl` host wrapper.
clif's compose joins fwd's `${FWD_NETWORK:-fwd_fwd-callers}` network (external) plus
its own `egress` bridge — fwd must be up first (it creates that network). For local
development without the daemon: `cd clif && poetry install && poetry run clif version`.

## Onboard rewards

Reward custody is a separate opt-in step. Start with the Songbird canary, prove
it, then add Flare:

```sh
sudo fwd onboard rewards \
  --identity 0xYOUR_OFFLINE_IDENTITY_ADDRESS \
  --recipient 0xYOUR_CLAIM_RECIPIENT_ADDRESS \
  --networks songbird
```

The wizard creates or imports the reward wallets, writes the policy, mints caller
tokens, and publishes a one-shot bundle to fwd's outbox (e.g. `/opt/fwd/handoff/clif-<net>.json`);
you then run `clifctl import-credentials <net> <bundle>` on the clif host to create clif's
per-network env files at `/opt/clif`. It also seeds
fresh wallets' nonces to 0, and prints the on-chain authorizations you perform from
the offline identity key (`setClaimExecutors` + allowed recipients — see
`docs/onchain-migration.md`). It **never invokes clif**; on-chain preflight and
seeding an *imported* wallet's nonce from chain truth are clif steps you run
(`clifctl run <net> preflight` / `chain nonce`). Compact by default; `--guided` for
the walk-through.

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

In `.env.example` and clif-only manual configuration, `FSP_AUTO_ENABLED=false`
is the safe default. The full-stack `sudo fwd onboard rewards ...` flow writes
`FSP_AUTO_ENABLED=true` into `/opt/clif/.env.<net>` by default; keep or set it
to `false` only when you intentionally want that network to idle. Uptime
signing is separately gated by `UPTIME_AUTO_ENABLED`, which defaults to false.

## Commands

Read-only:

```sh
poetry run clif list
poetry run clif preflight --identity 0x... --recipient 0x...
poetry run clif chain nonce --address 0x...
poetry run clif spec            # fwd integration spec (markdown handshake)
poetry run clif spec --json     # machine-readable capability-request (ADR-0001)
```

`clif spec --json` is clif's reference fwd **capability-request** — the per-network
capabilities clif needs (each keyed by an immutable `capability_id`), rendered as a
human-reviewable custody diff plus the compat tuple. It is the shape the (deferred)
`consumer-contract-v1` will formalize. See
`flaresystems/docs/adr/0001-fwd-consumer-deployment-contract.md`.

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
poetry run clif epoch status     # add --json for a machine-readable scrape
poetry run clif doctor           # consumer self-check: keyless, fwd, capabilities, compat
poetry run clif doctor --json    # the coordinator scrape surface (ADR-0001)
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

## Deploy & operate — `clifctl`

clif is its own compose project (`clif`), joining fwd's callers network (external)
plus its own `egress` bridge. The `clifctl` host wrapper (installed by `install.sh`)
is the operator surface — fwd never launches clif:

```sh
clifctl up songbird            # start that network's epoch sign→claim daemon
clifctl status songbird        # compose state + `clif epoch status`
clifctl logs songbird          # follow the daemon
clifctl run songbird claim --type fee     # one-shot manual op (reuses .env.songbird)
clifctl down songbird          # stop + remove
```

`clif-epoch-<net>` signs only when `FSP_AUTO_ENABLED=true` in that network's
`.env.<net>` (see Configure). If it is `false`, the daemon idles in a
healthy-disabled state and logs a disabled heartbeat instead of signing. Each
per-network service reads its own `.env.<network>` (set `CLIF_STATE_DIR` per
network). Set `FWD_NETWORK` if fwd's Docker network is not named
`fwd_fwd-callers`.

Under the hood `clifctl` is `docker compose -p clif --profile multichain …`; run those
directly (e.g. `docker compose --profile multichain up -d clif-epoch-flare`) if you prefer.

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
