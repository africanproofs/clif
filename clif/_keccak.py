"""Vendored, dependency-free Keccak-256 (Ethereum's keccak, NOT FIPS-202 SHA3).

Why vendored: clif must not depend on `pycryptodome` / `eth-hash` backends
(local-signing signals — fwd Core invariant #7). The only thing clif needs
keccak for is deriving the `RewardManager.claim` 4-byte function selector from
the ABI at runtime. This is the Keccak Team's readable-and-compact reference
(public domain / CC0, https://github.com/XKCP/XKCP), specialised to
rate=1088 bits, capacity=512, Keccak domain byte 0x01, 32-byte output.

Correctness is anchored two ways: `tests/test_keccak.py` checks published
vectors, and `clif/calldata.py` asserts the runtime-derived selector equals
the independently-verified constant `0x8e33aba5`.
"""

from __future__ import annotations

_RHO = [
    1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14,
    27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44,
]
_PI = [
    10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4,
    15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1,
]
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_MASK = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK


def _keccak_f1600(st: list[int]) -> None:
    for rnd in range(24):
        # theta
        c = [st[x] ^ st[x + 5] ^ st[x + 10] ^ st[x + 15] ^ st[x + 20] for x in range(5)]
        d = [c[(x + 4) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(0, 25, 5):
                st[y + x] ^= d[x]
        # rho + pi
        t = st[1]
        for i in range(24):
            j = _PI[i]
            t, st[j] = st[j], _rol(t, _RHO[i])
        # chi
        for y in range(0, 25, 5):
            row = st[y:y + 5]
            for x in range(5):
                st[y + x] = row[x] ^ ((~row[(x + 1) % 5]) & row[(x + 2) % 5])
        # iota
        st[0] ^= _RC[rnd]


def keccak256(data: bytes) -> bytes:
    """Return the 32-byte Keccak-256 digest of ``data``."""
    rate = 136  # 1088 bits / 8
    st = [0] * 25
    # absorb
    block = bytearray(data)
    block.append(0x01)  # Keccak (Ethereum) domain separation / pad start
    while len(block) % rate != 0:
        block.append(0x00)
    block[-1] ^= 0x80  # final bit of pad10*1
    for off in range(0, len(block), rate):
        for i in range(rate // 8):
            st[i] ^= int.from_bytes(block[off + i * 8: off + i * 8 + 8], "little")
        _keccak_f1600(st)
    # squeeze (one block is enough for 32 bytes at this rate)
    out = bytearray()
    for i in range(rate // 8):
        out += st[i].to_bytes(8, "little")
    return bytes(out[:32])
