"""Reusable widgets for the packer UI: file pickers, data-file list, log console."""
from __future__ import annotations

from tkinter import filedialog
from typing import Callable, List, Optional, Tuple

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES

    DND_AVAILABLE = True
except ImportError:  # optional dependency
    DND_FILES = None
    DND_AVAILABLE = False


class FilePathRow(ctk.CTkFrame):
    """Label + entry + Browse button, with optional drag-and-drop support."""

    def __init__(
        self,
        master,
        label: str,
        filetypes: List[Tuple[str, str]],
        on_change: Optional[Callable[[str], None]] = None,
        pick_folder: bool = False,
        **kwargs,
    ):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.filetypes = filetypes
        self.on_change = on_change
        self.pick_folder = pick_folder

        self.grid_columnconfigure(1, weight=1)

        self.label = ctk.CTkLabel(self, text=label, width=110, anchor="w")
        self.label.grid(row=0, column=0, padx=(0, 8), sticky="w")

        self.entry = ctk.CTkEntry(self, placeholder_text="Перетащите файл сюда или нажмите «Обзор»")
        self.entry.grid(row=0, column=1, sticky="ew")
        self.entry.bind("<KeyRelease>", lambda _e: self._notify())

        self.browse_btn = ctk.CTkButton(self, text="Обзор...", width=90, command=self._browse)
        self.browse_btn.grid(row=0, column=2, padx=(8, 0))

        if DND_AVAILABLE:
            try:
                self.entry.drop_target_register(DND_FILES)
                self.entry.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

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
        self.entry.delete(0, "end")
        self.entry.insert(0, value)
        self._notify()


class DataFilesPanel(ctk.CTkFrame):
    """Editable list of (source path -> destination folder inside the EXE)."""

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(0, weight=1)

        self._entries: List[Tuple[str, str]] = []

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        controls.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(controls, text="+ Добавить файл", command=self._add_file).grid(
            row=0, column=1, padx=(6, 0)
        )
        ctk.CTkButton(controls, text="+ Добавить папку", command=self._add_folder).grid(
            row=0, column=2, padx=(6, 0)
        )
        ctk.CTkButton(controls, text="Удалить выбранное", command=self._remove_selected).grid(
            row=0, column=3, padx=(6, 0)
        )

        self.list_frame = ctk.CTkScrollableFrame(self, height=140, label_text="Дополнительные файлы/папки")
        self.list_frame.grid(row=1, column=0, sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)

        self._row_widgets = []
        self._selected_index: Optional[int] = None

    def _add_file(self) -> None:
        path = filedialog.askopenfilename()
        if path:
            self._add_entry(path, ".")

    def _add_folder(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self._add_entry(path, ".")

    def _add_entry(self, src: str, dest: str) -> None:
        self._entries.append((src, dest))
        self._refresh()

    def _remove_selected(self) -> None:
        if self._selected_index is not None and 0 <= self._selected_index < len(self._entries):
            del self._entries[self._selected_index]
            self._selected_index = None
            self._refresh()

    def _select(self, index: int) -> None:
        self._selected_index = index
        self._refresh()

    def _refresh(self) -> None:
        for widget in self._row_widgets:
            widget.destroy()
        self._row_widgets.clear()

        for i, (src, dest) in enumerate(self._entries):
            selected = i == self._selected_index
            row = ctk.CTkFrame(
                self.list_frame,
                fg_color=("gray75", "gray28") if selected else "transparent",
            )
            row.grid(row=i, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)
            row.bind("<Button-1>", lambda _e, idx=i: self._select(idx))

            text = f"{src}  →  /{dest}"
            lbl = ctk.CTkLabel(row, text=text, anchor="w")
            lbl.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
            lbl.bind("<Button-1>", lambda _e, idx=i: self._select(idx))

            self._row_widgets.append(row)

    def get_entries(self) -> List[Tuple[str, str]]:
        return list(self._entries)

    def set_entries(self, entries: List[Tuple[str, str]]) -> None:
        self._entries = list(entries)
        self._selected_index = None
        self._refresh()


class LogConsole(ctk.CTkTextbox):
    """Read-only, auto-scrolling textbox used to show build output."""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("wrap", "word")
        kwargs.setdefault("font", ("Consolas", 12))
        super().__init__(master, **kwargs)
        self.configure(state="disabled")

    def write(self, text: str) -> None:
        self.configure(state="normal")
        self.insert("end", text)
        self.see("end")
        self.configure(state="disabled")

    def clear(self) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")
