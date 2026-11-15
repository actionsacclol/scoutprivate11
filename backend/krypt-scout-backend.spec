# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec — the SINGLE source of truth for what the frozen backend
# bundles. scripts/py-bundle.mjs builds from this file, never from inline CLI
# flags: the money-critical Polymarket signing stack (eth_account + the
# py_clob_client_v2 chain) is LAZY-imported inside kryptscout.autobet, so a
# plain `--collect-submodules` invocation silently drops its transitive deps
# and metadata, and the frozen backend then fails the moment a user connects a
# wallet ("eth-account is required for Polymarket signing"). This mirrors
# krypt-polybot's spec, which is battle-tested for exactly this.
#
# Entry: run_backend.py (programmatic uvicorn runner; also serves --selftest).
# Output (via --distpath dist-py): dist-py/krypt-scout-backend/krypt-scout-backend[.exe]
import sys

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

datas = []
binaries = []
hiddenimports = []

# ── Krypt Scout's own code + the server stack ─────────────────────────────
# Sport engines (kryptscout.sports.*) and odds providers are loaded via
# importlib, and uvicorn resolves its loop/protocol/lifespan classes from
# strings — neither is visible to static analysis, so collect both whole.
hiddenimports += collect_submodules('kryptscout')
hiddenimports += collect_submodules('uvicorn')

# The web stack is imported ONLY by kryptscout/api/main.py, which the hardened
# build compiles to a native module (HARDEN_MODULES). run_backend.py — the
# Analysis entry point — imports just uvicorn and `kryptscout.api.main`, so with
# main.py opaque there is no remaining source reference to fastapi/starlette/
# pydantic and PyInstaller would ship a backend that dies on its first import.
# Same reasoning as the numpy/pandas block below; harmless on a plain build.
# collect_submodules, NOT bare names: unlike numpy/pandas these have no hook
# that pulls their whole tree, so naming only the top level ships a fastapi
# package without fastapi.middleware.cors and the frozen backend dies on import
# (caught by the selftest — keep it that way).
hiddenimports += collect_submodules('fastapi')
hiddenimports += collect_submodules('starlette')
hiddenimports += collect_submodules('pydantic')

# The numeric stack (numpy/pandas) is imported ONLY by kryptscout.sports.* and
# core/sgp.py — nothing else in the app touches it. Normally PyInstaller sees
# those imports by parsing the .py source. But `npm run dist:hardened` compiles
# the sports engines to native .pyd (see scripts/py-harden.mjs + HARDENING.md
# Layer 3), and a .pyd is opaque to source analysis — so without naming these
# explicitly, a hardened build would drop the stack and every engine would die
# at import. Listing the top-level names triggers each library's PyInstaller
# hook to collect its tree. Harmless on a NON-hardened build (they're already
# pulled via the .py) — so it stays unconditional, keeping the two build paths
# identical here.
hiddenimports += ['numpy', 'pandas']

# Deliberately NOT bundled, even when the build venv happens to have them:
#   xgboost / sklearn  the trained NBA/NFL overlays were retired in November
#                      2026; no engine fits or loads a model any more.
#   scipy              only ever came along as their dependency. pandas names it
#                      inside a few optional code paths (interpolation methods,
#                      sparse matrices) that this app never calls, and that
#                      mention alone was enough to pull 47 MB into the bundle.
#   duckdb             dev-side order-book harvest (jobs/bookfill.py), a lazy
#                      import that already reports a clean "pip install duckdb"
#                      when it is missing. It was riding along at 36 MB.
# An exclude makes the bundle the same on every build box instead of a function
# of what is installed there. If an engine ever needs one of these again, take
# it off this list AND add it to hiddenimports above.
EXCLUDED_MODULES = ['xgboost', 'sklearn', 'scipy', 'duckdb']

# ── Polymarket signing: eth_account + its full transitive graph ───────────
# The ethereum libraries read their own version via importlib.metadata at
# import time (`__version__ = version("py_ecc")` and friends), so every
# .dist-info in the chain MUST ride along or the import raises
# PackageNotFoundError — which surfaces as the generic "eth-account is
# required" error. Copy metadata RECURSIVELY from the two roots rather than
# naming packages: the chain changes under us. 2026-09-03: a fresh CI venv
# resolved eth-keyfile 0.10, which newly imports py_ecc, whose __init__ reads
# its own metadata — three named copy_metadata lines shipped a bundle that
# failed the selftest on every runner ("No package metadata was found for
# py_ecc") while the older local venv passed. Collect the whole dependency
# chain's submodules too; several are pulled in by string/dynamic import that
# PyInstaller can't follow.
datas += collect_data_files('eth_account')
datas += copy_metadata('eth_account', recursive=True)
datas += copy_metadata('py_clob_client_v2', recursive=True)


def _collect_if_installed(name):
    """collect_submodules for a package that may or may not be in the venv
    (transitive deps that appear/disappear between library versions)."""
    import importlib.util
    return collect_submodules(name) if importlib.util.find_spec(name) else []


hiddenimports += _collect_if_installed('py_ecc')
hiddenimports += _collect_if_installed('eth_keyfile')
hiddenimports += _collect_if_installed('eth_typing')
hiddenimports += _collect_if_installed('ckzg')
hiddenimports += collect_submodules('cryptography')
hiddenimports += collect_submodules('httpx')
hiddenimports += collect_submodules('eth_account')
hiddenimports += collect_submodules('eth_keys')
hiddenimports += collect_submodules('eth_utils')
hiddenimports += collect_submodules('eth_abi')
hiddenimports += collect_submodules('eth_hash')
hiddenimports += collect_submodules('eth_rlp')
hiddenimports += collect_submodules('hexbytes')
hiddenimports += collect_submodules('rlp')
hiddenimports += collect_submodules('cytoolz')
hiddenimports += collect_submodules('toolz')
hiddenimports += collect_submodules('parsimonious')
hiddenimports += collect_submodules('bitarray')

# pyarrow: pandas resolves its parquet engine at call time via
# import_optional_dependency("pyarrow") — a string import PyInstaller's static
# analysis never sees, so without this the frozen backend's ncaab refresh dies
# with "Unable to find a usable engine". Collect in full: pyarrow spreads
# native DLLs and data across the package.
_d, _b, _h = collect_all('pyarrow')
datas += _d
binaries += _b
hiddenimports += _h

# certifi CA bundle: the frozen backend makes HTTPS calls to the Polymarket
# CLOB; a frozen interpreter has no usable OpenSSL default verify paths, so the
# .pem must be bundled or every wallet check / order POST fails TLS verify.
hiddenimports += ['certifi']
datas += collect_data_files('certifi')

# ── Deposit-wallet (POLY_1271) order signer chain ─────────────────────────
# Polymarket deposit-wallet order signing is delegated to the official
# py_clob_client_v2 (+ poly_eip712_structs / py_order_utils / pycryptodome).
# All lazy-imported, so collect each in FULL (submodules + data + native libs)
# — a partial collect signs nothing and only fails at the first live order.
for _pkg in ('py_clob_client_v2', 'poly_eip712_structs', 'py_order_utils', 'Crypto'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass

# ── OS keychain (macOS/Linux at-rest credential encryption) ───────────────
# keyring resolves its backends via package metadata + entry points, so collect
# it whole and copy its metadata. The Linux Secret Service backend needs
# secretstorage + jeepney (absent on a Windows build box — the except just skips
# them). All optional at runtime: a missing backend falls back to DPAPI (Windows)
# or plaintext, so a collect miss here can never break the build or the signer.
for _pkg in ('keyring', 'secretstorage', 'jeepney'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass
try:
    datas += copy_metadata('keyring')
except Exception:
    pass


a = Analysis(
    ['run_backend.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDED_MODULES,
    noarchive=False,
    # optimize=2 (-OO): strip asserts AND docstrings from the frozen bytecode.
    # Functionally safe here — no runtime code relies on asserts (verified) or
    # __doc__ except cosmetic /docs summaries + CLI --help text. The anti-RE
    # win is real: this codebase's docstrings spell out the money rules, and
    # stripping them removes that guidance from anything that decompiles the
    # pyc. (Does NOT stop decompilation — that needs native compilation; see
    # HARDENING.md — but it removes the free reading material.)
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='krypt-scout-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX disabled: UPX-packed unsigned exes are a common AV/EDR false-positive
    # heuristic and get quarantined on user machines. Ship unpacked.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Embed real VERSIONINFO + icon so the unsigned exe reads as legitimate
    # software to AV heuristics (bare, metadata-less binaries get flagged).
    # Windows-only fields; ignored on mac/Linux builds.
    icon='../resources/krypt.ico' if sys.platform == 'win32' else None,
    version='version_info.txt' if sys.platform == 'win32' else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='krypt-scout-backend',
)
