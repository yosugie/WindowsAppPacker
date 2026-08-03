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
import tempfile
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

# Also embed a real multi-size .ico — CustomTkinter schedules its own
# default titlebar icon on Windows shortly after startup unless
# iconbitmap() has already been called by then (see App.__init__), and
# iconbitmap() on Windows needs an actual .ico file, not a PNG PhotoImage.
_ICON_ICO = base64.b64decode(
    "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"
    "+wAAAABJRU5ErkJggg=="
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

        if sys.platform.startswith("win"):
            # iconphoto() alone isn't enough here: CustomTkinter's own
            # __init__ (already run via super().__init__() above) schedules
            # self.after(200, self._windows_set_titlebar_icon), which
            # replaces the window icon with CustomTkinter's own logo unless
            # iconbitmap() was already called by then. Calling it here,
            # synchronously and well before that 200ms fires, is what
            # actually keeps our icon on screen.
            icon_path = os.path.join(tempfile.gettempdir(), "windowsapppacker_icon.ico")
            with open(icon_path, "wb") as f:
                f.write(_ICON_ICO)
            self.iconbitmap(icon_path)

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
