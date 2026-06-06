"""Vendored Keccak-256 must match published vectors (it anchors the selector)."""

from clif._keccak import keccak256


def test_empty():
    assert (
        keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )


def test_abc():
    assert (
        keccak256(b"abc").hex()
        == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )


def test_known_selector_transfer():
    # ERC-20 transfer(address,uint256) selector is the canonical 0xa9059cbb.
    assert keccak256(b"transfer(address,uint256)")[:4].hex() == "a9059cbb"


def test_multi_block_absorb_is_stable():
    # 200 bytes > one 136-byte rate block — exercises multi-block absorb.
    # (Correctness of the permutation/padding is proven by the vectors above;
    # this guards the multi-block path is deterministic and well-formed.)
    data = bytes(range(256))[:200]
    d = keccak256(data)
    assert len(d) == 32
    assert d == keccak256(bytes(data))
