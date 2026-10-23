#!/usr/bin/env python3
"""Download pinned official uv binaries and write the launcher checksum manifest."""

from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
import urllib.request
import zipfile
from pathlib import Path


TARGETS = {
    "macos-x64": ("uv-x86_64-apple-darwin.tar.gz", "uv"),
    "macos-arm64": ("uv-aarch64-apple-darwin.tar.gz", "uv"),
    "windows-x64": ("uv-x86_64-pc-windows-msvc.zip", "uv.exe"),
    "windows-arm64": ("uv-aarch64-pc-windows-msvc.zip", "uv.exe"),
    "linux-x64": ("uv-x86_64-unknown-linux-gnu.tar.gz", "uv"),
    "linux-arm64": ("uv-aarch64-unknown-linux-gnu.tar.gz", "uv"),
}


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "goosnav-m1a-launcher"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def extract_executable(archive_name: str, archive: bytes, executable_name: str) -> bytes:
    if archive_name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            member = next((name for name in package.namelist() if Path(name).name == executable_name), None)
            if member is None:
                raise RuntimeError(f"{executable_name} not found in {archive_name}")
            return package.read(member)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        member = next((item for item in package.getmembers() if Path(item.name).name == executable_name and item.isfile()), None)
        if member is None:
            raise RuntimeError(f"{executable_name} not found in {archive_name}")
        extracted = package.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"cannot extract {executable_name} from {archive_name}")
        return extracted.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="Exact uv release tag, for example 0.11.28")
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--target", action="append", choices=tuple(TARGETS), help="Repeat to fetch a subset; default fetches all")
    args = parser.parse_args()
    base = f"https://github.com/astral-sh/uv/releases/download/{args.version}"
    tools = args.app_root / "launcher" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    selected = args.target or list(TARGETS)
    checksums: dict[str, str] = {}
    for target in selected:
        archive_name, executable_name = TARGETS[target]
        archive = download(f"{base}/{archive_name}")
        expected_text = download(f"{base}/{archive_name}.sha256").decode("ascii").strip()
        expected = expected_text.split()[0]
        actual = hashlib.sha256(archive).hexdigest()
        if actual.lower() != expected.lower():
            raise RuntimeError(f"official archive checksum mismatch for {archive_name}")
        executable = extract_executable(archive_name, archive, executable_name)
        destination = tools / target / executable_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(executable)
        destination.chmod(0o755)
        (destination.parent / ".keep").unlink(missing_ok=True)
        relative = destination.relative_to(args.app_root).as_posix()
        checksums[relative] = hashlib.sha256(executable).hexdigest()
        print(f"Installed {target}: {destination}")
    checksum_file = args.app_root / "launcher" / "checksums.sha256"
    existing: dict[str, str] = {}
    if checksum_file.exists():
        for line in checksum_file.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and len(fields[0]) == 64:
                existing[fields[-1].lstrip("*")] = fields[0]
    existing.update(checksums)
    checksum_file.write_text("".join(f"{digest}  {path}\n" for path, digest in sorted(existing.items())), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
