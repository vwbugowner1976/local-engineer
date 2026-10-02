"""Small, evidence-based helpers for ZMK project metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import re
import shutil
from typing import Mapping, Optional


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


def discover_zmk_project(files: Mapping[str, str], project_root: str = ".") -> dict:
    """Return metadata for a conventional west + build.yaml ZMK checkout."""
    manifests = {name: body for name, body in files.items()
                if name in ("config/west.yml", "west.yml")}
    version = detect_zmk_version(manifests)
    build_text = files.get("build.yaml", "")
    targets = []
    current = None
    in_include = False
    matrix = {"board": [], "shield": []}
    matrix_key = None
    for line in build_text.splitlines():
        if re.match(r"^include\s*:", line):
            in_include = True
            matrix_key = None
            continue
        if not in_include:
            key = re.match(r"^(board|shield)\s*:\s*(.*?)\s*$", line)
            if key:
                matrix_key = key.group(1)
                value = _yaml_scalar(key.group(2))
                if value.startswith("[") and value.endswith("]"):
                    matrix[matrix_key].extend(_yaml_list(value[1:-1]))
                    matrix_key = None
                elif value:
                    matrix[matrix_key].append(value)
                    matrix_key = None
                continue
            array_item = re.match(r"^\s+-\s+(.*?)\s*$", line)
            if matrix_key and array_item:
                matrix[matrix_key].append(_yaml_scalar(array_item.group(1)))
                continue
            if line.strip() and not line.lstrip().startswith("#") and not line[:1].isspace():
                matrix_key = None
            continue
        item = re.match(r"^\s+-\s+board\s*:\s*(.*?)\s*$", line)
        if item:
            if current and current.get("board"):
                targets.append(current)
            current = {"board": _yaml_scalar(item.group(1)), "shield": ""}
            continue
        if current is None:
            continue
        value = re.match(r"^\s+(shield|artifact-name|snippet|cmake-args)\s*:\s*(.*?)\s*$", line)
        if value:
            current[value.group(1)] = _yaml_scalar(value.group(2))
    if current and current.get("board"):
        targets.append(current)
    if matrix["board"]:
        shields = matrix["shield"] or [""]
        targets.extend({"board": board, "shield": shield}
                       for board in matrix["board"] for shield in shields)
    project_name = Path(project_root.rstrip("/")).name or "zmk-project"
    keyboard = ""
    if targets:
        shield = targets[0].get("shield", "").split()
        if shield:
            keyboard = re.sub(r"_(?:L|R|left|right)$", "", shield[0], flags=re.I)
    if not keyboard:
        keyboard = re.sub(r"[^A-Za-z0-9._-]+", "-", project_name).strip("-.") or "keyboard"
    return {"version": version.version, "revision": version.revision,
            "version_source": version.source, "version_reason": version.reason,
            "project": project_name, "keyboard": keyboard, "targets": targets,
            "is_zmk": bool(manifests and build_text and targets)}


def _yaml_scalar(value: str) -> str:
    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value.strip("\"'")


def _yaml_list(value: str) -> list[str]:
    return [item for raw in value.split(",") if (item := _yaml_scalar(raw))]


def select_destination(version: str, project: str) -> str:
    if version not in ("v0.3", "v0.4"):
        raise ValueError("unsupported or unknown ZMK version")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", project) or project in (".", ".."):
        raise ValueError("unsafe project name")
    return f"/mnt/d/ZMK-Firmware/zmk-dev/{version}/{project}"


def artifact_name(keyboard: str, role: str, timestamp: datetime, version: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", keyboard) or role not in ("Central", "Peripheral"):
        raise ValueError("invalid keyboard or split role")
    if version not in ("v0.3", "v0.4"):
        raise ValueError("unsupported or unknown ZMK version")
    return f"{keyboard}-{role}-{timestamp:%Y%m%d%H%M%S}-{version}.uf2"


def verify_copy(source: str, destination: str) -> dict:
    src, dst = Path(source), Path(destination)
    if not src.is_file() or src.suffix.lower() != ".uf2":
        raise ValueError("source must be an existing UF2 file")
    if not dst.is_file():
        raise ValueError("destination UF2 was not created")
    source_size, destination_size = src.stat().st_size, dst.stat().st_size
    if source_size <= 0 or source_size != destination_size:
        raise ValueError("source/destination UF2 size mismatch")
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    source_hash, destination_hash = digest(src), digest(dst)
    if source_hash != destination_hash:
        raise ValueError("source/destination UF2 content mismatch")
    return {"source": str(src), "destination": str(dst), "size": source_size,
            "sha256": source_hash, "verified": True}


def copy_uf2(source: str, destination_dir: str, filename: str,
             allowed_root: Optional[str] = None) -> dict:
    """Copy a UF2 into an already selected destination and verify bytes on disk."""
    if Path(filename).name != filename or not filename.endswith(".uf2"):
        raise ValueError("unsafe UF2 filename")
    src = Path(source).resolve(strict=True)
    if not src.is_file() or src.suffix.lower() != ".uf2":
        raise ValueError("source must be an existing UF2 file")
    directory = Path(destination_dir)
    directory.mkdir(parents=True, exist_ok=True)
    resolved_dir = directory.resolve(strict=True)
    if not resolved_dir.is_dir():
        raise ValueError("destination is not a directory")
    if allowed_root is not None:
        allowed = Path(allowed_root).resolve(strict=False)
        if allowed not in resolved_dir.parents:
            raise ValueError("destination escapes its allowed root")
    destination = resolved_dir / filename
    if destination.exists():
        raise ValueError("destination file already exists; refusing to overwrite it")
    shutil.copy2(src, destination)
    return verify_copy(str(src), str(destination))
