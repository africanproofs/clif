"""Fetch + validate fsp-rewards distribution tuples.

Port of `ftso-fee-claimer/src/reward-data.ts`: prefer a local cache at
``rewards-data/{network}/{epoch}/reward-distribution-data-tuples.json``
(operator-supplied, e.g. for offline/air-gapped runs); otherwise fetch from
the network's published URL. Returns ``None`` on any failure (mirrors the TS
behaviour: a missing/invalid epoch file is skipped, not fatal).
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
from pydantic import ValidationError

from clif.config import Settings
from clif.models import RewardDistributionData, RewardsData


def _local_path(network: str, epoch: int) -> Path | None:
    p = Path.cwd() / "rewards-data" / network / str(epoch) / "reward-distribution-data-tuples.json"
    return p if p.exists() else None


def get_reward_distribution_data(settings: Settings, epoch: int) -> RewardDistributionData | None:
    """Fetch reward-distribution-data.json (not the tuples variant).

    Prefers a local cache at rewards-data/{network}/{epoch}/reward-distribution-data.json,
    otherwise fetches from the network URL (reward_distribution_url). Returns None on
    any failure — the caller treats missing data as a terminal guard (never sign
    unverified rewardsHash).
    """
    try:
        local = (
            Path.cwd() / "rewards-data" / settings.network / str(epoch) / "reward-distribution-data.json"
        )
        if local.exists():
            return RewardDistributionData.model_validate_json(local.read_text())
        url = settings.reward_distribution_url(epoch)
        resp = httpx.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        return RewardDistributionData.model_validate(resp.json())
    except (httpx.HTTPError, ValidationError, ValueError, OSError) as exc:
        print(f"Error fetching reward distribution data for epoch {epoch}: {exc}", file=sys.stderr)
        return None


def get_reward_calculation_data(settings: Settings, epoch: int) -> RewardsData | None:
    try:
        local = _local_path(settings.network, epoch)
        if local is not None:
            return RewardsData.model_validate_json(local.read_text())
        url = settings.reward_data_url(epoch)
        resp = httpx.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        return RewardsData.model_validate(resp.json())
    except (httpx.HTTPError, ValidationError, ValueError, OSError) as exc:
        print(f"Error fetching rewards data for epoch {epoch}: {exc}", file=sys.stderr)
        return None
