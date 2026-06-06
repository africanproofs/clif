"""Fetch + validate fsp-rewards distribution tuples.

Port of `ftso-fee-claimer/src/reward-data.ts`: prefer a local cache at
``rewards-data/{network}/{epoch}/reward-distribution-data-tuples.json``
(operator-supplied, e.g. for offline/air-gapped runs); otherwise fetch from
the network's published URL. Returns ``None`` on any failure (mirrors the TS
behaviour: a missing/invalid epoch file is skipped, not fatal).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import httpx
from pydantic import ValidationError

from clif.config import Settings
from clif.models import RewardDistributionData, RewardsData

_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds; doubles each retry: 1s, 2s, 4s

log = logging.getLogger("clif")


def _local_path(network: str, epoch: int) -> Path | None:
    p = Path.cwd() / "rewards-data" / network / str(epoch) / "reward-distribution-data-tuples.json"
    return p if p.exists() else None


def _fetch_with_retry(url: str, timeout: float = 30.0) -> httpx.Response | None:
    """Fetch *url* with up to _MAX_RETRIES attempts and exponential backoff.

    Returns the successful Response, or None if all attempts fail.
    404 → return None immediately (file doesn't exist, no point retrying).
    Any other exception → retry up to _MAX_RETRIES times, then return None.
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            # 404 already handled above; re-raise for other HTTP errors
            if exc.response.status_code == 404:
                return None
            err = exc
        except Exception as exc:  # noqa: BLE001 — timeout, connection error, etc.
            err = exc
        log.warning("reward_data: fetch attempt %d/%d failed: %s", attempt, _MAX_RETRIES, err)
        if attempt < _MAX_RETRIES:
            time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
    log.error("reward_data: all %d fetch attempts failed for %s", _MAX_RETRIES, url)
    return None


def get_reward_distribution_data(settings: Settings, epoch: int) -> RewardDistributionData | None:
    """Fetch reward-distribution-data.json (not the tuples variant).

    Prefers a local cache at rewards-data/{network}/{epoch}/reward-distribution-data.json,
    otherwise fetches from the network URL (reward_distribution_url). Returns None on
    any failure — the caller treats missing data as a terminal guard (never sign
    unverified rewardsHash).
    """
    try:
        local = (
            Path.cwd()
            / "rewards-data"
            / settings.network
            / str(epoch)
            / "reward-distribution-data.json"
        )
        if local.exists():
            return RewardDistributionData.model_validate_json(local.read_text())
        url = settings.reward_distribution_url(epoch)
        resp = _fetch_with_retry(url)
        if resp is None:
            return None
        return RewardDistributionData.model_validate(resp.json())
    except (ValidationError, ValueError, OSError) as exc:
        print(f"Error fetching reward distribution data for epoch {epoch}: {exc}", file=sys.stderr)
        return None


def get_reward_calculation_data(settings: Settings, epoch: int) -> RewardsData | None:
    try:
        local = _local_path(settings.network, epoch)
        if local is not None:
            return RewardsData.model_validate_json(local.read_text())
        url = settings.reward_data_url(epoch)
        resp = _fetch_with_retry(url)
        if resp is None:
            return None
        return RewardsData.model_validate(resp.json())
    except (ValidationError, ValueError, OSError) as exc:
        print(f"Error fetching rewards data for epoch {epoch}: {exc}", file=sys.stderr)
        return None
