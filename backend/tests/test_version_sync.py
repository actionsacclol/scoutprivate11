"""The shipped Windows exe metadata must agree with package.json's version.

`backend/version_info.txt` is embedded into krypt-scout-backend.exe as Windows
VERSIONINFO (see HARDENING.md Layer 2) — it exists so an unsigned binary carries
real provenance and doesn't read as an anonymous blob to AV heuristics. Stale
provenance is worse than none: a 0.3.2 installer whose backend claims 0.3.1
makes every "which version is this user on?" support answer a coin flip.

It drifts silently because nothing reads it at runtime, and CI now turns
package.json's version straight into the release tag — so a bump that forgets
this file ships mislabelled. This test is the thing that notices.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PKG = _ROOT / "package.json"
_VERSION_INFO = _ROOT / "backend" / "version_info.txt"


def _package_version() -> str:
    return json.loads(_PKG.read_text(encoding="utf-8"))["version"]


@pytest.mark.skipif(not _VERSION_INFO.exists(), reason="version_info.txt not present")
def test_version_info_matches_package_json() -> None:
    version = _package_version()
    major, minor, patch = (int(p) for p in version.split(".")[:3])
    text = _VERSION_INFO.read_text(encoding="utf-8")

    # filevers=(0, 3, 2, 0) / prodvers=(...) — the tuple Windows actually sorts on.
    for field in ("filevers", "prodvers"):
        match = re.search(rf"{field}=\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)", text)
        assert match, f"{field} not found in version_info.txt"
        assert tuple(int(g) for g in match.groups()[:3]) == (major, minor, patch), (
            f"{field} is {match.group(0)} but package.json says {version} — "
            "bump backend/version_info.txt too."
        )

    # The human-readable strings shown in Explorer's Details tab.
    for field in ("FileVersion", "ProductVersion"):
        match = re.search(rf"StringStruct\('{field}',\s*'([\d.]+)'\)", text)
        assert match, f"{field} not found in version_info.txt"
        assert match.group(1).split(".")[:3] == [str(major), str(minor), str(patch)], (
            f"{field} is {match.group(1)} but package.json says {version}."
        )
