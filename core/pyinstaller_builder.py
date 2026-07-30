"""Builds the PyInstaller command line and runs it as a monitored subprocess."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import threading
from typing import Callable, List, Optional

from core.build_config import BuildConfig
from core.version_info import write_version_file

DATA_SEP = ";" if os.name == "nt" else ":"

OutputCallback = Callable[[str], None]
DoneCallback = Callable[[int], None]


def build_command(cfg: BuildConfig, version_file: Optional[str]) -> List[str]:
    cmd = [sys.executable, "-m", "PyInstaller", cfg.script_path, "--noconfirm"]

    cmd.append("--onefile" if cfg.onefile else "--onedir")
    cmd.append("--noconsole" if cfg.hide_console else "--console")

    if cfg.output_name:
        cmd += ["--name", cfg.output_name]
    if cfg.icon_path:
        cmd += ["--icon", cfg.icon_path]
    if cfg.admin_rights:
        cmd.append("--uac-admin")
    if cfg.output_dir:
        cmd += ["--distpath", cfg.output_dir]
    if version_file:
        cmd += ["--version-file", version_file]

    for src, dest in cfg.data_files:
        dest = dest or "."
        cmd += ["--add-data", f"{src}{DATA_SEP}{dest}"]

    if cfg.extra_args.strip():
        cmd += shlex.split(cfg.extra_args)

    return cmd


class BuildJob:
    """Runs a PyInstaller build in a background thread and streams output."""

    def __init__(self, cfg: BuildConfig, on_output: OutputCallback, on_done: DoneCallback):
        self.cfg = cfg
        self.on_output = on_output
        self.on_done = on_done
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._version_file_path: Optional[str] = None
        self._cancelled = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

    def _run(self) -> None:
        version_file = None
        try:
            if any([self.cfg.description, self.cfg.author, self.cfg.product_name, self.cfg.version]):
                fd, version_file = tempfile.mkstemp(suffix="_version_info.txt", text=True)
                os.close(fd)
                write_version_file(
                    version_file,
                    version=self.cfg.version,
                    description=self.cfg.description,
                    author=self.cfg.author,
                    product_name=self.cfg.product_name,
                    output_name=self.cfg.output_name,
                )
                self._version_file_path = version_file

            cmd = build_command(self.cfg, version_file)
            self.on_output("$ " + " ".join(shlex.quote(part) for part in cmd) + "\n")

            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=os.path.dirname(os.path.abspath(self.cfg.script_path)) or None,
            )

            assert self._process.stdout is not None
            for line in self._process.stdout:
                self.on_output(line)

            return_code = self._process.wait()
        except Exception as exc:  # surfaced to the log console
            self.on_output(f"[ошибка] {exc}\n")
            return_code = -1
        finally:
            if version_file and os.path.exists(version_file):
                try:
                    os.remove(version_file)
                except OSError:
                    pass

        if self._cancelled:
            self.on_output("\nСборка отменена пользователем.\n")
        self.on_done(return_code)
