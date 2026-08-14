"""Keyless JSON-RPC client (httpx, synchronous).

Read paths (existing): `eth_call` for the FtsoRewardManager /
FlareSystemsManager view functions used during discovery, plus
`eth_getTransactionReceipt` for on-chain verification.

Write paths (new, broadcast-only — no signing, no key):
  `send_raw_transaction` — broadcasts a fwd-signed blob via
    `eth_sendRawTransaction`. Accepting a fwd-signed blob is NOT signing:
    clif never constructs or holds private keys.

Fee estimation (new, keyless reads):
  `estimate_gas` — `eth_estimateGas` with a 25% buffer.
  `suggest_fees` — `eth_feeHistory` → (max_fee_per_gas, max_priority_fee_per_gas).

Receipt polling:
  `poll_receipt` — bounded `eth_getTransactionReceipt` loop (keyless read).

Synchronous by design: the signing path is short and sequential; an event
loop would add plumbing without benefit (Behavioural guideline 2).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from clif._keccak import keccak256
from clif.calldata import selector

_CB58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _bytes20_to_cb58(b: bytes) -> str:
    """Encode 20-byte node ID as Avalanche CB58 (sha256[-4:] checksum, base58)."""
    import hashlib

    checksum = hashlib.sha256(b).digest()[-4:]
    payload = b + checksum
    n = int.from_bytes(payload, "big")
    result: list[bytes] = []
    while n > 0:
        n, r = divmod(n, 58)
        result.append(_CB58_ALPHABET[r : r + 1])
    leading = sum(1 for byte in payload if byte == 0)
    result.extend([b"1"] * leading)
    return b"".join(reversed(result)).decode()


# Gas estimation buffer: 25% over the eth_estimateGas result.
_GAS_BUFFER = 1.25
# Tip: 1 gwei (in wei) — conservative default.
_DEFAULT_TIP_WEI = 1_000_000_000
# Sanity ceiling: if computed max_fee exceeds this, cap it. Must stay at or under
# fwd's FWD_MAX_FEE_PER_GAS, which rejects `max_fee > cap`.
#
# The cap is per-network, because a chain's minimum base fee is a chain property:
# Songbird's floor is 500 gwei (raised from 1 wei on 2026-07-07), so a 300 gwei
# cap makes every Songbird broadcast unsendable. Set CLIF_MAX_FEE_PER_GAS_WEI per
# daemon; the default suits a near-zero base fee.
# FlareContractRegistry — the same immutable address on every Flare-family network
# (flare, songbird, coston2). The chain's own directory of protocol contracts.
FLARE_CONTRACT_REGISTRY = "0xaD67FE66660Fb8dFE9d6b1b4240d8650e30F6019"

_DEFAULT_MAX_FEE_CAP_WEI = 300_000_000_000  # 300 gwei
_MAX_GAS_CAP = 10_000_000  # 10M — well under fwd's FWD_MAX_GAS default (15M)


def _max_fee_cap_wei() -> int:
    """Fee ceiling, overridable per network via CLIF_MAX_FEE_PER_GAS_WEI."""
    raw = os.getenv("CLIF_MAX_FEE_PER_GAS_WEI")
    if not raw:
        return _DEFAULT_MAX_FEE_CAP_WEI
    value = int(raw)
    if value <= 0:
        raise ValueError(f"CLIF_MAX_FEE_PER_GAS_WEI must be positive, got {value}")
    return value


class RpcError(RuntimeError):
    pass


# FlareSystemsManager per-signer signing events — emitted once per signer as
# signatures land. Both have rewardEpochId / signingPolicyAddress / voter indexed
# (topics 1-3) and carry a signed-message hash + thresholdReached in `data`. The
# full keccak (NOT the 4-byte selector) is the topic0 filter.
#   RewardsSigned    data = (bytes32 rewardsHash, (uint256,uint256)[] claims, uint64 ts, bool thresholdReached)
#   UptimeVoteSigned data = (bytes32 uptimeVoteHash,                       uint64 ts, bool thresholdReached)
_REWARDS_SIGNED_SIG = (
    "RewardsSigned(uint24,address,address,bytes32,(uint256,uint256)[],uint64,bool)"
)
_UPTIME_VOTE_SIGNED_SIG = "UptimeVoteSigned(uint24,address,address,bytes32,uint64,bool)"
REWARDS_SIGNED_TOPIC0 = "0x" + keccak256(_REWARDS_SIGNED_SIG.encode()).hex()
UPTIME_VOTE_SIGNED_TOPIC0 = "0x" + keccak256(_UPTIME_VOTE_SIGNED_SIG.encode()).hex()


@dataclass(frozen=True)
class _EventSpec:
    """How to filter + decode one signing event kind."""

    topic0: str
    data_types: list[str]
    threshold_idx: int  # index of the bool thresholdReached within the decoded data tuple


# message_hash is always data field [0] for both kinds.
_EVENT_SPECS: dict[str, _EventSpec] = {
    "rewards": _EventSpec(
        REWARDS_SIGNED_TOPIC0,
        ["bytes32", "(uint256,uint256)[]", "uint64", "bool"],
        threshold_idx=3,
    ),
    "uptime": _EventSpec(
        UPTIME_VOTE_SIGNED_TOPIC0,
        ["bytes32", "uint64", "bool"],
        threshold_idx=2,
    ),
}


@dataclass(frozen=True)
class SignedLog:
    """One decoded signing-event log (addresses 0x-prefixed, lowercased).

    `message_hash` is the candidate hash this voter signed (rewardsHash for the
    rewards kind, uptimeVoteHash for the uptime kind).
    """

    signing_policy_address: str
    voter: str
    message_hash: str
    threshold_reached: bool
    timestamp: int
    block_number: int


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

    def _abi_decode(self, types: list, data: bytes) -> tuple:
        try:
            return abi_decode(types, data)
        except Exception as exc:
            raise RpcError(f"abi decode failed {types}: {exc}") from exc

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

    # ---- write path (broadcast only — no key, no signing) ----

    def send_raw_transaction(self, signed_raw_tx: str) -> str:
        """Broadcast a fwd-signed raw tx blob via eth_sendRawTransaction.

        Returns the tx hash (0x-prefixed) as returned by the node.
        Raises RpcError on any node rejection (insufficient funds, nonce too
        low, etc.) — callers are responsible for classifying the error into
        the fwd broadcast-result outcome taxonomy.
        """
        result = self._call("eth_sendRawTransaction", [signed_raw_tx])
        return str(result)

    # ---- fee estimation (keyless reads) ----

    def estimate_gas(self, from_addr: str, to: str, data: str, value_wei: int = 0) -> int:
        """eth_estimateGas with a 25% buffer, capped at _MAX_GAS_CAP.

        `from_addr` is the fwd-custodied sender wallet address (public info,
        not a key). The node uses it for the simulation; without it, some
        contract calls fail (wrong msg.sender). Clif passes the configured
        wallet address, which is public.
        """
        params: dict = {
            "from": from_addr,
            "to": to,
            "data": data,
        }
        if value_wei:
            params["value"] = hex(value_wei)
        result = self._call("eth_estimateGas", [params, "latest"])
        raw = int(str(result), 16)
        buffered = int(raw * _GAS_BUFFER)
        return min(buffered, _MAX_GAS_CAP)

    def suggest_fees(self) -> tuple[int, int]:
        """eth_feeHistory → (max_fee_per_gas, max_priority_fee_per_gas) in wei.

        Strategy: baseFee × 2 + tip (1 gwei), capped at the per-network ceiling.
        Raises RpcError when the cap cannot cover baseFee + tip, rather than
        emitting a transaction the chain is certain to reject as underpriced.
        Returns (max_fee_per_gas, max_priority_fee_per_gas).
        """
        result = self._call("eth_feeHistory", [4, "latest", []])
        history: dict[str, Any] = cast(dict, result)
        base_fees: list[Any] = history.get("baseFeePerGas", [])
        # Take the latest base fee (last element in the list is the pending block).
        if base_fees:
            latest_base = int(str(base_fees[-1]), 16)
        else:
            latest_base = _DEFAULT_TIP_WEI  # fallback

        tip = _DEFAULT_TIP_WEI
        cap = _max_fee_cap_wei()
        if latest_base + tip > cap:
            raise RpcError(
                f"base fee {latest_base} wei + tip {tip} wei exceeds the fee cap "
                f"{cap} wei; the chain would reject this as underpriced. Raise "
                f"CLIF_MAX_FEE_PER_GAS_WEI to at least {latest_base * 2 + tip} "
                f"wei (and fwd's FWD_MAX_FEE_PER_GAS to match)."
            )
        max_fee = min(latest_base * 2 + tip, cap)
        # max_priority must not exceed max_fee
        max_priority = min(tip, max_fee)
        return max_fee, max_priority

    # ---- receipt polling (keyless read) ----

    def poll_receipt(
        self,
        tx_hash: str,
        timeout: float = 600.0,
        poll: float = 5.0,
    ) -> dict | None:
        """Poll eth_getTransactionReceipt until mined or timeout.

        Returns the receipt dict on success, or None on timeout (tx still
        pending). Raises RpcError on transport failures.
        """
        deadline = time.monotonic() + timeout
        while True:
            receipt = self.get_transaction_receipt(tx_hash)
            if receipt is not None:
                return receipt
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll)

    def eth_call(self, to: str, data: str) -> bytes:
        result = self._call("eth_call", [{"to": to, "data": data}, "latest"])
        return bytes.fromhex(str(result)[2:])

    def get_block(self, number: int, *, full_transactions: bool = False) -> dict | None:
        """eth_getBlockByNumber(number, full_transactions) → block dict (or None if unknown).
        With full_transactions=True the `transactions` array holds full tx objects (for the
        observer's per-block submit scan). Keyless read."""
        result = self._call("eth_getBlockByNumber", [hex(number), full_transactions])
        return cast(dict, result) if result else None

    def get_logs(
        self, address: str, topics: list, from_block: int, to_block: int
    ) -> list[dict]:
        """eth_getLogs for one address + topic filter over [from_block, to_block]. Keyless read.
        Public Flare/Songbird RPCs cap the range at ~30 blocks — the caller chunks accordingly
        (the per-block observer uses from==to)."""
        result = self._call(
            "eth_getLogs",
            [{"address": address, "topics": topics, "fromBlock": hex(from_block), "toBlock": hex(to_block)}],
        )
        return cast(list, result) if result else []

    def get_transaction_receipt(self, tx_hash: str) -> dict | None:
        result = self._call("eth_getTransactionReceipt", [tx_hash])
        return cast(dict, result) if result else None  # null until mined

    def get_transaction_by_hash(self, tx_hash: str) -> dict | None:
        """The mined tx (incl. on-chain `from`) — the fwd-custody proof read.

        `from` is the secp256k1-recovered sender: it equals the fwd-custodied
        executor wallet iff fwd signed. clif never signs, so this is how the
        rehearsal proves the custody path end-to-end.
        """
        result = self._call("eth_getTransactionByHash", [tx_hash])
        return cast(dict, result) if result else None  # null until propagated

    def get_transaction_count(self, address: str, block_tag: str = "latest") -> int:
        """eth_getTransactionCount(address, block_tag) → next nonce (int).

        block_tag "latest" = mined count; "pending" = incl. mempool. Keyless read
        (address is public; no signing). Raises RpcError on transport/JSON-RPC error
        (same as the other read methods).
        """
        result = self._call("eth_getTransactionCount", [address, block_tag])
        return int(str(result), 16)

    # ---- typed view helpers (keyless) ----

    def rewards_hash(self, flare_systems_manager: str, epoch_id: int) -> str:
        data = (
            "0x"
            + selector("rewardsHash(uint256)").hex()
            + abi_encode(["uint256"], [epoch_id]).hex()
        )
        (out,) = self._abi_decode(["bytes32"], self.eth_call(flare_systems_manager, data))
        return "0x" + out.hex()

    def next_claimable_reward_epoch_id(self, reward_manager: str, owner: str) -> int:
        data = (
            "0x"
            + selector("getNextClaimableRewardEpochId(address)").hex()
            + abi_encode(["address"], [owner]).hex()
        )
        (out,) = self._abi_decode(["uint256"], self.eth_call(reward_manager, data))
        return int(out)

    def reward_epoch_id_range(self, reward_manager: str) -> tuple[int, int]:
        data = "0x" + selector("getRewardEpochIdsWithClaimableRewards()").hex()
        start, end = self._abi_decode(["uint24", "uint24"], self.eth_call(reward_manager, data))
        return int(start), int(end)

    def get_current_reward_epoch_id(self, flare_systems_manager: str) -> int:
        """Read getCurrentRewardEpochId() → uint24 from FlareSystemsManager (keyless)."""
        # Selector: keccak256("getCurrentRewardEpochId()")[:4] = 0x70562697 (verified anchor)
        data = "0x" + selector("getCurrentRewardEpochId()").hex()
        (out,) = self._abi_decode(["uint24"], self.eth_call(flare_systems_manager, data))
        return int(out)

    def claim_executors(self, claim_setup_manager: str, owner: str) -> list[str]:
        """claimExecutors(address) → address[] — who can claim on behalf of owner."""
        data = (
            "0x"
            + selector("claimExecutors(address)").hex()
            + abi_encode(["address"], [owner]).hex()
        )
        (out,) = self._abi_decode(["address[]"], self.eth_call(claim_setup_manager, data))
        return [str(a) for a in out]

    def allowed_claim_recipients(self, claim_setup_manager: str, owner: str) -> list[str]:
        """allowedClaimRecipients(address) → address[] — allow-listed recipient addresses."""
        data = (
            "0x"
            + selector("allowedClaimRecipients(address)").hex()
            + abi_encode(["address"], [owner]).hex()
        )
        (out,) = self._abi_decode(["address[]"], self.eth_call(claim_setup_manager, data))
        return [str(a) for a in out]

    def get_balance(self, address: str) -> int:
        """eth_getBalance(address) → wei (int)."""
        result = self._call("eth_getBalance", [address, "latest"])
        return int(str(result), 16)

    def get_voter_addresses(self, entity_manager: str, voter: str) -> tuple[str, str, str]:
        """getVoterAddresses(address) → (submitAddress, submitSignaturesAddress, signingPolicyAddress)."""
        data = (
            "0x"
            + selector("getVoterAddresses(address)").hex()
            + abi_encode(["address"], [voter]).hex()
        )
        sa, ssa, spa = self._abi_decode(
            ["address", "address", "address"], self.eth_call(entity_manager, data)
        )
        return str(sa), str(ssa), str(spa)

    def get_delegation_address(self, entity_manager: str, voter: str) -> str:
        """getDelegationAddressOf(address) → address."""
        data = (
            "0x"
            + selector("getDelegationAddressOf(address)").hex()
            + abi_encode(["address"], [voter]).hex()
        )
        (da,) = self._abi_decode(["address"], self.eth_call(entity_manager, data))
        return str(da)

    def get_node_ids(self, entity_manager: str, voter: str) -> list[str]:
        """getNodeIdsOf(address) → bytes20[] as 'NodeID-<CB58>' strings."""
        data = (
            "0x" + selector("getNodeIdsOf(address)").hex() + abi_encode(["address"], [voter]).hex()
        )
        (ids,) = self._abi_decode(["bytes20[]"], self.eth_call(entity_manager, data))
        return [f"NodeID-{_bytes20_to_cb58(bytes(b))}" for b in ids]

    def uptime_vote_hash(self, flare_systems_manager: str, epoch_id: int) -> str:
        """Read uptimeVoteHash(uint256) → bytes32 from FlareSystemsManager.

        Returns the 0x-prefixed bytes32.  Zero (ZERO_BYTES32) = the uptime vote
        for this epoch has NOT yet been finalized (the >50% threshold not reached).
        Non-zero = uptime voting has finalized for this epoch (analogous to
        rewardsHash for REWARD_DISTRIBUTION).
        """
        data = (
            "0x"
            + selector("uptimeVoteHash(uint256)").hex()
            + abi_encode(["uint256"], [epoch_id]).hex()
        )
        (out,) = self._abi_decode(["bytes32"], self.eth_call(flare_systems_manager, data))
        return "0x" + out.hex()

    def reward_epoch_timing(self, flare_systems_manager: str) -> tuple[int, int]:
        """(firstRewardEpochStartTs, rewardEpochDurationSeconds) — both uint64, keyless.

        These constants never change, so the caller reads once and derives ANY
        reward epoch's boundaries by pure math (apgateway's model):
            epoch_end_ts(N) = first + (N + 1) * duration
        This works for the current/next (not-yet-closed) epoch too — unlike a
        per-epoch getRewardEpochStartInfo(N+1) read, which only exists once N+1
        has started.
        """
        first = "0x" + selector("firstRewardEpochStartTs()").hex()
        dur = "0x" + selector("rewardEpochDurationSeconds()").hex()
        (first_ts,) = self._abi_decode(["uint64"], self.eth_call(flare_systems_manager, first))
        (duration,) = self._abi_decode(["uint64"], self.eth_call(flare_systems_manager, dur))
        return int(first_ts), int(duration)

    def voter_rewards_sign_info(
        self, flare_systems_manager: str, epoch_id: int, voter: str
    ) -> tuple[int, int]:
        """getVoterRewardsSignInfo(uint24,address) → (signTs, signBlock).

        (0, 0) = this voter has NOT signed rewards for the epoch; a non-zero
        signTs = the voter (our signing-policy address) already signed. Keyless.
        """
        data = (
            "0x"
            + selector("getVoterRewardsSignInfo(uint24,address)").hex()
            + abi_encode(["uint24", "address"], [epoch_id, voter]).hex()
        )
        ts, blk = self._abi_decode(
            ["uint64", "uint64"], self.eth_call(flare_systems_manager, data)
        )
        return int(ts), int(blk)

    def voter_uptime_vote_sign_info(
        self, flare_systems_manager: str, epoch_id: int, voter: str
    ) -> tuple[int, int]:
        """getVoterUptimeVoteSignInfo(uint24,address) → (signTs, signBlock).

        (0, 0) = this voter has NOT signed the uptime vote for the epoch. Keyless.
        """
        data = (
            "0x"
            + selector("getVoterUptimeVoteSignInfo(uint24,address)").hex()
            + abi_encode(["uint24", "address"], [epoch_id, voter]).hex()
        )
        ts, blk = self._abi_decode(
            ["uint64", "uint64"], self.eth_call(flare_systems_manager, data)
        )
        return int(ts), int(blk)

    # ---- reward-signing progress reads (keyless) ----

    def block_number(self) -> int:
        """eth_blockNumber → latest block height (int). Keyless read."""
        result = self._call("eth_blockNumber", [])
        return int(str(result), 16)

    def block_timestamp(self, block_number: int) -> int:
        """eth_getBlockByNumber(n, false).timestamp → UNIX seconds (int). Keyless read.

        Used to anchor the signing-window scan to the epoch-end TIME rather than a
        block-count estimate (block time drifts). Raises RpcError if the block is
        missing.
        """
        result = self._call("eth_getBlockByNumber", [hex(block_number), False])
        if not result:
            raise RpcError(f"block {block_number} not found")
        return int(str(cast(dict, result)["timestamp"]), 16)

    def validator_uptime(self, node_id: str) -> tuple[float | None, bool] | None:
        """P-chain `platform.getCurrentValidators` for one NodeID → (uptime_percent, connected).

        The P-chain lives at `/ext/bc/P` on the SAME host as the C-chain RPC (which uses
        `/ext/bc/C/rpc`); it takes an OBJECT param, not the EVM list form. Returns None if the
        node isn't in the current validator set. Keyless read."""
        p_url = self._url.split("/ext/", 1)[0] + "/ext/bc/P"
        self._id += 1
        try:
            resp = self._client.post(
                p_url,
                json={
                    "jsonrpc": "2.0", "id": self._id,
                    "method": "platform.getCurrentValidators", "params": {"nodeIDs": [node_id]},
                },
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RpcError(f"platform.getCurrentValidators transport failure: {exc}") from exc
        if "error" in body:
            raise RpcError(f"platform.getCurrentValidators rpc error: {body['error']}")
        vals = (body.get("result") or {}).get("validators") or []
        if not vals:
            return None
        v = vals[0]
        up = v.get("uptime")
        return (float(up) if up is not None else None, bool(v.get("connected", False)))

    def signed_logs(
        self,
        flare_systems_manager: str,
        epoch_id: int,
        from_block: int,
        to_block: int,
        *,
        kind: str = "rewards",
    ) -> list[SignedLog]:
        """eth_getLogs for a FlareSystemsManager signing event of ONE epoch.

        `kind` is "rewards" (RewardsSigned) or "uptime" (UptimeVoteSigned). Filters
        on topic0 (the event signature) AND topic1 (the indexed reward epoch id),
        so the node returns only that epoch's per-signer signatures. Decodes the
        indexed signer (topic2 = signingPolicyAddress, topic3 = voter) and, from
        `data`, the signed message hash (field [0]) + `thresholdReached`. Keyless
        read; raises RpcError on transport / node error.
        """
        spec = _EVENT_SPECS[kind]
        epoch_topic = "0x" + epoch_id.to_bytes(32, "big").hex()
        flt = {
            "address": flare_systems_manager,
            "topics": [spec.topic0, epoch_topic],
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        result = self._call("eth_getLogs", [flt])
        logs = cast(list, result) if result else []
        out: list[SignedLog] = []
        for entry in logs:
            topics = entry.get("topics", [])
            if len(topics) < 4:
                continue  # not the indexed shape we expect — skip defensively
            spa = "0x" + topics[2][-40:]
            voter = "0x" + topics[3][-40:]
            data_hex = str(entry.get("data", "0x"))
            data = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
            decoded = self._abi_decode(spec.data_types, data)
            message_hash = decoded[0]
            timestamp = decoded[spec.threshold_idx - 1]  # uint64 ts is always just before the bool
            threshold_reached = decoded[spec.threshold_idx]
            out.append(
                SignedLog(
                    signing_policy_address=spa.lower(),
                    voter=voter.lower(),
                    message_hash="0x" + message_hash.hex(),
                    threshold_reached=bool(threshold_reached),
                    timestamp=int(timestamp),
                    block_number=int(str(entry.get("blockNumber", "0x0")), 16),
                )
            )
        return out

    def voter_normalised_weight(
        self, voter_registry: str, epoch_id: int, signing_policy_address: str
    ) -> tuple[str, int]:
        """getVoterWithNormalisedWeight(uint256,address) → (voter, normalisedWeight).

        Maps an FSP signing-policy address to its registered voter (entity) and
        the uint16 normalised signing-policy weight used for the reward-signing
        threshold. Keyless read.
        """
        data = (
            "0x"
            + selector("getVoterWithNormalisedWeight(uint256,address)").hex()
            + abi_encode(["uint256", "address"], [epoch_id, signing_policy_address]).hex()
        )
        voter, weight = self._abi_decode(
            ["address", "uint16"], self.eth_call(voter_registry, data)
        )
        return str(voter), int(weight)

    def weights_sums(self, voter_registry: str, epoch_id: int) -> tuple[int, int, int]:
        """getWeightsSums(uint256) → (weightsSum, normalisedWeightsSum, normalisedWeightsSumWithPublicKeys).

        The reward-signing % denominator is normalisedWeightsSum (field [1]). Keyless read.
        """
        data = (
            "0x"
            + selector("getWeightsSums(uint256)").hex()
            + abi_encode(["uint256"], [epoch_id]).hex()
        )
        ws, nws, nws_pk = self._abi_decode(
            ["uint128", "uint16", "uint16"], self.eth_call(voter_registry, data)
        )
        return int(ws), int(nws), int(nws_pk)

    # ---- registration readiness (VoterRegistry + FSM), revert-tolerant ----
    #
    # For a reward epoch whose registration has not been set up yet (typically N+1
    # before the FSM emits VotePowerBlockSelected), these views REVERT with
    # "reward epoch id not supported" (the same string that blindsided v0.5.43).
    # That is a NORMAL "window not open yet" state, not an error, so each read
    # below catches it and returns a not-open / not-registered / zero sentinel;
    # any OTHER RpcError still propagates.

    @staticmethod
    def _is_benign_registration_revert(exc: RpcError) -> bool:
        """A revert that means "not registered / window not open for this epoch" —
        a NORMAL readiness state, not a transport error. `isVoterRegistered` returns
        a clean bool, but `getVoterRegistrationWeight` REVERTS `voter not registered`
        for an absent voter, and future-epoch views revert `... not supported`."""
        m = str(exc).lower()
        return "not supported" in m or "voter not registered" in m

    def is_voter_registered(self, voter_registry: str, voter: str, epoch_id: int) -> bool:
        """isVoterRegistered(address,uint256) → bool — is `voter` in the registered
        set for `epoch_id`. THE positive membership signal (the RE423 blind spot).
        Returns a clean bool (no revert); a not-supported revert ⇒ False."""
        data = (
            "0x"
            + selector("isVoterRegistered(address,uint256)").hex()
            + abi_encode(["address", "uint256"], [voter, epoch_id]).hex()
        )
        try:
            (out,) = self._abi_decode(["bool"], self.eth_call(voter_registry, data))
        except RpcError as exc:
            if self._is_benign_registration_revert(exc):
                return False
            raise
        return bool(out)

    def voter_registration_weight(self, voter_registry: str, voter: str, epoch_id: int) -> int:
        """getVoterRegistrationWeight(address,uint256) → uint256 (wei) — the vote
        power `voter` will register / has registered with for `epoch_id`. 0 ⇒
        effectively excluded even if the tx lands. REVERTS `voter not registered`
        for an absent voter and `... not supported` for a future epoch ⇒ both 0."""
        data = (
            "0x"
            + selector("getVoterRegistrationWeight(address,uint256)").hex()
            + abi_encode(["address", "uint256"], [voter, epoch_id]).hex()
        )
        try:
            (out,) = self._abi_decode(["uint256"], self.eth_call(voter_registry, data))
        except RpcError as exc:
            if self._is_benign_registration_revert(exc):
                return 0
            raise
        return int(out)

    def get_registered_voters(self, voter_registry: str, epoch_id: int) -> list[str]:
        """getRegisteredVoters(uint256) → address[] — the identity addresses registered for
        the epoch (the voter set whose values form the consensus median). Keyless read."""
        data = (
            "0x"
            + selector("getRegisteredVoters(uint256)").hex()
            + abi_encode(["uint256"], [epoch_id]).hex()
        )
        (out,) = self._abi_decode(["address[]"], self.eth_call(voter_registry, data))
        return [str(a) for a in out]

    def voter_registration_data(
        self, flare_systems_manager: str, epoch_id: int
    ) -> tuple[int, bool]:
        """getVoterRegistrationData(uint256) → (votePowerBlock, enabled) — the
        registration window for `epoch_id`: enabled=True once it is open, against
        the vote-power block where weight is measured. Revert ⇒ (0, False) (the
        FSM has not yet emitted VotePowerBlockSelected — window not open)."""
        data = (
            "0x"
            + selector("getVoterRegistrationData(uint256)").hex()
            + abi_encode(["uint256"], [epoch_id]).hex()
        )
        try:
            vpb, enabled = self._abi_decode(
                ["uint256", "bool"], self.eth_call(flare_systems_manager, data)
            )
        except RpcError as exc:
            if self._is_benign_registration_revert(exc):
                return 0, False
            raise
        return int(vpb), bool(enabled)

    def signing_policy_threshold_ppm(self, flare_systems_manager: str) -> int:
        """signingPolicyThresholdPPM() → uint24 (e.g. 500000 = 50%). Keyless read."""
        data = "0x" + selector("signingPolicyThresholdPPM()").hex()
        (out,) = self._abi_decode(["uint24"], self.eth_call(flare_systems_manager, data))
        return int(out)

    def contract_address_by_name(self, name: str) -> str:
        """FlareContractRegistry.getContractAddressByName(string) → address.

        The registry is the chain's own source of truth for protocol contract
        addresses, which the Flare Foundation re-deploys from time to time (the
        VoterRegistry moved on both mainnets in July 2026). Keyless read; used to
        detect drift against clif's pinned addresses. Returns the zero address if
        the registry does not know the name.
        """
        data = "0x" + selector("getContractAddressByName(string)").hex() + abi_encode(
            ["string"], [name]
        ).hex()
        (out,) = self._abi_decode(["address"], self.eth_call(FLARE_CONTRACT_REGISTRY, data))
        return str(out)

    def get_revert_reason(self, tx_hash: str) -> str | None:
        """Attempt to decode the revert reason for a mined-reverted tx by replaying it.

        Fetches the tx (from/to/input/value) and receipt (blockNumber), then
        replays via eth_call at that block.  The node returns an error whose
        ``data`` (or ``message``) carries the ABI-encoded revert; we decode
        ``Error(string)`` when ``data`` starts with selector ``0x08c379a0``
        (after that 4-byte selector: 32-byte offset, 32-byte length, then the
        UTF-8 reason string).

        Returns the reason string on success, or ``None`` if it cannot be
        determined (non-archival node, empty data, unexpected encoding, any RPC
        failure).  Never raises — callers fall back to generic terminal
        classification on ``None``.
        """
        try:
            tx = self.get_transaction_by_hash(tx_hash)
            if tx is None:
                return None
            receipt = self.get_transaction_receipt(tx_hash)
            if receipt is None:
                return None
            block_number = receipt.get("blockNumber")
            if block_number is None:
                return None
            # Build eth_call params mirroring the original tx.
            call_obj: dict = {
                "to": tx.get("to") or "",
                "data": tx.get("input") or "0x",
            }
            if tx.get("from"):
                call_obj["from"] = tx["from"]
            value = tx.get("value")
            if value and value not in ("0x0", "0x", "0"):
                call_obj["value"] = value
            # Replay at the mined block — requires an archival node.
            self._id += 1
            try:
                resp = self._client.post(
                    self._url,
                    json={
                        "jsonrpc": "2.0",
                        "id": self._id,
                        "method": "eth_call",
                        "params": [call_obj, block_number],
                    },
                )
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPError, ValueError):
                return None
            # The node may surface the revert in the error object or in the result.
            raw_data: str | None = None
            if "error" in body:
                err = body["error"]
                if isinstance(err, dict):
                    raw_data = err.get("data") or err.get("message") or ""
                elif isinstance(err, str):
                    raw_data = err
            elif "result" in body:
                # Some nodes return the revert data as the result of eth_call.
                raw_data = str(body.get("result") or "")
            if not raw_data:
                return None
            # Decode Error(string): selector 0x08c379a0 + abi.encode(string).
            # Strip optional "Reverted " / "execution reverted: " prefixes.
            prefix = ""
            for pfx in ("Reverted 0x", "execution reverted: 0x", "0x"):
                if raw_data.startswith(pfx) or raw_data.lower().startswith(pfx.lower()):
                    prefix = pfx
                    break
            hex_data = raw_data[len(prefix) :].strip()
            if not hex_data.startswith("08c379a0"):
                # Some nodes embed the plain text in the message field.
                for pfx in ("execution reverted: ", "Reverted "):
                    if raw_data.startswith(pfx):
                        return raw_data[len(pfx) :].strip()
                return None
            data_bytes = bytes.fromhex(hex_data)
            # data_bytes = selector(4) + abi.encode(string)
            # abi.encode(string) = 32-byte offset + 32-byte length + padded utf-8
            if len(data_bytes) < 4 + 32 + 32:
                return None
            payload = data_bytes[4:]  # strip selector
            (reason,) = abi_decode(["string"], payload)
            return str(reason)
        except Exception:  # noqa: BLE001
            return None
