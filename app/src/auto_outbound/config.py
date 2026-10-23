from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir


def data_root() -> Path:
    override = os.getenv("AUTO_OUTBOUND_DATA") or os.getenv("GOOSNAV_APP_DATA")
    path = Path(override or user_data_dir("AutoOutbound", appauthor=False)).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def static_root() -> Path:
    return Path(__file__).resolve().parents[2] / "static"


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)
