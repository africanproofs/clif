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


@dataclass(frozen=True)
class NetworkConfig:
    name: Network
    chain_id: int
    reward_manager: str
    flare_systems_manager: str
    default_rpc: str
    # `{epoch}` is substituted with the reward epoch id.
    reward_data_url_template: str


_NETWORKS: dict[str, NetworkConfig] = {
    "flare": NetworkConfig(
        name="flare",
        chain_id=14,
        reward_manager="0xC8f55c5aA2C752eE285Bd872855C749f4ee6239B",
        flare_systems_manager="0x89e50DC0380e597ecE79c8494bAAFD84537AD0D4",
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
            name = name[len("export "):].strip()
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

    identity_address: str | None = None
    signing_policy_address: str | None = None
    claim_recipient_address: str | None = None
    wrap_rewards: bool = True

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
    # fwd /v1/sign-and-send to FlareSystemsManager). Distinct caller tokens and
    # wallet names from the claim path (D14: two different fwd wallets/callers).
    #
    # fwd cross-domain policy_path rule: fwd's policy loader forbids the SAME
    # policy_path key appearing in both `permissions` and `fsp_permissions`
    # (cross-domain key reuse = fail-fast boot). One caller → one policy_path
    # → one block. So one caller authorizes EITHER /v1/sign-fsp-message (Leg-1,
    # fsp_permissions) OR /v1/sign-and-send (Leg-2, permissions) — never both.
    # tx poll /v1/transactions/{id} is per-caller-scoped → it MUST use the
    # Leg-2 (submit) caller.
    fsp_sign_caller_token: str | None = None   # Leg-1: /v1/sign-fsp-message (fsp_permissions)
    fsp_submit_caller_token: str | None = None  # Leg-2 + tx poll: /v1/sign-and-send (permissions)
    fsp_auto_enabled: bool = False
    fsp_signing_wallet_name: str | None = None
    fsp_sender_wallet_name: str | None = None
    fsp_submit_gas: int = 500_000
    fsp_idempotency_retry: str | None = None
    fsp_poll_interval_sec: int = 900
    fsp_stale_after_sec: int = 86_400
    fsp_terminal_cooldown_sec: int = 3_600

    @property
    def net(self) -> NetworkConfig:
        return _NETWORKS[self.network]

    @property
    def rpc_url(self) -> str:
        override = getattr(self, f"{self.network}_rpc", None)
        return override or self.net.default_rpc

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


def load_settings() -> Settings:
    """Assert keyless (env **and** the .env source file) first, then load."""
    env_file = Settings.model_config.get("env_file", ".env")
    assert_keyless(env_file)
    return Settings()
