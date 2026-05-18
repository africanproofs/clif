"""Network table + keyless settings. No private keys, by construction.

Addresses and RPC/data URLs are ported verbatim from the upstream
`ftso-fee-claimer/src/configs/networks.ts` + `reward-data.ts`. Chain ids match
fwd's routing rail (Flare=14, Songbird=19, Coston2=114).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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
    """Raised when a *PRIVATE_KEY* variable is present — clif holds no keys."""


def assert_keyless() -> None:
    """fwd Core invariant #7, operationalised inside clif.

    If any environment variable name contains ``PRIVATE_KEY`` (e.g. a
    leftover ``CLAIM_EXECUTOR_PRIVATE_KEY`` from the TS tool), clif refuses
    to run. clif must never custody a signing key.
    """
    offenders = [k for k in os.environ if "PRIVATE_KEY" in k.upper()]
    if offenders:
        raise KeylessViolation(
            "clif holds no private keys (fwd Core invariant #7). "
            f"Remove these environment variables: {', '.join(sorted(offenders))}"
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

    @property
    def net(self) -> NetworkConfig:
        return _NETWORKS[self.network]

    @property
    def rpc_url(self) -> str:
        override = getattr(self, f"{self.network}_rpc", None)
        return override or self.net.default_rpc

    def reward_data_url(self, epoch: int) -> str:
        return self.net.reward_data_url_template.format(epoch=epoch)


def load_settings() -> Settings:
    """Assert keyless first, then load .env-backed settings."""
    assert_keyless()
    return Settings()
