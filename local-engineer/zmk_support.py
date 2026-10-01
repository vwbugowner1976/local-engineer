"""Small, evidence-based helpers for ZMK project metadata."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


ZMK_VERSION_UNKNOWN = "ZMK_VERSION_UNKNOWN"


@dataclass(frozen=True)
class ZmkVersionInfo:
    version: str
    revision: str = ""
    source: str = ""
    reason: str = ""


_ZMK_PROJECT = re.compile(r"^(\s*)-\s+name\s*:\s*[\"']?zmk[\"']?\s*(?:#.*)?$", re.I)
_NEXT_PROJECT = re.compile(r"^(\s*)-\s+name\s*:")
_REVISION = re.compile(r"^\s+revision\s*:\s*(.*?)\s*(?:#.*)?$")
_SUPPORTED_VERSION = re.compile(
    r"^(?:v)?0\.(3|4)(?:\.\d+)?(?:[-+][0-9A-Za-z._+-]+)?$",
    re.I,
)


def _zmk_revisions(manifest: str) -> list[str]:
    """Return only revision scalars belonging to a west project named zmk."""
    lines = manifest.splitlines()
    revisions: list[str] = []
    index = 0
    while index < len(lines):
        match = _ZMK_PROJECT.match(lines[index])
        if not match:
            index += 1
            continue

        item_indent = len(match.group(1))
        index += 1
        while index < len(lines):
            line = lines[index]
            next_project = _NEXT_PROJECT.match(line)
            if next_project and len(next_project.group(1)) <= item_indent:
                break
            revision = _REVISION.match(line)
            if revision:
                value = revision.group(1).strip().strip("\"'")
                if value:
                    revisions.append(value)
                    break
            index += 1

    return revisions


def detect_zmk_version(manifests: Mapping[str, str]) -> ZmkVersionInfo:
    """Detect v0.3/v0.4 only from an explicit ZMK revision in west manifests.

    Branch names such as ``main`` and opaque commit hashes are deliberately
    reported as unknown. Callers should not select a version-specific build
    environment from an unqualified revision.
    """
    found: list[tuple[str, str, str]] = []
    for source, contents in manifests.items():
        for revision in _zmk_revisions(contents):
            match = _SUPPORTED_VERSION.fullmatch(revision)
            version = f"v0.{match.group(1)}" if match else ZMK_VERSION_UNKNOWN
            found.append((source, revision, version))

    if not found:
        return ZmkVersionInfo(
            ZMK_VERSION_UNKNOWN,
            reason="no zmk project revision found in available west manifests",
        )

    explicit_versions = {version for _, _, version in found if version != ZMK_VERSION_UNKNOWN}
    has_unqualified_revision = any(version == ZMK_VERSION_UNKNOWN for _, _, version in found)
    if len(explicit_versions) > 1 or (explicit_versions and has_unqualified_revision):
        return ZmkVersionInfo(
            ZMK_VERSION_UNKNOWN,
            source=", ".join(source for source, _, _ in found),
            reason="conflicting zmk revisions across west manifests",
        )

    source, revision, version = found[0]
    if version == ZMK_VERSION_UNKNOWN:
        return ZmkVersionInfo(
            version,
            revision=revision,
            source=source,
            reason="zmk revision is not an explicit v0.3 or v0.4 release identifier",
        )
    return ZmkVersionInfo(version, revision=revision, source=source)
