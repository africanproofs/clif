"""clif CLI — keyless half (Phase 8b step 1).

Commands: `health`, `list`, `spec`. The signing/submission path (`claim`,
`auto`) is deliberately NOT here yet — it lands after the operator reviews
`docs/fwd-integration-spec.md` and provisions the matching fwd policy +
wallet + caller (canonical prompt: spec artifact before claim-submission
code; plan step 4, operator-gated).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from clif import __version__
from clif.calldata import (
    CLAIM_SELECTOR,
    CLAIM_SIGNATURE,
    EXPECTED_CLAIM_SELECTOR,
    build_claim_calldata,
)
from clif.config import KeylessViolation, Settings, _NETWORKS, load_settings
from clif.discovery import collect_reward_claims
from clif.fwd_client import FwdClient
from clif.models import ClaimType
from clif.rpc import RpcClient

app = typer.Typer(
    add_completion=False,
    help="Keyless FTSO reward claimer — signs via the fwd daemon (Phase 8b).",
)
console = Console()
err = Console(stderr=True)


def _settings() -> Settings:
    try:
        return load_settings()
    except KeylessViolation as exc:
        err.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from exc


def _enabled_claimers(s: Settings) -> list[tuple[ClaimType, str]]:
    out: list[tuple[ClaimType, str]] = []
    if s.identity_address:
        out.append((ClaimType.FEE, s.identity_address))
    if s.signing_policy_address:
        out.append((ClaimType.DIRECT, s.signing_policy_address))
    return out


@app.command()
def version() -> None:
    """Print the clif version."""
    console.print(f"clif {__version__}")


@app.command()
def health() -> None:
    """Probe fwd `/healthz`; exit non-zero unless `master == "ok"`."""
    s = _settings()
    with FwdClient(s.fwd_endpoint, s.fwd_caller_token) as fwd:
        try:
            h = fwd.health()
        except Exception as exc:  # noqa: BLE001 — surface any transport failure
            err.print(f"[bold red]fwd unreachable at {s.fwd_endpoint}: {exc}[/]")
            raise typer.Exit(1) from exc
    console.print(f"endpoint : {s.fwd_endpoint}")
    console.print(f"master   : {h.master}")
    console.print(f"rpc      : {h.rpc}")
    console.print(f"fwd      : {h.fwd}")
    if h.master != "ok":
        err.print("[bold red]fwd sealed master not ready (master != 'ok')[/]")
        raise typer.Exit(1)
    console.print("[bold green]fwd ready[/]")


@app.command(name="list")
def list_claimable(
    network: Annotated[Optional[str], typer.Option(help="Override NETWORK")] = None,
) -> None:
    """List AP's claimable FEE/DIRECT epochs and amounts (keyless)."""
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    claimers = _enabled_claimers(s)
    if not claimers:
        err.print(
            "[yellow]No beneficiary configured. Set IDENTITY_ADDRESS (FEE) "
            "and/or SIGNING_POLICY_ADDRESS (DIRECT).[/]"
        )
        raise typer.Exit(1)
    with RpcClient(s.rpc_url) as rpc:
        for claim_type, beneficiary in claimers:
            console.print(
                f"\n[bold]{claim_type.name}[/] beneficiary={beneficiary} "
                f"network={s.network}"
            )
            claims = collect_reward_claims(rpc, s, beneficiary, int(claim_type))
            if not claims:
                console.print(f"  No claimable {claim_type.name} rewards found")
                continue
            for c in claims:
                ether = c.body.amount / 1e18
                console.print(
                    f"  ✨ epoch {c.body.reward_epoch_id}: "
                    f"{c.body.amount} wei (~{ether:.6f})"
                )


@app.command()
def spec(
    out: Annotated[
        Path, typer.Option(help="Output path")
    ] = Path("docs/fwd-integration-spec.md"),
) -> None:
    """Emit Deliverable 2 — the fwd integration spec, from REAL captured bytes.

    Builds real `claim` calldata from the live keyless discovery path (never a
    hand-authored shape — canonical prompt constraint 3). If a real sample
    cannot be captured (no beneficiary set / RPC down / no claimable epoch),
    that section is written as explicitly PENDING — it is never fabricated.
    """
    s = _settings()
    samples: list[str] = []
    pending: list[str] = []
    claimers = _enabled_claimers(s)
    recipient = s.claim_recipient_address or "0x<CLAIM_RECIPIENT_ADDRESS unset>"

    if not claimers or not s.claim_recipient_address:
        pending.append(
            "No beneficiary/recipient configured: set NETWORK + "
            "IDENTITY_ADDRESS (+ SIGNING_POLICY_ADDRESS for DIRECT) + "
            "CLAIM_RECIPIENT_ADDRESS and re-run against a live RPC during a "
            "claimable epoch."
        )
    else:
        try:
            with RpcClient(s.rpc_url) as rpc:
                for claim_type, beneficiary in claimers:
                    claims = collect_reward_claims(
                        rpc, s, beneficiary, int(claim_type)
                    )
                    if not claims:
                        pending.append(
                            f"{claim_type.name}: no claimable rewards for "
                            f"{beneficiary} on {s.network} right now — real "
                            f"calldata sample pending a real reward epoch."
                        )
                        continue
                    last_epoch = claims[-1].body.reward_epoch_id
                    data = build_claim_calldata(
                        beneficiary, recipient, last_epoch, s.wrap_rewards, claims
                    )
                    samples.append(
                        f"### {claim_type.name} — network={s.network} "
                        f"epochs={[c.body.reward_epoch_id for c in claims]}\n\n"
                        f"- `_rewardOwner` = `{beneficiary}`\n"
                        f"- `_recipient`  = `{recipient}`\n"
                        f"- `_rewardEpochId` (last) = `{last_epoch}`\n"
                        f"- `_wrap` = `{s.wrap_rewards}`\n"
                        f"- `to` (RewardManager) = `{s.net.reward_manager}` "
                        f"chain=`{s.net.chain_id}`\n"
                        f"- calldata length = {len(data)} chars "
                        f"({(len(data) - 2) // 2} bytes)\n\n"
                        f"```\n{data}\n```\n"
                    )
        except Exception as exc:  # noqa: BLE001
            pending.append(f"Live capture failed ({exc}); re-run against a reachable RPC.")

    rows = "\n".join(
        f"| {n.name} | {n.chain_id} | `{n.reward_manager}` | "
        f"`{n.flare_systems_manager}` |"
        for n in _NETWORKS.values()
    )
    samples_md = "\n".join(samples) if samples else (
        "_No real sample captured in this run._"
    )
    pending_md = (
        "\n".join(f"- {p}" for p in pending) if pending else "- None."
    )

    doc = f"""# fwd integration spec — clif (Phase 8b, Deliverable 2)

> Generated by `clif spec`. **For operator review.** clif produces this; the
> operator writes fwd's least-privilege `policy.yaml` and provisions the
> wallet + caller token. clif never authors fwd policy or mints credentials.

## 1. Networks & RewardManager target

| network | chain_id | RewardManager (`to`) | FlareSystemsManager |
|---|---|---|---|
{rows}

## 2. Decoded intent fwd will gate

Canonical signature (reconstructed from the registered ABI, not a doc):

```
{CLAIM_SIGNATURE}
```

Runtime-computed selector: `0x{CLAIM_SELECTOR.hex()}`
Independently-verified anchor (canonical prompt constraint 4):
`0x{EXPECTED_CLAIM_SELECTOR}` — asserted equal at import (fail-loud).

fwd's decoder B1-projects only the **scalar** args into the gateable set:
`_rewardOwner` (address), `_recipient` (address), `_rewardEpochId` (uint24),
`_wrap` (bool). `_proofs` is decoded but not predicable (tuple array). The
fwd policy therefore bounds this method via `max_value_wei: "0"` + a
`_recipient` arg-predicate + rate — **not** a predicate on the proof.

**The value to pin in policy:** `_recipient` = `{recipient}`

## 3. Real captured calldata samples

{samples_md}

### Pending / not captured

{pending_md}

> Per canonical-prompt constraint 3, samples are captured from the live
> keyless discovery path only. A missing sample is reported as pending — it
> is never hand-authored.

## 4. fwd provisioning handshake (operator action)

1. Install a least-privilege `policy.yaml` permitting the clif caller to call
   `RewardManager.claim` on the chosen network's `to` address, with
   `_recipient` pinned to `{recipient}`, `max_value_wei: "0"`, and a sane rate.
2. `POST /v1/admin/wallets` → create the claim wallet (admin-keyed). Note its
   address — that becomes the new on-chain **executor**.
3. `POST /v1/admin/callers` → mint the clif caller token (returned once).
   Inject it into clif as `FWD_CALLER_TOKEN`; set `FWD_WALLET_NAME`.

## 5. On-chain rotation note (doctrine vs code — for the operator/Reviewer)

The fwd roadmap phrases Phase 8 rotation as "on-chain via `setClaimRecipient`".
The producing code (`ftso-fee-claimer/src/claimer.ts:118-142`, README §
Prerequisites) shows the keyed entity is the **executor**
(`CLAIM_EXECUTOR_PRIVATE_KEY`), authorized by the identity / signing-policy
address via **`ClaimSetupManager.setClaimExecutors`** (Flare
`0xD56c0Ea37B848939B59e6F5Cda119b3fA473b5eB`, Songbird
`0xDD138B38d87b0F95F6c3e13e78FFDF2588F1732d`). The recipient
(`{recipient}`) is a keyless argument, separately allow-listed via
`ClaimSetupManager.setAllowedClaimRecipient`. So the Phase 8b rotation is:
authorize fwd's new wallet as **executor** via `setClaimExecutors` from the
offline identity key (operator-only — fwd does not custody identity keys;
clif does not touch this). clif does **not** edit fwd; this drift note is
surfaced for the operator/Reviewer to close fwd-side (canonical prompt
constraint 2). `docs/policy.example.yaml` in fwd also shows the wrong claim
signature — the correct one is §2 above.
"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc)
    console.print(f"[bold green]wrote[/] {out}")
    if samples:
        console.print(f"captured {len(samples)} real calldata sample(s)")
    if pending:
        err.print(f"[yellow]{len(pending)} section(s) PENDING — see the doc[/]")


if __name__ == "__main__":
    app()
