# Adjudicated decisions (do not relitigate)

> These are settled. Changing one requires explicit operator direction and a
> matching update here + in code/docs (doctrine and code must not drift).

- **D1 — Keyless.** clif holds zero private keys. No `eth-account`,
  `eth-keys`, `pycryptodome`, `web3`, `argon2`. keccak-256 is vendored
  (`clif/_keccak.py`) only to derive the `claim` selector. `assert_keyless()`
  refuses to start if any `*PRIVATE_KEY*` env var is present. *Why: clif's
  whole reason to exist is to delete the `.env PRIVATE_KEY=` (fwd Core inv #7).*
- **D2 — FEE + DIRECT parity.** One engine, `--type fee|direct`; same
  keyless→fwd path, only the beneficiary arg differs. *Why: operator-chosen
  2026-05-18; parity with the TS source, no extra key/fwd surface. Caveat: a
  real DIRECT calldata sample needs AP to actually accrue signing-policy
  direct rewards — never fabricate it (spec constraint 3).*
- **D3 — Automation = both forms.** `clif auto` (resilient daemon, Docker,
  restart:unless-stopped) **and** `clif claim` one-shot (rehearsal + manual).
  *Why: operator-chosen 2026-05-18. A scheduled Claude agent is the wrong tool
  for an unattended money path.*
- **D4 — Escalation = loud log + degraded status.** No external notification
  dep. `clif status` exits non-zero on degraded OR a dead/stale daemon; Docker
  healthcheck scrapes it. *Why: unclaimed FTSO rewards expire — a silent
  failure is the real risk; the TS tool's log-and-exit was rejected.*
- **D5 — Rotation is `setClaimExecutors`, not `setClaimRecipient`.** The keyed
  entity is the executor; the recipient is a keyless arg. fwd roadmap wording
  is drift; clif does not fix fwd (`docs/onchain-migration.md`).
- **D6 — Selector derived-from-ABI, anchored.** `clif/calldata.py`
  reconstructs the canonical signature from the vendored ABI, computes the
  selector, and asserts it `== 0x8e33aba5` at import (fail-loud). *Why: spec
  constraint 3/4 — never hardcode; cross-check the verified anchor.*
- **D7 — signing-tool deferred to fwd Phase 9.** `SIGNING_POLICY_PRIVATE_KEY`
  is never a local clif key. One README paragraph; no scaffold.
- **D8 — Operator gates production.** The Flare claim and the `.env` deletion
  happen only under explicit operator approval (Core invariant #15, inherited).
- **D9 — Commit doctrine.** Single terse conventional line; **never** any
  Claude/AI co-author or "Generated with" trailer; operator sole author; no
  push if a remote block exists (ask).
- **D10 — Sync, simple substrate.** httpx sync (short sequential paths),
  Typer, eth-abi, Pydantic v2. Stateless re "already claimed" (on-chain
  `getNextClaimableRewardEpochId` + idempotency replay are the source of
  truth); the only persisted artifact is the status JSON (no secrets).
- **D11 — Keyless gate covers the .env *file*, not just live env.**
  `assert_keyless` scans both `os.environ` and the resolved `.env` source
  file for `*PRIVATE_KEY*` names (pydantic silently ignores unknown `.env`
  keys, so a file-resident key would otherwise run green). Strengthens D1;
  a clean env+file still passes. *Why: fwd v1.0.0 audit STOP-SHIP #1 — the
  headline "the `.env PRIVATE_KEY=` line is gone" must not be falsifiable.*
- **D12 — Production idempotency = deterministic + explicit retry knob.**
  `make_idempotency_key(..., retry=None)` is byte-identical to the legacy
  key (same logical attempt — incl. network-retry / crash-rerun — dedups;
  no double-claim). An operator-controlled `retry` (`clif claim --retry` /
  `IDEMPOTENCY_RETRY` for `auto`) yields a fresh key for a **deliberate**
  post-on-chain-failure re-attempt (fwd replay is status-blind by design —
  fwd D14). Never auto-randomised. The `rehearse` `-r<tag>` discriminator
  stays walled off from this path. *Why: fwd v1.0.0 audit STOP-SHIP #2.*
- **D13 — fwd taxonomy + transport resilience.** 400/401/403/404/422
  (`transaction_rejected`) terminal; 502/503/any httpx transport error
  retryable. Transport errors are converted to `FwdRetryableError` in the
  sign + status + health paths — a down/restarting fwd degrades `clif auto`,
  never crashes it (D4 reward-expiry monitoring must survive fwd downtime).
  *Why: fwd v1.0.0 audit STOP-SHIP #3; fwd is correct-as-designed (a down
  fwd cannot emit a status — resilience is the consumer's).*
