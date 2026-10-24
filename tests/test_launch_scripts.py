"""Command files must resolve their own release and preserve visible failures."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell contract")
def test_command_launches_shared_supervisor_from_unicode_path(tmp_path):
    release = tmp_path / "Leads & reports Ω"
    release.mkdir()
    for name in ("Open Auto Outbound.command", "Open Auto Outbound.sh"):
        shutil.copy2(ROOT / name, release / name)
    for platform in ("macos", "linux"):
        for architecture in ("x64", "arm64"):
            binary = release / "app" / "launcher" / "bin" / f"{platform}-{architecture}" / "launcher"
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nprintf "%s" "$GOOSNAV_RELEASE_ROOT"\nexit 7\n')
            binary.chmod(0o755)
    result = subprocess.run(["sh", str(release / "Open Auto Outbound.command")], cwd=tmp_path, capture_output=True, text=True)
    assert result.stdout == str(release)
    assert result.returncode == 7


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell contract")
def test_missing_release_shows_recovery_without_developer_tools(tmp_path):
    shutil.copy2(ROOT / "Open Auto Outbound.sh", tmp_path)
    result = subprocess.run(["sh", str(tmp_path / "Open Auto Outbound.sh")], input="", capture_output=True, text=True)
    assert result.returncode == 1
    assert "LAUNCH-ROOT" in result.stderr
    assert "Extract the complete desktop ZIP" in result.stderr
