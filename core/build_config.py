"""Data model describing a single EXE build configuration."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Tuple


@dataclass
class BuildConfig:
    script_path: str = ""
    output_name: str = ""
    icon_path: str = ""
    output_dir: str = "dist"

    onefile: bool = True
    hide_console: bool = True
    admin_rights: bool = False

    # list of (source_path, dest_folder_inside_exe)
    data_files: List[Tuple[str, str]] = field(default_factory=list)

    version: str = "1.0.0.0"
    description: str = ""
    author: str = ""
    product_name: str = ""

    extra_args: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["data_files"] = [list(pair) for pair in self.data_files]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "BuildConfig":
        cfg = cls()
        for key, value in data.items():
            if key == "data_files":
                value = [tuple(pair) for pair in value]
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    def validate(self) -> List[str]:
        errors = []
        if not self.script_path:
            errors.append("Не выбран исходный .py файл")
        elif not self.script_path.lower().endswith(".py"):
            errors.append("Исходный файл должен иметь расширение .py")

        if self.icon_path and not self.icon_path.lower().endswith(".ico"):
            errors.append("Иконка должна быть файлом .ico")

        for src, _dest in self.data_files:
            if not src:
                errors.append("Пустой путь в списке дополнительных файлов")
        return errors
