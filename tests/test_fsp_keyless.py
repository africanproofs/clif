"""Keyless invariant: no signing dependency imported after loading FSP modules.

The Core invariant (D1) requires that clif never imports eth-account, eth-keys,
pycryptodome, web3, or argon2 — even transitively through the new FSP modules.
keccak-256 is vendored (clif/_keccak.py) and is the ONLY permitted crypto
primitive. eth-abi is permitted for ABI encoding only (not signing).
"""

import sys


def test_no_signing_deps_after_fsp_calldata_import():
    """Importing fsp_calldata must not pull in any local-signing library."""
    import clif.fsp_calldata  # noqa: F401

    _assert_no_signing_deps()


def test_no_signing_deps_after_fsp_import():
    """Importing fsp (orchestrator) must not pull in any local-signing library."""
    import clif.fsp  # noqa: F401

    _assert_no_signing_deps()


def test_no_signing_deps_after_models_import():
    """Importing models (SignFspMessageResponse etc.) must not pull in signing libs."""
    import clif.models  # noqa: F401

    _assert_no_signing_deps()


def test_no_signing_deps_after_fwd_client_import():
    """Importing fwd_client (sign_fsp_message) must not pull in signing libs."""
    import clif.fwd_client  # noqa: F401

    _assert_no_signing_deps()


def test_no_signing_deps_after_reward_data_import():
    import clif.reward_data  # noqa: F401

    _assert_no_signing_deps()


def test_no_new_pyproject_dependencies():
    """The FSP feature must not add pyproject.toml dependencies.

    Only eth-abi (ABI encoding), httpx, pydantic, typer/rich are permitted.
    We verify that NO signing library appears as a real dependency (not a comment)
    in the [tool.poetry.dependencies] section of pyproject.toml.
    """
    from pathlib import Path

    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    # Extract only the [tool.poetry.dependencies] block (not comments).
    in_deps = False
    dep_lines: list[str] = []
    for line in pyproject.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_deps = stripped == "[tool.poetry.dependencies]"
            continue
        if in_deps:
            # Skip comment lines — they explicitly enumerate forbidden libs by name.
            if not stripped.startswith("#"):
                dep_lines.append(stripped)

    dep_text = "\n".join(dep_lines)
    for forbidden in ("eth-account", "eth-keys", "pycryptodome", "web3", "argon2"):
        assert forbidden not in dep_text, (
            f"{forbidden!r} must not appear as a real dependency in "
            f"[tool.poetry.dependencies] — it is a signing dep"
        )


def _assert_no_signing_deps():
    forbidden = {"eth_account", "eth_keys", "pycryptodome", "web3", "argon2"}
    loaded = set(sys.modules.keys())
    # Check exact names and any submodule prefix.
    for mod in loaded:
        for dep in forbidden:
            assert not (mod == dep or mod.startswith(dep + ".")), (
                f"Forbidden signing dependency {dep!r} was imported (found {mod!r} in sys.modules). "
                "clif holds zero private keys — this is a regression."
            )
