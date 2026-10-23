#!/usr/bin/env python3
"""Create a customer ZIP from the explicit M1a root whitelist."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path


FORBIDDEN_PARTS = {"dev", "dev-private", ".git", ".github", "node_modules", ".venv", "logs", "__pycache__"}
FORBIDDEN_NAMES = {"AGENTS.md", "CLAUDE.md", "CODEX.md", "GEMINI.md"}
FORBIDDEN_SUFFIXES = {".db", ".key", ".log", ".p12", ".pem", ".pfx", ".sqlite", ".sqlite3"}
REQUIRED_TOOLS = (
    "launcher/tools/macos-x64/uv",
    "launcher/tools/macos-arm64/uv",
    "launcher/tools/windows-x64/uv.exe",
    "launcher/tools/windows-arm64/uv.exe",
    "launcher/tools/linux-x64/uv",
    "launcher/tools/linux-arm64/uv",
)


def add_path(archive: zipfile.ZipFile, path: Path, relative: Path) -> None:
    if path.is_symlink():
        raise SystemExit(f"Symlinks are not allowed in the customer release: {relative}")
    if path.is_dir():
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            add_path(archive, child, relative / child.name)
        return
    if (
        any(part in FORBIDDEN_PARTS for part in relative.parts)
        or path.name in FORBIDDEN_NAMES
        or path.name.startswith(".env")
        or path.suffix.lower() in FORBIDDEN_SUFFIXES
        or path.name.lower().startswith(("credentials", "secrets"))
    ):
        raise SystemExit(f"Forbidden release path: {relative}")
    info = zipfile.ZipInfo.from_file(path, relative.as_posix())
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = path.stat().st_mode
    if mode & stat.S_IXUSR:
        info.external_attr = (mode & 0xFFFF) << 16
    with path.open("rb") as source, archive.open(info, "w") as destination:
        destination.write(source.read())


def validate_app(app: Path) -> None:
    required = (
        ".python-version",
        "pyproject.toml",
        "uv.lock",
        "launcher/bootstrap.py",
        "launcher/manifest.json",
        "launcher/checksums.sha256",
        *REQUIRED_TOOLS,
    )
    missing = [name for name in required if not (app / name).is_file()]
    missing += [name for name in ("src", "static") if not (app / name).is_dir()]
    if missing:
        raise SystemExit(f"Incomplete app payload; missing={sorted(missing)}")
    for directory in (app / "src", app / "static"):
        if not any(path.is_file() and path.name != ".keep" for path in directory.rglob("*")):
            raise SystemExit(f"Application payload is still a placeholder: {directory.relative_to(app)}")
    try:
        manifest = json.loads((app / "launcher/manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Invalid launcher manifest: {error}") from error
    if manifest.get("schema_version") != 1:
        raise SystemExit("Unsupported launcher manifest schema")
    if str(manifest.get("app_id", "")).startswith("com.example."):
        raise SystemExit("Customize the example app_id before packaging")
    python = (app / ".python-version").read_text(encoding="utf-8").strip()
    if manifest.get("python") != python:
        raise SystemExit("manifest.json and .python-version disagree")
    checksums: dict[str, str] = {}
    for line in (app / "launcher/checksums.sha256").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            checksums[fields[-1].lstrip("*")] = fields[0].lower()
    for name in REQUIRED_TOOLS:
        expected = checksums.get(name)
        actual = hashlib.sha256((app / name).read_bytes()).hexdigest()
        if expected != actual:
            raise SystemExit(f"Missing or incorrect bundled-tool checksum: {name}")


def validate_customer_document(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.strip() or "{{" in text or "Replace this placeholder" in text:
        raise SystemExit(f"Customize the customer document before packaging: {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.root.is_dir():
        raise SystemExit(f"Release root is not a directory: {args.root}")
    expected = [
        f"Open {args.name} — macOS.app",
        f"Open {args.name} — Windows x64.exe",
        f"Open {args.name} — Windows ARM64.exe",
        f"Open {args.name} — Linux x86_64.AppImage",
        f"Open {args.name} — Linux ARM64.AppImage",
        "README.txt",
        "LICENSE.txt",
        "app",
    ]
    actual = sorted(path.name for path in args.root.iterdir() if not path.name.startswith("."))
    if sorted(expected) != actual:
        missing, extra = sorted(set(expected) - set(actual)), sorted(set(actual) - set(expected))
        raise SystemExit(f"Release root mismatch; missing={missing}, extra={extra}")
    validate_app(args.root / "app")
    validate_customer_document(args.root / "README.txt")
    validate_customer_document(args.root / "LICENSE.txt")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    top = f"{args.name}-M1a"
    temporary = args.output.with_name(args.output.name + ".partial")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in expected:
                add_path(archive, args.root / name, Path(top) / name)
        temporary.replace(args.output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
