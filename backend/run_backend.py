"""Entry point for the packaged (PyInstaller onedir) Krypt Scout backend.

The Electron supervisor launches this with `--port <port>` (a pre-probed free
127.0.0.1 port) and health-checks GET /api/health. In dev the supervisor runs
`python -m uvicorn kryptscout.api.main:app` directly instead — this file only
exists so PyInstaller has a static import root and a programmatic uvicorn
runner (the `python -m uvicorn` CLI path doesn't survive freezing).

`--selftest` is the build-time gate (scripts/py-bundle.mjs runs the FROZEN exe
with it): it confirms the money-critical Polymarket signing stack is present
and functional in THIS bundle and exits non-zero if not, so a build that
silently dropped a lazy-imported dep can never reach a release installer.
"""
from __future__ import annotations

import argparse
import sys

import uvicorn

from kryptscout.api.main import app


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

    ok = all(c[1] for c in checks)
    print("=== krypt-scout backend selftest ===")
    for name, passed, err in checks:
        print(f"  [{'OK  ' if passed else 'FAIL'}] {name}{('  -> ' + err) if err else ''}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    parser = argparse.ArgumentParser(description="Krypt Scout backend")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--selftest", action="store_true",
                        help="Verify the frozen bundle can import deps + sign an order, then exit")
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
