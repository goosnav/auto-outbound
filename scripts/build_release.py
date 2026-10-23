#!/usr/bin/env python3
"""Build stable native launchers and assemble an explicitly whitelisted local ZIP.

This is build automation, not the customer's startup path. Build success does
not claim clean-machine, browser, signing, or architecture acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "launcher"
PINS = json.loads((LAUNCHER / "tool-versions.json").read_text("utf-8"))
TARGETS = ("macos", "windows-x64", "windows-arm64", "linux-x64", "linux-arm64")


def run(*args: str | Path, cwd: Path | None = None, env: dict | None = None) -> None:
    subprocess.run([str(arg) for arg in args], check=True, cwd=cwd or ROOT, env=env)


def fetch(url: str, destination: Path, digest: str) -> None:
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "AutoOutbound-build", "Accept": "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(100_000_001)
    if len(payload) > 100_000_000 or hashlib.sha256(payload).hexdigest() != digest:
        raise SystemExit(f"Build-tool checksum mismatch: {destination.name}. Review the pinned tool release.")
    destination.write_bytes(payload)
    destination.chmod(0o755)


def prepare_tools(directory: Path) -> None:
    if not shutil.which("go"):
        raise SystemExit(f"Build host needs Go {PINS['go']} on PATH. Customers do not need Go.")
    env = dict(os.environ, GOBIN=str(directory / "bin"))
    run("go", "install", f"github.com/akavel/rsrc@v{PINS['rsrc']}", env=env)
    if sys.platform.startswith("linux"):
        archive = directory / "appimagetool.AppImage"
        fetch(
            f"https://api.github.com/repos/AppImage/appimagetool/releases/assets/{PINS['appimagetool_asset_id']}",
            archive,
            PINS["appimagetool_sha256"],
        )
        destination = directory / "appimagetool"
        if not destination.is_dir():
            extraction = directory / "extract"
            extraction.mkdir(parents=True, exist_ok=True)
            run(archive, "--appimage-extract", cwd=extraction)
            (extraction / "squashfs-root").rename(destination)
            extraction.rmdir()
        for arch, spec in PINS["appimage_runtimes"].items():
            fetch(
                f"https://api.github.com/repos/AppImage/type2-runtime/releases/assets/{spec['asset_id']}",
                directory / f"runtime-{arch}",
                spec["sha256"],
            )


def build_target(target: str, directory: Path, output: Path) -> None:
    try:
        version = subprocess.check_output(["go", "version"], text=True, timeout=15).split()
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Build host needs Go {PINS['go']} on PATH.") from error
    if len(version) < 3 or version[2] != f"go{PINS['go']}":
        raise SystemExit(f"Use pinned Go {PINS['go']} to preserve stable image hashes.")
    manifest = json.loads((ROOT / "app" / "launcher" / "manifest.json").read_text("utf-8"))
    args = [sys.executable, str(LAUNCHER / "build.py"), "--target", target,
            "--name", manifest["display_name"], "--bundle-id", manifest["app_id"],
            "--icons", str(LAUNCHER / "icons"), "--output", str(output)]
    if target.startswith("windows-"):
        arch = "amd64" if target.endswith("x64") else "arm64"
        resource = directory / f"icon-windows-{arch}.syso"
        binary = directory / "bin" / ("rsrc.exe" if os.name == "nt" else "rsrc")
        if not binary.is_file():
            raise SystemExit("Missing rsrc build tool. Run with --prepare-tools.")
        run(binary, "-arch", arch, "-ico", LAUNCHER / "icons" / "AppIcon.ico", "-o", resource)
        args += ["--windows-resource", str(resource)]
    if target.startswith("linux-"):
        if not sys.platform.startswith("linux"):
            raise SystemExit("Build AppImages on a Linux build host.")
        arch = "x86_64" if target.endswith("x64") else "aarch64"
        args += ["--appimagetool", str(directory / "appimagetool" / "AppRun"),
                 "--runtime-file", str(directory / f"runtime-{arch}")]
    run(*args)


def stage_payload(staging: Path, images: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    app = staging / "app"
    app.mkdir(exist_ok=True)
    for name in (".python-version", "pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / "app" / name, app / name)
    for name in ("src", "static", "launcher"):
        shutil.copytree(
            ROOT / "app" / name, app / name, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".keep", "tools", "checksums.sha256"),
        )
    for name in ("README.txt", "LICENSE.txt"):
        shutil.copy2(ROOT / name, staging / name)
    for image in images.iterdir():
        if image.name.startswith("Open Auto Outbound"):
            if image.is_dir():
                shutil.copytree(image, staging / image.name, dirs_exist_ok=True)
            else:
                shutil.copy2(image, staging / image.name)
    # Pinned tool archives and their checksums are generated into the release,
    # never into source control. Cached local copies are copied only if valid.
    cached = ROOT / "app" / "launcher"
    if (cached / "tools").is_dir() and (cached / "checksums.sha256").is_file():
        shutil.copytree(cached / "tools", app / "launcher" / "tools", dirs_exist_ok=True)
        shutil.copy2(cached / "checksums.sha256", app / "launcher" / "checksums.sha256")
    else:
        run(sys.executable, LAUNCHER / "fetch_uv.py", "--version", PINS["uv"], "--app-root", app)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=(*TARGETS, "all"), default="all")
    parser.add_argument("--prepare-tools", action="store_true", help="Download pinned build tools (requires Go on build host)")
    parser.add_argument("--reuse-images", action="store_true", help="Reuse the stable existing images; only repackage app payload")
    parser.add_argument("--package", action="store_true", help="Assemble universal ZIP; requires all five images")
    parser.add_argument("--tools", type=Path, default=ROOT / "build" / "tooling")
    parser.add_argument("--images", type=Path, default=ROOT / "build" / "images")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "Auto-Outbound-M1a-unverified.zip")
    args = parser.parse_args()
    args.tools = args.tools.resolve()
    args.images = args.images.resolve()
    args.tools.mkdir(parents=True, exist_ok=True)
    args.images.mkdir(parents=True, exist_ok=True)
    if args.prepare_tools:
        prepare_tools(args.tools)
    if not args.reuse_images:
        for target in TARGETS if args.target == "all" else [args.target]:
            build_target(target, args.tools, args.images)
    hashes = {}
    for path in sorted(args.images.rglob("*")):
        relative = path.relative_to(args.images)
        if path.is_file() and relative.parts[0].startswith("Open Auto Outbound"):
            hashes[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (args.images / "launcher-hashes.json").write_text(json.dumps(hashes, indent=2) + "\n", "utf-8")
    if args.package:
        staging = ROOT / "build" / "release"
        if staging.exists():
            shutil.rmtree(staging)
        stage_payload(staging, args.images)
        run(sys.executable, LAUNCHER / "package_universal.py", "--root", staging,
            "--name", "Auto Outbound", "--output", args.output.resolve())
        digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
        args.output.with_suffix(args.output.suffix + ".sha256").write_text(f"{digest}  {args.output.name}\n", "utf-8")
        print(f"Built {args.output}; native acceptance remains unverified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
