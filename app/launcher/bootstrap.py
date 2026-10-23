#!/usr/bin/env python3
"""Run the manifest entry module after the native supervisor prepares runtime state."""

from __future__ import annotations

import importlib
import json
import os
import re
import runpy
import sys
from pathlib import Path


def main() -> int:
    sys.dont_write_bytecode = True
    try:
        app_root = Path(os.environ["GOOSNAV_APP_ROOT"]).resolve(strict=True)
        manifest = json.loads((app_root / "launcher" / "manifest.json").read_text("utf-8"))
        entry_module = manifest["entry_module"]
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", entry_module):
            raise ValueError("Invalid application entry module")
        os.chdir(app_root)
        sys.path.insert(0, str(app_root / "src"))
        if os.environ.get("GOOSNAV_VALIDATE_ONLY") == "1":
            importlib.import_module(entry_module)
        else:
            runpy.run_module(entry_module, run_name="__main__", alter_sys=True)
    except SystemExit:
        raise
    except Exception as error:
        # No document contents, credentials or provider output in bootstrap logs.
        print(f"APP-START: Application startup failed ({type(error).__name__}).", file=sys.stderr)
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
