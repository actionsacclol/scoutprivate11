"""Entry point for the packaged (PyInstaller onedir) Krypt Scout backend.

The Electron supervisor launches this with `--port <port>` (a pre-probed free
127.0.0.1 port) and health-checks GET /api/health. In dev the supervisor runs
`python -m uvicorn kryptscout.api.main:app` directly instead — this file only
exists so PyInstaller has a static import root and a programmatic uvicorn
runner (the `python -m uvicorn` CLI path doesn't survive freezing).

`--mcp` turns this binary into the stdio relay an AI client launches
(kryptscout/mcp/stdio.py). It is dispatched before anything heavy is imported:
a relay must start fast, must not open the backend's log file, and must never
print to stdout, which is the protocol.

`--selftest` is the build-time gate (scripts/py-bundle.mjs runs the FROZEN exe
with it): it confirms the money-critical Polymarket signing stack is present
and functional in THIS bundle and exits non-zero if not, so a build that
silently dropped a lazy-imported dep can never reach a release installer.
"""
from __future__ import annotations

import argparse
import sys


def _selftest() -> int:
    """Frozen-build smoke test: confirm the trading deps AND the deposit-wallet
    order signer are importable + functional in THIS build. Prints a short
    report; exit 0 = all good, 1 = something's missing (e.g. the PyInstaller
    bundle dropped eth_account / py_clob_client_v2 → the app can't sign an order
    and wallet-connect fails with "eth-account is required")."""
    from kryptscout.autobet import polymarket_auth

    checks: list[tuple[str, bool, str]] = []

    def _try(name: str, fn) -> None:
        try:
            fn()
            checks.append((name, True, ""))
        except Exception as e:  # noqa: BLE001 — report every failure mode
            checks.append((name, False, f"{type(e).__name__}: {e}"))

    _try("import eth_account", lambda: __import__("eth_account"))
    _try("import httpx", lambda: __import__("httpx"))
    _try("import cryptography", lambda: __import__("cryptography"))
    _try("import py_clob_client_v2", lambda: __import__("py_clob_client_v2"))
    _try("sign POLY_1271 order (offline)", polymarket_auth.verify_signer)
    # pandas resolves its parquet engine by string import at call time, which
    # PyInstaller can't see — a bundle that dropped pyarrow only fails at the
    # first ncaab refresh. get_engine("auto") is that exact resolution path.
    _try("pandas parquet engine (pyarrow)",
         lambda: __import__("pandas.io.parquet", fromlist=["get_engine"])
         .get_engine("auto"))
    # The MCP relay is this same binary started with --mcp. Its modules are
    # reached only through that flag, so a bundle that dropped them would
    # build clean and fail the first time a user connected an AI client.
    _try("mcp relay modules", _mcp_selftest)
    # Kalshi and Polymarket US orders are signed with cryptography primitives
    # nothing else in the app touches (RSA-PSS, Ed25519). Lazy-imported, so a
    # bundle missing them builds clean and fails on the first live order.
    _try("sign Kalshi + Polymarket US requests (offline)", _exchange_selftest)

    ok = all(c[1] for c in checks)
    print("=== krypt-scout backend selftest ===")
    for name, passed, err in checks:
        print(f"  [{'OK  ' if passed else 'FAIL'}] {name}{('  -> ' + err) if err else ''}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _exchange_selftest() -> None:
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from kryptscout.autobet import agent, kalshi_trade, pmus_trade, venues  # noqa: F401

    kalshi_trade.verify_signer()
    pmus_trade.verify_signer()
    # Most Kalshi keys are RSA: sign the documented message and verify it.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    headers = kalshi_trade.sign_headers(
        "post", f"{kalshi_trade.PATH_PREFIX}/portfolio/events/orders",
        now_ms=1700000000000, creds=("selftest", key),
    )
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000POST/trade-api/v2/portfolio/events/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert set(venues.VENUE_KEYS) == {"polymarket", "kalshi", "polymarket_us"}


def _mcp_selftest() -> None:
    from kryptscout.mcp import catalog, protocol, stdio  # noqa: F401
    if not catalog.list_tools():
        raise RuntimeError("the MCP tool catalog is empty")
    if protocol.initialize_result({})["serverInfo"]["name"] != catalog.SERVER_NAME:
        raise RuntimeError("the MCP handshake is malformed")


def main() -> None:
    if "--mcp" in sys.argv:
        from kryptscout.mcp import stdio
        raise SystemExit(stdio.main(sys.argv))
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    parser = argparse.ArgumentParser(description="Krypt Scout backend")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--selftest", action="store_true",
                        help="Verify the frozen bundle can import deps + sign an order, then exit")
    parser.add_argument("--mcp", action="store_true",
                        help="Run as the stdio relay an AI client launches (see kryptscout/mcp)")
    args = parser.parse_args()

    # Imported here, not at module top: the relay above must not pay for (or
    # log through) the whole API. PyInstaller still sees these — it reads
    # imports inside functions.
    import os

    import uvicorn

    # The MCP bridge file tells a relay which port this launch is on.
    os.environ.setdefault("KRYPT_SCOUT_PORT", str(args.port))

    from kryptscout.api.main import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
