"""Network table + keyless settings. No private keys, by construction.

Addresses and RPC/data URLs are ported verbatim from the upstream
`ftso-fee-claimer/src/configs/networks.ts` + `reward-data.ts`. Chain ids match
fwd's routing rail (Flare=14, Songbird=19, Coston2=114).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Network = Literal["flare", "songbird", "coston2"]

ZERO_BYTES32 = "0x" + "00" * 32

# The fwd HTTP/ABI contract version clif is built against — the single source for
# the compat tuple (mirrors docs/fwd-contract.md's "verified against fwd vX").
FWD_CONTRACT_EXPECTED = "v1.1.0a69"


@dataclass(frozen=True)
class NetworkConfig:
    name: Network
    chain_id: int
    reward_manager: str
    flare_systems_manager: str
    claim_setup_manager: str
    entity_manager: str
    # VoterRegistry — per-voter normalised signing weight + the epoch weight sums
    # (reward-signing progress). "" = not configured (e.g. coston2 testnet).
    voter_registry: str
    default_rpc: str
    # `{epoch}` is substituted with the reward epoch id.
    reward_data_url_template: str


_NETWORKS: dict[str, NetworkConfig] = {
    "flare": NetworkConfig(
        name="flare",
        chain_id=14,
        reward_manager="0xC8f55c5aA2C752eE285Bd872855C749f4ee6239B",
        flare_systems_manager="0x89e50DC0380e597ecE79c8494bAAFD84537AD0D4",
        claim_setup_manager="0xD56c0Ea37B848939B59e6F5Cda119b3fA473b5eB",
        entity_manager="0x134b3311C6BdeD895556807a30C7f047D99DfdC2",
        voter_registry="0x2580101692366e2f331e891180d9ffdF861Fce83",
        default_rpc="https://flare-api.flare.network/ext/bc/C/rpc",
        reward_data_url_template=(
            "https://raw.githubusercontent.com/flare-foundation/fsp-rewards/"
            "refs/heads/main/flare/{epoch}/reward-distribution-data-tuples.json"
        ),
    ),
    "songbird": NetworkConfig(
        name="songbird",
        chain_id=19,
        reward_manager="0xE26AD68b17224951b5740F33926Cc438764eB9a7",
        flare_systems_manager="0x421c69E22f48e14Fc2d2Ee3812c59bfb81c38516",
        claim_setup_manager="0xDD138B38d87b0F95F6c3e13e78FFDF2588F1732d",
        entity_manager="0x46C417D0760198E94fee455CE0e223262a3D0049",
        voter_registry="0x31B9EC65C731c7D973a33Ef3FC83B653f540dC8D",
        default_rpc="https://songbird-api.flare.network/ext/bc/C/rpc",
        reward_data_url_template=(
            "https://raw.githubusercontent.com/flare-foundation/fsp-rewards/"
            "refs/heads/main/songbird/{epoch}/reward-distribution-data-tuples.json"
        ),
    ),
    "coston2": NetworkConfig(
        name="coston2",
        chain_id=114,
        reward_manager="0xB4f43E342c5c77e6fe060c0481Fe313Ff2503454",
        flare_systems_manager="0xbC1F76CEB521Eb5484b8943B5462D08ea96617A1",
        claim_setup_manager="",  # not needed for testnet onboarding; verify before use
        entity_manager="",  # not yet known
        voter_registry="",  # not yet known on coston2; signing-progress degrades gracefully
        default_rpc="https://coston2-api.flare.network/ext/bc/C/rpc",
        reward_data_url_template=(
            "https://gitlab.com/timivesel/ftsov2-testnet-rewards/-/raw/main/"
            "rewards-data/coston2/{epoch}/reward-distribution-data-tuples.json"
        ),
    ),
}


class KeylessViolation(RuntimeError):
    """Raised when a *PRIVATE_KEY* name is present — clif holds no keys."""


def _env_file_offenders(env_file: str | os.PathLike[str] | None) -> list[str]:
    """`*PRIVATE_KEY*` key *names* declared in the resolved .env source file.

    Pydantic silently ignores unknown .env keys, so a `.env` containing
    ``CLAIM_EXECUTOR_PRIVATE_KEY=…`` would let clif start green while the
    Phase-8b headline ("the `.env PRIVATE_KEY=` line is gone") is false. The
    file is parsed for key names only — values are never read into the
    environment. Missing/None file → no offenders (clean env+file passes).
    """
    if env_file is None:
        return []
    path = Path(env_file)
    if not path.is_file():
        return []
    offenders: list[str] = []
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        name = s.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if "PRIVATE_KEY" in name.upper():
            offenders.append(name)
    return offenders


def assert_keyless(env_file: str | os.PathLike[str] | None = ".env") -> None:
    """fwd Core invariant #7, operationalised inside clif.

    clif refuses to run if any ``*PRIVATE_KEY*`` name is present **either** in
    the live environment **or** in the configured ``.env`` source file (the
    latter is what pydantic would otherwise ignore). This *strengthens*
    keylessness — clif still holds zero keys; a clean env+file passes.
    """
    env_off = [k for k in os.environ if "PRIVATE_KEY" in k.upper()]
    file_off = _env_file_offenders(env_file)
    if env_off or file_off:
        parts = []
        if env_off:
            parts.append(f"environment: {', '.join(sorted(env_off))}")
        if file_off:
            parts.append(f"{env_file} file: {', '.join(file_off)}")
        raise KeylessViolation(
            "clif holds no private keys (fwd Core invariant #7). Remove the "
            f"offending *PRIVATE_KEY* name(s) — {'; '.join(parts)}"
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    network: Network = "flare"

    flare_rpc: str | None = None
    songbird_rpc: str | None = None
    coston2_rpc: str | None = None

    # Optional separate RPC for eth_getLogs scans (reward-signing progress only).
    # The public Flare/Songbird RPCs cap eth_getLogs at ~30 blocks/request, which
    # makes a full signing-window scan infeasible; point these at a full/archive
    # node for complete coverage. Unset ⇒ fall back to the main rpc_url
    # (best-effort partial scan).
    flare_logs_rpc: str | None = None
    songbird_logs_rpc: str | None = None
    coston2_logs_rpc: str | None = None

    identity_address: str | None = None
    signing_policy_address: str | None = None
    claim_recipient_address: str | None = None
    wrap_rewards: bool = False

    fwd_endpoint: str = "http://fwd:8080"
    fwd_wallet_name: str | None = None
    fwd_caller_token: str | None = None

    # Operator-controlled idempotency retry discriminator (production money
    # path). Default None ⇒ the key is byte-identical to the legacy
    # deterministic key, so a network retry / crash-rerun of the SAME logical
    # attempt still collides on one key and fwd dedups (no double-claim). The
    # operator bumps this ONLY for a deliberate logical re-attempt after an
    # on-chain failure (fwd replay is status-blind by design — fwd D14): a new
    # value yields a fresh idempotency key. Never auto-randomised by clif.
    idempotency_retry: str | None = None

    # Automation (clif auto). Reward epochs are ~3.5 days; the rewardsHash
    # flip happens hours after epoch close, so a ~15 min keyless poll is
    # ample. stale_after = how long an epoch may stay claimable-but-unclaimed
    # before clif goes degraded (FTSO rewards eventually expire — silence is
    # the danger). terminal_cooldown = after a terminal fwd error for an
    # epoch, don't re-submit (and spam fwd denials) for this long, but stay
    # degraded and loud.
    clif_state_dir: str = ".clif-state"
    poll_interval_sec: int = 900
    stale_after_sec: int = 86_400
    terminal_cooldown_sec: int = 3_600

    # FSP signing-tool (keyless — Leg-1 is fwd /v1/sign-fsp-message, Leg-2 is
    # fwd /v1/sign-transaction to FlareSystemsManager). Distinct caller tokens and
    # wallet names from the claim path (D14: two different fwd wallets/callers).
    #
    # fwd cross-domain policy_path rule: fwd's policy loader forbids the SAME
    # policy_path key appearing in both `permissions` and `fsp_permissions`
    # (cross-domain key reuse = fail-fast boot). One caller → one policy_path
    # → one block. So one caller authorizes EITHER /v1/sign-fsp-message (Leg-1,
    # fsp_permissions) OR /v1/sign-transaction (Leg-2, permissions) — never both.
    fsp_sign_caller_token: str | None = None  # Leg-1: /v1/sign-fsp-message (fsp_permissions)
    fsp_submit_caller_token: str | None = None  # Leg-2: /v1/sign-transaction (permissions)
    fsp_auto_enabled: bool = False
    fsp_signing_wallet_name: str | None = None
    fsp_sender_wallet_name: str | None = None
    fsp_submit_gas: int = 500_000
    fsp_idempotency_retry: str | None = None
    fsp_poll_interval_sec: int = 900
    fsp_stale_after_sec: int = 86_400
    fsp_terminal_cooldown_sec: int = 3_600

    # Epoch-anchored sign→claim state machine (`clif epoch`). One daemon per
    # network drives each reward epoch through its phases instead of the two
    # always-on 15-min pollers. Signing stays behind fsp_auto_enabled (the D15
    # hard-off gate); the uptime phase is additionally gated OFF by default.
    # initial_delay = wait this long after epoch end before the first
    # reward-publication check (reward calc takes time); poll_interval = the
    # active-window cadence.
    uptime_auto_enabled: bool = False
    epoch_reward_initial_delay_sec: int = 3_600  # 1h
    epoch_poll_interval_sec: int = 1_800  # 30m
    epoch_stale_after_sec: int = 86_400  # 24h: an active epoch stuck this long ⇒ degraded
    epoch_terminal_cooldown_sec: int = 3_600

    @property
    def net(self) -> NetworkConfig:
        return _NETWORKS[self.network]

    @property
    def rpc_url(self) -> str:
        override = getattr(self, f"{self.network}_rpc", None)
        return override or self.net.default_rpc

    @property
    def logs_rpc(self) -> str:
        """RPC for eth_getLogs scans (reward-signing progress). Prefer
        <NET>_LOGS_RPC (a full/archive node — the public RPC caps getLogs at ~30
        blocks/request); else fall back to the main rpc_url (partial)."""
        override = getattr(self, f"{self.network}_logs_rpc", None)
        return override or self.rpc_url

    def reward_data_url(self, epoch: int) -> str:
        return self.net.reward_data_url_template.format(epoch=epoch)

    def reward_distribution_url(self, epoch: int) -> str:
        return self.net.reward_data_url_template.format(epoch=epoch).replace(
            "reward-distribution-data-tuples.json", "reward-distribution-data.json"
        )

    @property
    def status_file(self) -> Path:
        return Path(self.clif_state_dir) / f"auto-status-{self.network}.json"

    @property
    def fsp_status_file(self) -> Path:
        return Path(self.clif_state_dir) / f"fsp-auto-status-{self.network}.json"

    @property
    def epoch_status_file(self) -> Path:
        return Path(self.clif_state_dir) / f"epoch-status-{self.network}.json"


# --- fwd capability model (ADR-0001 §3) ----------------------------------------
#
# clif's per-network fwd capabilities. Each is one immutable, consumer-namespaced
# capability_id = "claim/<network>/<role>" — the join key across the
# request → grant → handoff → import → reconcile lifecycle. A capability describes
# the AUTHORIZATION clif requests (endpoint, contract, method, pinned args); the
# caller TOKEN is a secret clif reads from its env var and is NEVER emitted here.
#
# Consumer identity is `claim` (ADR-0004): the reward-harvester. The fsp-sign / fsp-submit
# roles ride here TRANSITIONALLY — until the `fsp` consumer (keyless flare-system-client)
# lands, then they migrate to `fsp/<net>/…` and are revoked here. Until then they keep
# live FSP signing working (dropping them now would halt it; `fsp` does not exist yet).

# Suggested per-role rate ceilings — REQUEST ONLY (fwd policy is authoritative).
_SUGGESTED_RATE = {
    "ftso-reward": "8/day",  # claims fire ~once per ~3.5-day reward epoch per type
    "fsp-sign": "16/day",
    "fsp-submit": "16/day",
}

_CLAIM_METHOD = "claim(address,address,uint24,bool,(bytes32[],(uint24,bytes20,uint120,uint8))[])"


@dataclass(frozen=True)
class Capability:
    capability_id: str
    role: str
    endpoint: str
    caller_token_env: str  # env var name clif reads the granted token from (NOT the value)
    wallet_env: str  # env var name for the fwd wallet name
    wallet_name: str | None  # the configured fwd wallet NAME (a name, not a key); None if unset
    contract: str | None  # contract address, or None for a detached signature
    contract_name: str | None
    method: str | None
    value_wei: str | None  # "0" for tx capabilities; None for /v1/sign-fsp-message
    recipient_pinned: str | None
    suggested_rate: str | None


def capabilities(settings: Settings) -> list[Capability]:
    """clif's three fwd capabilities for the configured network (ADR-0001 §3).

    Config-derived and deterministic (no RPC). Carries NAMES only — never a
    caller-token value. The set is what clif *requests*; grant/import is separate.
    """
    net = settings.net
    n = settings.network
    return [
        Capability(
            capability_id=f"claim/{n}/ftso-reward",
            role="ftso-reward",
            endpoint="/v1/sign-transaction",
            caller_token_env="FWD_CALLER_TOKEN",
            wallet_env="FWD_WALLET_NAME",
            wallet_name=settings.fwd_wallet_name,
            contract=net.reward_manager,
            contract_name="RewardManager",
            method=_CLAIM_METHOD,
            value_wei="0",
            recipient_pinned=settings.claim_recipient_address,
            suggested_rate=_SUGGESTED_RATE["ftso-reward"],
        ),
        Capability(
            capability_id=f"claim/{n}/fsp-sign",
            role="fsp-sign",
            endpoint="/v1/sign-fsp-message",
            caller_token_env="FSP_SIGN_CALLER_TOKEN",
            wallet_env="FSP_SIGNING_WALLET_NAME",
            wallet_name=settings.fsp_signing_wallet_name,
            contract=None,
            contract_name=None,
            method="signUptimeVote / signRewards (FSP messages: UPTIME, REWARD_DISTRIBUTION)",
            value_wei=None,
            recipient_pinned=None,
            suggested_rate=_SUGGESTED_RATE["fsp-sign"],
        ),
        Capability(
            capability_id=f"claim/{n}/fsp-submit",
            role="fsp-submit",
            endpoint="/v1/sign-transaction",
            caller_token_env="FSP_SUBMIT_CALLER_TOKEN",
            wallet_env="FSP_SENDER_WALLET_NAME",
            wallet_name=settings.fsp_sender_wallet_name,
            contract=net.flare_systems_manager,
            contract_name="FlareSystemsManager",
            method="signUptimeVote / signRewards",
            value_wei="0",
            recipient_pinned=None,
            suggested_rate=_SUGGESTED_RATE["fsp-submit"],
        ),
    ]


def config_env_allowlist(settings: Settings) -> frozenset[str]:
    """Env-var NAMES the v2 handoff bundle's ``config`` section may set.

    The v2 bundle is the COMPLETE onboard handoff (ADR-0003 / consumer-contract-v1
    §4): the entire ``.env.<network>`` is sourced from it, so the bundle MUST be
    able to set clif's config env-vars — but ONLY clif's own (the bundle must not
    inject an arbitrary env var). The allowlist is **derived from clif's own
    ``Settings`` fields** (uppercased = the env-var names pydantic reads), so it
    stays in sync automatically when a config knob is added.

    Two exclusions keep the membrane intact:
    - the per-capability **caller-token** env-vars (``FWD_CALLER_TOKEN``,
      ``FSP_SIGN_CALLER_TOKEN``, ``FSP_SUBMIT_CALLER_TOKEN``) — those secrets
      travel ONLY via the per-capability token path, which guards them and never
      logs the value; the ``config`` section carries non-secret config;
    - any ``*PRIVATE_KEY*`` name (clif holds no keys — Core invariant #7).

    Wallet-env NAMES (``FWD_WALLET_NAME`` etc.) stay IN the allowlist — they are
    names, not keys; the per-capability path is their canonical writer, but the
    allowlist accepting them keeps the two writers consistent.
    """
    token_envs = {c.caller_token_env for c in capabilities(settings)}
    return frozenset(
        name.upper()
        for name in Settings.model_fields
        if name.upper() not in token_envs and "PRIVATE_KEY" not in name.upper()
    )


def load_settings() -> Settings:
    """Assert keyless (env **and** the .env source file) first, then load."""
    env_file = Settings.model_config.get("env_file", ".env")
    assert_keyless(env_file)
    return Settings()
