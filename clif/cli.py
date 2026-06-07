"""clif CLI.

Keyless: `version`, `health`, `list`, `spec`. The CANONICAL automation is
`epoch run` (`epoch status`) — one epoch-anchored sign→claim state machine per
network (operator-gated: fwd provisioned + the wallet authorized on-chain as
executor + `FSP_AUTO_ENABLED=true`). Per reward epoch: optional uptime sign →
wait → reward-publication poll → sign rewards → wait for the >threshold
`rewardsHash` finalization → claim that epoch → idle. One-shots + legacy loops:
`claim`, `rehearse`, `auto`/`status` (claim-only), `fsp uptime|rewards|status`,
`fsp auto` (sign-only) — superseded as the daemon by `epoch run`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from clif import __version__
from clif.autostate import (
    AutoState,
    build_report,
    read_status,
    status_exit_code,
    stream_key,
    write_status_atomic,
)
from clif.calldata import (
    CLAIM_SELECTOR,
    CLAIM_SIGNATURE,
    EXPECTED_CLAIM_SELECTOR,
    build_claim_calldata,
)
from clif.claimer import ClaimOutcome, OutcomeStatus, run_claim, submit_claims
from clif.config import KeylessViolation, Settings, _NETWORKS, load_settings
from clif.discovery import classify_claim_frontier, collect_reward_claims
from clif.fwd_client import (
    FwdClient,
    FwdRetryableError,
    FwdTerminalError,
    make_idempotency_key,
)
from clif.models import ClaimType
from clif.fsp import FspOutcome, run_sign_rewards, run_sign_uptime
from clif.fsp_autostate import (
    build_fsp_report,
    fsp_status_exit_code,
    fsp_stream_key,
)
from clif.epoch_auto import (
    build_epoch_report,
    make_epoch_end_ts,
    next_sleep_seconds,
    resolve_voter,
    run_cycle,
)
from clif.reward_data import get_reward_distribution_data
from clif.rpc import RpcClient, RpcError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s clif %(message)s")
log = logging.getLogger("clif")

app = typer.Typer(
    add_completion=False,
    help=(
        "Keyless FTSO reward claimer + FSP signing-tool — signs via the fwd daemon. "
        "Canonical daemon: epoch run / epoch status (per-epoch sign→claim). "
        "One-shots + legacy: claim, rehearse, auto, status, fsp uptime|rewards|status|auto."
    ),
)

fsp_app = typer.Typer(
    add_completion=False,
    help=(
        "Keyless FSP signing-tool — fwd signs the FSP message/tx; clif broadcasts "
        "and reports back. clif holds zero keys."
    ),
)
app.add_typer(fsp_app, name="fsp")

chain_app = typer.Typer(
    add_completion=False,
    help="Keyless chain reads (nonce, ...). No keys; public RPC reads only.",
)
app.add_typer(chain_app, name="chain")

epoch_app = typer.Typer(
    add_completion=False,
    help=(
        "Epoch-anchored sign→claim daemon (replaces `auto` + `fsp auto`). "
        "`epoch run` is the daemon; `epoch status` is the monitoring health."
    ),
)
app.add_typer(epoch_app, name="epoch")
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
    # h.rpc is a retired field (fwd v1.1.0a9+: sign-only, no outbound RPC);
    # omit it to avoid printing "rpc: None" which misleads the operator.
    console.print(f"fwd      : {h.fwd}")
    if h.master != "ok":
        err.print("[bold red]fwd sealed master not ready (master != 'ok')[/]")
        raise typer.Exit(1)
    console.print("[bold green]fwd ready[/]")


@app.command(name="list")
def list_claimable(
    network: Annotated[Optional[str], typer.Option(help="Override NETWORK")] = None,
) -> None:
    """List configured claimable FEE/DIRECT epochs and amounts (keyless)."""
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
                f"\n[bold]{claim_type.name}[/] beneficiary={beneficiary} " f"network={s.network}"
            )
            claims = collect_reward_claims(rpc, s, beneficiary, int(claim_type))
            if not claims:
                # Don't print a bare "none found" — show WHY per frontier epoch
                # (already-claimed vs not-yet-signed vs no-accrual), so a reader
                # never mistakes a DONE state for a PENDING one.
                try:
                    frontier = classify_claim_frontier(rpc, s, beneficiary, int(claim_type))
                except RpcError as exc:
                    console.print(f"  [yellow]could not classify state (rpc): {exc}[/]")
                    continue
                console.print(f"  No claimable {claim_type.name} rewards — current state:")
                for epoch, reason in frontier:
                    console.print(f"    epoch {epoch}: {reason}")
                continue
            for c in claims:
                ether = c.body.amount / 1e18
                console.print(
                    f"  ✨ epoch {c.body.reward_epoch_id}: " f"{c.body.amount} wei (~{ether:.6f})"
                )


@app.command()
def spec(
    out: Annotated[Path, typer.Option(help="Output path")] = Path("docs/fwd-integration-spec.md"),
) -> None:
    """Generate the fwd integration spec from live claim discovery.

    Builds real `claim` calldata from the live keyless discovery path (never a
    hand-authored shape). If a real sample
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
                    claims = collect_reward_claims(rpc, s, beneficiary, int(claim_type))
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
        f"| {n.name} | {n.chain_id} | `{n.reward_manager}` | " f"`{n.flare_systems_manager}` |"
        for n in _NETWORKS.values()
    )
    samples_md = "\n".join(samples) if samples else ("_No real sample captured in this run._")
    pending_md = "\n".join(f"- {p}" for p in pending) if pending else "- None."

    doc = f"""# fwd integration spec - clif

> Generated by `clif spec`. **For operator review.** Regenerate this file for
> the active environment before provisioning fwd. clif produces this; the
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
Independently-verified anchor: `0x{EXPECTED_CLAIM_SELECTOR}` — asserted equal at import (fail-loud).

fwd's decoder B1-projects only the **scalar** args into the gateable set:
`_rewardOwner` (address), `_recipient` (address), `_rewardEpochId` (uint24),
`_wrap` (bool). `_proofs` is decoded but not predicable (tuple array). The
fwd policy therefore bounds this method via `max_value_wei: "0"` + a
`_recipient` arg-predicate + rate — **not** a predicate on the proof. Because
`claim` carries an array/tuple proof argument, fwd policy also needs
`allow_unconstrained_args: true`.

**The value to pin in policy:** `_recipient` = `{recipient}`

## 3. Real captured calldata samples

{samples_md}

### Pending / not captured

{pending_md}

> Samples are captured from the live keyless discovery path only. A missing
> sample is reported as pending — it is never hand-authored.

## 4. fwd provisioning handshake (operator action)

fwd runs with **no host port** (an `internal: true` compose network), so the
operator drives admin through the `clifwd` host wrapper (`docker exec fwd
clifwd …`), not raw HTTP. `fwd onboard rewards` provisions all of the below in
one operator-gated step.

1. Install a least-privilege policy permitting the clif caller to call
   `RewardManager.claim` on the chosen network's `to` address, with
   `_recipient` pinned to `{recipient}`,
   `max_value_wei: "0"`, `allow_unconstrained_args: true`, and a sane rate
   (`clifwd policy init` / `validate`).
2. Create the claim wallet (`clifwd wallets create`, admin-keyed). Note its
   address — that becomes the new on-chain **executor**.
3. Mint the clif caller token (`clifwd callers create`, returned once). Inject
   it into clif as `FWD_CALLER_TOKEN`; set `FWD_WALLET_NAME`.
4. Seed the (wallet, chain) nonce (`clifwd nonce init`) before the first claim.

## 5. On-chain authorization note (for the operator)

The keyed entity is the **executor** (the fwd-managed wallet address from step 2
above), authorized by the identity / signing-policy address via
**`ClaimSetupManager.setClaimExecutors`** (Flare
`0xD56c0Ea37B848939B59e6F5Cda119b3fA473b5eB`, Songbird
`0xDD138B38d87b0F95F6c3e13e78FFDF2588F1732d`). The recipient
(`{recipient}`) is a keyless argument, separately allow-listed via
`ClaimSetupManager.setAllowedClaimRecipients`. Authorization is performed from
the offline identity key (operator-only — fwd does not custody identity keys;
clif does not touch this).
"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc)
    console.print(f"[bold green]wrote[/] {out}")
    if samples:
        console.print(f"captured {len(samples)} real calldata sample(s)")
    if pending:
        err.print(f"[yellow]{len(pending)} section(s) PENDING — see the doc[/]")


def _resolve_claimers(s: Settings, type_filter: str | None) -> list[tuple[ClaimType, str]]:
    pairs = _enabled_claimers(s)
    if type_filter:
        tf = type_filter.lower()
        if tf not in ("fee", "direct"):
            err.print("[bold red]--type must be 'fee' or 'direct'[/]")
            raise typer.Exit(2)
        want = ClaimType.FEE if tf == "fee" else ClaimType.DIRECT
        pairs = [(t, b) for (t, b) in pairs if t == want]
    return pairs


def _exit_for(status: OutcomeStatus) -> int:
    if status == OutcomeStatus.FAILED_TERMINAL:
        return 2
    if status == OutcomeStatus.FAILED_RETRYABLE:
        return 1
    return 0


def _print_outcome(o: ClaimOutcome) -> None:
    line = (
        f"{o.claim_type_name} {o.beneficiary} epochs={o.epochs} " f"→ {o.status.value} ({o.detail})"
    )
    if o.tx_hash:
        line += f" tx={o.tx_hash}"
    if o.status == OutcomeStatus.SUBMITTED_MINED:
        console.print(f"[bold green]{line}[/]")
    elif o.status == OutcomeStatus.FAILED_TERMINAL:
        err.print(f"[bold red]{line}[/]")
    elif o.status == OutcomeStatus.FAILED_RETRYABLE:
        err.print(f"[yellow]{line}[/]")
    else:
        console.print(line)


@app.command()
def preflight(
    identity: Annotated[
        str, typer.Option("--identity", "-i", help="Provider identity / reward owner address")
    ],
    recipient: Annotated[
        Optional[str], typer.Option("--recipient", "-r", help="Intended claim recipient")
    ] = None,
    signing_policy: Annotated[
        Optional[str],
        typer.Option("--signing-policy", help="Registered FSP signing-policy address"),
    ] = None,
    network: Annotated[Optional[str], typer.Option(help="Override NETWORK env")] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json", help="Machine-readable JSON output (exits 0 on RPC error with empty arrays)"
        ),
    ] = False,
    fast_updates_address: Annotated[
        Optional[list[str]],
        typer.Option(
            "--fast-updates-address",
            help="Fast Updates gas wallet (repeatable; not on-chain registered)",
        ),
    ] = None,
) -> None:
    """On-chain pre-flight: registered identity + executor/recipient state (keyless)."""
    import os

    _HEX_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
    _addrs_to_check = ([identity] if identity else []) + list(fast_updates_address or [])
    for _addr in _addrs_to_check:
        if not _HEX_ADDR_RE.match(_addr):
            typer.echo(
                f"error: invalid address format: {_addr!r} (expected 0x + 40 hex chars)", err=True
            )
            raise typer.Exit(1)

    net = network or os.environ.get("NETWORK") or "flare"
    if net not in _NETWORKS:
        if not json_output:
            err.print(f"[bold red]--network must be one of: {', '.join(_NETWORKS)}[/]")
        raise typer.Exit(2)
    netcfg = _NETWORKS[net]
    native = "SGB" if net == "songbird" else ("C2FLR" if net == "coston2" else "FLR")

    executors: list[str] = []
    recipients_on_chain: list[str] = []
    submit_addr = submit_sig_addr = signing_policy_addr = delegation_addr = ""
    node_ids: list[str] = []
    balances: dict[str, int] = {}
    fu_addrs: list[str] = [a for a in (fast_updates_address or []) if a]

    try:
        with RpcClient(netcfg.default_rpc) as rpc:
            if netcfg.entity_manager:
                submit_addr, submit_sig_addr, signing_policy_addr = rpc.get_voter_addresses(
                    netcfg.entity_manager, identity
                )
                delegation_addr = rpc.get_delegation_address(netcfg.entity_manager, identity)
                node_ids = rpc.get_node_ids(netcfg.entity_manager, identity)
                for addr in [
                    identity,
                    delegation_addr,
                    submit_addr,
                    submit_sig_addr,
                    signing_policy_addr,
                ]:
                    if addr:
                        balances[addr.lower()] = rpc.get_balance(addr)
                for addr in fu_addrs:
                    balances[addr.lower()] = rpc.get_balance(addr)
            if netcfg.claim_setup_manager:
                executors = rpc.claim_executors(netcfg.claim_setup_manager, identity)
                recipients_on_chain = rpc.allowed_claim_recipients(
                    netcfg.claim_setup_manager, identity
                )
    except RpcError as exc:
        if json_output:
            print(
                json.dumps(
                    {
                        "network": net,
                        "chain_id": netcfg.chain_id,
                        "identity": identity,
                        "executors": [],
                        "allowed_recipients": [],
                        "fast_updates_addresses": fu_addrs,
                    }
                )
            )
            return
        err.print(f"[bold red]  RPC error: {exc}[/]")
        raise typer.Exit(1)

    if json_output:
        out: dict = {
            "network": net,
            "chain_id": netcfg.chain_id,
            "identity": identity,
            "delegation_address": delegation_addr,
            "submit_address": submit_addr,
            "submit_signatures_address": submit_sig_addr,
            "signing_policy_address": signing_policy_addr or signing_policy or "",
            "node_ids": node_ids,
            "fast_updates_addresses": fu_addrs,
            "executors": executors,
            "allowed_recipients": recipients_on_chain,
        }
        print(json.dumps(out))
        return

    def _bal(addr: str) -> str:
        wei = balances.get(addr.lower(), 0)
        return f"{wei / 10**18:.2f} {native}"

    console.print(f"\n[bold cyan]Preflight — {net} (chain {netcfg.chain_id})[/]")

    if netcfg.entity_manager:
        console.print(f"\n[bold]Registered identity[/] (EntityManager {netcfg.entity_manager})")
        console.print(f"  {'Identity (IA):':<22} {identity}   {_bal(identity)}")
        if delegation_addr:
            console.print(f"  {'Delegation (DA):':<22} {delegation_addr}   {_bal(delegation_addr)}")
        if submit_addr:
            console.print(f"  {'Submit (SA):':<22} {submit_addr}   {_bal(submit_addr)}")
        if submit_sig_addr:
            console.print(
                f"  {'Submit Sigs (SSA):':<22} {submit_sig_addr}   {_bal(submit_sig_addr)}"
            )
        if signing_policy_addr:
            console.print(
                f"  {'Signing Policy (SPA):':<22} {signing_policy_addr}   {_bal(signing_policy_addr)}"
            )
        for i, addr in enumerate(fu_addrs, 1):
            label = f"Fast Updates ({i}):"
            console.print(f"  {label:<22} {addr}   {_bal(addr)}")
        for nid in node_ids:
            console.print(f"  {'Node ID:':<22} {nid}")
    else:
        console.print(f"  identity  : {identity}")
        if recipient:
            console.print(f"  recipient : {recipient}")
        if signing_policy:
            console.print(f"  FSP signer: {signing_policy}")

    if not netcfg.claim_setup_manager:
        console.print(
            f"\n[yellow]  claim setup manager address unknown for {net} — skipping executor/recipient checks[/]"
        )
    else:
        console.print(f"\n[bold]Claim Setup[/] (ClaimSetupManager {netcfg.claim_setup_manager})")
        if executors:
            for ex in executors:
                console.print(f"  executor  : {ex} [dim](authorized)[/]")
        else:
            console.print(
                "  executor  : [yellow]none set — run ClaimSetupManager.setClaimExecutors([new_wallet]) after onboarding[/]"
            )

        if recipients_on_chain:
            for rc in recipients_on_chain:
                match = recipient and rc.lower() == recipient.lower()
                tag = " [bold green]✓ matches --recipient[/]" if match else ""
                console.print(f"  recipient : {rc}{tag}")
            if recipient and recipient.lower() not in [r.lower() for r in recipients_on_chain]:
                console.print(
                    f"  [yellow]WARNING: {recipient} is NOT in the allowed recipients list — run setAllowedClaimRecipients after onboarding[/]"
                )
        else:
            console.print(
                "  recipients: [yellow]none set — run ClaimSetupManager.setAllowedClaimRecipients([recipient]) after onboarding[/]"
            )
            if recipient:
                console.print(
                    f"  [yellow]  → {recipient} will not be able to receive claims until added[/]"
                )

    effective_spa = signing_policy_addr or signing_policy
    if effective_spa:
        console.print("\n[bold]FSP Signing[/]")
        console.print(f"  key       : {effective_spa}")
        console.print(
            "  [dim]use `clif fsp status` to verify voter registration and recent signing activity[/]"
        )

    console.print("\n[bold]Gas Wallets[/]")
    console.print("  [dim]wallet balances available after onboarding via `clifwd wallets list`[/]")
    console.print()


@app.command()
def claim(
    type: Annotated[Optional[str], typer.Option("--type", "-t", help="fee|direct")] = None,
    epoch: Annotated[Optional[int], typer.Option("--epoch", "-e")] = None,
    no_wait: Annotated[bool, typer.Option("--no-wait", help="don't poll to mined")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="skip confirmation prompt")] = False,
    retry: Annotated[
        Optional[str],
        typer.Option(
            "--retry",
            help="DELIBERATE post-on-chain-failure re-attempt discriminator "
            "(overrides IDEMPOTENCY_RETRY). Omit for a normal claim / a "
            "network-retry of the same attempt (fwd dedups on the SAME key — "
            "no double-claim). Only set this to force a fresh idempotency key "
            "after fwd's status-blind replay pinned a failed tx (fwd D14).",
        ),
    ] = None,
) -> None:
    """One-shot claim through fwd (rehearsal-ladder + manual ops).

    Exit: 0 = claimed/nothing-to-do/pending; 1 = transient (retry); 2 =
    terminal (operator action needed).
    """
    s = _settings()
    pairs = _resolve_claimers(s, type)
    if not pairs:
        err.print("[yellow]No beneficiary configured for the requested type.[/]")
        raise typer.Exit(2)
    if retry:
        log.info(
            "claim: DELIBERATE retry discriminator=%r (fresh idempotency key — "
            "operator-intended post-failure re-attempt)",
            retry,
        )
    recipient = s.claim_recipient_address or "[CLAIM_RECIPIENT_ADDRESS not set]"
    native = "SGB" if str(s.network).lower() == "songbird" else "FLR"
    worst = 0
    with RpcClient(s.rpc_url) as rpc:
        confirmed_pairs: list[tuple[ClaimType, str]] = []
        for ct, benef in pairs:
            try:
                preview = collect_reward_claims(rpc, s, benef, int(ct), epoch)
            except RpcError as exc:
                err.print(f"[yellow]{ct.name} discovery failed: {exc} (skipping)[/]")
                continue
            if not preview:
                console.print(f"{ct.name} {benef}: nothing claimable")
                continue
            total_wei = sum(c.body.amount for c in preview)
            epochs_list = [c.body.reward_epoch_id for c in preview]
            console.print(f"\n[bold]{ct.name} claim[/]")
            console.print(f"  beneficiary : {benef}")
            console.print(f"  recipient   : [bold green]{recipient}[/]")
            console.print(f"  epochs      : {epochs_list}")
            console.print(f"  amount      : {total_wei} wei (~{total_wei / 1e18:.6f} {native})")
            console.print(f"  wrap        : {s.wrap_rewards}")
            console.print(f"  network     : {s.network}")
            if not yes:
                typer.confirm("Proceed with claim?", abort=True)
            confirmed_pairs.append((ct, benef))
        if not confirmed_pairs:
            raise typer.Exit(0)
        with FwdClient(s.fwd_endpoint, s.fwd_caller_token) as fwd:
            for ct, benef in confirmed_pairs:
                o = run_claim(
                    s,
                    rpc,
                    fwd,
                    int(ct),
                    benef,
                    only_epoch=epoch,
                    wait=not no_wait,
                    retry=retry,
                )
                _print_outcome(o)
                worst = max(worst, _exit_for(o.status))
    raise typer.Exit(worst)


@app.command()
def rehearse(
    gas: Annotated[int, typer.Option(help="explicit gas limit (clif estimates if 0)")] = 500_000,
    no_wait: Annotated[bool, typer.Option("--no-wait", help="don't poll to mined")] = False,
    idem_tag: Annotated[
        Optional[str],
        typer.Option(
            "--idem-tag",
            help="rehearsal-only idempotency discriminator (default: unix ts). "
            "Each rehearse attempt is a distinct logical request so fwd does "
            "not replay a stale prior outcome. The production claim/auto path "
            "is unaffected — its key stays deterministic (D10).",
        ),
    ] = None,
) -> None:
    """Submit a real-shaped rehearsal claim and prove fwd custody.

    Builds a REAL-shaped `RewardManager.claim` via the real builder / real ABI
    / anchored selector — real discovery first, empty *real* proofs if nothing
    is genuinely claimable (the least hand-modeled valid shape; never a
    hand-authored hex string). POSTs it to fwd `/v1/sign-transaction`; clif
    broadcasts and reports back. Then proves fwd's custody path: the mined
    tx's on-chain `from` == the fwd-custodied executor wallet. clif holds no
    key — `from` is recovered from fwd's signature.

    Exit: 0 proof captured / submitted; 1 transient; 2 terminal (operator).
    """
    s = _settings()
    missing = [
        n
        for n, v in (
            ("FWD_WALLET_NAME", s.fwd_wallet_name),
            ("FWD_CALLER_TOKEN", s.fwd_caller_token),
            ("CLAIM_RECIPIENT_ADDRESS", s.claim_recipient_address),
        )
        if not v
    ]
    if missing:
        err.print(
            f"[bold red]rehearse pre-flight: missing {', '.join(missing)} — "
            "operator must inject these (no broadcast attempted)[/]"
        )
        raise typer.Exit(2)
    recipient = s.claim_recipient_address
    reward_owner = recipient  # not policy-gated; a self-shaped rehearsal claim
    log.info(
        "rehearse network=%s to=%s recipient=%s gas=%s",
        s.network,
        s.net.reward_manager,
        recipient,
        gas,
    )

    with RpcClient(s.rpc_url) as rpc, FwdClient(s.fwd_endpoint, s.fwd_caller_token) as fwd:
        try:
            h = fwd.health()
        except Exception as exc:  # noqa: BLE001 — surface any transport failure
            err.print(f"[bold red]fwd unreachable at {s.fwd_endpoint}: {exc}[/]")
            raise typer.Exit(2) from exc
        if h.master != "ok":
            err.print(f"[bold red]fwd sealed master not ready (master={h.master!r})[/]")
            raise typer.Exit(2)
        log.info("fwd healthy master=ok endpoint=%s", s.fwd_endpoint)

        claims: list = []
        if s.identity_address:
            try:
                claims = collect_reward_claims(rpc, s, s.identity_address, int(ClaimType.FEE))
            except RpcError as exc:
                log.warning("discovery rpc failure (rehearse uses empty proofs): %s", exc)
        log.info(
            "discovery FEE owner=%s claims=%d",
            s.identity_address or "<unset>",
            len(claims),
        )

        epoch_src = "reward_epoch_id_range.end"
        try:
            _, epoch = rpc.reward_epoch_id_range(s.net.reward_manager)
        except RpcError as exc1:
            log.warning("reward_epoch_id_range failed (%s); falling back", exc1)
            epoch_src = "next_claimable_reward_epoch_id"
            try:
                epoch = rpc.next_claimable_reward_epoch_id(s.net.reward_manager, reward_owner)
            except RpcError as exc2:
                err.print(
                    f"[bold red]no real epoch id readable from chain ({exc2}); "
                    "refusing to hand-pick — abort[/]"
                )
                raise typer.Exit(2) from exc2
        if claims:
            epoch = claims[-1].body.reward_epoch_id
            epoch_src = "discovery.last"
        log.info("epoch=%s source=%s", epoch, epoch_src)

        data = build_claim_calldata(reward_owner, recipient, epoch, s.wrap_rewards, claims)
        nbytes = (len(data) - 2) // 2
        console.print(f"[bold]calldata[/] ({nbytes} bytes): {data}")
        log.info(
            "built claim calldata selector=0x%s len=%dB epoch=%s proofs=%d",
            CLAIM_SELECTOR.hex(),
            nbytes,
            epoch,
            len(claims),
        )

        # Production determinism (D10) is preserved: the base key is the exact
        # `make_idempotency_key` the claim/auto path uses. The rehearse-only
        # `-r<tag>` suffix makes each rehearsal a distinct logical request, so
        # fwd cannot replay a stale prior outcome (e.g. a pre-fix failed tx)
        # when the epoch has not rolled. Never applied to the money path.
        tag = idem_tag or str(int(time.time()))
        idem = make_idempotency_key(s.network, int(ClaimType.FEE), reward_owner, epoch) + f"-r{tag}"
        log.info("rehearse idempotency-key=%s (tag=%s)", idem, tag)

        # Estimate EIP-1559 fees for sign-transaction request.
        try:
            max_fee, max_priority = rpc.suggest_fees()
        except Exception as exc:  # noqa: BLE001 — surface any rpc failure
            err.print(f"[bold red]fee estimation failed: {exc}[/]")
            raise typer.Exit(2) from exc

        try:
            resp = fwd.sign_transaction(
                wallet=s.fwd_wallet_name,
                chain=s.net.chain_id,
                to=s.net.reward_manager,
                data=data,
                value_wei="0",
                gas=gas,
                max_fee_per_gas=max_fee,
                max_priority_fee_per_gas=max_priority,
                idempotency_key=idem,
            )
        except FwdTerminalError as exc:
            err.print(f"[bold red]fwd TERMINAL (no broadcast): {exc} — escalate to operator[/]")
            raise typer.Exit(2) from exc
        except FwdRetryableError as exc:
            err.print(f"[yellow]fwd retryable: {exc} (retry later)[/]")
            raise typer.Exit(1) from exc

        console.print(
            f"[bold green]fwd signed[/] tx_id={resp.tx_id} hash={resp.hash} " f"nonce={resp.nonce}"
        )
        log.info(
            "fwd sign-transaction OK tx_id=%s hash=%s nonce=%s",
            resp.tx_id,
            resp.hash,
            resp.nonce,
        )

        # Broadcast the signed tx.
        try:
            broadcast_hash = rpc.send_raw_transaction(resp.signed_raw_tx)
        except Exception as exc:  # noqa: BLE001 — node rejection or transport error
            from clif.claimer import _classify_broadcast_error
            from clif.rpc import RpcError as _RpcError

            if isinstance(exc, _RpcError):
                fwd_outcome, err_class = _classify_broadcast_error(exc)
                try:
                    fwd.report_broadcast_result(resp.tx_id, resp.hash, fwd_outcome, err_class)
                except Exception:  # noqa: BLE001
                    pass
            err.print(f"[bold red]broadcast failed: {exc}[/]")
            raise typer.Exit(2) from exc

        try:
            fwd.report_broadcast_result(resp.tx_id, broadcast_hash, "accepted")
        except Exception:  # noqa: BLE001 — best-effort
            pass

        console.print(f"[bold green]broadcasted[/] hash={broadcast_hash}")
        log.info("rehearse broadcasted hash=%s", broadcast_hash)

        if no_wait:
            console.print("[yellow]--no-wait: not polling to mined[/]")
            return

        receipt_poll = rpc.poll_receipt(broadcast_hash, timeout=600.0)
        if receipt_poll is None:
            err.print(f"[yellow]submitted; receipt poll timed out (tx_id={resp.tx_id})[/]")
            raise typer.Exit(1)

        block_number = int(str(receipt_poll.get("blockNumber", "0x0")), 16)
        rstatus = receipt_poll.get("status")
        mined_ok = int(str(rstatus or "0x0"), 16) == 1
        receipt_outcome = "mined_success" if mined_ok else "mined_reverted"
        try:
            fwd.report_receipt(resp.tx_id, broadcast_hash, receipt_outcome, block_number)
        except Exception:  # noqa: BLE001 — best-effort
            pass

        onchain = rpc.get_transaction_by_hash(broadcast_hash) or {}
        ofrom = onchain.get("from")
        block = receipt_poll.get("blockNumber") or onchain.get("blockNumber")
        console.print("[bold]── Coston2 fwd-custody proof ──[/]")
        console.print(
            f"  fwd     : tx_id={resp.tx_id} hash={broadcast_hash} " f"nonce={resp.nonce}"
        )
        console.print(f"  chain   : block={block} receipt.status={rstatus} from={ofrom}")
        console.print(
            f"  to      : {s.net.reward_manager} (RewardManager, chain=" f"{s.net.chain_id})"
        )
        console.print(f"  recipient (pinned arg) : {recipient}")
        console.print(f"  calldata: {data}")
        log.info(
            "custody proof from=%s block=%s receipt.status=%s",
            ofrom,
            block,
            rstatus,
        )

        # The rehearsal custody proof = the tx is ON-CHAIN (in a block) with a
        # recovered `from`. That `from` is the secp256k1-recovered signer; it
        # being the fwd-custodied executor proves fwd signed and clif holds no
        # key. A REVERTED receipt (status 0x0) is EXPECTED and acceptable — the
        # executor is unauthorised / nothing is claimable (the v1.0.0a3
        # precedent); the proof is `from`, not claim success.
        # The proof is absent only if the tx never landed (no `from` / no block
        # — e.g. a fwd nonce gap): then fail loud + terminal.
        mined_on_chain = bool(ofrom) and block is not None
        reverted = str(rstatus).lower() in ("0x0", "0x00")
        if not mined_on_chain:
            err.print(
                f"[bold red]PROOF NOT CAPTURED — tx not on-chain "
                f"(from={ofrom!r} block={block!r}). "
                "Escalate (likely fwd-side, e.g. a wallet nonce gap; clif "
                "holds no key and does not touch fwd).[/]"
            )
            raise typer.Exit(2)
        tail = (
            f"reverted on-chain (receipt.status={rstatus}) — EXPECTED for a "
            "rehearsal; the proof is `from`, not claim success"
            if reverted
            else f"receipt.status={rstatus}"
        )
        console.print(
            f"[bold green]CUSTODY PROOF CAPTURED[/] — on-chain "
            f"from={ofrom} (secp256k1-recovered == fwd-custodied executor; "
            f"clif holds no key), mined in block {block}; {tail}"
        )


@app.command()
def auto(
    interval: Annotated[Optional[int], typer.Option("--interval", help="poll seconds")] = None,
    type: Annotated[Optional[str], typer.Option("--type", "-t", help="fee|direct")] = None,
) -> None:
    """Legacy claim-only daemon.

    `clif epoch run` is the canonical daemon. This loop remains for
    backward-compatible/manual operation and surfaces degraded state through
    `clif status`.
    """
    s = _settings()
    pairs = _resolve_claimers(s, type)
    if not pairs:
        err.print("[yellow]No beneficiary configured for the requested type.[/]")
        raise typer.Exit(2)
    iv = interval or s.poll_interval_sec
    state = AutoState()
    log.info(
        "auto start network=%s interval=%ss streams=%d state=%s " "idempotency-retry=%s",
        s.network,
        iv,
        len(pairs),
        s.status_file,
        s.idempotency_retry or "<none>",
    )
    try:
        while True:
            now = time.time()
            with RpcClient(s.rpc_url) as rpc, FwdClient(s.fwd_endpoint, s.fwd_caller_token) as fwd:
                for ct, benef in pairs:
                    key = stream_key(s.network, int(ct), benef)
                    try:
                        claims = collect_reward_claims(rpc, s, benef, int(ct))
                    except RpcError as exc:
                        log.warning("%s discovery rpc failure: %s (retry)", key, exc)
                        state.record_attempt(key, now, "discovery-rpc-failure")
                        continue
                    epochs = [c.body.reward_epoch_id for c in claims]
                    claimed = state.observe(key, epochs, now)
                    if claimed:
                        state.record_success(key, now)
                        log.info("%s confirmed claimed epochs=%s", key, claimed)
                    if not claims:
                        # Record WHY nothing is claimable (already-claimed /
                        # not-signed / no-accrual), not a bare conflated string.
                        try:
                            frontier = classify_claim_frontier(rpc, s, benef, int(ct))
                            reason = "nothing-claimable: " + "; ".join(
                                f"{e}:{r}" for e, r in frontier
                            )
                        except RpcError:
                            reason = "nothing-claimable"
                        state.record_attempt(key, now, reason)
                        continue
                    last = epochs[-1]
                    if state.in_cooldown(key, last, now):
                        log.error(
                            "%s epoch %s in terminal cooldown — NOT resubmitting "
                            "(degraded; operator action likely needed)",
                            key,
                            last,
                        )
                        state.record_attempt(key, now, "terminal-cooldown")
                        continue
                    # CLIF-AUTO-DAEMON-002 fix: pass rpc and wait=True so the daemon
                    # broadcasts and polls for receipt confirmation.  wait=False with
                    # rpc=None previously signed but never broadcast — a nonce was
                    # consumed each cycle but no tx ever hit the chain.
                    o = submit_claims(s, fwd, int(ct), benef, claims, wait=True, rpc=rpc)
                    state.record_attempt(key, now, o.status.value)
                    if o.status == OutcomeStatus.SUBMITTED_MINED:
                        # OBS-008: include claim amount in log (from discovered claims).
                        total_wei = sum(c.body.amount for c in claims)
                        recipient = s.claim_recipient_address or "unknown"
                        log.info(
                            "%s claim: epochs=%s amount=%s wei recipient=%s tx=%s",
                            key,
                            o.epochs,
                            total_wei,
                            recipient,
                            o.tx_hash,
                        )
                    elif o.status == OutcomeStatus.SUBMITTED_PENDING:
                        log.info(
                            "%s submitted epochs=%s tx=%s (pending receipt confirmation)",
                            key,
                            o.epochs,
                            o.tx_hash,
                        )
                    elif o.status == OutcomeStatus.MINED_NOOP:
                        log.info(
                            "%s mined noop epochs=%s tx=%s (already claimed)",
                            key,
                            o.epochs,
                            o.tx_hash,
                        )
                    elif o.status == OutcomeStatus.FAILED_RETRYABLE:
                        log.warning("%s transient: %s (retry next cycle)", key, o.detail)
                    elif o.status == OutcomeStatus.FAILED_TERMINAL:
                        if o.last_epoch is not None:
                            state.record_terminal(key, o.last_epoch, now, s.terminal_cooldown_sec)
                        log.error(
                            "%s TERMINAL epochs=%s: %s — operator action likely needed",
                            key,
                            o.epochs,
                            o.detail,
                        )
            report = build_report(state, s.network, iv, s.stale_after_sec, time.time())
            write_status_atomic(s.status_file, report)
            if report["degraded"]:
                log.error("DEGRADED: %s", "; ".join(report["reasons"]))
            time.sleep(iv)
    except KeyboardInterrupt:
        log.info("auto stopped")


@app.command()
def status() -> None:
    """Health for the legacy claim-only daemon.

    Exit: 0 healthy; 2 degraded or daemon dead/stale; 3 no daemon state.
    """
    s = _settings()
    report = read_status(s.status_file)
    code, line = status_exit_code(report)
    (console.print if code == 0 else err.print)(
        f"[{'green' if code == 0 else 'bold red'}]{line}[/]"
    )
    if report is not None:
        for st in report.get("streams", []):
            console.print(
                f"  {st['stream']}  claimable={st['claimable_epochs']}  "
                f"last={st['last_outcome']}"
            )
    raise typer.Exit(code)


def _print_fsp_outcome(o: FspOutcome) -> None:
    line = f"{o.message_type} epoch={o.reward_epoch_id} " f"→ {o.status.value} ({o.detail})"
    if o.tx_hash:
        line += f" tx={o.tx_hash}"
    if o.message_hash:
        line += f" msg_hash={o.message_hash}"
    if o.status == OutcomeStatus.SUBMITTED_MINED:
        console.print(f"[bold green]{line}[/]")
    elif o.status == OutcomeStatus.FAILED_TERMINAL:
        err.print(f"[bold red]{line}[/]")
    elif o.status == OutcomeStatus.FAILED_RETRYABLE:
        err.print(f"[yellow]{line}[/]")
    else:
        console.print(line)


@fsp_app.command()
def uptime(
    epoch: Annotated[int, typer.Option("--epoch", "-e", help="Reward epoch ID to sign")],
    no_wait: Annotated[bool, typer.Option("--no-wait", help="don't poll to mined")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="skip confirmation prompt")] = False,
    retry: Annotated[
        Optional[str],
        typer.Option("--retry", help="deliberate post-on-chain-failure retry discriminator"),
    ] = None,
) -> None:
    """Sign an UPTIME vote (keyless — fwd signs; clif broadcasts + reports back).

    Exit: 0 = mined/pending; 1 = transient; 2 = terminal (operator action needed).
    """
    s = _settings()
    if not yes:
        typer.confirm(f"Sign UPTIME for epoch {epoch}?", abort=True)
    with RpcClient(s.rpc_url) as rpc:
        o = run_sign_uptime(s, epoch, wait=not no_wait, retry=retry, rpc=rpc)
    _print_fsp_outcome(o)
    raise typer.Exit(_exit_for(o.status))


@fsp_app.command()
def rewards(
    epoch: Annotated[int, typer.Option("--epoch", "-e", help="Reward epoch ID to sign")],
    no_wait: Annotated[bool, typer.Option("--no-wait", help="don't poll to mined")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="skip confirmation prompt")] = False,
    retry: Annotated[
        Optional[str],
        typer.Option("--retry", help="deliberate post-on-chain-failure retry discriminator"),
    ] = None,
) -> None:
    """Sign a REWARD_DISTRIBUTION for an epoch (keyless — fwd signs; clif broadcasts + reports back).

    Fetches and validates reward-distribution-data.json first. Never signs an
    unverified rewardsHash. Shows merkle_root + n before prompting.

    Exit: 0 = mined/pending; 1 = transient; 2 = terminal (operator action needed).
    """
    s = _settings()
    rdd = get_reward_distribution_data(s, epoch)
    if rdd is None:
        err.print(
            f"[bold red]reward-distribution-data unavailable for epoch {epoch} "
            "— cannot sign unverified rewardsHash[/]"
        )
        raise typer.Exit(2)
    console.print(
        f"epoch={epoch} merkle_root={rdd.merkle_root} "
        f"no_of_weight_based_claims={rdd.no_of_weight_based_claims}"
    )
    if not yes:
        typer.confirm(
            f"Sign REWARD_DISTRIBUTION for epoch {epoch} with the above data?", abort=True
        )
    with RpcClient(s.rpc_url) as rpc:
        o = run_sign_rewards(s, epoch, wait=not no_wait, retry=retry, rpc=rpc)
    _print_fsp_outcome(o)
    raise typer.Exit(_exit_for(o.status))


@fsp_app.command(name="status")
def fsp_status() -> None:
    """Health for the legacy FSP signing daemon.

    Exit: 0 healthy; 2 degraded or daemon dead/stale; 3 no daemon state.
    """
    s = _settings()
    report = read_status(s.fsp_status_file)
    code, line = fsp_status_exit_code(report)
    (console.print if code == 0 else err.print)(
        f"[{'green' if code == 0 else 'bold red'}]{line}[/]"
    )
    if report is not None:
        for st in report.get("streams", []):
            console.print(
                f"  {st['stream']}  pending={st.get('pending_epochs', st.get('claimable_epochs', []))}  "
                f"last={st['last_outcome']}"
            )
    # Best-effort: read current epoch from chain.
    try:
        with RpcClient(s.rpc_url) as rpc:
            current_epoch = rpc.get_current_reward_epoch_id(s.net.flare_systems_manager)
            console.print(f"  current_reward_epoch_id (chain): {current_epoch}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]current_reward_epoch_id: unavailable ({exc})[/]")
    raise typer.Exit(code)


_FSP_AUTO_LOCK_FILE = "/tmp/clif-fsp-auto.lock"


def _acquire_fsp_auto_lock() -> None:
    """Acquire the fsp-auto singleton lock.

    Writes the current PID to /tmp/clif-fsp-auto.lock.  If the file already
    exists and the recorded PID is still running, print an error and exit — two
    concurrent fsp-auto processes would double-sign epochs.  Stale locks (PID
    gone) are silently overwritten.
    """
    lock_path = Path(_FSP_AUTO_LOCK_FILE)
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
            # Check if that PID is still alive.
            os.kill(existing_pid, 0)
            # If os.kill succeeds, the process exists.
            err.print(
                f"[bold red]clif fsp auto is already running (PID {existing_pid}). "
                f"Lock file: {_FSP_AUTO_LOCK_FILE}. "
                "Two concurrent fsp-auto processes would double-sign epochs. Aborting.[/]"
            )
            raise typer.Exit(2)
        except (ProcessLookupError, PermissionError):
            # Process is gone (ProcessLookupError) or we can't signal it
            # (PermissionError = exists but different user). Treat as stale.
            pass
        except ValueError:
            pass  # malformed PID file — treat as stale
    lock_path.write_text(str(os.getpid()))


def _release_fsp_auto_lock() -> None:
    """Remove the fsp-auto lock file on clean exit."""
    try:
        Path(_FSP_AUTO_LOCK_FILE).unlink(missing_ok=True)
    except OSError:
        pass


@fsp_app.command(name="auto")
def fsp_auto(
    interval: Annotated[
        Optional[int], typer.Option("--interval", help="poll interval seconds")
    ] = None,
    from_epoch: Annotated[
        Optional[int], typer.Option("--from-epoch", help="start from this epoch (default: current)")
    ] = None,
) -> None:
    """Legacy FSP signing daemon.

    Polls the chain for closed epochs and signs UPTIME + REWARD_DISTRIBUTION
    for each unseen epoch. Rewards data must be fetchable before a
    REWARD_DISTRIBUTION sign is attempted — never signs an unverified rewardsHash.
    Writes status to fsp_status_file (scraped by `clif fsp status`).
    """
    s = _settings()
    # Hard-off gate: FSP_AUTO_ENABLED must be explicitly set to true.
    # An unattended signer that signs over WRONG data still produces a
    # cryptographically valid signature — irreversible on-chain (D15 §5 risk).
    if not s.fsp_auto_enabled:
        err.print(
            "[bold red]clif fsp auto is HARD-DISABLED by default. The unattended "
            "REWARDS auto-signer was operator-accepted 2026-05-19 (decisions.md D15), "
            "gated on the MAJOR-1 epoch-bind. To run it the operator must explicitly "
            "set FSP_AUTO_ENABLED=true. Refusing: a valid signature over wrong data is "
            "irreversible on-chain (D15 §5 risk).[/]"
        )
        raise typer.Exit(2)

    # Concurrency guard: one fsp-auto process at a time.
    _acquire_fsp_auto_lock()

    iv = interval or s.fsp_poll_interval_sec
    state = AutoState()

    # Determine watermark epoch: sign only epochs that close while we run,
    # unless --from-epoch overrides.
    watermark: int | None = None
    if from_epoch is not None:
        watermark = from_epoch
        log.info("fsp auto watermark from --from-epoch=%s", watermark)
    else:
        try:
            with RpcClient(s.rpc_url) as rpc:
                watermark = rpc.get_current_reward_epoch_id(s.net.flare_systems_manager)
                log.info("fsp auto watermark from chain current_epoch=%s", watermark)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "fsp auto: could not read current epoch (%s); watermark=None (will init from chain on first poll)",
                exc,
            )

    log.info(
        "fsp auto start network=%s interval=%ss watermark=%s state=%s",
        s.network,
        iv,
        watermark,
        s.fsp_status_file,
    )

    message_types = ["UPTIME", "REWARD_DISTRIBUTION"]
    try:
        while True:
            now = time.time()
            try:
                with RpcClient(s.rpc_url) as rpc:
                    current_epoch = rpc.get_current_reward_epoch_id(s.net.flare_systems_manager)
                    if watermark is None:
                        watermark = current_epoch
                        log.info(
                            "fsp auto: watermark initialized to current_epoch=%s (startup read had failed)",
                            watermark,
                        )
                    # Act on closed epochs (< current) that are >= watermark.
                    closed_epochs = list(range(watermark, current_epoch))
                    for mt in message_types:
                        key = fsp_stream_key(s.network, mt)
                        # Track unsigned epochs as "pending" in the stream.
                        _ = state.observe(key, closed_epochs, now)
                        for epoch in closed_epochs:
                            if state.in_cooldown(key, epoch, now):
                                log.error(
                                    "fsp auto %s epoch %s in terminal cooldown — skipping "
                                    "(degraded; operator action likely needed)",
                                    key,
                                    epoch,
                                )
                                state.record_attempt(key, now, "terminal-cooldown")
                                continue
                            # CLIF-FSP-FLOW-001 fix: always broadcast — do NOT pass wait=False.
                            # wait=False skips _broadcast_and_confirm entirely, consuming the
                            # fwd nonce without ever sending the tx to the chain.
                            o = (run_sign_uptime if mt == "UPTIME" else run_sign_rewards)(
                                s,
                                epoch,
                                wait=True,
                                rpc=rpc,
                            )
                            state.record_attempt(key, now, o.status.value)
                            if o.ok:
                                log.info(
                                    "fsp auto %s epoch %s ok status=%s tx=%s",
                                    key,
                                    epoch,
                                    o.status.value,
                                    o.tx_hash,
                                )
                                # CLIF-FSP-EPOCH-001: advance the watermark after each
                                # successful (or already-finalized) epoch so we never
                                # re-process the same epoch on the next poll cycle.
                                watermark = epoch + 1
                            elif o.status == OutcomeStatus.FAILED_RETRYABLE:
                                log.warning(
                                    "fsp auto %s epoch %s transient: %s (retry next cycle)",
                                    key,
                                    epoch,
                                    o.detail,
                                )
                            elif o.status == OutcomeStatus.FAILED_TERMINAL:
                                state.record_terminal(key, epoch, now, s.fsp_terminal_cooldown_sec)
                                log.error(
                                    "fsp auto %s epoch %s TERMINAL: %s — operator action likely needed",
                                    key,
                                    epoch,
                                    o.detail,
                                )
                                # Advance watermark past terminal epochs too, so we don't
                                # re-attempt until the cooldown expires and they re-appear.
                                watermark = epoch + 1
            except RpcError as exc:
                log.warning("fsp auto rpc failure: %s (retry next cycle)", exc)
            except FwdRetryableError as exc:
                # CLIF-AUTO-DAEMON-007: fwd 429 (rate-limit) is a retryable condition —
                # log and retry next cycle rather than entering terminal cooldown.
                log.warning("fsp auto fwd retryable: %s (retry next cycle)", exc)

            report = build_fsp_report(state, s.network, iv, s.fsp_stale_after_sec, time.time())
            write_status_atomic(s.fsp_status_file, report)
            if report["degraded"]:
                log.error("fsp auto DEGRADED: %s", "; ".join(report["reasons"]))
            time.sleep(iv)
    except KeyboardInterrupt:
        log.info("fsp auto stopped")
    finally:
        _release_fsp_auto_lock()


@epoch_app.command(name="run")
def epoch_run(
    interval: Annotated[
        Optional[int], typer.Option("--interval", help="poll seconds (default EPOCH_POLL_INTERVAL_SEC=1800)")
    ] = None,
    from_epoch: Annotated[
        Optional[int],
        typer.Option("--from-epoch", help="backfill start (default: only epochs that close while running)"),
    ] = None,
) -> None:
    """Epoch-anchored sign→claim daemon — one flow per reward epoch.

    Per epoch N (once it closes): (optional) sign uptime → wait until
    epoch_end+initial_delay, poll for reward publication → sign rewards →
    wait for the >threshold rewardsHash finalization → claim ONLY epoch N →
    idle until the next epoch. Idempotency is chain-derived (getVoter*SignInfo
    + rewardsHash + claim pre-flight), so restarts resume safely.

    Replaces `clif auto` + `clif fsp auto` as the daemon entrypoint. Shares the
    fsp-auto singleton lock (only one signer process per host).
    """
    s = _settings()
    # Hard-off gate (D15): the state machine SIGNS. A valid signature over wrong
    # data is irreversible on-chain, so signing is opt-in.
    if not s.fsp_auto_enabled:
        err.print(
            "[bold red]clif epoch is HARD-DISABLED by default (it signs). Set "
            "FSP_AUTO_ENABLED=true to run it (decisions.md D15). The UPTIME phase "
            "is additionally gated by UPTIME_AUTO_ENABLED (default false).[/]"
        )
        raise typer.Exit(2)

    # One signer at a time (shared with fsp auto — both sign → double-sign risk).
    _acquire_fsp_auto_lock()
    try:
        iv = interval or s.epoch_poll_interval_sec
        claimers = [(int(ct), benef) for ct, benef in _enabled_claimers(s)]
        if not claimers:
            err.print(
                "[bold red]epoch: no claim beneficiary configured "
                "(set IDENTITY_ADDRESS and/or SIGNING_POLICY_ADDRESS).[/]"
            )
            raise typer.Exit(2)
        state = AutoState()

        # Resume the low-watermark: --from-epoch, else the prior status file,
        # else None (handle only epochs that close while we run).
        last_done: int | None = (from_epoch - 1) if from_epoch is not None else None
        if last_done is None:
            prior = read_status(s.epoch_status_file)
            if prior is not None and prior.get("last_done_epoch") is not None:
                last_done = int(prior["last_done_epoch"])

        with RpcClient(s.rpc_url) as rpc0:
            voter = resolve_voter(s, rpc0)
        if not voter:
            err.print(
                "[bold red]epoch: cannot resolve the FSP voter address — set "
                "SIGNING_POLICY_ADDRESS (or IDENTITY_ADDRESS with a known EntityManager).[/]"
            )
            raise typer.Exit(2)

        log.info(
            "epoch start network=%s interval=%ss uptime=%s initial_delay=%ss voter=%s last_done=%s state=%s",
            s.network, iv, s.uptime_auto_enabled, s.epoch_reward_initial_delay_sec,
            voter, last_done, s.epoch_status_file,
        )
        # Reward-epoch timing constants (firstRewardEpochStartTs +
        # rewardEpochDurationSeconds) — read once, then epoch boundaries are pure
        # math (apgateway's model). Read lazily inside the loop so a startup RPC
        # blip just retries next cycle instead of crashing.
        timing: tuple[int, int] | None = None
        try:
            while True:
                now = time.time()
                observations = []
                current = None
                sleep_s = float(iv)  # fallback when timing/RPC unavailable this cycle
                try:
                    with RpcClient(s.rpc_url) as rpc, FwdClient(
                        s.fwd_endpoint, s.fwd_caller_token
                    ) as fwd:
                        if timing is None:
                            timing = rpc.reward_epoch_timing(s.net.flare_systems_manager)
                            log.info(
                                "epoch timing: first_reward_epoch_start_ts=%s reward_epoch_duration_sec=%s",
                                timing[0], timing[1],
                            )
                        epoch_end_ts = make_epoch_end_ts(*timing)
                        last_done, current, observations = run_cycle(
                            s, rpc, fwd, voter, claimers, state, last_done, now,
                            uptime_enabled=s.uptime_auto_enabled,
                            initial_delay=s.epoch_reward_initial_delay_sec,
                            terminal_cooldown=s.epoch_terminal_cooldown_sec,
                            epoch_end_ts=epoch_end_ts,
                        )
                        for o in observations:
                            acts = "".join(f" [{leg}={st}]" for leg, st, _ in o.actions)
                            log.info(
                                "epoch %s phase=%s done=%s: %s%s",
                                o.epoch, o.phase.value, o.done, o.detail, acts,
                            )
                        sleep_s = next_sleep_seconds(
                            observations, current, epoch_end_ts, time.time(),
                            poll_interval=iv, initial_delay=s.epoch_reward_initial_delay_sec,
                        )
                except RpcError as exc:
                    log.warning("epoch rpc failure: %s (retry next cycle)", exc)
                except FwdRetryableError as exc:
                    log.warning("epoch fwd retryable: %s (retry next cycle)", exc)

                report = build_epoch_report(
                    state, s.network, iv, s.epoch_stale_after_sec,
                    last_done, current, observations, time.time(),
                )
                write_status_atomic(s.epoch_status_file, report)
                if report["degraded"]:
                    log.error("epoch DEGRADED: %s", "; ".join(report["reasons"]))
                log.info("epoch sleeping %.0fs", sleep_s)
                time.sleep(sleep_s)
        except KeyboardInterrupt:
            log.info("epoch stopped")
    finally:
        _release_fsp_auto_lock()


@epoch_app.command(name="status")
def epoch_status() -> None:
    """Monitoring health for `clif epoch run` (Docker healthcheck / monitoring).

    Exit: 0 healthy; 2 degraded or daemon dead/stale; 3 no daemon state.
    """
    s = _settings()
    report = read_status(s.epoch_status_file)
    code, line = status_exit_code(report)
    (console.print if code == 0 else err.print)(
        f"[{'green' if code == 0 else 'bold red'}]{line}[/]"
    )
    if report is not None:
        console.print(
            f"  network={report.get('network')} last_done_epoch={report.get('last_done_epoch')} "
            f"current_epoch={report.get('current_epoch')}"
        )
        for e in report.get("epochs", []):
            console.print(f"  epoch {e['epoch']}: {e['phase']} — {e['detail']}")
    raise typer.Exit(code)


@chain_app.command()
def nonce(
    address: Annotated[str, typer.Option("--address", help="Account address (0x...)")],
    network: Annotated[
        Optional[str],
        typer.Option("--network", help="Network override (default: NETWORK env / selected .env)"),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON to stdout")
    ] = False,
) -> None:
    """Read an address's on-chain transaction count (next nonce), keyless.

    Returns latest (mined) + pending (incl. mempool). Used by fwd onboarding to
    seed nonces without fwd touching the chain. --network defaults from the NETWORK
    env (so the `clif` wrapper's leading --network env-selector form works); an
    explicit --network overrides it. Exit: 0 ok; 1 RPC error; 2 keyless.
    """
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    if not address.startswith("0x"):
        err.print("[bold red]--address must be a 0x-prefixed address[/]")
        raise typer.Exit(2)
    with RpcClient(s.rpc_url) as rpc:
        try:
            latest = rpc.get_transaction_count(address, "latest")
            pending = rpc.get_transaction_count(address, "pending")
        except RpcError as exc:
            err.print(f"[bold red]RPC error: {exc}[/]")
            raise typer.Exit(1) from exc
    out = {
        "network": s.network,
        "chain_id": s.net.chain_id,
        "address": address,
        "latest": latest,
        "pending": pending,
    }
    if json_out:
        # Raw stdout — NOT rich/console — so the host can capture byte-clean JSON.
        print(json.dumps(out))
    else:
        console.print(
            f"{s.network} chain_id={out['chain_id']} {address} "
            f"latest={latest} pending={pending}"
        )


if __name__ == "__main__":
    app()
