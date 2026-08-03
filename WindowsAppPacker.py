"""WindowsAppPacker — GUI-упаковщик Python-скриптов в standalone Windows EXE.

Графическая обёртка над PyInstaller: выбираешь скрипт, иконку и настройки,
жмёшь кнопку и видишь лог сборки прямо в окне.

Запуск: python WindowsAppPacker.py
Зависимости: pip install customtkinter pyinstaller
Опционально (drag-and-drop): pip install tkinterdnd2
"""
from __future__ import annotations

import base64
import os
import queue
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from tkinter import PhotoImage, filedialog
from typing import Callable, List, Optional, Tuple

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES

    DND_AVAILABLE = True
except ImportError:  # optional dependency
    DND_FILES = None
    DND_AVAILABLE = False

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

PY_FILETYPES = [("Python files", "*.py"), ("All files", "*.*")]
ICO_FILETYPES = [("Icon files", "*.ico"), ("All files", "*.*")]

DATA_SEP = ";" if os.name == "nt" else ":"

OutputCallback = Callable[[str], None]
DoneCallback = Callable[[int], None]

# Rough progress milestones based on recognizable lines in PyInstaller's own
# log output — not a real byte/step count (PyInstaller doesn't expose one),
# just enough to make the bar visibly advance instead of sitting still.
_BUILD_MILESTONES = [
    ("Analyzing ", 10),
    ("Building PYZ", 35),
    ("Building PKG", 55),
    ("Building EXE", 80),
    ("Build complete", 100),
]

# --------------------------------------------------------------------------
# color palette: dark and light variants, both with a blue accent
# --------------------------------------------------------------------------

THEMES = {
    "dark": dict(
        bg="#131316",
        card="#1b1c22",
        input="#212228",
        border="#2a2b33",
        text="#e6e6ea",
        muted="#8b8d98",
        accent="#0b63f6",
        accent_hover="#3c82f8",
        accent_text="#ffffff",
        danger="#e5484d",
        danger_hover="#c93d41",
    ),
    "light": dict(
        bg="#f4f5f8",
        card="#ffffff",
        input="#eef1f6",
        border="#dde2ea",
        text="#15171c",
        muted="#6b7280",
        accent="#2563eb",
        accent_hover="#1d4ed8",
        accent_text="#ffffff",
        danger="#dc2626",
        danger_hover="#b91c1c",
    ),
}


# --------------------------------------------------------------------------
# core: build configuration model
# --------------------------------------------------------------------------


@dataclass
class BuildConfig:
    script_path: str = ""
    output_name: str = ""
    icon_path: str = ""
    output_dir: str = ""

    hide_console: bool = True
    admin_rights: bool = False

    def validate(self) -> List[str]:
        errors = []
        if not self.script_path:
            errors.append("Не выбран исходный .py файл")
        elif not self.script_path.lower().endswith(".py"):
            errors.append("Исходный файл должен иметь расширение .py")

        if self.icon_path and not self.icon_path.lower().endswith(".ico"):
            errors.append("Иконка должна быть файлом .ico")
        return errors


# --------------------------------------------------------------------------
# core: PyInstaller command construction and build job
# --------------------------------------------------------------------------


def build_command(cfg: BuildConfig) -> List[str]:
    cmd = [sys.executable, "-m", "PyInstaller", cfg.script_path, "--noconfirm", "--onefile"]

    cmd.append("--noconsole" if cfg.hide_console else "--console")

    if cfg.output_name:
        cmd += ["--name", cfg.output_name]
    if cfg.icon_path:
        cmd += ["--icon", cfg.icon_path]
    if cfg.admin_rights:
        cmd.append("--uac-admin")
    if cfg.output_dir:
        cmd += ["--distpath", cfg.output_dir]

    return cmd


class BuildJob:
    """Runs a PyInstaller build in a background thread and streams output."""

    def __init__(self, cfg: BuildConfig, on_output: OutputCallback, on_done: DoneCallback):
        self.cfg = cfg
        self.on_output = on_output
        self.on_done = on_done
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._cancelled = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

    def _run(self) -> None:
        try:
            cmd = build_command(self.cfg)
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

        if self._cancelled:
            self.on_output("\nСборка отменена пользователем.\n")
        self.on_done(return_code)


# --------------------------------------------------------------------------
# ui: reusable widgets
# --------------------------------------------------------------------------


def _clear_entry(entry: ctk.CTkEntry) -> None:
    """Clears an entry back to its placeholder.

    CTkEntry.delete() only re-shows the placeholder if the widget has
    already received a real <FocusOut> event at least once (its internal
    "_is_focused" flag defaults to True) — for a field the user never
    clicked into, a plain delete() leaves it blank with no hint at all.
    """
    entry.delete(0, "end")
    entry._activate_placeholder()


class FilePathRow(ctk.CTkFrame):
    """Label + entry + Browse button, with optional drag-and-drop support."""

    def __init__(
        self,
        master,
        label: str,
        filetypes: List[Tuple[str, str]],
        on_change: Optional[Callable[[str], None]] = None,
        pick_folder: bool = False,
        placeholder: str = "Не выбрано",
        **kwargs,
    ):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.filetypes = filetypes
        self.on_change = on_change
        self.pick_folder = pick_folder

        self.grid_columnconfigure(1, weight=1)

        self.label = ctk.CTkLabel(self, text=label, width=LABEL_WIDTH, anchor="w")
        self.label.grid(row=0, column=0, padx=(0, 8), sticky="w")

        self.entry = ctk.CTkEntry(self, placeholder_text=placeholder)
        self.entry.grid(row=0, column=1, sticky="ew")
        self.entry.bind("<KeyRelease>", lambda _e: self._notify())

        self.browse_btn = ctk.CTkButton(self, text="Обзор...", width=90, command=self._browse, corner_radius=20)
        self.browse_btn.grid(row=0, column=2, padx=(8, 0))

        if DND_AVAILABLE:
            try:
                self.entry.drop_target_register(DND_FILES)
                self.entry.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def apply_theme(self, colors: dict) -> None:
        self.label.configure(text_color=colors["text"])
        self.entry.configure(fg_color=colors["input"], border_color=colors["border"], text_color=colors["text"])
        self.browse_btn.configure(fg_color=colors["input"], hover_color=colors["border"], text_color=colors["text"])

    def _browse(self) -> None:
        if self.pick_folder:
            path = filedialog.askdirectory()
        else:
            path = filedialog.askopenfilename(filetypes=self.filetypes)
        if path:
            self.set(path)

    def _on_drop(self, event) -> None:
        raw = event.data
        path = raw.strip("{}") if raw.startswith("{") else raw.split()[0]
        self.set(path)

    def _notify(self) -> None:
        if self.on_change:
            self.on_change(self.get())

    def get(self) -> str:
        return self.entry.get().strip()

    def set(self, value: str) -> None:
        if value:
            self.entry.delete(0, "end")
            self.entry.insert(0, value)
        else:
            _clear_entry(self.entry)
        self._notify()


class LogConsole(ctk.CTkTextbox):
    """Read-only, auto-scrolling textbox used to show build output."""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("wrap", "word")
        kwargs.setdefault("font", ("Consolas", 12))
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("corner_radius", 16)
        super().__init__(master, **kwargs)
        self.tag_config("success", foreground="#22c55e")
        self.tag_config("error", foreground="#ef4444")
        self.configure(state="disabled")

    def apply_theme(self, colors: dict) -> None:
        self.configure(fg_color=colors["card"], text_color=colors["text"], border_color=colors["border"])

    def write(self, text: str, tag: Optional[str] = None) -> None:
        self.configure(state="normal")
        self.insert("end", text, tag)
        self.see("end")
        self.configure(state="disabled")

    def clear(self) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")


class MessageDialog(ctk.CTkToplevel):
    """Themed replacement for tkinter.messagebox — the native dialog ignores
    the app's dark/light palette entirely (it's an OS-drawn window)."""

    def __init__(self, master: ctk.CTk, colors: dict, title: str, message: str):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.configure(fg_color=colors["bg"])
        self.transient(master)

        content = ctk.CTkFrame(self, fg_color=colors["card"], corner_radius=16, border_width=1, border_color=colors["border"])
        content.grid(row=0, column=0, padx=16, pady=16)
        content.grid_columnconfigure(0, weight=1)

        msg_label = ctk.CTkLabel(
            content, text=message, text_color=colors["text"], wraplength=360, justify="center"
        )
        msg_label.grid(row=0, column=0, padx=32, pady=(28, 16))

        ok_btn = ctk.CTkButton(
            content,
            text="OK",
            command=self.destroy,
            width=110,
            height=32,
            corner_radius=20,
            fg_color=colors["accent"],
            hover_color=colors["accent_hover"],
            text_color=colors["accent_text"],
        )
        ok_btn.grid(row=1, column=0, pady=(0, 24))

        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

        self.grab_set()
        self.wait_window()


# --------------------------------------------------------------------------
# ui: main application window
# --------------------------------------------------------------------------


# App's own window/taskbar icon — three gears in the brand blue palette,
# embedded as base64 PNG so the script ships as a single file with no
# external .ico/.png asset alongside it. Two sizes so the OS can pick
# whichever renders sharpest for the titlebar vs. taskbar/alt-tab.
_ICON_PNG_64 = base64.b64decode(
    "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"
)

_ICON_PNG_32 = base64.b64decode(
    "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"
    "RK5CYII="
)


WINDOW_WIDTH = 1000
LABEL_WIDTH = 110  # fits the longest label ("Автор/компания:") with little slack


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("WindowsAppPacker")
        self.geometry(f"{WINDOW_WIDTH}x700")

        # Keep references on the instance — PhotoImage is garbage-collected
        # (and the icon silently vanishes) if nothing else holds onto it.
        self._icon_images = [
            PhotoImage(data=_ICON_PNG_64),
            PhotoImage(data=_ICON_PNG_32),
        ]
        self.iconphoto(True, *self._icon_images)

        self.theme = "dark"
        self.colors = THEMES[self.theme]
        self._themed: List[Tuple[object, str]] = []

        self._build_job: Optional[BuildJob] = None
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._done_queue: "queue.Queue[int]" = queue.Queue()
        self._milestone_idx = 0
        self._log_expanded = True

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self._build_topbar()
        self._build_content()
        self._build_action_bar()
        self._build_log_console()

        self._apply_theme()
        self._check_pyinstaller()
        self._poll_queues()

        # Measure the window's natural size both with and without the log
        # panel instead of guessing constants — the expanded size becomes
        # the starting geometry (log starts open), and the collapsed size
        # becomes minsize() (a hardcoded minsize taller than actually
        # needed is what made shrinking the window feel "stuck" before).
        self.update_idletasks()
        self._expanded_height = self.winfo_reqheight()
        self._apply_log_layout(False)
        self.update_idletasks()
        self._collapsed_height = self.winfo_reqheight()
        self._apply_log_layout(True)

        self.geometry(f"{WINDOW_WIDTH}x{self._expanded_height}")
        self.minsize(760, self._collapsed_height)

    # ------------------------------------------------------------- theming

    def _reg(self, widget, kind: str):
        self._themed.append((widget, kind))
        return widget

    def _apply_theme(self) -> None:
        c = self.colors
        self.configure(fg_color=c["bg"])
        for widget, kind in self._themed:
            if kind == "card":
                widget.configure(fg_color=c["card"], border_color=c["border"])
            elif kind == "label":
                widget.configure(text_color=c["text"])
            elif kind == "muted":
                widget.configure(text_color=c["muted"])
            elif kind == "input":
                widget.configure(fg_color=c["input"], border_color=c["border"], text_color=c["text"])
            elif kind == "checkbox":
                widget.configure(
                    fg_color=c["accent"], hover_color=c["accent_hover"], border_color=c["border"], text_color=c["text"]
                )
            elif kind == "accent_button":
                widget.configure(fg_color=c["accent"], hover_color=c["accent_hover"], text_color=c["accent_text"])
            elif kind == "danger_button":
                widget.configure(fg_color=c["danger"], hover_color=c["danger_hover"], text_color="#ffffff")
            elif kind == "progress":
                widget.configure(fg_color=c["input"], progress_color=c["accent"])
            elif kind == "log_toggle":
                widget.configure(hover_color=c["input"], text_color=c["text"])
            elif kind in ("file_row", "log_console"):
                widget.apply_theme(c)

    def _toggle_theme(self) -> None:
        # Note: deliberately not calling ctk.set_appearance_mode() here — it
        # broadcasts to every CTk widget in the app and forces a full redraw,
        # which looks like the window restarting. Recoloring through the
        # registry above is enough since every color is set explicitly.
        self.theme = "light" if self.theme == "dark" else "dark"
        self.colors = THEMES[self.theme]
        self.theme_toggle_btn.configure(text="Тёмная" if self.theme == "dark" else "Светлая")
        self._apply_theme()

    # ------------------------------------------------------------------ UI

    def _card(self, parent, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, corner_radius=16, border_width=1)
        self._reg(card, "card")
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=13, weight="bold"), anchor="w")
        self._reg(header, "muted")
        header.grid(row=0, column=0, sticky="w", padx=16, pady=(8, 6))
        return card

    def _build_topbar(self) -> None:
        topbar = ctk.CTkFrame(self, fg_color="transparent")
        topbar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))
        topbar.grid_columnconfigure(1, weight=1)

        title_label = ctk.CTkLabel(topbar, text="WindowsAppPacker", font=ctk.CTkFont(size=16, weight="bold"), anchor="w")
        self._reg(title_label, "label")
        title_label.grid(row=0, column=0, sticky="w", padx=(16, 0))

        theme_label = ctk.CTkLabel(topbar, text="Тема:")
        self._reg(theme_label, "label")
        theme_label.grid(row=0, column=2, padx=(0, 8))

        self.theme_toggle_btn = ctk.CTkButton(
            topbar, text="Тёмная", command=self._toggle_theme, height=32, width=100, corner_radius=20
        )
        self._reg(self.theme_toggle_btn, "accent_button")
        self.theme_toggle_btn.grid(row=0, column=3)

    def _build_content(self) -> None:
        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.grid(row=1, column=0, sticky="ew", padx=12, pady=(10, 6))
        wrapper.grid_columnconfigure(0, weight=1)

        # --- "Основное" card ----------------------------------------------
        card_general = self._card(wrapper, "Основное")
        card_general.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        self.script_row = FilePathRow(
            card_general,
            "Скрипт (.py):",
            PY_FILETYPES,
            on_change=self._on_script_change,
            placeholder="Файл не выбран",
        )
        self._reg(self.script_row, "file_row")
        self.script_row.grid(row=1, column=0, sticky="ew", padx=16, pady=4)

        # Extra trailing column matching the Browse button's width (90px +
        # 8px gap), so this row's entry lines up with the FilePathRow rows.
        output_name_row = ctk.CTkFrame(card_general, fg_color="transparent")
        output_name_row.grid(row=2, column=0, sticky="ew", padx=16, pady=4)
        output_name_row.grid_columnconfigure(1, weight=1)
        name_label = ctk.CTkLabel(output_name_row, text="Имя EXE:", width=LABEL_WIDTH, anchor="w")
        self._reg(name_label, "label")
        name_label.grid(row=0, column=0, padx=(0, 8))
        self.output_name_entry = ctk.CTkEntry(output_name_row, placeholder_text="MyApp (без расширения)")
        self._reg(self.output_name_entry, "input")
        self.output_name_entry.grid(row=0, column=1, sticky="ew")
        name_spacer = ctk.CTkLabel(output_name_row, text="", width=90, fg_color="transparent")
        name_spacer.grid(row=0, column=2, padx=(8, 0))

        self.icon_row = FilePathRow(
            card_general, "Иконка (.ico):", ICO_FILETYPES, placeholder="Иконка не выбрана"
        )
        self._reg(self.icon_row, "file_row")
        self.icon_row.grid(row=3, column=0, sticky="ew", padx=16, pady=4)

        self.output_dir_row = FilePathRow(
            card_general, "Папка вывода:", [], pick_folder=True, placeholder="Папка не выбрана"
        )
        self._reg(self.output_dir_row, "file_row")
        self.output_dir_row.grid(row=4, column=0, sticky="ew", padx=16, pady=4)

        options_row = ctk.CTkFrame(card_general, fg_color="transparent")
        options_row.grid(row=5, column=0, sticky="ew", padx=16, pady=(10, 16))
        options_row.grid_columnconfigure(2, weight=1)

        self.hide_console_var = ctk.BooleanVar(value=True)
        hide_console_cb = ctk.CTkCheckBox(options_row, text="Скрыть консоль (cmd)", variable=self.hide_console_var)
        self._reg(hide_console_cb, "checkbox")
        hide_console_cb.grid(row=0, column=0, padx=(0, 20))

        self.admin_var = ctk.BooleanVar(value=False)
        admin_cb = ctk.CTkCheckBox(
            options_row, text="Запуск от имени администратора", variable=self.admin_var
        )
        self._reg(admin_cb, "checkbox")
        admin_cb.grid(row=0, column=1)

        self.clear_general_btn = ctk.CTkButton(
            options_row, text="Очистить", command=self._clear_general, height=32, width=110, corner_radius=20
        )
        self._reg(self.clear_general_btn, "danger_button")
        self.clear_general_btn.grid(row=0, column=2, sticky="e")

    def _build_action_bar(self) -> None:
        action_bar = ctk.CTkFrame(self, fg_color="transparent")
        action_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        action_bar.grid_columnconfigure(2, weight=1)

        self.build_btn = ctk.CTkButton(
            action_bar, text="Собрать EXE", command=self._start_build, height=36, width=140, corner_radius=20
        )
        self._reg(self.build_btn, "accent_button")
        self.build_btn.grid(row=0, column=0)

        self.cancel_btn = ctk.CTkButton(
            action_bar,
            text="Отмена",
            command=self._cancel_build,
            height=36,
            width=100,
            state="disabled",
            corner_radius=20,
        )
        self._reg(self.cancel_btn, "danger_button")
        self.cancel_btn.grid(row=0, column=1, padx=(8, 0))

        self.progress_bar = ctk.CTkProgressBar(action_bar, mode="determinate")
        self.progress_bar.set(0)
        self._reg(self.progress_bar, "progress")
        self.progress_bar.grid(row=0, column=2, sticky="ew", padx=16)

        self.status_label = ctk.CTkLabel(action_bar, text="Готово к сборке", anchor="e")
        self._reg(self.status_label, "muted")
        self.status_label.grid(row=0, column=3)

    def _build_log_console(self) -> None:
        self.log_card = ctk.CTkFrame(self, corner_radius=16, border_width=1)
        self._reg(self.log_card, "card")
        self.log_card.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.log_card.grid_columnconfigure(0, weight=1)
        self.log_card.grid_rowconfigure(1, weight=1)

        header_row = ctk.CTkFrame(self.log_card, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 6))
        header_row.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(header_row, text="Журнал", font=ctk.CTkFont(size=13, weight="bold"), anchor="w")
        self._reg(title_label, "muted")
        title_label.grid(row=0, column=0, sticky="w")

        self.log_toggle_btn = ctk.CTkButton(
            header_row,
            text="⌃",
            command=self._toggle_log,
            width=28,
            height=24,
            corner_radius=20,
            fg_color="transparent",
        )
        self._reg(self.log_toggle_btn, "log_toggle")
        self.log_toggle_btn.grid(row=0, column=1, sticky="e")

        self.log_console = LogConsole(self.log_card, height=100, fg_color="transparent", border_width=0, corner_radius=0)
        self._reg(self.log_console, "log_console")
        self.log_console.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

    def _toggle_log(self) -> None:
        self._set_log_expanded(not self._log_expanded)

    def _apply_log_layout(self, expanded: bool) -> None:
        self._log_expanded = expanded
        self.log_toggle_btn.configure(text="⌃" if expanded else "⌄")
        if expanded:
            self.log_console.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
            self.log_card.grid_rowconfigure(1, weight=1)
            self.grid_rowconfigure(3, weight=1)
        else:
            self.log_console.grid_remove()
            self.log_card.grid_rowconfigure(1, weight=0)
            self.grid_rowconfigure(3, weight=0)

    def _set_log_expanded(self, expanded: bool) -> None:
        self._apply_log_layout(expanded)
        target_height = self._expanded_height if expanded else self._collapsed_height
        self.geometry(f"{self.winfo_width()}x{target_height}")

    # ------------------------------------------------------------- helpers

    def _check_pyinstaller(self) -> None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            self.log_console.write(
                "[внимание] Модуль PyInstaller не найден. Установите его: pip install pyinstaller\n"
            )

    def _on_script_change(self, path: str) -> None:
        if not path:
            return
        if not self.output_name_entry.get().strip():
            name = os.path.splitext(os.path.basename(path))[0]
            self.output_name_entry.insert(0, name)
        if not self.output_dir_row.get():
            self.output_dir_row.set(os.path.dirname(os.path.abspath(path)))

    def _collect_config(self) -> BuildConfig:
        return BuildConfig(
            script_path=self.script_row.get(),
            output_name=self.output_name_entry.get().strip(),
            icon_path=self.icon_row.get(),
            output_dir=self.output_dir_row.get(),
            hide_console=self.hide_console_var.get(),
            admin_rights=self.admin_var.get(),
        )

    def _clear_general(self) -> None:
        default = BuildConfig()
        self.script_row.set(default.script_path)
        _clear_entry(self.output_name_entry)
        self.icon_row.set(default.icon_path)
        self.output_dir_row.set(default.output_dir)
        self.hide_console_var.set(default.hide_console)
        self.admin_var.set(default.admin_rights)

    # -------------------------------------------------------------- build

    def _start_build(self) -> None:
        cfg = self._collect_config()
        errors = cfg.validate()
        if errors:
            MessageDialog(self, self.colors, "Проверьте настройки", "\n".join(errors))
            return

        if not self._log_expanded:
            self._set_log_expanded(True)

        self.log_console.clear()
        self._milestone_idx = 0
        self.progress_bar.set(0)
        self.build_btn.configure(state="disabled")
        self.clear_general_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.status_label.configure(text="Идёт сборка... 0%")

        self._build_job = BuildJob(
            cfg,
            on_output=lambda line: self._log_queue.put(line),
            on_done=lambda code: self._done_queue.put(code),
        )
        self._build_job.start()

    def _cancel_build(self) -> None:
        if self._build_job:
            self._build_job.cancel()
            self.status_label.configure(text="Отмена...")

    def _advance_progress(self, line: str) -> None:
        while self._milestone_idx < len(_BUILD_MILESTONES):
            marker, pct = _BUILD_MILESTONES[self._milestone_idx]
            if marker not in line:
                break
            self._milestone_idx += 1
            self.progress_bar.set(pct / 100)
            self.status_label.configure(text=f"Идёт сборка... {pct}%")

    def _poll_queues(self) -> None:
        try:
            while True:
                line = self._log_queue.get_nowait()
                self.log_console.write(line)
                self._advance_progress(line)
        except queue.Empty:
            pass

        try:
            code = self._done_queue.get_nowait()
        except queue.Empty:
            code = None

        if code is not None:
            self.build_btn.configure(state="normal")
            self.clear_general_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            if code == 0:
                self.progress_bar.set(1.0)
                self.status_label.configure(text="Завершено!")
                self.log_console.write("\nСборка успешно завершена!\n", tag="success")
            else:
                self.status_label.configure(text="Ошибка!")
                self.log_console.write(f"\nСборка завершилась с ошибкой (код {code}).\n", tag="error")
            self._build_job = None

        self.after(150, self._poll_queues)


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
