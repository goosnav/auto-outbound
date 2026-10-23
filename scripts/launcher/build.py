#!/usr/bin/env python3
"""Build stable M1a launcher images from the reusable Go supervisor."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SUPERVISOR = ROOT / "supervisor"


def run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def go_build(work: Path, goos: str, goarch: str, output: Path, windows_gui: bool = False) -> None:
    env = os.environ.copy()
    env.update(GOOS=goos, GOARCH=goarch, CGO_ENABLED="0")
    args = ["go", "build", "-trimpath", "-buildvcs=false"]
    if windows_gui:
        args += ["-ldflags", "-s -w -H=windowsgui"]
    else:
        args += ["-ldflags", "-s -w"]
    args += ["-o", str(output), "."]
    run(*args, cwd=work, env=env)


def copy_supervisor(destination: Path) -> None:
    shutil.copytree(SUPERVISOR, destination)


def require(path: Path, label: str) -> Path:
    if not path.is_file():
        raise SystemExit(f"Missing {label}: {path}")
    return path


def universal_macho(amd64: Path, arm64: Path, output: Path) -> None:
    """Combine Go's two Mach-O slices without requiring a macOS build host."""
    slices = [amd64.read_bytes(), arm64.read_bytes()]
    architecture = [(0x01000007, 3), (0x0100000C, 0)]
    alignment = 16384
    offset = alignment
    records = []
    for data, (cpu, subtype) in zip(slices, architecture, strict=True):
        if data[:4] != b"\xcf\xfa\xed\xfe":
            raise ValueError("Go output is not a 64-bit Mach-O slice")
        actual_cpu = struct.unpack_from("<I", data, 4)[0]
        if actual_cpu != cpu:
            raise ValueError("Incorrect architecture in Mach-O slice")
        records.append((cpu, subtype, offset, len(data), 14))
        offset = (offset + len(data) + alignment - 1) // alignment * alignment
    with output.open("wb") as file:
        file.write(struct.pack(">II", 0xCAFEBABE, 2))
        for record in records:
            file.write(struct.pack(">IIIII", *record))
        for data, record in zip(slices, records, strict=True):
            file.seek(record[2])
            file.write(data)


def build_macos(name: str, bundle_id: str, icons: Path, output: Path) -> Path:
    require(icons / "AppIcon.icns", "macOS icon")
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp) / "supervisor"
        copy_supervisor(work)
        amd64, arm64 = Path(temp) / "launcher-amd64", Path(temp) / "launcher-arm64"
        go_build(work, "darwin", "amd64", amd64)
        go_build(work, "darwin", "arm64", arm64)
        app = output / f"Open {name} — macOS.app"
        executable = app / "Contents" / "MacOS" / "launcher"
        resources = app / "Contents" / "Resources"
        executable.parent.mkdir(parents=True, exist_ok=True)
        resources.mkdir(parents=True, exist_ok=True)
        if shutil.which("lipo"):
            run("lipo", "-create", str(amd64), str(arm64), "-output", str(executable))
        else:
            universal_macho(amd64, arm64, executable)
        executable.chmod(0o755)
        shutil.copy2(icons / "AppIcon.icns", resources / "AppIcon.icns")
        info = {
            "CFBundleDevelopmentRegion": "en",
            "CFBundleDisplayName": name,
            "CFBundleExecutable": "launcher",
            "CFBundleIconFile": "AppIcon",
            "CFBundleIdentifier": bundle_id,
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": name,
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1",
            "LSMinimumSystemVersion": "13.0",
        }
        with (app / "Contents" / "Info.plist").open("wb") as file:
            plistlib.dump(info, file, sort_keys=True)
        return app


def build_windows(name: str, arch: str, icons: Path, output: Path, resource: Path | None) -> Path:
    require(icons / "AppIcon.ico", "Windows icon")
    if resource is None:
        raise SystemExit("Windows builds require --windows-resource with an architecture-matched .syso compiled from platform/windows/launcher.rc.in")
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp) / "supervisor"
        copy_supervisor(work)
        shutil.copy2(require(resource, "compiled Windows .syso icon resource"), work / "resource.syso")
        target = output / f"Open {name} — Windows {'x64' if arch == 'amd64' else 'ARM64'}.exe"
        go_build(work, "windows", arch, target, windows_gui=True)
        return target


def build_linux(name: str, arch: str, icons: Path, output: Path, appimagetool: str, runtime_file: Path | None) -> Path:
    require(icons / "AppIcon.png", "Linux icon")
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        work, appdir = temp_path / "supervisor", temp_path / "AppDir"
        copy_supervisor(work)
        (appdir / "usr" / "bin").mkdir(parents=True)
        launcher = appdir / "usr" / "bin" / "launcher"
        go_build(work, "linux", arch, launcher)
        launcher.chmod(0o755)
        app_run = (ROOT / "platform" / "linux" / "AppRun").read_text(encoding="utf-8")
        (appdir / "AppRun").write_text(app_run, encoding="utf-8")
        (appdir / "AppRun").chmod(0o755)
        slug = "".join(character.lower() if character.isalnum() else "-" for character in name).strip("-")
        desktop = (ROOT / "platform" / "linux" / "launcher.desktop.in").read_text(encoding="utf-8")
        desktop = desktop.replace("@NAME@", name).replace("@SLUG@", slug)
        (appdir / f"{slug}.desktop").write_text(desktop, encoding="utf-8")
        shutil.copy2(icons / "AppIcon.png", appdir / f"{slug}.png")
        target = output / f"Open {name} — Linux {'x86_64' if arch == 'amd64' else 'ARM64'}.AppImage"
        env = os.environ.copy()
        env["ARCH"] = "x86_64" if arch == "amd64" else "aarch64"
        if runtime_file is None:
            raise SystemExit("Linux builds require a pinned --runtime-file; do not download an unpinned runtime during packaging")
        require(runtime_file, "pinned AppImage runtime")
        runtime_bytes = runtime_file.read_bytes()
        expected_machine = 62 if arch == "amd64" else 183
        if runtime_bytes[:4] != b"\x7fELF" or struct.unpack_from("<H", runtime_bytes, 18)[0] != expected_machine:
            raise SystemExit("The supplied AppImage runtime has the wrong CPU architecture")
        options = ["-processors", "2", "-all-time", "0", "-mkfs-time", "0", "-all-root"]
        squashfs_args = [value for option in options for value in ("--mksquashfs-opt", option)]
        run(appimagetool, "--runtime-file", str(runtime_file), *squashfs_args, str(appdir), str(target), env=env)
        target.chmod(0o755)
        return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, choices=("macos", "windows-x64", "windows-arm64", "linux-x64", "linux-arm64"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--icons", type=Path, required=True, help="Directory containing AppIcon.icns, AppIcon.ico, and AppIcon.png")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows-resource", type=Path, help="Required for Windows: precompiled architecture-matched .syso icon resource")
    parser.add_argument("--appimagetool", default=os.environ.get("APPIMAGETOOL", "appimagetool"))
    parser.add_argument("--runtime-file", type=Path, help="Pinned architecture-matched AppImage runtime")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if not args.name.strip() or any(character in args.name for character in "/\\\0"):
        parser.error("--name must be a safe display name")
    if args.target == "macos":
        build_macos(args.name, args.bundle_id, args.icons, args.output)
    elif args.target == "windows-x64":
        build_windows(args.name, "amd64", args.icons, args.output, args.windows_resource)
    elif args.target == "windows-arm64":
        build_windows(args.name, "arm64", args.icons, args.output, args.windows_resource)
    elif args.target == "linux-x64":
        build_linux(args.name, "amd64", args.icons, args.output, args.appimagetool, args.runtime_file)
    else:
        build_linux(args.name, "arm64", args.icons, args.output, args.appimagetool, args.runtime_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
