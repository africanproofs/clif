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

## Commands (keyless half — pre-operator-gate)

```
clif health      # probe fwd /healthz (require master == "ok")
clif list        # enumerate AP's claimable FEE/DIRECT epochs + amounts (keyless)
clif spec        # emit docs/fwd-integration-spec.md from REAL captured calldata
```

`clif claim` and `clif auto` (the signing/submission path and the Docker
auto-claimer) land after the operator reviews `docs/fwd-integration-spec.md`
and provisions the matching fwd policy, wallet, and caller token.

## Configuration

Copy `.env.example` to `.env`. `NETWORK`, the beneficiary addresses
(`IDENTITY_ADDRESS` for FEE, `SIGNING_POLICY_ADDRESS` for DIRECT),
`CLAIM_RECIPIENT_ADDRESS`, and the fwd connection (`FWD_ENDPOINT`,
`FWD_WALLET_NAME`, `FWD_CALLER_TOKEN`) are all keyless.

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
