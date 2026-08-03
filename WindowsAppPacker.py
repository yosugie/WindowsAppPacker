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
        if getattr(sys, "frozen", False):
            # A packaged WindowsAppPacker.exe has no real Python interpreter
            # behind it — sys.executable then points at the EXE itself, so
            # build_command() would try to re-launch WindowsAppPacker as if
            # it were "python -m PyInstaller ...", which just opens another
            # copy of this same app instead of building anything.
            return [
                "Собранный EXE-файл WindowsAppPacker не может сам запускать "
                "сборку — для этого нужен Python с установленным PyInstaller. "
                "Запустите WindowsAppPacker.py через python вместо EXE."
            ]

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
        # Every top-level window (this dialog included) has its own icon
        # state in Tk — inheriting the main window's icon isn't automatic,
        # so it has to be set here too, reusing master's PhotoImages.
        _apply_window_icon(self, master._icon_images)

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


# App's own window/taskbar icon — an unboxing/package icon (arrow
# dropping into an open box), recolored pixel-for-pixel from a reference
# the user supplied into the brand blue accent — embedded as base64 PNG
# (two sizes, for titlebar vs. taskbar/alt-tab) and as a real .ico, so the
# script ships as a single file with no external icon asset alongside it.
_ICON_PNG_64 = base64.b64decode(
    "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"
    "IdcOpGrQqwAAAABJRU5ErkJggg=="
)

_ICON_PNG_32 = base64.b64decode(
    "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"
    "BveJ7H+L7GfIWkQiLAAAAABJRU5ErkJggg=="
)

_ICON_ICO = base64.b64decode(
    "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"
    "RU5ErkJggg=="
)

_icon_ico_path: Optional[str] = None


def _apply_window_icon(window, icon_images: List[PhotoImage]) -> None:
    """Sets the app icon on a Tk window (App's own window or a CTkToplevel
    dialog) — each top-level window has its own icon state in Tk, so this
    has to be called per-window, not just once on the main App."""
    window.iconphoto(True, *icon_images)
    if sys.platform.startswith("win"):
        # CustomTkinter schedules its own default titlebar icon 200ms after
        # a window is created (CTk and CTkToplevel both do this) unless
        # iconbitmap() was already called by then — iconphoto() alone
        # doesn't trip that flag. Write the .ico out once and reuse the
        # path for every window instead of writing it again each time.
        global _icon_ico_path
        if _icon_ico_path is None:
            _icon_ico_path = os.path.join(tempfile.gettempdir(), "windowsapppacker_icon.ico")
            with open(_icon_ico_path, "wb") as f:
                f.write(_ICON_ICO)
        window.iconbitmap(_icon_ico_path)


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
        _apply_window_icon(self, self._icon_images)

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
        if getattr(sys, "frozen", False):
            self.log_console.write(
                "[внимание] Это собранный EXE — сборка недоступна, нужен Python "
                "с установленным PyInstaller. Запустите WindowsAppPacker.py через python.\n"
            )
            return
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
