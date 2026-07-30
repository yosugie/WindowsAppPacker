"""Save/load named build presets as JSON files under the user's home directory."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

from core.build_config import BuildConfig

PRESETS_DIR = Path.home() / ".windowsapppacker" / "presets"

_SAFE_NAME_RE = re.compile(r"[^\w\-. ]+", re.UNICODE)


def _slugify(name: str) -> str:
    return _SAFE_NAME_RE.sub("_", name).strip() or "preset"


def _ensure_dir() -> None:
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)


def list_presets() -> List[str]:
    if not PRESETS_DIR.exists():
        return []
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json"))


def save_preset(name: str, cfg: BuildConfig) -> Path:
    _ensure_dir()
    path = PRESETS_DIR / f"{_slugify(name)}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, ensure_ascii=False, indent=2)
    return path


def load_preset(name: str) -> BuildConfig:
    path = PRESETS_DIR / f"{_slugify(name)}.json"
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return BuildConfig.from_dict(data)


def delete_preset(name: str) -> None:
    path = PRESETS_DIR / f"{_slugify(name)}.json"
    if path.exists():
        path.unlink()
