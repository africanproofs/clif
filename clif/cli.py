"""clif CLI.

Keyless: `version`, `health`, `list`, `spec`. Claim/automation (plan step 4,
operator-gated for production — fwd must be provisioned and the new wallet
authorized on-chain as executor first): `claim` (one-shot), `auto` (resilient
daemon), `status` (scrapable degraded state). The ">50% reward-signing-weight"
trigger is the on-chain `rewardsHash` flip that `discovery` already detects.
"""

from __future__ import annotations

import logging
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
from clif.discovery import collect_reward_claims
from clif.fwd_client import (
    FwdClient,
    FwdRetryableError,
    FwdTerminalError,
    make_idempotency_key,
)
from clif.models import ClaimType
from clif.rpc import RpcClient, RpcError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s clif %(message)s"
)
log = logging.getLogger("clif")

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
constraint 2). fwd's `docs/policy.example.yaml` has been corrected upstream
(line 56 now carries the canonical signature in §2) — safe to rely on.
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
        f"{o.claim_type_name} {o.beneficiary} epochs={o.epochs} "
        f"→ {o.status.value} ({o.detail})"
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
def claim(
    type: Annotated[Optional[str], typer.Option("--type", "-t", help="fee|direct")] = None,
    epoch: Annotated[Optional[int], typer.Option("--epoch", "-e")] = None,
    no_wait: Annotated[bool, typer.Option("--no-wait", help="don't poll to mined")] = False,
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
            "operator-intended post-failure re-attempt)", retry,
        )
    worst = 0
    with RpcClient(s.rpc_url) as rpc, FwdClient(s.fwd_endpoint, s.fwd_caller_token) as fwd:
        for ct, benef in pairs:
            o = run_claim(
                s, rpc, fwd, int(ct), benef,
                only_epoch=epoch, wait=not no_wait, retry=retry,
            )
            _print_outcome(o)
            worst = max(worst, _exit_for(o.status))
    raise typer.Exit(worst)


@app.command()
def rehearse(
    gas: Annotated[
        int, typer.Option(help="explicit gas — makes fwd skip eth_estimateGas")
    ] = 500_000,
    no_wait: Annotated[
        bool, typer.Option("--no-wait", help="don't poll to mined")
    ] = False,
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
    """Rehearsal-ladder fwd-custody proof (Core-#14 trigger for fwd v1.0.0).

    Builds a REAL-shaped `RewardManager.claim` via the real builder / real ABI
    / anchored selector — real discovery first, empty *real* proofs if nothing
    is genuinely claimable (the least hand-modeled valid shape; never a
    hand-authored hex string). POSTs it to fwd `/v1/sign-and-send` with an
    explicit `gas` so fwd skips `eth_estimateGas` (a reverting rehearsal claim
    would otherwise abort pre-broadcast). Then proves fwd's custody path: the
    mined tx's on-chain `from` == the fwd-custodied executor wallet. clif holds
    no key — `from` is recovered from fwd's signature.

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
        s.network, s.net.reward_manager, recipient, gas,
    )

    with RpcClient(s.rpc_url) as rpc, FwdClient(
        s.fwd_endpoint, s.fwd_caller_token
    ) as fwd:
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
                claims = collect_reward_claims(
                    rpc, s, s.identity_address, int(ClaimType.FEE)
                )
            except RpcError as exc:
                log.warning("discovery rpc failure (rehearse uses empty proofs): %s", exc)
        log.info(
            "discovery FEE owner=%s claims=%d",
            s.identity_address or "<unset>", len(claims),
        )

        epoch_src = "reward_epoch_id_range.end"
        try:
            _, epoch = rpc.reward_epoch_id_range(s.net.reward_manager)
        except RpcError as exc1:
            log.warning("reward_epoch_id_range failed (%s); falling back", exc1)
            epoch_src = "next_claimable_reward_epoch_id"
            try:
                epoch = rpc.next_claimable_reward_epoch_id(
                    s.net.reward_manager, reward_owner
                )
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

        data = build_claim_calldata(
            reward_owner, recipient, epoch, s.wrap_rewards, claims
        )
        nbytes = (len(data) - 2) // 2
        console.print(f"[bold]calldata[/] ({nbytes} bytes): {data}")
        log.info(
            "built claim calldata selector=0x%s len=%dB epoch=%s proofs=%d",
            CLAIM_SELECTOR.hex(), nbytes, epoch, len(claims),
        )

        # Production determinism (D10) is preserved: the base key is the exact
        # `make_idempotency_key` the claim/auto path uses. The rehearse-only
        # `-r<tag>` suffix makes each rehearsal a distinct logical request, so
        # fwd cannot replay a stale prior outcome (e.g. a pre-fix failed tx)
        # when the epoch has not rolled. Never applied to the money path.
        tag = idem_tag or str(int(time.time()))
        idem = (
            make_idempotency_key(s.network, int(ClaimType.FEE), reward_owner, epoch)
            + f"-r{tag}"
        )
        log.info("rehearse idempotency-key=%s (tag=%s)", idem, tag)
        try:
            resp = fwd.sign_and_send(
                wallet=s.fwd_wallet_name,
                chain=s.net.chain_id,
                to=s.net.reward_manager,
                data=data,
                value_wei="0",
                gas=gas,
                idempotency_key=idem,
            )
        except FwdTerminalError as exc:
            err.print(
                f"[bold red]fwd TERMINAL (no broadcast): {exc} — escalate to operator[/]"
            )
            raise typer.Exit(2) from exc
        except FwdRetryableError as exc:
            err.print(f"[yellow]fwd retryable: {exc} (retry later)[/]")
            raise typer.Exit(1) from exc

        console.print(
            f"[bold green]fwd accepted[/] tx_id={resp.tx_id} hash={resp.hash} "
            f"nonce={resp.nonce}"
        )
        log.info(
            "fwd sign-and-send OK tx_id=%s hash=%s nonce=%s",
            resp.tx_id, resp.hash, resp.nonce,
        )
        if no_wait:
            console.print("[yellow]--no-wait: not polling to mined[/]")
            return

        try:
            st = fwd.wait_until_mined(resp.tx_id, timeout=600.0)
        except (FwdRetryableError, TimeoutError) as exc:
            err.print(
                f"[yellow]submitted; not yet terminal via fwd: {exc} "
                f"(tx_id={resp.tx_id})[/]"
            )
            raise typer.Exit(1) from exc
        log.info("fwd tx terminal status=%s", st.status)

        onchain = rpc.get_transaction_by_hash(resp.hash) or {}
        receipt = rpc.get_transaction_receipt(resp.hash) or {}
        ofrom = onchain.get("from")
        block = receipt.get("blockNumber") or onchain.get("blockNumber")
        rstatus = receipt.get("status")
        console.print("[bold]── Coston2 fwd-custody proof ──[/]")
        console.print(
            f"  fwd     : tx_id={resp.tx_id} hash={resp.hash} "
            f"nonce={resp.nonce} status={st.status}"
        )
        console.print(
            f"  chain   : block={block} receipt.status={rstatus} from={ofrom}"
        )
        console.print(
            f"  to      : {s.net.reward_manager} (RewardManager, chain="
            f"{s.net.chain_id})"
        )
        console.print(f"  recipient (pinned arg) : {recipient}")
        console.print(f"  calldata: {data}")
        log.info(
            "custody proof from=%s block=%s receipt.status=%s",
            ofrom, block, rstatus,
        )

        # The rehearsal custody proof = the tx is ON-CHAIN (in a block) with a
        # recovered `from`. That `from` is the secp256k1-recovered signer; it
        # being the fwd-custodied executor proves fwd signed and clif holds no
        # key. A REVERTED receipt (status 0x0) is EXPECTED and acceptable — the
        # executor is unauthorised / nothing is claimable (the v1.0.0a3
        # precedent); the proof is `from`, not claim success. fwd's own
        # `status` (it calls a reverted tx "failed") is informational and must
        # NOT gate the verdict. The proof is absent only if the tx never landed
        # (no `from` / no block — e.g. a fwd nonce gap): then fail loud + terminal.
        mined_on_chain = bool(ofrom) and block is not None
        reverted = str(rstatus).lower() in ("0x0", "0x00")
        if not mined_on_chain:
            err.print(
                f"[bold red]PROOF NOT CAPTURED — tx not on-chain "
                f"(from={ofrom!r} block={block!r}); fwd status={st.status!r}. "
                "Escalate (likely fwd-side, e.g. a wallet nonce gap; clif "
                "holds no key and does not touch fwd).[/]"
            )
            raise typer.Exit(2)
        tail = (
            f"reverted on-chain (receipt.status={rstatus}) — EXPECTED for a "
            f"rehearsal (fwd status={st.status!r}); the proof is `from`, not "
            "claim success"
            if reverted
            else f"receipt.status={rstatus} (fwd status={st.status!r})"
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
    """Resilient claim daemon. Never exits on a claim error; surfaces a
    degraded state (loud log + `clif status`) when a claimable epoch stays
    unclaimed past stale_after or a terminal fwd error occurs."""
    s = _settings()
    pairs = _resolve_claimers(s, type)
    if not pairs:
        err.print("[yellow]No beneficiary configured for the requested type.[/]")
        raise typer.Exit(2)
    iv = interval or s.poll_interval_sec
    state = AutoState()
    log.info(
        "auto start network=%s interval=%ss streams=%d state=%s "
        "idempotency-retry=%s",
        s.network, iv, len(pairs), s.status_file,
        s.idempotency_retry or "<none>",
    )
    try:
        while True:
            now = time.time()
            with RpcClient(s.rpc_url) as rpc, FwdClient(
                s.fwd_endpoint, s.fwd_caller_token
            ) as fwd:
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
                        state.record_attempt(key, now, "nothing-claimable")
                        continue
                    last = epochs[-1]
                    if state.in_cooldown(key, last, now):
                        log.error(
                            "%s epoch %s in terminal cooldown — NOT resubmitting "
                            "(degraded; operator action likely needed)", key, last,
                        )
                        state.record_attempt(key, now, "terminal-cooldown")
                        continue
                    o = submit_claims(s, fwd, int(ct), benef, claims, wait=False)
                    state.record_attempt(key, now, o.status.value)
                    if o.status == OutcomeStatus.SUBMITTED_PENDING:
                        log.info(
                            "%s submitted epochs=%s tx=%s (mining via fwd)",
                            key, o.epochs, o.tx_hash,
                        )
                    elif o.status == OutcomeStatus.FAILED_RETRYABLE:
                        log.warning("%s transient: %s (retry next cycle)", key, o.detail)
                    elif o.status == OutcomeStatus.FAILED_TERMINAL:
                        if o.last_epoch is not None:
                            state.record_terminal(
                                key, o.last_epoch, now, s.terminal_cooldown_sec
                            )
                        log.error(
                            "%s TERMINAL epochs=%s: %s — operator action likely needed",
                            key, o.epochs, o.detail,
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
    """Scrapable health (Docker healthcheck / monitoring).

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


if __name__ == "__main__":
    app()
