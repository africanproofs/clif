"""HTTP transport for the fwd signing daemon (synchronous httpx).

Contract verified against `fwd/src/fwd/api/sign.py:38-161` this session:

  POST /v1/sign-and-send  -> 200 {tx_id,hash,nonce}
      errors {error,message}: 400 bad-req / bad-idempotency / chain_not_allowed
      401 unauthorized | 403 policy_denied | 404 wallet_not_found
      502 rpc_unreachable | 503 vault_unavailable
  GET  /v1/transactions/{tx_id}  -> {status,hashes,confirmed_at} (caller-gated)
  GET  /healthz                  -> {master,rpc,fwd}

Retry policy (prompt §"Verified fwd integration contract"):
  401/403/404/503 and 400 are TERMINAL — never retry.
  502 (rpc_unreachable) is RETRYABLE.

This module is pure transport. It does not build claim calldata and does not
decide what to sign — that orchestration is `claimer.py` (Phase 8b step 4,
post operator gate).
"""

from __future__ import annotations

import hashlib
import time

import httpx

from clif.models import Health, SignAndSendResponse, TxStatus

_TERMINAL_STATUSES = {400, 401, 403, 404, 503}
_RETRYABLE_STATUSES = {502}


class FwdError(RuntimeError):
    def __init__(self, status: int, error_code: str, message: str) -> None:
        super().__init__(f"fwd {status} {error_code}: {message}")
        self.status = status
        self.error_code = error_code
        self.message = message


class FwdTerminalError(FwdError):
    """Do not retry (auth/policy/wallet/bad-request/sealed-master)."""


class FwdRetryableError(FwdError):
    """May retry after backoff (RPC unreachable)."""


def make_idempotency_key(
    network: str, claim_type: int, beneficiary: str, last_epoch_id: int
) -> str:
    """Deterministic per logical claim.

    A retry of the same (network, claim type, beneficiary, last epoch) claim
    replays to the same fwd `tx_id` instead of broadcasting twice. Stable
    across processes; ≤128 chars (fwd's limit).
    """
    raw = f"clif:{network}:{claim_type}:{beneficiary.lower()}:{last_epoch_id}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"clif-{network}-{claim_type}-{last_epoch_id}-{digest[:16]}"


class FwdClient:
    def __init__(self, endpoint: str, caller_token: str | None, timeout: float = 60.0) -> None:
        self._base = endpoint.rstrip("/")
        self._token = caller_token
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FwdClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    @staticmethod
    def _raise_for_error(resp: httpx.Response) -> None:
        if resp.status_code == 200:
            return
        try:
            body = resp.json()
            err = str(body.get("error", "unknown"))
            msg = str(body.get("message", resp.text))
        except ValueError:
            err, msg = "unknown", resp.text
        if resp.status_code in _RETRYABLE_STATUSES:
            raise FwdRetryableError(resp.status_code, err, msg)
        if resp.status_code in _TERMINAL_STATUSES:
            raise FwdTerminalError(resp.status_code, err, msg)
        # Unmapped status: treat as terminal (fail closed).
        raise FwdTerminalError(resp.status_code, err, msg)

    def health(self) -> Health:
        resp = self._client.get(f"{self._base}/healthz")
        resp.raise_for_status()
        return Health.model_validate(resp.json())

    def sign_and_send(
        self,
        wallet: str,
        chain: int,
        to: str,
        data: str = "0x",
        value_wei: str = "0",
        gas: int | None = None,
        idempotency_key: str | None = None,
    ) -> SignAndSendResponse:
        payload: dict = {
            "wallet": wallet,
            "chain": chain,
            "to": to,
            "value_wei": value_wei,
            "data": data,
        }
        if gas is not None:
            payload["gas"] = gas
        headers = dict(self._auth)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        resp = self._client.post(
            f"{self._base}/v1/sign-and-send", json=payload, headers=headers
        )
        self._raise_for_error(resp)
        return SignAndSendResponse.model_validate(resp.json())

    def get_transaction(self, tx_id: str) -> TxStatus:
        resp = self._client.get(
            f"{self._base}/v1/transactions/{tx_id}", headers=self._auth
        )
        self._raise_for_error(resp)
        return TxStatus.model_validate(resp.json())

    def wait_until_mined(
        self, tx_id: str, timeout: float = 600.0, poll: float = 5.0
    ) -> TxStatus:
        deadline = time.monotonic() + timeout
        while True:
            st = self.get_transaction(tx_id)
            if st.status in ("mined", "failed", "replaced", "dropped"):
                return st
            if time.monotonic() >= deadline:
                raise TimeoutError(f"tx {tx_id} not terminal after {timeout}s (status={st.status})")
            time.sleep(poll)
