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

import fcntl
import json
import logging
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, NoReturn, Optional

import fwd_client
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
from clif.config import (
    Capability,
    FWD_CONTRACT_EXPECTED,
    KeylessViolation,
    Settings,
    _NETWORKS,
    capabilities,
    load_settings,
)
from clif.credentials import BundleError, check_bundle_mode, import_credentials
from clif.discovery import classify_claim_frontier, collect_reward_claims
from clif.funding import (
    ACCOUNTS as FUNDING_ACCOUNTS,
    SYMBOL,
    apply_plan,
    read_health,
    render_health,
    run_funding,
    validate_plan,
)
from clif.alert import alert_level, decide, format_alert, heartbeat, post_webhook
from clif.registration import read_readiness, render_readiness
from clif.observe import (
    read_observe_status,
    render_iqr_windows,
    render_observe,
    render_protocol_report,
)
from clif.observe.engine import run_engine
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
    _fmt_dur,
    _fmt_ts,
    build_disabled_report,
    build_epoch_report,
    make_epoch_end_ts,
    next_sleep_seconds,
    resolve_voter,
    run_cycle,
    schedule_line,
)
from clif.reward_data import get_reward_distribution_data
from clif.rpc import RpcClient, RpcError
from clif.signing_progress import compute_signing_progress, refresh_signing_progress

logging.Formatter.converter = (
    time.gmtime
)  # all clif log timestamps in UTC (match on-chain/epoch times)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC %(levelname)s clif %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
# httpx/httpcore log one INFO "HTTP Request: …" line per RPC call. The epoch daemon
# (esp. the per-signer signing-progress scan: one eth_call per signer × 2 kinds) makes
# ~100 calls/cycle, which floods the log and drowns clif's own lines. Silence them to
# WARNING — clif logs every meaningful outcome (signing %, phase, fwd denials) itself.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("clif")

app = typer.Typer(
    add_completion=False,
    # Typer defaults to rich tracebacks WITH LOCALS, which dump caller bearer
    # tokens to the terminal on any uncaught error (observed live 2026-07-24).
    pretty_exceptions_show_locals=False,
    help=(
        "Keyless FTSO reward claimer + FSP signing-tool — signs via the fwd daemon. "
        "Canonical daemon: epoch run / epoch status (per-epoch sign→claim). "
        "One-shots + legacy: claim, rehearse, auto, status, fsp uptime|rewards|status|auto."
    ),
)

fsp_app = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help=(
        "Keyless FSP signing-tool — fwd signs the FSP message/tx; clif broadcasts "
        "and reports back. clif holds zero keys."
    ),
)
app.add_typer(fsp_app, name="fsp")

chain_app = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help="Keyless chain reads (nonce, ...). No keys; public RPC reads only.",
)
app.add_typer(chain_app, name="chain")

epoch_app = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help=(
        "Epoch-anchored sign→claim daemon (replaces `auto` + `fsp auto`). "
        "`epoch run` is the daemon; `epoch status` is the monitoring health."
    ),
)
app.add_typer(epoch_app, name="epoch")

fund_app = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help=(
        "Keyless gas-funding — keep the FSP accounts within their balance band "
        "(gas-payers 250→400, identity/delegation 150→200) via fwd's ap-funder. "
        "`fund health` reads state; `fund once` tops up; `fund run` is the daemon."
    ),
)
app.add_typer(fund_app, name="fund")

registration_app = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help=(
        "Registration readiness — is AP registered, and ready to register, for the "
        "current and next reward epoch (the RE423 defence). `registration status` "
        "reads state; `registration run` is the boundary-aware daemon. OBSERVE-only "
        "— never signs or sends."
    ),
)
app.add_typer(registration_app, name="registration")

observe_app = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help=(
        "Per-block FTSO participation observer (the fsp-observer native port). `observe run` "
        "streams blocks and tracks whether AP's own submit/signatures addresses participate "
        "on-chain each ~90s voting round (on-time + commit/reveal match); `observe status` "
        "reads the rolling health. OBSERVE-only — never signs or sends."
    ),
)
app.add_typer(observe_app, name="observe")

alert_app = typer.Typer(
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help=(
        "Push alerting — the last mile from detected+logged to paged. `alert run` pulls "
        "registration + funding health on the boundary-aware cadence and POSTs a webhook on "
        "CRIT/WARN (debounced; re-pages while bad; RESOLVED on recovery). `alert check` is a "
        "one-shot (add --send to actually post). OBSERVE + send-only — holds no key."
    ),
)
app.add_typer(alert_app, name="alert")
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


def _compat() -> dict:
    """The per-consumer compatibility tuple (ADR-0001 §7)."""
    return {
        "fwd_contract_expected": FWD_CONTRACT_EXPECTED,
        "fwd_client": fwd_client.__version__,
        "claim": __version__,
    }


@app.command()
def doctor(
    network: Annotated[Optional[str], typer.Option("--network", help="Override NETWORK")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON to stdout")] = False,
) -> None:
    """Consumer self-check for the coordinator seam (ADR-0001).

    Aggregates keyless status, fwd reachability, configured capabilities (clif's
    imported view — caller-token presence, NAMES only), the compat tuple, and the
    epoch daemon status. Exit: 0 healthy; 2 fwd unreachable or a running daemon
    degraded. The machine-readable form (`--json`) is the coordinator scrape surface.
    """
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]

    fwd_info: dict = {"endpoint": s.fwd_endpoint, "reachable": False, "master": None}
    try:
        with FwdClient(s.fwd_endpoint, s.fwd_caller_token) as fwd:
            h = fwd.health()
        fwd_info["reachable"] = True
        fwd_info["master"] = h.master
    except Exception as exc:  # noqa: BLE001 — any transport failure ⇒ unreachable
        fwd_info["error"] = str(exc)
    fwd_ok = bool(fwd_info["reachable"]) and fwd_info.get("master") == "ok"

    # Configured = clif holds the caller token (its "imported" view). NAMES only.
    token_by_role = {
        "ftso-reward-claim": s.fwd_caller_token,
        "uptime-vote-sign": s.fsp_uptime_sign_caller_token,
        "uptime-vote-submit": s.fsp_uptime_submit_caller_token,
        "reward-distribution-sign": s.fsp_reward_sign_caller_token,
        "reward-distribution-submit": s.fsp_reward_submit_caller_token,
    }
    cap_status = [
        {
            "capability_id": c.capability_id,
            "role": c.role,
            "configured": bool(token_by_role.get(c.role)),
        }
        for c in capabilities(s)
    ]

    # Contract-address drift: clif PINS protocol addresses (auditable, not
    # hijackable by a compromised registry), but the Foundation re-deploys them
    # from time to time — the VoterRegistry moved on both mainnets in July 2026 and
    # silently broke signing-progress. Compare the pins against the chain's own
    # registry so the next migration surfaces here instead of as a stuck epoch.
    # Best-effort: an unreachable RPC reports "unknown", never a doctor failure.
    contracts: list[dict] = []
    pinned = {
        "RewardManager": s.net.reward_manager,
        "FlareSystemsManager": s.net.flare_systems_manager,
        "ClaimSetupManager": s.net.claim_setup_manager,
        "EntityManager": s.net.entity_manager,
        "VoterRegistry": s.net.voter_registry,
    }
    try:
        with RpcClient(s.rpc_url) as _rpc:
            for cname, addr in pinned.items():
                if not addr:
                    continue
                live = _rpc.contract_address_by_name(cname)
                contracts.append(
                    {
                        "name": cname,
                        "pinned": addr,
                        "onchain": live,
                        "stale": live.lower() != addr.lower(),
                    }
                )
    except Exception as exc:  # noqa: BLE001 — diagnostics only, never fail doctor
        contracts = [{"error": str(exc)}]
    stale_contracts = [c["name"] for c in contracts if c.get("stale")]

    # Registration readiness — the RE423 detector, surfaced here for the coordinator/
    # MCP scrape. Informational: it does NOT gate doctor's exit code (doctor is about
    # clif+fwd health; the `registration status`/daemon own the registration alert).
    registration: dict = {}
    try:
        with RpcClient(s.rpc_url) as _rpc:
            registration = read_readiness(
                _rpc, s.network,
                flare_systems_manager=s.net.flare_systems_manager,
                voter_registry=s.net.voter_registry,
                entity_manager=s.net.entity_manager,
                gas_floor=s.registration_gas_floor,
                sender_account=s.registration_sender_account,
            ).to_dict()
    except Exception as exc:  # noqa: BLE001 — diagnostics only, never fail doctor
        registration = {"error": str(exc)}

    report = read_status(s.epoch_status_file)
    daemon_code, daemon_line = status_exit_code(report)
    daemon = {
        "present": report is not None,
        "degraded": bool(report.get("degraded")) if report else None,
        "summary": daemon_line,
        "exit_code": daemon_code,
    }
    daemon_fail = report is not None and daemon_code != 0  # absence is not a failure

    overall_ok = fwd_ok and not daemon_fail
    code = 0 if overall_ok else 2

    if json_out:
        print(
            json.dumps(
                {
                    "consumer": "claim",
                    "network": s.network,
                    "ok": overall_ok,
                    "keyless": True,
                    "compat": _compat(),
                    "fwd": fwd_info,
                    "capabilities": cap_status,
                    "contracts": contracts,
                    "registration": registration,
                    "daemon": daemon,
                },
                indent=2,
            )
        )
        raise typer.Exit(code)

    head = "[green]" if overall_ok else "[bold red]"
    (console.print if overall_ok else err.print)(
        f"{head}clif doctor — {s.network} — {'OK' if overall_ok else 'ISSUES'}[/]"
    )
    console.print("  keyless  : yes")
    console.print(
        f"  fwd      : {s.fwd_endpoint} reachable={fwd_info['reachable']} "
        f"master={fwd_info.get('master')}"
    )
    for cs in cap_status:
        console.print(f"  {cs['capability_id']}: configured={cs['configured']}")
    if stale_contracts:
        err.print(
            f"  [yellow]contracts: STALE PIN — {', '.join(stale_contracts)} "
            f"moved on-chain; update clif/config.py[/]"
        )
    else:
        console.print(f"  contracts: {len(contracts)} pinned, no drift")
    _rsev = registration.get("severity") or "CRIT"  # the except-branch dict has no severity
    _rmsg = (
        f"read error: {registration.get('error')}"
        if registration.get("error")
        else (
            f"RE{registration.get('current_epoch')} registered={registration.get('current_registered')} "
            f"next=RE{registration.get('next_epoch')} window={'open' if registration.get('next_window_enabled') else 'closed'}"
        )
    )
    if _rsev == "OK":
        console.print(f"  register : {_rmsg}")
    else:
        err.print(f"  [{'yellow' if _rsev == 'WARN' else 'bold red'}]register : {_rsev} — {_rmsg}[/]")
    console.print(f"  daemon   : {daemon_line}")
    c = _compat()
    console.print(
        f"  compat   : fwd_contract={c['fwd_contract_expected']} "
        f"fwd_client={c['fwd_client']} clif={c['claim']}"
    )
    raise typer.Exit(code)


@app.command(name="import-credentials")
def import_credentials_cmd(
    bundle: Annotated[
        Path, typer.Option("--bundle", help="Path to the fwd-emitted credential bundle (JSON)")
    ],
    env_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--env-dir",
            help="Directory holding the per-network .env.<net> (default: cwd, where clif reads)",
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON to stdout")
    ] = False,
) -> None:
    """Consumer side of the fwd credential handoff (ADR-0001 / ADR-0003).

    Reads a one-shot fwd-emitted bundle (JSON), VALIDATES it against the
    capabilities clif actually requests for the bundle's network
    (consumer=="claim", not expired, every capability_id governed), then writes
    the credentials into `<env-dir>/.env.<net>` IDEMPOTENTLY (the rotation
    channel — re-mint the same id + re-import to replace in place) and CONSUMES
    (deletes) the bundle.

    A **v2** bundle is the COMPLETE handoff (ADR-0003 Unit 4b): per capability it
    writes the caller TOKEN and the fwd WALLET NAME, plus a top-level `config`
    section (allowlisted against clif's own env-vars) — so the entire
    `.env.<net>` is sourced from the bundle. A **v1** bundle (tokens only) is
    still accepted for back-compat. One-shot, keyless (the tokens are bearer
    caller tokens, not signing keys; the config carries no key). A token VALUE is
    NEVER printed or logged — output reports capability_ids, counts, and the env
    var NAMES written.

    Exit: 0 imported; 1 bundle missing/unreadable; 2 invalid/expired/ungoverned.

    NOTE: end-to-end verification against a REAL fwd-emitted **v2** bundle is
    PENDING — fwd's v2 bundle-emission (Unit 4b) is the lockstep half and not yet
    deployed; the Songbird canary flips this to proven.
    """
    s = _settings()
    target_dir = env_dir or Path.cwd()

    def _fail(reason: str, code: int) -> NoReturn:
        # Machine-readable error on the --json path; rich line otherwise. Never the token.
        if json_output:
            print(json.dumps({"consumer": "claim", "ok": False, "error": reason, "exit": code}))
        else:
            err.print(f"[bold red]import-credentials: {reason}[/]")
        raise typer.Exit(code)

    if not bundle.is_file():
        _fail(f"bundle not found: {bundle}", 1)
    try:
        check_bundle_mode(bundle)  # spec MUST: refuse a non-0600 plaintext-secret carrier
    except BundleError as exc:
        _fail(str(exc), 2)
    try:
        parsed = json.loads(bundle.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read bundle {bundle}: {exc}", 1)

    try:
        result = import_credentials(parsed, s, target_dir)
    except BundleError as exc:
        _fail(f"rejected — {exc}", 2)
    except OSError as exc:  # env write failed AFTER validation — bundle left intact for retry
        _fail(f"could not write env ({exc}); bundle left intact for retry", 1)

    # Success: CONSUME the one-shot bundle (delete it). Done only after the env
    # write succeeded, so a failed import leaves the bundle for a retry.
    try:
        bundle.unlink()
        consumed = True
    except OSError as exc:  # noqa: BLE001 — write succeeded; surface but don't fail the import
        consumed = False
        err.print(f"[yellow]import-credentials: wrote env but could not delete bundle: {exc}[/]")

    if json_output:
        print(
            json.dumps(
                {
                    "consumer": "claim",
                    "network": result.network,
                    "bundle_version": result.version,
                    "env_file": result.env_file,
                    "imported": len(result.imported),
                    "capability_ids": result.capability_ids,
                    "env_vars_written": result.env_vars_written,  # token NAMES only, never values
                    "wallet_envs_written": result.wallet_envs_written,  # NAMES only (v2)
                    "config_keys_written": result.config_keys,  # NAMES only (v2)
                    "bundle_consumed": consumed,
                },
                indent=2,
            )
        )
        raise typer.Exit(0)

    console.print(
        f"[bold green]imported {len(result.imported)} credential(s)[/] — "
        f"v{result.version} network={result.network} → {result.env_file}"
    )
    for c in result.imported:
        wallet = f" + wallet env {c.wallet_env}" if c.wallet_env else ""
        console.print(f"  {c.capability_id}: wrote env {c.caller_token_env}{wallet}")
    if result.config_keys:
        console.print(f"  config: wrote {len(result.config_keys)} key(s) — {', '.join(result.config_keys)}")
    console.print(
        f"  bundle {'consumed (deleted)' if consumed else 'NOT deleted — remove manually'}"
    )
    raise typer.Exit(0)


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


def _capability_block(c: Capability) -> str:
    """Render one capability as a human-reviewable custody diff (ADR-0001 §4)."""
    lines = [
        f"### `{c.capability_id}`  ({c.role})",
        f"- endpoint: `{c.endpoint}`",
        f"- fwd wallet: `{c.wallet_name or f'<{c.wallet_env} unset>'}`  (env `{c.wallet_env}`)",
        f"- caller token: clif holds it in env `{c.caller_token_env}` "
        "(granted by fwd; the value is never in this doc)",
    ]
    if c.contract:
        lines.append(f"- contract: {c.contract_name} `{c.contract}`")
    lines.append(f"- method: `{c.method}`")
    if c.value_wei is not None:
        lines.append(f"- value: `{c.value_wei}`")
    if c.role == "ftso-reward-claim":
        lines.append(
            f"- recipient pinned: `{c.recipient_pinned or '<CLAIM_RECIPIENT_ADDRESS unset>'}`"
        )
    lines.append(
        f"- suggested rate: {c.suggested_rate}  (request only — fwd policy is authoritative)"
    )
    lines.append("- → approve / reject")
    return "\n".join(lines)


@app.command()
def spec(
    out: Annotated[Path, typer.Option(help="Output path")] = Path("docs/fwd-integration-spec.md"),
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit clif's machine-readable capability-request (ADR-0001) to stdout "
            "instead of writing the markdown handshake.",
        ),
    ] = False,
) -> None:
    """Generate clif's fwd capability-request / integration spec.

    clif's per-network fwd capabilities (ADR-0001 §3) render as a human-reviewable
    custody diff (default markdown) or a machine-readable capability-request
    (`--json`) keyed by `capability_id` + the compat tuple. `clif spec --json` is
    clif's **reference capability-request** — the shape the (deferred)
    `consumer-contract-v1` will formalize. The markdown form also captures a real
    `claim` calldata sample from the live keyless discovery path (PENDING if none —
    never hand-authored).
    """
    s = _settings()
    caps = capabilities(s)
    compat = _compat()
    if json_output:
        payload = {
            "consumer": "claim",
            "network": s.network,
            "compat": compat,
            "capabilities": [asdict(c) for c in caps],
        }
        print(json.dumps(payload, indent=2))
        return

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
    capability_blocks = "\n\n".join(_capability_block(c) for c in caps)

    doc = f"""# fwd integration spec - clif

> Generated by `clif spec`. **For operator review.** Regenerate this file for
> the active environment before provisioning fwd. clif produces this; the
> operator writes fwd's least-privilege `policy.yaml` and provisions the
> wallet + caller token. clif never authors fwd policy or mints credentials.

## Capability requests — claim/{s.network} (ADR-0001 §3/§4)

The custody review for this consumer. Each block is one capability the operator
approves or rejects; the granted caller token is a secret clif holds, never shown
here. Compat: fwd_contract=`{compat['fwd_contract_expected']}` ·
fwd_client=`{compat['fwd_client']}` · clif=`{compat['clif']}`.

{capability_blocks}

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

    net = network or os.environ.get("NETWORK")
    if not net:
        if not json_output:
            err.print(
                "[bold red]no network selected: set NETWORK in the environment "
                "(.env.<net> carries it post-import) or pass --network. "
                "Refusing to default silently to flare.[/]"
            )
        raise typer.Exit(2)
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
def status(
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON to stdout")
    ] = False,
) -> None:
    """Health for the legacy claim-only daemon.

    Exit: 0 healthy; 2 degraded or daemon dead/stale; 3 no daemon state.
    """
    s = _settings()
    report = read_status(s.status_file)
    code, line = status_exit_code(report)
    if json_out:
        print(
            json.dumps(
                {"ok": code == 0, "exit_code": code, "summary": line, "report": report}, indent=2
            )
        )
        raise typer.Exit(code)
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
def fsp_status(
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON to stdout")
    ] = False,
) -> None:
    """Health for the legacy FSP signing daemon.

    Exit: 0 healthy; 2 degraded or daemon dead/stale; 3 no daemon state.
    """
    s = _settings()
    report = read_status(s.fsp_status_file)
    code, line = fsp_status_exit_code(report)
    if json_out:
        print(
            json.dumps(
                {"ok": code == 0, "exit_code": code, "summary": line, "report": report}, indent=2
            )
        )
        raise typer.Exit(code)
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
_fsp_auto_lock_fd: int | None = None


def _acquire_fsp_auto_lock() -> None:
    """Acquire the fsp-auto singleton lock.

    The lock is an ``fcntl.flock`` held on an open fd kept alive for the process
    lifetime.  The kernel releases the flock automatically on ANY process death
    — clean exit, crash, SIGKILL, or reboot — so a stale lock can never exist.
    This is immune to the stale-PID / PID-1-container misfire that the old
    ``os.kill(pid, 0)`` check suffered (the daemon is PID 1 in its container, so
    after a restart it would signal itself and refuse to start).  The PID written
    into the file is informational only.
    """
    global _fsp_auto_lock_fd
    fd = os.open(_FSP_AUTO_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Lock held by a live process (covers BlockingIOError/EWOULDBLOCK).
        existing_pid = "unknown"
        try:
            existing_pid = os.read(fd, 64).decode().strip() or "unknown"
        except OSError:
            pass
        os.close(fd)
        err.print(
            f"[bold red]clif fsp auto is already running (PID {existing_pid}). "
            f"Lock file: {_FSP_AUTO_LOCK_FILE}. "
            "Two concurrent fsp-auto processes would double-sign epochs. Aborting.[/]"
        )
        raise typer.Exit(2)
    # Acquired. Record our PID for diagnostics only, then keep the fd open.
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _fsp_auto_lock_fd = fd


def _release_fsp_auto_lock() -> None:
    """Release the fsp-auto lock on clean exit.

    Releases the ``fcntl.flock`` held on the open fd and closes it (the kernel
    would do this anyway on process death — the lock is auto-released and
    immune to the stale-PID / PID-1-container misfire), then best-effort unlinks
    the lock file.
    """
    global _fsp_auto_lock_fd
    if _fsp_auto_lock_fd is not None:
        try:
            fcntl.flock(_fsp_auto_lock_fd, fcntl.LOCK_UN)
            os.close(_fsp_auto_lock_fd)
        except OSError:
            pass
        _fsp_auto_lock_fd = None
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
        Optional[int],
        typer.Option("--interval", help="poll seconds (default EPOCH_POLL_INTERVAL_SEC=1800)"),
    ] = None,
    from_epoch: Annotated[
        Optional[int],
        typer.Option(
            "--from-epoch",
            envvar="FROM_EPOCH",
            help="backfill start (default: only epochs that close while running). Env: FROM_EPOCH=N",
        ),
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
    # Refuse to start without an explicit NETWORK. `Settings.network` carries a
    # load-bearing pydantic default of "flare" (chicken-and-egg with
    # import-credentials), so a daemon launched with no NETWORK in its env would
    # silently sign/claim on flare — an irreversible wrong-chain action. The
    # post-import `.env.<net>` always carries NETWORK, so this only fires on a
    # mis-provisioned env.
    if not os.environ.get("NETWORK"):
        err.print(
            "[bold red]epoch run refuses to start without an explicit NETWORK "
            "(env or --network); a silent flare default could sign/claim on the "
            "wrong chain[/]"
        )
        raise typer.Exit(2)

    s = _settings()
    _log_daemon_start("epoch", s.network)
    # Hard-off gate (D15): the state machine SIGNS. A valid signature over wrong
    # data is irreversible on-chain, so signing is opt-in.
    if not s.fsp_auto_enabled:
        # D15 hard-off gate: the state machine SIGNS, so signing is opt-in. Rather than
        # exit (which makes `restart: unless-stopped` re-run + re-log the notice forever),
        # IDLE: one clear timestamped line + a fresh "disabled" status (healthcheck stays
        # green) + an hourly heartbeat. Enable with FSP_AUTO_ENABLED=true then `clifctl
        # restart <net>` (env is read at startup).
        log.warning(
            "epoch daemon DISABLED — FSP_AUTO_ENABLED is not true; idling (NOT signing). "
            "Set FSP_AUTO_ENABLED=true in .env.%s and run `clifctl restart %s` to enable "
            "(decisions.md D15; UPTIME additionally gated by UPTIME_AUTO_ENABLED).",
            s.network,
            s.network,
        )
        try:
            while True:
                write_status_atomic(
                    s.epoch_status_file,
                    build_disabled_report(s.network, s.epoch_poll_interval_sec, time.time()),
                )
                time.sleep(3600)
                log.info(
                    "epoch daemon still DISABLED (FSP_AUTO_ENABLED!=true) — network=%s; idling",
                    s.network,
                )
        except KeyboardInterrupt:
            log.info("epoch stopped")
        return

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

        recipient = s.claim_recipient_address or "[CLAIM_RECIPIENT_ADDRESS not set]"
        log.info(
            "epoch start network=%s interval=%ss uptime=%s initial_delay=%ss voter=%s "
            "recipient=%s wrap=%s last_done=%s state=%s",
            s.network,
            iv,
            s.uptime_auto_enabled,
            s.epoch_reward_initial_delay_sec,
            voter,
            recipient,
            s.wrap_rewards,
            last_done,
            s.epoch_status_file,
        )
        if s.logs_rpc == s.rpc_url and s.net.voter_registry:
            log.warning(
                "%s_LOGS_RPC not set — live signing-%% logging AND the event-based "
                "already-signed check (restart re-sign prevention) are INERT (a 409 "
                "idempotency_conflict then falls back to retryable). Set %s_LOGS_RPC to a "
                "full/archive node (e.g. AP's Songbird/Flare archive) to enable both.",
                s.network.upper(),
                s.network.upper(),
            )
        # Reward-epoch timing constants (firstRewardEpochStartTs +
        # rewardEpochDurationSeconds) — read once, then epoch boundaries are pure
        # math (apgateway's model). Read lazily inside the loop so a startup RPC
        # blip just retries next cycle instead of crashing.
        timing: tuple[int, int] | None = None
        # Per-(epoch,kind) signing-progress cache — persists across cycles so the
        # narration scan is incremental (immutable weights/total/threshold fetched
        # once; only new blocks + new signers cost RPC calls each cycle).
        prog_cache: dict = {}
        # In-memory {epoch: reward-sign retry count}, persists across cycles for the
        # daemon's lifetime. Bumps the leg-2 idempotency discriminator on a retryable
        # failure so a transient nonce-too-low self-heals next cycle instead of
        # wedging the epoch on a dead key (epoch_auto._sign_retry_token).
        retry_counts: dict[int, int] = {}
        try:
            while True:
                now = time.time()
                observations = []
                current = None
                sleep_s = float(iv)  # fallback when timing/RPC unavailable this cycle
                try:
                    with (
                        RpcClient(s.rpc_url) as rpc,
                        FwdClient(s.fwd_endpoint, s.fwd_caller_token) as fwd,
                    ):
                        if timing is None:
                            timing = rpc.reward_epoch_timing(s.net.flare_systems_manager)
                            log.info(
                                "epoch timing: first_reward_epoch_start_ts=%s reward_epoch_duration_sec=%s",
                                timing[0],
                                timing[1],
                            )
                        epoch_end_ts = make_epoch_end_ts(*timing)

                        def _our_signed(ep: int) -> bool:
                            """Chain-truth 'have we already signed rewards for ep' via the
                            RewardsSigned events — so a restart before finalization (when
                            getVoterRewardsSignInfo reverts) doesn't re-sign and hit fwd's
                            idempotency_conflict → false TERMINAL. Needs a logs/archive RPC;
                            unavailable ⇒ False (prior behaviour: may re-sign)."""
                            if s.logs_rpc == s.rpc_url or not s.net.voter_registry:
                                return False
                            try:
                                with RpcClient(s.logs_rpc) as lrpc:
                                    return refresh_signing_progress(
                                        prog_cache, lrpc, s.net, ep, voter,
                                        epoch_end_ts=float(epoch_end_ts(ep)), kind="rewards",
                                    ).our_signed
                            except RpcError:
                                return False

                        last_done, current, observations = run_cycle(
                            s,
                            rpc,
                            fwd,
                            voter,
                            claimers,
                            state,
                            last_done,
                            now,
                            uptime_enabled=s.uptime_auto_enabled,
                            initial_delay=s.epoch_reward_initial_delay_sec,
                            terminal_cooldown=s.epoch_terminal_cooldown_sec,
                            epoch_end_ts=epoch_end_ts,
                            our_signed_fn=_our_signed,
                            retry_counts=retry_counts,
                        )
                        for o in observations:
                            acts = "".join(f" [{leg}={st}]" for leg, st, _ in o.actions)
                            log.info(
                                "epoch %s phase=%s done=%s: %s%s",
                                o.epoch,
                                o.phase.value,
                                o.done,
                                o.detail,
                                acts,
                            )
                        # Per-cycle narration: ALWAYS log the recipient (where claimed
                        # funds go), then — for EVERY active epoch — both uptime% and
                        # reward% signing progress. The % scans need a full/archive node
                        # (the public RPC caps eth_getLogs at 30 blocks AND uptime events
                        # sit near epoch-end, so a public-RPC partial would misread 0%):
                        # gate on a configured <NET>_LOGS_RPC and otherwise log one notice.
                        # Self-contained so an RPC hiccup never disrupts the cycle.
                        active = [o for o in observations if not o.done]
                        if active:
                            log.info(
                                "epoch recipient=%s wrap=%s beneficiaries: %s",
                                recipient,
                                s.wrap_rewards,
                                ", ".join(
                                    f"{ClaimType(int(ct)).name}={b}" for ct, b in claimers
                                ),
                            )
                        # Narrate signing % for every non-done epoch (incl. a terminal/cooldown
                        # one — cheap with the 0.5.30 cache, and useful: shows where a stuck
                        # epoch's signing stands). The restart re-sign no longer goes terminal
                        # (event-based already-signed check), so this is the genuine-failure case.
                        if active and s.net.voter_registry:
                            if s.logs_rpc == s.rpc_url:
                                log.warning(
                                    "epoch signing-%% logging disabled — set %s_LOGS_RPC to a "
                                    "full/archive node (public RPC caps eth_getLogs at 30 blocks)",
                                    s.network.upper(),
                                )
                            else:
                                try:
                                    with RpcClient(s.logs_rpc) as lrpc:
                                        for o in active:
                                            for knd in ("uptime", "rewards"):
                                                sp = refresh_signing_progress(
                                                    prog_cache, lrpc, s.net, o.epoch, voter,
                                                    epoch_end_ts=float(epoch_end_ts(o.epoch)),
                                                    kind=knd,
                                                )
                                                log.info(
                                                    "epoch %s %s-signing %s%.2f%% signed "
                                                    "(need %.0f%%); our vote on-chain: %s; "
                                                    "%s signers; finalized=%s%s",
                                                    o.epoch,
                                                    knd,
                                                    "" if sp.complete else "≥",
                                                    sp.signed_pct,
                                                    sp.threshold_pct,
                                                    "yes" if sp.our_signed else "no",
                                                    sp.signer_count,
                                                    sp.finalized,
                                                    "" if sp.complete else " [partial]",
                                                )
                                                # Turn a SILENT miss loud: if the epoch
                                                # finalized WITHOUT our vote for a kind we
                                                # sign, we lost that reward — alarm (the
                                                # benign-vs-missed distinction is definitive
                                                # once finalized: signing is closed).
                                                if (
                                                    sp.complete
                                                    and sp.finalized
                                                    and not sp.our_signed
                                                    and (knd == "rewards" or s.uptime_auto_enabled)
                                                ):
                                                    log.warning(
                                                        "epoch %s %s FINALIZED WITHOUT OUR VOTE — "
                                                        "missed signing window (lost this epoch's %s "
                                                        "reward); investigate fwd/RPC/timing",
                                                        o.epoch,
                                                        knd,
                                                        knd,
                                                    )
                                except RpcError as exc:
                                    log.warning("epoch signing-progress unavailable: %s", exc)
                        _now2 = time.time()
                        sleep_s = next_sleep_seconds(
                            observations,
                            current,
                            epoch_end_ts,
                            _now2,
                            poll_interval=iv,
                            initial_delay=s.epoch_reward_initial_delay_sec,
                        )
                        log.info(
                            "\033[1;36mEPCH\033[0m %s",
                            schedule_line(
                                observations,
                                current,
                                epoch_end_ts,
                                _now2,
                                poll_interval=iv,
                                initial_delay=s.epoch_reward_initial_delay_sec,
                                last_done=last_done,
                            ),
                        )
                        # Funding health, surfaced EVERY cycle, COLOR-CODED, so the
                        # state of the gas-funding is impossible to miss while the
                        # operator is watching reward-signing — louder (🔴🔴 + ERROR
                        # level) when an epoch is active and something is wrong.
                        # Read-only (enforcement is the separate clif-fund-<net>
                        # daemon); read_health never raises, so it can't disrupt the
                        # epoch cycle. A CRIT here = an account is below its floor
                        # (gas-starved risk) or the ap-funder is exhausted.
                        try:
                            _fh = read_health(rpc, s.network)
                            _fline = render_health(_fh, active=bool(active))
                            if _fh.severity == "OK":
                                log.info("%s", _fline)
                            elif _fh.severity == "WARN":
                                log.warning("%s", _fline)
                            else:
                                log.error("%s", _fline)
                        except Exception as _fexc:  # noqa: BLE001 — never break the cycle
                            log.warning("funding-health read skipped: %s", _fexc)
                        # Registration readiness, surfaced the same way (the RE423
                        # detector): are we in the registered voter set for THIS epoch,
                        # and ready for the next? A CRIT here = a live exclusion (no
                        # rewards this epoch) or a prereq that will fail the next
                        # registerVoter. OBSERVE-only; read_readiness never raises.
                        try:
                            _rr = read_readiness(
                                rpc, s.network,
                                flare_systems_manager=s.net.flare_systems_manager,
                                voter_registry=s.net.voter_registry,
                                entity_manager=s.net.entity_manager,
                                gas_floor=s.registration_gas_floor,
                                sender_account=s.registration_sender_account,
                            )
                            _rline = render_readiness(_rr, active=bool(active))
                            if _rr.severity == "OK":
                                log.info("%s", _rline)
                            elif _rr.severity == "WARN":
                                log.warning("%s", _rline)
                            else:
                                log.error("%s", _rline)
                        except Exception as _rexc:  # noqa: BLE001 — never break the cycle
                            log.warning("registration-readiness read skipped: %s", _rexc)
                except RpcError as exc:
                    log.warning("epoch rpc failure: %s (retry next cycle)", exc)
                except FwdRetryableError as exc:
                    log.warning("epoch fwd retryable: %s (retry next cycle)", exc)

                report = build_epoch_report(
                    state,
                    s.network,
                    iv,
                    s.epoch_stale_after_sec,
                    last_done,
                    current,
                    observations,
                    time.time(),
                )
                write_status_atomic(s.epoch_status_file, report)
                if report["degraded"]:
                    log.error("epoch DEGRADED: %s", "; ".join(report["reasons"]))
                log.info(
                    "epoch sleeping %s (until %s)",
                    _fmt_dur(sleep_s),
                    _fmt_ts(time.time() + sleep_s),
                )
                time.sleep(sleep_s)
        except KeyboardInterrupt:
            log.info("epoch stopped")
    finally:
        _release_fsp_auto_lock()


@epoch_app.command(name="status")
def epoch_status(
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON to stdout")
    ] = False,
) -> None:
    """Monitoring health for `clif epoch run` (Docker healthcheck / monitoring).

    Exit: 0 healthy; 2 degraded or daemon dead/stale; 3 no daemon state.
    """
    s = _settings()
    report = read_status(s.epoch_status_file)
    code, line = status_exit_code(report)
    if json_out:
        print(
            json.dumps(
                {"ok": code == 0, "exit_code": code, "summary": line, "report": report}, indent=2
            )
        )
        raise typer.Exit(code)
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


def _sp_dict(sp) -> dict:
    """Serialize a SigningProgress to a JSON-friendly dict."""
    return {
        "kind": sp.kind,
        "signed_pct": round(sp.signed_pct, 2),
        "threshold_pct": round(sp.threshold_pct, 2),
        "signed_weight": sp.signed_weight,
        "total_weight": sp.total_weight,
        "threshold_weight": sp.threshold_weight,
        "finalized": sp.finalized,
        "our_signed": sp.our_signed,
        "message_hash": sp.message_hash,
        "complete": sp.complete,
        "scanned_from_block": sp.scanned_from_block,
        "signer_count": sp.signer_count,
        "signers": [
            {"signing_policy_address": e.signing_policy_address, "voter": e.voter, "weight": e.weight}
            for e in sp.signers
        ],
    }


def _sp_line(sp, voter: str | None) -> str:
    """One human-readable progress line for a SigningProgress (uptime or rewards)."""
    ours = f"{voter[:8]}…: {'signed' if sp.our_signed else 'absent'}" if voter else "—"
    pct = f"{sp.signed_pct:.2f}%" if sp.complete else f"≥{sp.signed_pct:.2f}%"
    label = "uptime-signing" if sp.kind == "uptime" else "reward-signing"
    return (
        f"{label} [bold]{pct}[/] / threshold {sp.threshold_pct:.0f}% — "
        f"{sp.signer_count} signers — finalized: {'yes' if sp.finalized else 'no'} — "
        f"our vote ({ours})"
    )


@epoch_app.command(name="signing-progress")
def epoch_signing_progress(
    epoch: Annotated[
        Optional[int],
        typer.Option(
            "--epoch",
            help="reward epoch id (default: the epoch currently being signed = current-1)",
        ),
    ] = None,
    network: Annotated[
        Optional[str],
        typer.Option("--network", help="network override (default: NETWORK env / selected .env)"),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="emit machine-readable JSON to stdout")
    ] = False,
) -> None:
    """Live signing progress for an epoch — uptime AND reward % of signing weight signed.

    Aggregates the FlareSystemsManager `UptimeVoteSigned` + `RewardsSigned` events for the
    epoch and sums each signer's normalised signing-policy weight (the same basis the >50%
    finalization threshold uses), answering what the on-chain view functions cannot: how close
    each vote is to finalizing, and whether OUR signature is on-chain yet. Also shows the claim
    recipient. Keyless. Exit: 0 ok; 1 RPC error; 2 keyless / misconfig.
    """
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    if not s.net.voter_registry:
        err.print(
            f"[bold red]signing-progress: VoterRegistry not configured for network {s.network}[/]"
        )
        raise typer.Exit(2)
    recipient = s.claim_recipient_address or "[CLAIM_RECIPIENT_ADDRESS not set]"
    try:
        # getLogs scan uses logs_rpc (a full/archive node if <NET>_LOGS_RPC is set —
        # the public RPC caps getLogs at ~30 blocks → partial coverage).
        with RpcClient(s.logs_rpc) as rpc:
            voter = resolve_voter(s, rpc)
            fsm = s.net.flare_systems_manager
            epoch_end_ts = make_epoch_end_ts(*rpc.reward_epoch_timing(fsm))
            target = epoch if epoch is not None else rpc.get_current_reward_epoch_id(fsm) - 1
            up = compute_signing_progress(
                rpc, s.net, target, voter,
                epoch_end_ts=float(epoch_end_ts(target)), kind="uptime",
            )
            rw = compute_signing_progress(
                rpc, s.net, target, voter,
                epoch_end_ts=float(epoch_end_ts(target)), kind="rewards",
            )
    except RpcError as exc:
        err.print(f"[bold red]RPC error: {exc}[/]")
        raise typer.Exit(1) from exc
    out = {
        "network": s.network,
        "epoch": target,
        "recipient": recipient,
        "our_voter": voter,
        "uptime": _sp_dict(up),
        "rewards": _sp_dict(rw),
    }
    if json_out:
        # Raw stdout — NOT rich/console — so the host can capture byte-clean JSON.
        print(json.dumps(out))
    else:
        console.print(f"{s.network} epoch {target} — recipient [bold green]{recipient}[/]")
        for sp in (up, rw):
            console.print("  " + _sp_line(sp, voter))
            if not sp.complete:
                console.print(
                    f"    [yellow]partial scan from block {sp.scanned_from_block} — set "
                    f"{s.network.upper()}_LOGS_RPC to a full/archive node for exact %[/]"
                )


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


def _log_daemon_start(name: str, network: str) -> None:
    """One clear startup line — clif VERSION · daemon · network — logged the instant a daemon
    boots (enabled OR idling), so a redeploy/restart is obvious in `clifctl logs` and the
    running version is easy to track."""
    log.info("clif v%s · %s daemon starting · network=%s", __version__, name, network)


def _fund_log(sev: str, msg: str, *args: object) -> None:
    (log.info if sev == "OK" else log.warning if sev == "WARN" else log.error)(msg, *args)


# A steady HEALTHY fund line re-logs at most this often (a heartbeat) when run as the
# daemon — the poll + any top-up still happen every cycle; only the OK line is tempered.
_FUND_OK_HEARTBEAT_SEC = 6 * 3600


def _fund_pass(*, dry_run: bool, s: Settings, ok_hb: dict | None = None) -> None:
    """One funding pass for s.network: surface health, then top up any account
    below its band's lower bound to its upper bound. Health is ALWAYS surfaced
    (color-coded) even when nothing needs funding — except that a daemon caller
    (passing `ok_hb`) tempers the steady OK line to a heartbeat: it re-logs only on
    a change or every `_FUND_OK_HEARTBEAT_SEC`. WARN/CRIT/error are never tempered;
    one-shot callers (`ok_hb=None`) always log."""
    with RpcClient(s.rpc_url) as rpc:
        fh = read_health(rpc, s.network)
        line = render_health(fh, active=False)
        if ok_hb is not None and fh.severity == "OK":
            now = time.monotonic()
            if line != ok_hb.get("line") or (now - ok_hb.get("ts", 0.0)) >= _FUND_OK_HEARTBEAT_SEC:
                _fund_log("OK", "%s", line)
                ok_hb["line"], ok_hb["ts"] = line, now
        else:
            _fund_log(fh.severity, "%s", line)
        if not fh.below and not fh.funder_crit and fh.error is None:
            return
        if dry_run:
            for a in fh.below:
                log.info(
                    "fund DRY-RUN would top %s %.2f -> %.2f %s",
                    a.name, a.balance, a.band.upper, SYMBOL.get(s.network, ""),
                )
            return
        if not s.funding_caller_token or not s.funding_wallet_name:
            log.error(
                "\033[1;31m🔴 fund: FUNDING_CALLER_TOKEN / FUNDING_WALLET_NAME not set "
                "— cannot fund %s\033[0m", s.network,
            )
            return
        with FwdClient(s.fwd_endpoint, s.funding_caller_token) as fwd:
            res = run_funding(
                rpc=rpc, fwd=fwd, network=s.network,
                wallet=s.funding_wallet_name, chain_id=s.net.chain_id,
            )
    sym = SYMBOL.get(s.network, "")
    for t in res.funded:
        log.info(
            "\033[32m💰 funded %s %.2f -> %.2f %s (+%.2f) %s\033[0m",
            t.name, t.before, t.after, sym, t.after - t.before, t.tx_hash[:16],
        )
    for t in res.failed:
        log.error("\033[1;31m🔴 fund FAILED %s: %s\033[0m", t.name, t.detail)
    if res.error:
        log.error("\033[1;31m🔴 fund pass error: %s\033[0m", res.error)


@fund_app.command(name="health")
def fund_health(
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable (the MCP/agent scrape surface)")] = False,
) -> None:
    """Print the funding health (read-only). Color line, or --json. Exit 0/1/2 = OK/WARN/CRIT."""
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    with RpcClient(s.rpc_url) as rpc:
        fh = read_health(rpc, s.network)
    if json_out:
        print(
            json.dumps(
                {
                    "network": fh.network,
                    "severity": fh.severity,
                    "funder_balance": fh.funder_balance,
                    "error": fh.error,
                    "accounts": [
                        {
                            "name": a.name,
                            "address": a.address,
                            "balance": round(a.balance, 4),
                            "lower": a.band.lower,
                            "upper": a.band.upper,
                            "below": a.below,
                        }
                        for a in fh.accounts
                    ],
                },
                indent=2,
            )
        )
    else:
        print(render_health(fh, active=False))
    raise typer.Exit(0 if fh.severity == "OK" else (1 if fh.severity == "WARN" else 2))


def _resolve_plan(plan: str) -> list[dict]:
    """Parse the --plan JSON: either a list of items or {"topups":[...]}."""
    doc = json.loads(plan)
    items = doc.get("topups", doc) if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError("plan must be a JSON list or {\"topups\": [...]}")
    return items


@fund_app.command(name="propose")
def fund_propose(
    plan: Annotated[str, typer.Option("--plan", help='JSON: [{"account":"FastUpdates-1","amount":200}] or {"account":..,"target":400}')],
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """VALIDATE a proposed funding plan against the hard bounds — execute NOTHING.

    The agent-facing dry-run (ADR-0006): accepts/rejects each line (registry
    allowlist, per-tx cap, band upper, funder runway) so a human (or the agent)
    can review before `fund apply`. Read-only."""
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    items = _resolve_plan(plan)
    reg = {a.name.lower(): a for a in FUNDING_ACCOUNTS.get(s.network, [])}
    with RpcClient(s.rpc_url) as rpc:
        balances: dict[str, float] = {}
        for it in items:
            a = reg.get(str(it.get("account", "")).lower())
            if a is not None:
                balances[a.address.lower()] = rpc.get_balance(a.address) / 1e18
        funder = read_health(rpc, s.network).funder_balance or 0.0
    lines = validate_plan(s.network, items, balances, funder)
    if json_out:
        print(json.dumps([vars(ln) for ln in lines], indent=2))
    else:
        for ln in lines:
            mark = "\033[32m✓ accept\033[0m" if ln.accepted else "\033[1;31m✗ reject\033[0m"
            print(f"  {mark} {ln.account:<16} {ln.amount:>8.2f}  {ln.reason}")
    raise typer.Exit(0 if any(ln.accepted for ln in lines) else 1)


@fund_app.command(name="apply")
def fund_apply(
    plan: Annotated[str, typer.Option("--plan", help="same JSON as `fund propose`")],
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="emit the FundingResult as JSON (the MCP ACT surface)")] = False,
) -> None:
    """VALIDATE a proposed plan, then EXECUTE the accepted lines keyless (the ACT
    surface). Rejected lines are never touched; fwd's policy is the final gate."""
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    if not s.funding_caller_token or not s.funding_wallet_name:
        if json_out:
            print(json.dumps({"error": "FUNDING_CALLER_TOKEN / FUNDING_WALLET_NAME not set"}))
        else:
            err.print("[bold red]fund apply: FUNDING_CALLER_TOKEN / FUNDING_WALLET_NAME not set[/]")
        raise typer.Exit(2)
    items = _resolve_plan(plan)
    reg = {a.name.lower(): a for a in FUNDING_ACCOUNTS.get(s.network, [])}
    sym = SYMBOL.get(s.network, "")
    with RpcClient(s.rpc_url) as rpc:
        balances = {}
        for it in items:
            a = reg.get(str(it.get("account", "")).lower())
            if a is not None:
                balances[a.address.lower()] = rpc.get_balance(a.address) / 1e18
        funder = read_health(rpc, s.network).funder_balance or 0.0
        lines = validate_plan(s.network, items, balances, funder)
        if not json_out:
            for ln in lines:
                if not ln.accepted:
                    log.warning("fund apply REJECTED %s %.2f: %s", ln.account, ln.amount, ln.reason)
        with FwdClient(s.fwd_endpoint, s.funding_caller_token) as fwd:
            res = apply_plan(
                rpc=rpc, fwd=fwd, network=s.network,
                wallet=s.funding_wallet_name, chain_id=s.net.chain_id, lines=lines,
            )
    ok = not res.failed and not res.error
    if json_out:
        print(
            json.dumps(
                {
                    "network": res.network,
                    "ok": ok,
                    "error": res.error,
                    "skipped_ok": res.skipped_ok,
                    "rejected": [
                        {"account": ln.account, "amount": round(ln.amount, 6), "reason": ln.reason}
                        for ln in lines if not ln.accepted
                    ],
                    "funded": [
                        {"account": t.name, "address": t.address, "before": round(t.before, 6),
                         "after": round(t.after, 6), "sent": round(t.sent, 6), "tx_hash": t.tx_hash}
                        for t in res.funded
                    ],
                    "failed": [
                        {"account": t.name, "detail": t.detail} for t in res.failed
                    ],
                },
                indent=2,
            )
        )
        raise typer.Exit(0 if ok else 2)
    for t in res.funded:
        log.info(
            "\033[32m💰 funded %s %.2f -> %.2f %s (+%.2f) %s\033[0m",
            t.name, t.before, t.after, sym, t.after - t.before, t.tx_hash[:16],
        )
    for t in res.failed:
        log.error("\033[1;31m🔴 fund FAILED %s: %s\033[0m", t.name, t.detail)
    if res.error:
        log.error("\033[1;31m🔴 fund apply error: %s\033[0m", res.error)
    raise typer.Exit(0 if ok else 2)


@fund_app.command(name="once")
def fund_once(
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="show what would be funded; send nothing")
    ] = False,
) -> None:
    """One funding pass now (manual). --dry-run shows the plan without sending."""
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    _fund_pass(dry_run=dry_run, s=s)


@fund_app.command(name="run")
def fund_run(
    interval: Annotated[
        Optional[int], typer.Option("--interval", help="poll seconds (default FUNDING_POLL_INTERVAL_SEC)")
    ] = None,
) -> None:
    """Funding daemon — enforce the bands every funding_poll_interval_sec.

    Hard-off by default (FUNDING_ENABLED) so a stray daemon can't move value.
    Refuses to start without an explicit NETWORK (a silent flare default would
    fund the wrong chain's accounts)."""
    if not os.environ.get("NETWORK"):
        err.print("[bold red]fund run refuses to start without an explicit NETWORK[/]")
        raise typer.Exit(2)
    s = _settings()
    _log_daemon_start("fund", s.network)
    if not s.funding_enabled:
        log.warning(
            "funding daemon DISABLED — FUNDING_ENABLED is not true; idling (NOT funding). "
            "Set FUNDING_ENABLED=true in .env.%s and restart to enable.", s.network,
        )
        try:
            while True:
                time.sleep(3600)
                log.info("funding daemon still DISABLED (FUNDING_ENABLED!=true) network=%s", s.network)
        except KeyboardInterrupt:
            log.info("fund stopped")
        return
    iv = interval or s.funding_poll_interval_sec
    log.info(
        "fund start network=%s interval=%ss wallet=%s bands: gas 250→400 · id 150→200",
        s.network, iv, s.funding_wallet_name,
    )
    ok_hb: dict = {}  # heartbeat state: temper the steady OK line to _FUND_OK_HEARTBEAT_SEC
    try:
        while True:
            try:
                _fund_pass(dry_run=False, s=s, ok_hb=ok_hb)
            except Exception as exc:  # noqa: BLE001 — a bad cycle must not kill the daemon
                log.error("\033[1;31m🔴 fund cycle error: %s\033[0m", exc)
            time.sleep(iv)
    except KeyboardInterrupt:
        log.info("fund stopped")


def _read_readiness(s):
    """One readiness read for the current settings' network (opens its own RPC)."""
    with RpcClient(s.rpc_url) as rpc:
        return read_readiness(
            rpc, s.network,
            flare_systems_manager=s.net.flare_systems_manager,
            voter_registry=s.net.voter_registry,
            entity_manager=s.net.entity_manager,
            gas_floor=s.registration_gas_floor,
            sender_account=s.registration_sender_account,
        )


@registration_app.command(name="status")
def registration_status(
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable (the MCP scrape surface)")] = False,
) -> None:
    """Are we registered, and ready to register, for the current + next reward epoch?

    The RE423 detector (read-only). Exit 0/1/2 = OK/WARN/CRIT. A CRIT means either a
    LIVE exclusion (not in the current registered set) or a prereq that will make the
    next registerVoter fail (gas below floor / 0 vote power / entity gap) — or a read
    error (never green on unknown)."""
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    r = _read_readiness(s)
    if json_out:
        print(json.dumps(r.to_dict(), indent=2))
    else:
        print(render_readiness(r, active=False))
    raise typer.Exit(0 if r.severity == "OK" else (1 if r.severity == "WARN" else 2))


# An unchanged healthy registration line re-logs at most this often (a heartbeat,
# so `clifctl logs` still proves the daemon is alive without the per-cycle spam).
_REG_HEARTBEAT_SEC = 3600


@registration_app.command(name="run")
def registration_run(
    interval: Annotated[
        Optional[int], typer.Option("--interval", help="override the far-from-boundary poll seconds")
    ] = None,
) -> None:
    """Boundary-aware registration-readiness daemon (the `clif-registration-<net>` service).

    OBSERVE-only — reads the on-chain registered set + window and logs a COLOR-CODED
    readiness line each cycle; NEVER signs or sends. Tightens its cadence within
    `registration_tight_window_sec` of the reward-epoch boundary (where the registerVoter
    window opens for ~6.7 min). Hard-off unless REGISTRATION_ENABLED=true; refuses to
    start without an explicit NETWORK."""
    if not os.environ.get("NETWORK"):
        err.print("[bold red]registration run refuses to start without an explicit NETWORK[/]")
        raise typer.Exit(2)
    s = _settings()
    _log_daemon_start("registration", s.network)
    if not s.registration_enabled:
        log.warning(
            "registration daemon DISABLED — REGISTRATION_ENABLED is not true; idling. "
            "Set REGISTRATION_ENABLED=true in .env.%s and restart to enable.", s.network,
        )
        try:
            while True:
                time.sleep(3600)
                log.info("registration daemon still DISABLED network=%s", s.network)
        except KeyboardInterrupt:
            log.info("registration stopped")
        return
    far = interval or s.registration_poll_interval_sec
    log.info(
        "registration start network=%s cadence=%ss (tight %ss within %ss of boundary) gas_floor=%s",
        s.network, far, s.registration_tight_interval_sec, s.registration_tight_window_sec,
        s.registration_gas_floor,
    )
    last_key: tuple | None = None
    last_log = 0.0
    try:
        while True:
            sleep_for = far
            try:
                r = _read_readiness(s)
                write_status_atomic(s.registration_status_file, r.to_dict())
                line = render_readiness(r, active=False)
                sev = r.severity
                # Temperance — a steady green line every 2–10 min is metronome noise.
                # Log on a STATE change (the volatile T-countdown is excluded from the
                # key), on ANY non-OK severity (alarms are never tempered), else a bare
                # heartbeat at most hourly. So the log shows registration transitions,
                # not the unchanged success repeated.
                key = (
                    sev, r.current_registered, r.next_registered,
                    r.next_window_enabled, r.gas_ok, r.entity_ok, r.votepower_ok,
                )
                now = time.monotonic()
                if sev != "OK" or key != last_key or (now - last_log) >= _REG_HEARTBEAT_SEC:
                    (log.info if sev == "OK" else log.warning if sev == "WARN" else log.error)(line)
                    last_key, last_log = key, now
                # Tighten the cadence only when there is an OPEN, UNRESOLVED registration
                # to catch: the window is actually enabled (poll fast to catch WARN→OK or
                # a will-FAIL the moment it resolves), or we're within the boundary window
                # (belt-and-suspenders if the on-chain window opens before the heuristic
                # guesses) — AND we are not yet registered for the next epoch. Once
                # RE(N+1) is ✓ the catch is done, so we fall back to the far cadence
                # instead of re-polling the success every 2 min. NOT on a persistent
                # current-epoch exclusion either (known, unfixable — the far line shows it).
                ttb = r.time_to_boundary_sec
                near_boundary = ttb is not None and ttb <= s.registration_tight_window_sec
                if (r.next_window_enabled or near_boundary) and not r.next_registered:
                    sleep_for = s.registration_tight_interval_sec
            except Exception as exc:  # noqa: BLE001 — a bad cycle must not kill the daemon
                log.error("\033[1;31m🔴 registration cycle error: %s\033[0m", exc)
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        log.info("registration stopped")


@observe_app.command(name="status")
def observe_status(
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable (the MCP scrape surface)")] = False,
    full: Annotated[bool, typer.Option("--full", help="explicit per-protocol FSP health report")] = False,
) -> None:
    """The rolling FTSO participation health (read from the engine's status file). Exit 0/1/2
    = OK/WARN/CRIT. CRIT = a reveal offence, sustained non-participation, or a stale engine."""
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    h = read_observe_status(s.observe_status_file, enabled=s.observe_enabled)
    if json_out:
        print(json.dumps(h.to_dict(), indent=2))
    elif full:
        for line in render_protocol_report(h):
            print(line)
    else:
        print(render_observe(h, active=False))
        for line in render_iqr_windows(h):
            print(line)
    raise typer.Exit(0 if h.severity == "OK" else (1 if h.severity == "WARN" else 2))


@observe_app.command(name="run")
def observe_run() -> None:
    """The per-block FTSO observer engine (the `clif-observe-<net>` service). Streams blocks,
    classifies AP's submissions per voting round, writes the rolling status file. Hard-off
    unless OBSERVE_ENABLED=true; refuses to start without an explicit NETWORK. OBSERVE-only."""
    if not os.environ.get("NETWORK"):
        err.print("[bold red]observe run refuses to start without an explicit NETWORK[/]")
        raise typer.Exit(2)
    s = _settings()
    _log_daemon_start("observe", s.network)
    if not s.observe_enabled:
        log.warning(
            "observer DISABLED — OBSERVE_ENABLED is not true; idling. "
            "Set OBSERVE_ENABLED=true in .env.%s and restart to enable.", s.network,
        )
        try:
            while True:
                time.sleep(3600)
                log.info("observer still DISABLED network=%s", s.network)
        except KeyboardInterrupt:
            log.info("observe stopped")
        return
    accts = {a.name: a.address for a in FUNDING_ACCOUNTS.get(s.network, [])}
    our_submit = accts.get("Submit")
    our_sig = accts.get("SubmitSignatures")
    identity = accts.get("Identity")  # the registered voter key — for the registration overlay
    ap_signing_policy = accts.get("SigningPolicy")  # for fast-update (255) attribution
    if not our_submit or not our_sig:
        err.print(f"[bold red]observe: no Submit/SubmitSignatures address for {s.network}[/]")
        raise typer.Exit(2)
    with RpcClient(s.observe_rpc_url) as rpc:
        # Startup contract resolution — retry transient RPC blips (conn reset / node hiccup)
        # with backoff and a ONE-LINE warning, rather than crashing the daemon with a full
        # traceback (the engine's own loop already retries head/block reads once running).
        submission = None
        for attempt in range(1, 13):
            try:
                submission = rpc.contract_address_by_name("Submission")
                break
            except RpcError as exc:
                log.warning("observe startup: Submission resolve failed (attempt %d): %s", attempt, exc)
                time.sleep(min(5 * attempt, 30))
        if not submission or int(submission, 16) == 0:
            err.print("[bold red]observe: could not resolve the Submission contract[/]")
            raise typer.Exit(2)
        try:  # FdcHub is optional — FDC tracking degrades off if it can't be resolved
            fdc_hub = rpc.contract_address_by_name("FdcHub")
            if fdc_hub and int(fdc_hub, 16) == 0:
                fdc_hub = None
        except RpcError:
            fdc_hub = None
        # Read the PRIOR run's last processed block (before we overwrite the status file) so the
        # engine resumes from where it left off — gap-free across restarts, not just quick ones.
        prior_last_block = read_observe_status(s.observe_status_file, enabled=s.observe_enabled).last_block
        run_engine(
            rpc=rpc, network=s.network, submission_address=submission,
            our_submit=our_submit, our_sig=our_sig,
            status_writer=lambda d: write_status_atomic(s.observe_status_file, d),
            prior_last_block=prior_last_block,
            resume_max_blocks=s.observe_resume_max_blocks,
            ftso_round_reward_flr=s.observe_ftso_round_reward_flr,
            lookback_blocks=s.observe_lookback_blocks,
            window_rounds=s.observe_window_rounds,
            poll_sec=s.observe_poll_sec,
            status_log_sec=s.observe_status_log_sec,
            degraded_log_sec=s.observe_degraded_log_sec,
            confirmations=s.observe_confirmations,
            live_lag_blocks=s.observe_live_lag_blocks,
            max_backfill_blocks=s.observe_max_backfill_blocks,
            gaps_file=str(s.observe_gaps_file),
            voter_registry=s.net.voter_registry,
            flare_systems_manager=s.net.flare_systems_manager,
            identity=identity,
            fdc_hub=fdc_hub,
            ap_signing_policy=ap_signing_policy,
            validator_node_id=s.net.validator_node_id or None,
            delegation_addr=accts.get("Delegation"),
            deleg_history_file=str(s.observe_deleg_history_file),
            mincond_history_file=str(s.observe_mincond_history_file),
            entity_manager=s.net.entity_manager,
            verify_rpc_url=(s.verify_rpc_url if s.quorum_enabled else None),
            quorum_crit=s.observe_quorum_crit,
            iqr_cache_dir=str(s.clif_state_dir),
            iqr_enabled=s.observe_iqr,
            iqr_history_file=str(s.observe_iqr_history_file),
            log=log,
        )


@observe_app.command(name="iqr")
def observe_iqr(
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    rounds: Annotated[int, typer.Option("--rounds", help="how many recent fully-revealed rounds to score")] = 10,
    top: Annotated[int, typer.Option("--top", help="show the N worst-inner feeds (text mode)")] = 25,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Score AP's inner (primary/IQR) + outer (secondary/PCT) reward-band HIT RATES over the last
    N fully-revealed rounds. Native: computes the consensus median/quartiles from ALL registered
    voters' reveals, then scores AP's own submitted values. OBSERVE-only; works even while AP is
    excluded (scores AP's values vs the registered consensus = would-be reward quality). One-shot
    (scans the reveal windows) — a few minutes for ~10 rounds."""
    from clif.observe.iqr import build_voter_weight_map, overall, score_ap
    from clif.observe.reward_rule import get_offer_params, reward_epoch_id_for_vr
    from clif.observe.timing import voting_factory

    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    ap_submit = {a.name: a.address for a in FUNDING_ACCOUNTS.get(s.network, [])}.get("Submit")
    if not ap_submit:
        err.print(f"[bold red]observe iqr: no Submit address for {s.network}[/]")
        raise typer.Exit(2)
    with RpcClient(s.observe_rpc_url) as rpc:
        submission = rpc.contract_address_by_name("Submission")
        f = voting_factory(s.network)
        epoch = reward_epoch_id_for_vr(f.now_id())
        log.info("iqr: resolving offer params + voter weights for %s epoch %s …", s.network, epoch)
        offer = get_offer_params(rpc, s.network, epoch, cache_dir=str(s.clif_state_dir))
        wmap = build_voter_weight_map(
            rpc, voter_registry=s.net.voter_registry, entity_manager=s.net.entity_manager, epoch=epoch
        )
        scores, scored = score_ap(
            rpc, network=s.network, submission=submission, ap_submit=ap_submit,
            voter_registry=s.net.voter_registry, entity_manager=s.net.entity_manager,
            offer=offer, weight_map=wmap, factory=f, rounds=rounds, log=log,
        )
    ov = overall(scores)
    active = [fs for fs in scores.values() if fs.rounds > 0]
    if json_out:
        print(json.dumps({
            "network": s.network, "reward_epoch": epoch, "scored_rounds": scored,
            "registered_voters": len(wmap), "overall": ov,
            "feeds": [fs.to_dict() for fs in active],
        }, indent=2))
    else:
        console.print(
            f"[bold]IQR scoring {s.network} epoch {epoch}[/] — {scored} rounds, {len(wmap)} voters"
        )
        oi, oo = ov["inner_pct"], ov["outer_pct"]
        console.print(f"  [bold]OVERALL[/]: inner {oi}% · outer {oo}%  ({ov['feed_rounds']} feed-rounds)")
        for fs in sorted(active, key=lambda x: (x.expected_inner_pct or 0))[:top]:
            cap = " [dim](capped)[/]" if fs.capped >= fs.rounds * 0.5 else ""
            console.print(
                f"  {fs.feed:<12} n={fs.rounds:>2}  inner {fs.expected_inner_pct:>5}%  outer {fs.outer_pct:>5}%{cap}"
            )
    raise typer.Exit(0)


def _load_json(path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _redact_url(url: str) -> str:
    """Show the host but hide a webhook's secret token path."""
    try:
        from urllib.parse import urlparse

        u = urlparse(url)
        return f"{u.scheme}://{u.hostname}/…"
    except ValueError:
        return "<webhook>"


@alert_app.command(name="check")
def alert_check(
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    send: Annotated[bool, typer.Option("--send", help="actually POST to the webhook")] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """One-shot: compute the current alert level (registration + funding) + the message. Exit
    0/1/2 = OK/WARN/CRIT. `--send` posts it to ALERT_WEBHOOK_URL."""
    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    readiness = _read_readiness(s).to_dict()
    with RpcClient(s.rpc_url) as rpc:
        fh = read_health(rpc, s.network)
    level, reasons = alert_level(readiness, fh)
    epoch = readiness.get("current_epoch")
    msg = format_alert(s.network, epoch, level, reasons, "CHECK")
    if json_out:
        print(json.dumps({"network": s.network, "level": level, "epoch": epoch, "reasons": reasons}, indent=2))
    else:
        console.print(msg)
    if send:
        if not (s.alert_webhook_url or s.alert_heartbeat_url):
            err.print("[bold red]alert check --send: neither ALERT_WEBHOOK_URL nor ALERT_HEARTBEAT_URL set[/]")
            raise typer.Exit(2)
        if s.alert_webhook_url:
            console.print(f"[dim]webhook: {'sent' if post_webhook(s.alert_webhook_url, msg) else 'FAILED'}[/]")
        if s.alert_heartbeat_url:
            console.print(f"[dim]heartbeat ({level}): {'sent' if heartbeat(s.alert_heartbeat_url, level) else 'FAILED'}[/]")
    raise typer.Exit(0 if level == "OK" else (1 if level == "WARN" else 2))


@alert_app.command(name="run")
def alert_run() -> None:
    """The push-alert daemon (`clif-alert-<net>`). Pulls registration + funding health on the
    boundary-aware cadence; POSTs to the webhook on CRIT/WARN (debounced by `alert_confirm_cycles`,
    re-pages every `alert_repeat_sec`, RESOLVED on recovery). Hard-off unless ALERT_ENABLED=true."""
    if not os.environ.get("NETWORK"):
        err.print("[bold red]alert run refuses to start without an explicit NETWORK[/]")
        raise typer.Exit(2)
    s = _settings()
    _log_daemon_start("alert", s.network)

    def _idle(reason: str) -> None:
        log.warning("alert daemon idling — %s. Set ALERT_ENABLED=true + ALERT_WEBHOOK_URL and restart.", reason)
        try:
            while True:
                time.sleep(3600)
                log.info("alert daemon still idle network=%s (%s)", s.network, reason)
        except KeyboardInterrupt:
            log.info("alert stopped")

    if not s.alert_enabled:
        return _idle("ALERT_ENABLED is not true")
    if not (s.alert_webhook_url or s.alert_heartbeat_url):
        return _idle("neither ALERT_WEBHOOK_URL nor ALERT_HEARTBEAT_URL is set")

    def _beat(level: str) -> None:
        if s.alert_heartbeat_url:
            heartbeat(s.alert_heartbeat_url, level)

    far = s.registration_poll_interval_sec
    log.info(
        "alert start network=%s cadence=%ss (tight %ss near boundary) webhook=%s heartbeat=%s repeat=%ss confirm=%s",
        s.network, far, s.registration_tight_interval_sec,
        _redact_url(s.alert_webhook_url) if s.alert_webhook_url else "off",
        _redact_url(s.alert_heartbeat_url) if s.alert_heartbeat_url else "off",
        s.alert_repeat_sec, s.alert_confirm_cycles,
    )
    state = _load_json(s.alert_status_file)
    try:
        while True:
            sleep_for = far
            try:
                readiness = _read_readiness(s).to_dict()
                with RpcClient(s.rpc_url) as rpc:
                    fh = read_health(rpc, s.network)
                level, reasons = alert_level(readiness, fh)
                epoch = readiness.get("current_epoch")
                send, kind, state = decide(
                    state, level, time.time(),
                    repeat_sec=s.alert_repeat_sec, confirm=s.alert_confirm_cycles,
                )
                write_status_atomic(
                    s.alert_status_file, {**state, "network": s.network, "epoch": epoch, "reasons": reasons}
                )
                lvl_log = log.info if level == "OK" else (log.warning if level == "WARN" else log.error)
                lvl_log("\033[38;5;198m ALRT\033[0m %s — %s", level, f"paged ({kind})" if send else "no page")
                if send and s.alert_webhook_url and not post_webhook(
                    s.alert_webhook_url, format_alert(s.network, epoch, state["level"], reasons, kind)
                ):
                    log.error("\033[1;31m ALRT webhook POST failed — retrying next cycle\033[0m")
                _beat(level)  # dead-man's-switch: healthy → base ping, CRIT → /fail
                ttb = readiness.get("time_to_boundary_sec")
                if ttb is not None and ttb <= s.registration_tight_window_sec:
                    sleep_for = s.registration_tight_interval_sec
            except RpcError as exc:
                log.error(" ALRT cycle RPC error: %s (retry next cycle)", exc)
                _beat("CRIT")  # can't read chain ⇒ blind ⇒ signal /fail (if the ping itself reaches out)
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        log.info("alert stopped")


@app.command(name="budget")
def budget_cmd(
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Per-epoch minimal-conditions budget — the 'cannot breach 20%' tracker. FTSO submission rate
    for the CURRENT reward epoch (reconstructed for the whole epoch via the Submit nonce delta) vs
    the 80% floor, as a depleting miss-budget + at-current-pace projection. Exit 0/1/2 = OK/WARN/CRIT."""
    from clif.observe.budget import read_ftso_budget
    from clif.observe.timing import voting_factory

    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    submit = {a.name: a.address for a in FUNDING_ACCOUNTS.get(s.network, [])}.get("Submit")
    # Needs archive state (a historical epoch-start nonce) → the verify/public RPC, not the
    # pruned observe node.
    with RpcClient(s.verify_rpc_url) as rpc:
        b = read_ftso_budget(rpc, submit_addr=submit, factory=voting_factory(s.network))
    if json_out:
        print(json.dumps(b, indent=2))
    else:
        console.print(f"[bold]minimal-conditions budget — {s.network} RE{b['epoch']}[/] "
                      f"({b['rounds_elapsed']}/{b['rounds_total']} rounds elapsed)")
        console.print(f"  FTSO submission : [bold]{b['rate_pct']}%[/] (floor {b['threshold_pct']}%)")
        console.print(f"  miss-budget     : {b['budget_left']}/{b['miss_budget']} rounds left "
                      f"({b['budget_left_pct']}%) · missed {b['missed']}")
        console.print(f"  at current pace : projected {b['projected_final_pct']}% at epoch end "
                      f"→ [bold]{b['severity']}[/]")
    raise typer.Exit(0 if b["severity"] == "OK" else (1 if b["severity"] == "WARN" else 2))


@app.command(name="delegation")
def delegation_cmd(
    network: Annotated[Optional[str], typer.Option("--network", envvar="NETWORK")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Live delegation snapshot — validator (P-chain self-bond + delegated) + FTSO (WNat vote power)."""
    import time as _time

    from clif.observe.deleg_history import DelegSnap, compute_deltas, load_snaps
    from clif.observe.delegation import read_delegation
    from clif.observe.reward_rule import reward_epoch_id_for_vr
    from clif.observe.timing import voting_factory

    s = _settings()
    if network:
        s.network = network  # type: ignore[assignment]
    deleg_addr = {a.name: a.address for a in FUNDING_ACCOUNTS.get(s.network, [])}.get("Delegation")
    with RpcClient(s.observe_rpc_url) as rpc:
        d = read_delegation(rpc, network=s.network, node_id=s.net.validator_node_id or None,
                            delegation_addr=deleg_addr)
    # 24h / reward-epoch deltas from the observer's persisted snapshot log (read-only here).
    now = int(_time.time())
    epoch = reward_epoch_id_for_vr(voting_factory(s.network).now_id())
    v0, f0 = d.get("validator") or {}, d.get("ftso") or {}
    if v0 or f0:
        cur = DelegSnap(ts=now, epoch=epoch, val_delegated=float(v0.get("delegated") or 0.0),
                        val_dels=int(v0.get("delegators") or 0), ftso_vp=float(f0.get("vote_power") or 0.0))
        d["deltas"] = compute_deltas(
            load_snaps(s.observe_deleg_history_file, now=now), now=now, epoch=epoch, current=cur
        )
    if json_out:
        print(json.dumps(d, indent=2))
    else:
        console.print(f"[bold]delegation — {s.network} RE{epoch}[/]")
        v = d.get("validator")
        if v:
            console.print(f"  validator : {v['total']:,.0f} FLR total = {v['self_bond']:,.0f} self-bond "
                          f"+ {v['delegated']:,.0f} delegated by {v['delegators']} · {v['fee_pct']:g}% fee "
                          f"· uptime {v['uptime']}%")
        else:
            console.print("  validator : · (none on this net)")
        f = d.get("ftso")
        console.print(f"  FTSO      : {f['vote_power']:,.0f} WFLR vote power" if f else "  FTSO      : · (unavailable)")
        dl = d.get("deltas") or {}
        for hz, lbl in (("h24", "Δ 24h "), ("epoch", "Δ epoch")):
            vd = (dl.get("val_delegated") or {}).get(hz)
            fv = (dl.get("ftso_vp") or {}).get(hz)
            if vd is not None or fv is not None:
                vc = (dl.get("val_dels") or {}).get(hz)
                console.print(f"  {lbl}   : validator {vd:+,.0f} FLR ({vc:+d} dels) · FTSO {fv:+,.0f} WFLR")
    raise typer.Exit(0)


if __name__ == "__main__":
    app()
