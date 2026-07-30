"""Main application window for WindowsAppPacker."""
from __future__ import annotations

import os
import queue
from tkinter import messagebox, simpledialog
from typing import Optional

import customtkinter as ctk

from core import presets
from core.build_config import BuildConfig
from core.pyinstaller_builder import BuildJob
from ui.widgets import DataFilesPanel, FilePathRow, LogConsole

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

PY_FILETYPES = [("Python files", "*.py"), ("All files", "*.*")]
ICO_FILETYPES = [("Icon files", "*.ico"), ("All files", "*.*")]


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("WindowsAppPacker — упаковщик Python в EXE")
        self.geometry("900x720")
        self.minsize(760, 600)

        self._build_job: Optional[BuildJob] = None
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._done_queue: "queue.Queue[int]" = queue.Queue()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_preset_bar()
        self._build_tabs()
        self._build_log_console()

        self._check_pyinstaller()
        self._poll_queues()

    # ------------------------------------------------------------------ UI

    def _build_preset_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(bar, text="Пресет:").grid(row=0, column=0, padx=(0, 6))

        self.preset_var = ctk.StringVar(value="")
        self.preset_menu = ctk.CTkOptionMenu(
            bar, variable=self.preset_var, values=presets.list_presets() or ["(нет пресетов)"]
        )
        self.preset_menu.grid(row=0, column=1, sticky="ew")

        ctk.CTkButton(bar, text="Загрузить", width=90, command=self._load_preset).grid(
            row=0, column=2, padx=(6, 0)
        )
        ctk.CTkButton(bar, text="Сохранить как...", width=130, command=self._save_preset).grid(
            row=0, column=3, padx=(6, 0)
        )
        ctk.CTkButton(bar, text="Удалить", width=90, command=self._delete_preset).grid(
            row=0, column=4, padx=(6, 0)
        )

    def _build_tabs(self) -> None:
        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        tab_general = self.tabview.add("Основное")
        tab_data = self.tabview.add("Доп. файлы")
        tab_meta = self.tabview.add("Метаданные")
        tab_adv = self.tabview.add("Дополнительно")

        # --- General tab -------------------------------------------------
        tab_general.grid_columnconfigure(0, weight=1)

        self.script_row = FilePathRow(tab_general, "Скрипт (.py):", PY_FILETYPES, on_change=self._on_script_change)
        self.script_row.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))

        self.output_name_row = ctk.CTkFrame(tab_general, fg_color="transparent")
        self.output_name_row.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        self.output_name_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.output_name_row, text="Имя EXE:", width=110, anchor="w").grid(row=0, column=0)
        self.output_name_entry = ctk.CTkEntry(
            self.output_name_row, placeholder_text="MyApp (без расширения)"
        )
        self.output_name_entry.grid(row=0, column=1, sticky="ew")

        self.icon_row = FilePathRow(tab_general, "Иконка (.ico):", ICO_FILETYPES)
        self.icon_row.grid(row=2, column=0, sticky="ew", padx=10, pady=4)

        self.output_dir_row = FilePathRow(tab_general, "Папка вывода:", [], pick_folder=True)
        self.output_dir_row.set("dist")
        self.output_dir_row.grid(row=3, column=0, sticky="ew", padx=10, pady=4)

        options_row = ctk.CTkFrame(tab_general, fg_color="transparent")
        options_row.grid(row=4, column=0, sticky="ew", padx=10, pady=(10, 4))

        ctk.CTkLabel(options_row, text="Тип сборки:").grid(row=0, column=0, padx=(0, 8))
        self.build_type_var = ctk.StringVar(value="Onefile")
        ctk.CTkSegmentedButton(
            options_row, values=["Onefile", "Onedir"], variable=self.build_type_var
        ).grid(row=0, column=1, padx=(0, 20))

        self.hide_console_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            options_row, text="Скрыть окно консоли (GUI-приложение)", variable=self.hide_console_var
        ).grid(row=0, column=2, padx=(0, 20))

        self.admin_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            options_row, text="Требовать права администратора (UAC)", variable=self.admin_var
        ).grid(row=0, column=3)

        # --- Data files tab ----------------------------------------------
        tab_data.grid_columnconfigure(0, weight=1)
        tab_data.grid_rowconfigure(0, weight=1)
        self.data_files_panel = DataFilesPanel(tab_data)
        self.data_files_panel.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        # --- Metadata tab --------------------------------------------------
        tab_meta.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            tab_meta,
            text="Эти данные отображаются во вкладке «Подробности» свойств EXE в Windows.",
            wraplength=600,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 10))

        self.version_entry = self._meta_field(tab_meta, 1, "Версия:", "1.0.0.0")
        self.product_name_entry = self._meta_field(tab_meta, 2, "Имя продукта:", "")
        self.author_entry = self._meta_field(tab_meta, 3, "Автор/компания:", "")
        self.description_entry = self._meta_field(tab_meta, 4, "Описание:", "")

        # --- Advanced tab --------------------------------------------------
        tab_adv.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(tab_adv, text="Дополнительные аргументы PyInstaller:").grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4)
        )
        self.extra_args_entry = ctk.CTkEntry(
            tab_adv, placeholder_text="например: --hidden-import requests --exclude-module tkinter"
        )
        self.extra_args_entry.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))

    def _meta_field(self, parent, row: int, label: str, placeholder: str) -> ctk.CTkEntry:
        ctk.CTkLabel(parent, text=label, width=130, anchor="w").grid(
            row=row, column=0, sticky="w", padx=10, pady=4
        )
        entry = ctk.CTkEntry(parent, placeholder_text=placeholder)
        entry.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=4)
        return entry

    def _build_log_console(self) -> None:
        # Action bar (build/cancel/progress) sits above the log console.
        action_bar = ctk.CTkFrame(self, fg_color="transparent")
        action_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        action_bar.grid_columnconfigure(2, weight=1)

        self.build_btn = ctk.CTkButton(
            action_bar, text="Собрать EXE", command=self._start_build, height=36, width=140
        )
        self.build_btn.grid(row=0, column=0)

        self.cancel_btn = ctk.CTkButton(
            action_bar,
            text="Отмена",
            command=self._cancel_build,
            height=36,
            width=100,
            state="disabled",
            fg_color="#8a2d2d",
            hover_color="#6e2323",
        )
        self.cancel_btn.grid(row=0, column=1, padx=(8, 0))

        self.progress_bar = ctk.CTkProgressBar(action_bar, mode="indeterminate")
        self.progress_bar.grid(row=0, column=2, sticky="ew", padx=16)

        self.status_label = ctk.CTkLabel(action_bar, text="Готово к сборке", anchor="e")
        self.status_label.grid(row=0, column=3)

        self.grid_rowconfigure(3, weight=1)
        self.log_console = LogConsole(self)
        self.log_console.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))

    # ------------------------------------------------------------- helpers

    def _check_pyinstaller(self) -> None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            self.log_console.write(
                "[внимание] Модуль PyInstaller не найден. Установите его: pip install pyinstaller\n"
            )

    def _on_script_change(self, path: str) -> None:
        if path and not self.output_name_entry.get().strip():
            name = os.path.splitext(os.path.basename(path))[0]
            self.output_name_entry.insert(0, name)

    def _collect_config(self) -> BuildConfig:
        return BuildConfig(
            script_path=self.script_row.get(),
            output_name=self.output_name_entry.get().strip(),
            icon_path=self.icon_row.get(),
            output_dir=self.output_dir_row.get() or "dist",
            onefile=self.build_type_var.get() == "Onefile",
            hide_console=self.hide_console_var.get(),
            admin_rights=self.admin_var.get(),
            data_files=self.data_files_panel.get_entries(),
            version=self.version_entry.get().strip() or "1.0.0.0",
            description=self.description_entry.get().strip(),
            author=self.author_entry.get().strip(),
            product_name=self.product_name_entry.get().strip(),
            extra_args=self.extra_args_entry.get().strip(),
        )

    def _apply_config(self, cfg: BuildConfig) -> None:
        self.script_row.set(cfg.script_path)
        self.output_name_entry.delete(0, "end")
        self.output_name_entry.insert(0, cfg.output_name)
        self.icon_row.set(cfg.icon_path)
        self.output_dir_row.set(cfg.output_dir)
        self.build_type_var.set("Onefile" if cfg.onefile else "Onedir")
        self.hide_console_var.set(cfg.hide_console)
        self.admin_var.set(cfg.admin_rights)
        self.data_files_panel.set_entries(cfg.data_files)
        self.version_entry.delete(0, "end")
        self.version_entry.insert(0, cfg.version)
        self.description_entry.delete(0, "end")
        self.description_entry.insert(0, cfg.description)
        self.author_entry.delete(0, "end")
        self.author_entry.insert(0, cfg.author)
        self.product_name_entry.delete(0, "end")
        self.product_name_entry.insert(0, cfg.product_name)
        self.extra_args_entry.delete(0, "end")
        self.extra_args_entry.insert(0, cfg.extra_args)

    # ------------------------------------------------------------- presets

    def _refresh_preset_menu(self, select: Optional[str] = None) -> None:
        names = presets.list_presets()
        self.preset_menu.configure(values=names or ["(нет пресетов)"])
        if select and select in names:
            self.preset_var.set(select)
        elif names:
            self.preset_var.set(names[0])
        else:
            self.preset_var.set("(нет пресетов)")

    def _load_preset(self) -> None:
        name = self.preset_var.get()
        if not name or name == "(нет пресетов)":
            return
        try:
            cfg = presets.load_preset(name)
        except Exception as exc:
            messagebox.showerror("Ошибка", f"Не удалось загрузить пресет: {exc}")
            return
        self._apply_config(cfg)

    def _save_preset(self) -> None:
        name = simpledialog.askstring("Сохранить пресет", "Имя пресета:")
        if not name:
            return
        presets.save_preset(name, self._collect_config())
        self._refresh_preset_menu(select=name)

    def _delete_preset(self) -> None:
        name = self.preset_var.get()
        if not name or name == "(нет пресетов)":
            return
        if messagebox.askyesno("Удалить пресет", f"Удалить пресет «{name}»?"):
            presets.delete_preset(name)
            self._refresh_preset_menu()

    # -------------------------------------------------------------- build

    def _start_build(self) -> None:
        cfg = self._collect_config()
        errors = cfg.validate()
        if errors:
            messagebox.showerror("Проверьте настройки", "\n".join(errors))
            return

        self.log_console.clear()
        self.build_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.status_label.configure(text="Идёт сборка...")
        self.progress_bar.start()

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

    def _poll_queues(self) -> None:
        try:
            while True:
                line = self._log_queue.get_nowait()
                self.log_console.write(line)
        except queue.Empty:
            pass

        try:
            code = self._done_queue.get_nowait()
        except queue.Empty:
            code = None

        if code is not None:
            self.progress_bar.stop()
            self.progress_bar.set(0)
            self.build_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            if code == 0:
                self.status_label.configure(text="Сборка успешно завершена")
            else:
                self.status_label.configure(text=f"Сборка завершилась с ошибкой (код {code})")
            self._build_job = None

        self.after(150, self._poll_queues)
