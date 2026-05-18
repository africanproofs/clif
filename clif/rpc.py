"""Keyless JSON-RPC client (httpx, synchronous).

Only read paths: `eth_call` for the FtsoRewardManager / FlareSystemsManager
view functions used during discovery, plus `eth_getTransactionReceipt` for
on-chain verification. No signing, no key, no `eth_sendRawTransaction` — that
is fwd's job. Synchronous by design: the discovery path is short and
sequential; an event loop would add plumbing without benefit (Behavioural
guideline 2).
"""

from __future__ import annotations

import httpx
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from clif.calldata import selector


class RpcError(RuntimeError):
    pass


class RpcClient:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self._url = url
        self._client = httpx.Client(timeout=timeout)
        self._id = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RpcClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _call(self, method: str, params: list) -> object:
        self._id += 1
        try:
            resp = self._client.post(
                self._url,
                json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RpcError(f"{method} transport failure: {exc}") from exc
        if "error" in body:
            raise RpcError(f"{method} rpc error: {body['error']}")
        return body["result"]

    def eth_call(self, to: str, data: str) -> bytes:
        result = self._call("eth_call", [{"to": to, "data": data}, "latest"])
        return bytes.fromhex(str(result)[2:])

    def get_transaction_receipt(self, tx_hash: str) -> dict | None:
        result = self._call("eth_getTransactionReceipt", [tx_hash])
        return result if result else None  # null until mined

    # ---- typed view helpers (keyless) ----

    def rewards_hash(self, flare_systems_manager: str, epoch_id: int) -> str:
        data = "0x" + selector("rewardsHash(uint256)").hex() + abi_encode(
            ["uint256"], [epoch_id]
        ).hex()
        (out,) = abi_decode(["bytes32"], self.eth_call(flare_systems_manager, data))
        return "0x" + out.hex()

    def next_claimable_reward_epoch_id(self, reward_manager: str, owner: str) -> int:
        data = "0x" + selector("getNextClaimableRewardEpochId(address)").hex() + abi_encode(
            ["address"], [owner]
        ).hex()
        (out,) = abi_decode(["uint256"], self.eth_call(reward_manager, data))
        return int(out)

    def reward_epoch_id_range(self, reward_manager: str) -> tuple[int, int]:
        data = "0x" + selector("getRewardEpochIdsWithClaimableRewards()").hex()
        start, end = abi_decode(["uint24", "uint24"], self.eth_call(reward_manager, data))
        return int(start), int(end)
