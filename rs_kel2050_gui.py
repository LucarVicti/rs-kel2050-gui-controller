from __future__ import annotations

import argparse
import csv
import json
import platform
import queue
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from kel2050_client import (
    BAUD_RATES,
    DeviceLimits,
    KEL2050Client,
    PortInfo,
    Snapshot,
    list_serial_ports,
)

try:
    import winreg
except ImportError:
    winreg = None

try:
    import darkdetect
except ImportError:
    darkdetect = None


PRESET_FILE = Path.home() / ".rs_kel2050_presets.json"
DEFAULT_PRESETS = {
    "CC": [
        {"label": "0.5 A", "value": 0.500},
        {"label": "1.0 A", "value": 1.000},
        {"label": "5.0 A", "value": 5.000},
        {"label": "10.0 A", "value": 10.000},
    ],
    "CV": [
        {"label": "5.0 V", "value": 5.000},
        {"label": "12.0 V", "value": 12.000},
        {"label": "24.0 V", "value": 24.000},
        {"label": "48.0 V", "value": 48.000},
    ],
    "CR": [
        {"label": "5 Ω", "value": 5.000},
        {"label": "10 Ω", "value": 10.000},
        {"label": "50 Ω", "value": 50.000},
        {"label": "100 Ω", "value": 100.000},
    ],
    "CW": [
        {"label": "10 W", "value": 10.00},
        {"label": "50 W", "value": 50.00},
        {"label": "100 W", "value": 100.00},
        {"label": "250 W", "value": 250.00},
    ],
}

MODE_META = {
    "CC": ("Current (A)", "A", "i_set", "i_min", "i_max"),
    "CV": ("Voltage (V)", "V", "v_set", "v_min", "v_max"),
    "CR": ("Resistance (Ω)", "OHM", "r_set", "r_min", "r_max"),
    "CW": ("Power (W)", "W", "p_set", "p_min", "p_max"),
}


def _ui_font(size: int, weight: str = "normal", mono: bool = False) -> tuple[str, int, str] | tuple[str, int]:
    sys_name = platform.system()
    if mono:
        family = "Menlo" if sys_name == "Darwin" else ("Consolas" if sys_name == "Windows" else "DejaVu Sans Mono")
    else:
        family = "SF Pro Text" if sys_name == "Darwin" else ("Segoe UI" if sys_name == "Windows" else "DejaVu Sans")
    if weight == "normal":
        return (family, size)
    return (family, size, weight)


class MetricTile(ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        title: str,
        value_var: tk.StringVar,
        value_style: str = "TileValue.TLabel",
    ) -> None:
        super().__init__(master, padding=(14, 10), style="Card.TFrame")
        ttk.Label(self, text=title, style="TileTitle.TLabel").pack(anchor="w")
        ttk.Label(self, textvariable=value_var, style=value_style).pack(anchor="w", pady=(6, 0))



class RSKEL2050App(tk.Tk):
    def __init__(self, preselected_port: str = "") -> None:
        super().__init__()
        self.title("RS-KEL2050 Electronic Load Controller")
        self.geometry("1440x920")
        self.minsize(1200, 820)

        self.device: Optional[KEL2050Client] = None
        self.limits = DeviceLimits()
        self.ports_cache: list[PortInfo] = []
        self.stop_event = threading.Event()
        self.poll_thread: Optional[threading.Thread] = None
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self.theme_mode = "system"
        self.current_theme = self._resolve_theme(self.theme_mode)
        self.theme_toggle_btn: Optional[ttk.Button] = None

        self._csv_file = None
        self._csv_writer = None
        self._csv_path: Optional[Path] = None

        self.port_var = tk.StringVar(value=preselected_port)
        self.baud_var = tk.StringVar(value="115200")
        self.line_end_var = tk.StringVar(value="LF (\\n)")
        self.connection_var = tk.StringVar(value="Disconnected")
        self.mode_status_var = tk.StringVar(value="--")
        self.input_status_var = tk.StringVar(value="OFF")
        self.polarity_var = tk.StringVar(value="OK")
        self.idn_var = tk.StringVar(value="--")
        self.footer_var = tk.StringVar(value="Ready.")
        self.tx_last_var = tk.StringVar(value="TX: -")
        self.rx_last_var = tk.StringVar(value="RX: -")

        self.v_meas_var = tk.StringVar(value="--.--- V")
        self.i_meas_var = tk.StringVar(value="--.---- A")
        self.p_meas_var = tk.StringVar(value="--.--- W")
        self.temp_var = tk.StringVar(value="--.- °C")
        self.v_set_read_var = tk.StringVar(value="--.--- V")
        self.i_set_read_var = tk.StringVar(value="--.---- A")
        self.r_set_read_var = tk.StringVar(value="--.--- Ω")
        self.p_set_read_var = tk.StringVar(value="--.--- W")
        self.limits_info_var = tk.StringVar(value="Limits: 0.1–80 V | 0–85 A | 0.2–7500 Ω | 0–1200 W")

        self.active_mode_var = tk.StringVar(value="CC")
        self.setpoint_label_var = tk.StringVar(value="Setpoint Current (A)")
        self.setpoint_var = tk.StringVar(value="1.000")
        self.hw_limit_var = tk.StringVar(value="0.000 – 85.000 A (read-only)")
        self.csv_status_var = tk.StringVar(value="CSV Logger: Idle")
        self._action_gen = 0
        self._action_lock = threading.Lock()
        self._logo_label: Optional[tk.Label] = None

        self.dyn_mode_var = tk.StringVar(value="CC")
        self.dyn_val1_var = tk.StringVar(value="1.000")
        self.dyn_val2_var = tk.StringVar(value="2.000")
        self.dyn_rise_var = tk.StringVar(value="0.500")
        self.dyn_fall_var = tk.StringVar(value="0.500")
        self.dyn_freq_var = tk.StringVar(value="100.0")
        self.dyn_duty_var = tk.StringVar(value="50.0")
        self.dyn_readback_var = tk.StringVar(value="Dynamic: --")

        self.batt_slot_var = tk.StringVar(value="1")
        self.batt_range_var = tk.StringVar(value="30.0")
        self.batt_curr_var = tk.StringVar(value="2.000")
        self.batt_vcut_var = tk.StringVar(value="10.500")
        self.batt_capcut_var = tk.StringVar(value="5.000")
        self.batt_timelimit_var = tk.StringVar(value="60")
        self.batt_readback_var = tk.StringVar(value="Time: -- | Cap: --")

        self.mem_slot_var = tk.StringVar(value="1")
        self.beep_var = tk.BooleanVar(value=True)
        self.lock_var = tk.BooleanVar(value=False)
        self.comp_var = tk.BooleanVar(value=False)
        self.ext_trig_var = tk.StringVar(value="0 - OFF")

        self.manual_cmd_var = tk.StringVar(value="*IDN?")
        self.manual_query_var = tk.BooleanVar(value=True)

        # Sequence / Timer state & variables
        self.seq_steps: list[dict[str, object]] = [
            {"mode": "CC", "value": 1.0, "duration": 10.0},
            {"mode": "OFF", "value": 0.0, "duration": 5.0},
            {"mode": "CV", "value": 12.0, "duration": 10.0},
        ]
        self.seq_stop_event = threading.Event()
        self.seq_pause_event = threading.Event()
        self.seq_thread: Optional[threading.Thread] = None
        self.seq_mode_var = tk.StringVar(value="CC")
        self.seq_val_var = tk.StringVar(value="1.000")
        self.seq_dur_var = tk.StringVar(value="10")
        self.seq_repeat_var = tk.StringVar(value="1")
        self.seq_dwell_var = tk.StringVar(value="1.0")
        self.seq_safe_stop_var = tk.BooleanVar(value=True)
        self.seq_status_var = tk.StringVar(value="Sequence: Idle")
        self.seq_remaining_var = tk.StringVar(value="Remaining: --")
        self.seq_progress_var = tk.DoubleVar(value=0.0)
        self.seq_tree: Optional[ttk.Treeview] = None
        self.seq_start_btn: Optional[ttk.Button] = None
        self.seq_pause_btn: Optional[ttk.Button] = None
        self.seq_stop_btn: Optional[ttk.Button] = None

        self.connect_btn: Optional[ttk.Button] = None
        self.port_combo: Optional[ttk.Combobox] = None
        self.input_badge: Optional[ttk.Label] = None
        self.polarity_badge: Optional[ttk.Label] = None
        self.log_text: Optional[tk.Text] = None
        self.csv_btn: Optional[ttk.Button] = None
        self.nav_buttons: dict[str, ttk.Button] = {}
        self.mode_buttons: dict[str, ttk.Button] = {}
        self.preset_buttons: list[ttk.Button] = []
        self.pages: dict[str, ttk.Frame] = {}

        self.presets = self._load_presets()
        self._configure_style(self.current_theme)
        self._build_ui()
        self._apply_window_icon()
        self.refresh_ports()
        self._refresh_preset_buttons()
        self.show_page("Overview")
        self.after(100, self._process_ui_queue)
        self._start_system_theme_listener()
        self.protocol("WM_DELETE_WINDOW", self._on_close)


    # --- Theme & Styling ---
    def _resolve_theme(self, mode: str) -> str:
        if mode in ("light", "dark"):
            return mode
        if platform.system() == "Windows" and winreg is not None:
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                )
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if int(value) == 1 else "dark"
            except Exception:
                pass
        if darkdetect is not None:
            try:
                return "light" if darkdetect.theme() == "Light" else "dark"
            except Exception:
                return "dark"
        return "dark"

    def _theme_button_label(self) -> str:
        active = "Dark" if self.current_theme == "dark" else "Light"
        return f"Theme: {self.theme_mode.title()} ({active})"

    def _toggle_theme_mode(self) -> None:
        order = ["system", "light", "dark"]
        self.theme_mode = order[(order.index(self.theme_mode) + 1) % len(order)]
        self._apply_theme(self._resolve_theme(self.theme_mode))

    def _on_system_theme_changed(self, theme_name: str) -> None:
        if self.theme_mode == "system":
            self.after(0, lambda: self._apply_theme("light" if theme_name == "Light" else "dark"))

    def _poll_system_theme(self) -> None:
        if self.theme_mode == "system":
            resolved = self._resolve_theme("system")
            if resolved != self.current_theme:
                self._apply_theme(resolved)
        self.after(1500, self._poll_system_theme)

    def _start_system_theme_listener(self) -> None:
        self.after(1500, self._poll_system_theme)
        if darkdetect is None:
            return
        try:
            threading.Thread(
                target=darkdetect.listener,
                args=(self._on_system_theme_changed,),
                daemon=True,
            ).start()
        except Exception:
            pass

    def _apply_theme(self, theme_name: str) -> None:
        self.current_theme = theme_name
        self._configure_style(theme_name)
        if self.theme_toggle_btn is not None:
            self.theme_toggle_btn.configure(text=self._theme_button_label())
        if self._logo_label is not None:
            self._logo_label.configure(bg=self._theme_colors["bg"])
        if self.log_text is not None:
            c = self._theme_colors
            self.log_text.configure(
                background=c["text_bg"],
                foreground=c["text_fg"],
                insertbackground=c["insert_fg"],
            )

    def _configure_style(self, theme_name: str) -> None:
        self.option_add("*tearOff", False)
        style = ttk.Style(self)
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except tk.TclError:
            pass

        if theme_name == "light":
            bg, card, card2 = "#eef3f8", "#ffffff", "#e7edf4"
            fg, muted = "#1c2430", "#5d6b7a"
            cyan, teal, amber, red, green = "#1998d6", "#198f83", "#c98a1a", "#c94a4a", "#1f9d68"
            field, border, footer_fg = "#f7fbff", "#c7d3df", "#516170"
            badge_on_bg, badge_on_fg = "#dff5ea", "#176746"
            badge_off_bg, badge_off_fg = "#fbe1e1", "#8d2d2d"
            text_bg, text_fg, insert_fg = "#e8edf3", "#111111", "#111111"
        else:
            bg, card, card2 = "#151a20", "#1d242d", "#232c37"
            fg, muted = "#edf3f8", "#8f9dab"
            cyan, teal, amber, red, green = "#59d7ff", "#27c3b2", "#f3b85a", "#e46c6c", "#38d39f"
            field, border, footer_fg = "#0f141a", "#33404d", "#c5d2de"
            badge_on_bg, badge_on_fg = "#16362f", "#98f2cb"
            badge_off_bg, badge_off_fg = "#43282c", "#ffc0c0"
            text_bg, text_fg, insert_fg = "#13171d", "#d7e3ec", "#ffffff"

        self.configure(bg=bg)
        self._theme_colors = {
            "bg": bg, "card": card, "card2": card2, "fg": fg, "muted": muted,
            "cyan": cyan, "teal": teal, "amber": amber, "red": red, "green": green,
            "field": field, "border": border, "footer_fg": footer_fg,
            "badge_on_bg": badge_on_bg, "badge_on_fg": badge_on_fg,
            "badge_off_bg": badge_off_bg, "badge_off_fg": badge_off_fg,
            "text_bg": text_bg, "text_fg": text_fg, "insert_fg": insert_fg,
        }

        style.configure("App.TFrame", background=bg)
        style.configure("Card.TFrame", background=card)
        style.configure("Panel.TLabelframe", background=card, foreground=fg, borderwidth=1, relief="solid", bordercolor=border)
        style.configure("Panel.TLabelframe.Label", background=card, foreground=fg, font=_ui_font(11, "bold"), padding=(8, 2))
        style.configure("Header.TLabel", background=bg, foreground=fg, font=_ui_font(22, "bold"))
        style.configure("SubHeader.TLabel", background=bg, foreground=muted, font=_ui_font(10))
        style.configure("TLabel", background=card, foreground=fg, font=_ui_font(11))
        style.configure("MutedDark.TLabel", background=bg, foreground=muted, font=_ui_font(10))
        style.configure("Footer.TLabel", background=bg, foreground=footer_fg, font=_ui_font(10))
        style.configure("Subtle.TLabel", background=card, foreground=muted, font=_ui_font(10))
        style.configure("Section.TLabel", background=card, foreground=fg, font=_ui_font(11, "bold"))
        style.configure("BigValue.TLabel", background=card, foreground=cyan, font=_ui_font(24, "bold", mono=True), anchor="w")
        style.configure("SmallValue.TLabel", background=card, foreground=fg, font=_ui_font(12, "bold", mono=True), anchor="w")
        style.configure("TileTitle.TLabel", background=card, foreground=muted, font=_ui_font(10))
        style.configure("TileValue.TLabel", background=card, foreground=fg, font=_ui_font(14, "bold"), anchor="w", width=18)

        style.configure("Primary.TButton", font=_ui_font(10, "bold"), padding=(12, 8), background=cyan, foreground="#081018")
        style.map("Primary.TButton", background=[("active", "#7be3ff" if theme_name == "dark" else "#38b2eb"), ("pressed", "#29b6e6"), ("disabled", card2)], foreground=[("disabled", muted)])
        style.configure("Secondary.TButton", font=_ui_font(10), padding=(10, 7), background=card2, foreground=fg)
        style.map("Secondary.TButton", background=[("active", "#323e4d" if theme_name == "dark" else "#d6e0ec"), ("pressed", "#3e4d60" if theme_name == "dark" else "#c4d2e3"), ("disabled", card)], foreground=[("disabled", muted)])
        style.configure("Danger.TButton", font=_ui_font(10, "bold"), padding=(12, 8), background=red, foreground="#ffffff")
        style.map("Danger.TButton", background=[("active", "#ef7e7e"), ("pressed", "#b84444"), ("disabled", card2)], foreground=[("disabled", muted)])
        style.configure("Preset.TButton", font=_ui_font(10, "bold"), padding=(10, 8), background=teal, foreground="#081018" if theme_name == "dark" else "#ffffff")
        style.map("Preset.TButton", background=[("active", "#45dbc9" if theme_name == "dark" else "#12a596"), ("pressed", "#1aa192"), ("disabled", card2)], foreground=[("disabled", muted)])
        style.configure("Nav.TButton", font=_ui_font(10, "bold"), padding=(14, 8), background=card2, foreground=fg)
        style.map("Nav.TButton", background=[("active", "#323e4d" if theme_name == "dark" else "#d6e0ec")])
        style.configure("NavActive.TButton", font=_ui_font(10, "bold"), padding=(14, 8), background=cyan, foreground="#081018")
        style.map("NavActive.TButton", background=[("active", "#7be3ff" if theme_name == "dark" else "#38b2eb")])
        style.configure("Mode.TButton", font=_ui_font(10, "bold"), padding=(12, 7), background=card2, foreground=fg, width=8)
        style.map("Mode.TButton", background=[("active", "#323e4d" if theme_name == "dark" else "#d6e0ec")])
        style.configure("ModeActive.TButton", font=_ui_font(10, "bold"), padding=(12, 7), background=amber, foreground="#1f1a10", width=8)
        style.map("ModeActive.TButton", background=[("active", "#ffd074")])

        style.configure("Field.TEntry", fieldbackground=field, foreground=fg, bordercolor=border, lightcolor=border, darkcolor=border, insertcolor=insert_fg, padding=(10, 7))
        style.map("Field.TEntry", bordercolor=[("focus", cyan)], lightcolor=[("focus", cyan)], darkcolor=[("focus", cyan)])
        style.configure("Field.TCombobox", fieldbackground=field, background=field, foreground=fg, arrowcolor=cyan, bordercolor=border, lightcolor=border, darkcolor=border, padding=(8, 6))
        style.map(
            "Field.TCombobox",
            fieldbackground=[("readonly", field), ("active", field)],
            background=[("readonly", field), ("active", card2)],
            foreground=[("readonly", fg), ("disabled", muted)],
            bordercolor=[("focus", cyan), ("readonly", border)],
            lightcolor=[("focus", cyan), ("readonly", border)],
            darkcolor=[("focus", cyan), ("readonly", border)],
            arrowcolor=[("active", cyan), ("readonly", cyan)],
        )
        style.configure("Field.TCheckbutton", background=card, foreground=fg)
        style.map("Field.TCheckbutton", background=[("active", card)], foreground=[("active", cyan)])
        style.configure(
            "Seq.Treeview",
            background=field,
            fieldbackground=field,
            foreground=fg,
            bordercolor=border,
            rowheight=28,
            font=_ui_font(10),
        )
        style.map("Seq.Treeview", background=[("selected", cyan)], foreground=[("selected", "#081018")])
        style.configure(
            "Seq.Treeview.Heading",
            background=card2,
            foreground=fg,
            font=_ui_font(10, "bold"),
            relief="flat",
        )
        style.map("Seq.Treeview.Heading", background=[("active", card2)])
        style.configure("Seq.Horizontal.TProgressbar", troughcolor=field, background=cyan, bordercolor=border, lightcolor=cyan, darkcolor=cyan)
        self.option_add("*TCombobox*Listbox.background", field)
        self.option_add("*TCombobox*Listbox.foreground", fg)
        self.option_add("*TCombobox*Listbox.selectBackground", cyan)
        self.option_add("*TCombobox*Listbox.selectForeground", "#081018" if theme_name == "dark" else "#ffffff")
        style.configure("StateBadgeOn.TLabel", background=badge_on_bg, foreground=badge_on_fg, font=_ui_font(11, "bold"), padding=(12, 5), anchor="center", width=10)
        style.configure("StateBadgeOff.TLabel", background=badge_off_bg, foreground=badge_off_fg, font=_ui_font(11, "bold"), padding=(12, 5), anchor="center", width=10)

    # --- UI Building ---
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14, style="App.TFrame")
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        self._build_top_bar(root).grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._build_status_bar(root).grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self._build_nav_bar(root).grid(row=2, column=0, sticky="ew", pady=(0, 10))

        container = ttk.Frame(root, style="App.TFrame")
        container.grid(row=3, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        self.pages["Overview"] = self._build_overview_page(container)
        self.pages["Modes & Tests"] = self._build_modes_page(container)
        self.pages["Sequence / Timer"] = self._build_sequence_page(container)
        self.pages["Advanced & SCPI"] = self._build_advanced_page(container)

        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        footer = ttk.Frame(root, style="App.TFrame")
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.footer_var, style="Footer.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(footer, textvariable=self.tx_last_var, style="Footer.TLabel").grid(row=0, column=1, sticky="e", padx=(16, 8))
        ttk.Label(footer, textvariable=self.rx_last_var, style="Footer.TLabel").grid(row=0, column=2, sticky="e", padx=(8, 8))
        ttk.Button(footer, text="Clear Log", style="Secondary.TButton", command=self.clear_log).grid(row=0, column=3, sticky="e")

    @staticmethod
    def _resource_path(filename: str) -> Optional[Path]:
        candidates = []
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(getattr(sys, "_MEIPASS")) / filename)
        candidates.append(Path(__file__).with_name(filename))
        return next((p for p in candidates if p.exists()), None)

    def _apply_window_icon(self) -> None:
        try:
            from PIL import Image, ImageTk
            icon_path = self._resource_path("icon.png") or self._resource_path("logo.png")
            if icon_path is None:
                return
            img = Image.open(icon_path).convert("RGBA")
            self._window_icons = []
            for sz in (128, 64, 32, 16):
                resized = img.resize((sz, sz), Image.Resampling.LANCZOS)
                self._window_icons.append(ImageTk.PhotoImage(resized))
            self.iconphoto(False, *self._window_icons)
        except Exception:
            pass

    def _build_top_bar(self, parent: tk.Widget) -> ttk.Frame:
        frame = ttk.Frame(parent, style="App.TFrame")
        frame.columnconfigure(1, weight=1)

        logo_path = self._resource_path("logo.png")
        if logo_path is not None:
            try:
                from PIL import Image, ImageTk
                logo_img = Image.open(logo_path).convert("RGBA")
                logo_img.thumbnail((68, 68), Image.Resampling.LANCZOS)
                self._header_logo_img = ImageTk.PhotoImage(logo_img)
                self._logo_label = tk.Label(frame, image=self._header_logo_img, bg=self._theme_colors["bg"], bd=0)
                self._logo_label.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
            except Exception:
                pass

        title_box = ttk.Frame(frame, style="App.TFrame")
        title_box.grid(row=0, column=1, rowspan=2, sticky="w")
        ttk.Label(title_box, text="RS-KEL2050 Controller", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            title_box,
            text="Cross-platform DC Electronic Load GUI (USB CDC / SCPI)",
            style="SubHeader.TLabel",
        ).pack(anchor="w")

        ttk.Label(frame, text="Serial Port", style="MutedDark.TLabel").grid(row=0, column=2, sticky="w", padx=(12, 6))
        self.port_combo = ttk.Combobox(frame, textvariable=self.port_var, width=32, state="readonly", style="Field.TCombobox")
        self.port_combo.grid(row=1, column=2, sticky="w", padx=(12, 6))
        ttk.Button(frame, text="Refresh", style="Secondary.TButton", command=self.refresh_ports).grid(row=1, column=3, sticky="w", padx=(0, 8))

        ttk.Label(frame, text="Baud", style="MutedDark.TLabel").grid(row=0, column=4, sticky="w", padx=(6, 6))
        ttk.Combobox(
            frame,
            textvariable=self.baud_var,
            values=[str(b) for b in BAUD_RATES],
            width=9,
            state="readonly",
            style="Field.TCombobox",
        ).grid(row=1, column=4, sticky="w", padx=(6, 8))

        ttk.Label(frame, text="Ending", style="MutedDark.TLabel").grid(row=0, column=5, sticky="w", padx=(6, 6))
        ttk.Combobox(
            frame,
            textvariable=self.line_end_var,
            values=["LF (\\n)", "CR (\\r)", "CRLF (\\r\\n)"],
            width=11,
            state="readonly",
            style="Field.TCombobox",
        ).grid(row=1, column=5, sticky="w", padx=(6, 10))

        self.connect_btn = ttk.Button(frame, text="Connect", style="Primary.TButton", command=self.toggle_connection)
        self.connect_btn.grid(row=1, column=6, sticky="e", padx=(0, 8))
        self.theme_toggle_btn = ttk.Button(
            frame,
            text=self._theme_button_label(),
            style="Secondary.TButton",
            command=self._toggle_theme_mode,
        )
        self.theme_toggle_btn.grid(row=1, column=7, sticky="e")
        return frame

    def _build_status_bar(self, parent: tk.Widget) -> ttk.Frame:
        frame = ttk.Frame(parent, style="App.TFrame")
        for i in range(5):
            frame.columnconfigure(i, weight=1)
        MetricTile(frame, "Connection", self.connection_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        MetricTile(frame, "Mode", self.mode_status_var).grid(row=0, column=1, sticky="ew", padx=6)
        MetricTile(frame, "Input", self.input_status_var).grid(row=0, column=2, sticky="ew", padx=6)
        MetricTile(frame, "Polarity", self.polarity_var).grid(row=0, column=3, sticky="ew", padx=6)
        MetricTile(frame, "Device IDN", self.idn_var).grid(row=0, column=4, sticky="ew", padx=(6, 0))
        return frame

    def _build_nav_bar(self, parent: tk.Widget) -> ttk.Frame:
        frame = ttk.Frame(parent, style="App.TFrame")
        left = ttk.Frame(frame, style="App.TFrame")
        left.pack(side=tk.LEFT)
        right = ttk.Frame(frame, style="App.TFrame")
        right.pack(side=tk.RIGHT)

        for page in ("Overview", "Modes & Tests", "Sequence / Timer", "Advanced & SCPI"):
            btn = ttk.Button(left, text=page, style="Nav.TButton", command=lambda p=page: self.show_page(p))
            btn.pack(side=tk.LEFT, padx=(0, 8))
            self.nav_buttons[page] = btn

        ttk.Button(
            right,
            text="INPUT ON",
            style="Primary.TButton",
            command=lambda: self.run_action(lambda: self._set_input(True), "Input ON"),
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            right,
            text="INPUT OFF",
            style="Danger.TButton",
            command=lambda: self.run_action(lambda: self._set_input(False), "Input OFF"),
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            right,
            text="Refresh",
            style="Secondary.TButton",
            command=lambda: self.run_action(self._refresh_snapshot_now, "Refreshed"),
        ).pack(side=tk.LEFT)
        return frame

    def _build_overview_page(self, parent: tk.Widget) -> ttk.Frame:
        page = ttk.Frame(parent, style="App.TFrame")
        page.columnconfigure(0, weight=1)
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)

        live = ttk.LabelFrame(page, text="Live Measurements", style="Panel.TLabelframe", padding=14)
        live.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        live.columnconfigure(0, weight=1)
        live.columnconfigure(1, weight=1)

        badge_row = ttk.Frame(live, style="Card.TFrame")
        badge_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        ttk.Label(badge_row, text="Load Input:", style="Section.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        self.input_badge = ttk.Label(badge_row, textvariable=self.input_status_var, style="StateBadgeOff.TLabel")
        self.input_badge.pack(side=tk.LEFT, padx=(0, 18))
        ttk.Label(badge_row, text="Polarity:", style="Section.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        self.polarity_badge = ttk.Label(badge_row, textvariable=self.polarity_var, style="StateBadgeOn.TLabel")
        self.polarity_badge.pack(side=tk.LEFT)

        ttk.Label(live, text="Voltage (V)", style="Subtle.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(live, textvariable=self.v_meas_var, style="BigValue.TLabel").grid(row=2, column=0, sticky="w", pady=(2, 14))
        ttk.Label(live, text="Current (A)", style="Subtle.TLabel").grid(row=1, column=1, sticky="w")
        ttk.Label(live, textvariable=self.i_meas_var, style="BigValue.TLabel").grid(row=2, column=1, sticky="w", pady=(2, 14))

        ttk.Label(live, text="Power (W)", style="Subtle.TLabel").grid(row=3, column=0, sticky="w")
        ttk.Label(live, textvariable=self.p_meas_var, style="BigValue.TLabel").grid(row=4, column=0, sticky="w", pady=(2, 14))
        ttk.Label(live, text="Heatsink Temp (°C)", style="Subtle.TLabel").grid(row=3, column=1, sticky="w")
        ttk.Label(live, textvariable=self.temp_var, style="BigValue.TLabel").grid(row=4, column=1, sticky="w", pady=(2, 14))

        ttk.Separator(live, orient="horizontal").grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        ttk.Label(live, text="Stored Setpoints", style="Section.TLabel").grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 6))

        sp_grid = ttk.Frame(live, style="Card.TFrame")
        sp_grid.grid(row=7, column=0, columnspan=2, sticky="ew")
        for i in range(4):
            sp_grid.columnconfigure(i, weight=1)
        ttk.Label(sp_grid, text="CV Set", style="Subtle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(sp_grid, textvariable=self.v_set_read_var, style="SmallValue.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(sp_grid, text="CC Set", style="Subtle.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(sp_grid, textvariable=self.i_set_read_var, style="SmallValue.TLabel").grid(row=1, column=1, sticky="w")
        ttk.Label(sp_grid, text="CR Set", style="Subtle.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Label(sp_grid, textvariable=self.r_set_read_var, style="SmallValue.TLabel").grid(row=1, column=2, sticky="w")
        ttk.Label(sp_grid, text="CW Set", style="Subtle.TLabel").grid(row=0, column=3, sticky="w")
        ttk.Label(sp_grid, textvariable=self.p_set_read_var, style="SmallValue.TLabel").grid(row=1, column=3, sticky="w")

        ttk.Label(live, textvariable=self.limits_info_var, style="Subtle.TLabel").grid(row=8, column=0, columnspan=2, sticky="w", pady=(14, 0))

        ctrl = ttk.LabelFrame(page, text="Mode & Setpoint Control", style="Panel.TLabelframe", padding=14)
        ctrl.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctrl.columnconfigure(0, weight=1, uniform="ctrl_half")
        ctrl.columnconfigure(1, weight=1, uniform="ctrl_half")

        ttk.Label(ctrl, text="Operating Mode", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        mode_bar = ttk.Frame(ctrl, style="Card.TFrame")
        mode_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        for m in ("CC", "CV", "CR", "CW", "SHORT"):
            btn = ttk.Button(
                mode_bar,
                text=m,
                style="Mode.TButton",
                command=lambda mode=m: self.run_action(lambda: self._apply_mode(mode), f"Mode -> {mode}"),
            )
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self.mode_buttons[m] = btn

        ttk.Label(ctrl, textvariable=self.setpoint_label_var).grid(row=2, column=0, sticky="w")
        ttk.Label(ctrl, text="Hardware Protection Limit").grid(row=2, column=1, sticky="w", padx=(10, 0))

        ttk.Entry(ctrl, textvariable=self.setpoint_var, style="Field.TEntry", width=18).grid(row=3, column=0, sticky="ew", pady=(4, 8))
        ttk.Label(ctrl, textvariable=self.hw_limit_var, style="Subtle.TLabel").grid(row=3, column=1, sticky="w", padx=(10, 0), pady=(4, 8))

        ttk.Button(
            ctrl,
            text="Apply Setpoint",
            style="Primary.TButton",
            command=lambda: self.run_action(self._apply_setpoint, "Setpoint applied"),
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 14))

        ttk.Label(ctrl, text="Quick Presets (active mode)", style="Section.TLabel").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 6))
        preset_frame = ttk.Frame(ctrl, style="Card.TFrame")
        preset_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        for i in range(4):
            preset_frame.columnconfigure(i, weight=1)
            btn = ttk.Button(preset_frame, text="-", style="Preset.TButton")
            btn.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 4, 0))
            self.preset_buttons.append(btn)

        ttk.Button(
            ctrl,
            text="Save Current Setpoint to Preset...",
            style="Secondary.TButton",
            command=self._save_current_to_preset_dialog,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 16))

        ttk.Separator(ctrl, orient="horizontal").grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        ttk.Label(ctrl, text="Live CSV Data Logger", style="Section.TLabel").grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.csv_btn = ttk.Button(ctrl, text="Start CSV Logging...", style="Secondary.TButton", command=self._toggle_csv_logging)
        self.csv_btn.grid(row=10, column=0, sticky="w")
        ttk.Label(ctrl, textvariable=self.csv_status_var, style="Subtle.TLabel").grid(row=10, column=1, sticky="w", padx=(10, 0))
        return page

    def _build_modes_page(self, parent: tk.Widget) -> ttk.Frame:
        page = ttk.Frame(parent, style="App.TFrame")
        page.columnconfigure(0, weight=1)
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)

        dyn = ttk.LabelFrame(page, text="Dynamic Test (:DYN)", style="Panel.TLabelframe", padding=14)
        dyn.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        dyn.columnconfigure(0, weight=1)
        dyn.columnconfigure(1, weight=1)

        ttk.Label(dyn, text="Dynamic Mode").grid(row=0, column=0, sticky="w")
        ttk.Combobox(dyn, textvariable=self.dyn_mode_var, values=["CC", "CV", "CR", "CW"], state="readonly", style="Field.TCombobox").grid(row=1, column=0, sticky="ew", pady=(4, 8))
        ttk.Label(dyn, text="Frequency (Hz)").grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Entry(dyn, textvariable=self.dyn_freq_var, style="Field.TEntry").grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(4, 8))

        ttk.Label(dyn, text="Level 1 (V / A / Ω / W)").grid(row=2, column=0, sticky="w")
        ttk.Entry(dyn, textvariable=self.dyn_val1_var, style="Field.TEntry").grid(row=3, column=0, sticky="ew", pady=(4, 8))
        ttk.Label(dyn, text="Level 2 (V / A / Ω / W)").grid(row=2, column=1, sticky="w", padx=(10, 0))
        ttk.Entry(dyn, textvariable=self.dyn_val2_var, style="Field.TEntry").grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=(4, 8))

        ttk.Label(dyn, text="CC Rise Slope (A/uS)").grid(row=4, column=0, sticky="w")
        ttk.Entry(dyn, textvariable=self.dyn_rise_var, style="Field.TEntry").grid(row=5, column=0, sticky="ew", pady=(4, 8))
        ttk.Label(dyn, text="CC Fall Slope (A/uS)").grid(row=4, column=1, sticky="w", padx=(10, 0))
        ttk.Entry(dyn, textvariable=self.dyn_fall_var, style="Field.TEntry").grid(row=5, column=1, sticky="ew", padx=(10, 0), pady=(4, 8))

        ttk.Label(dyn, text="Duty Cycle (%)").grid(row=6, column=0, sticky="w")
        ttk.Entry(dyn, textvariable=self.dyn_duty_var, style="Field.TEntry").grid(row=7, column=0, sticky="ew", pady=(4, 12))

        ttk.Button(dyn, text="Apply Dynamic", style="Primary.TButton", command=lambda: self.run_action(self._apply_dynamic, "Dynamic config sent")).grid(row=8, column=0, sticky="ew")
        ttk.Button(dyn, text="Query :DYN?", style="Secondary.TButton", command=lambda: self.run_action(self._query_dynamic, "Queried :DYN?")).grid(row=8, column=1, sticky="ew", padx=(10, 0))
        ttk.Button(dyn, text="Send *TRG Trigger", style="Secondary.TButton", command=lambda: self.run_action(lambda: self._dev().trigger(), "Sent *TRG")).grid(row=9, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(dyn, textvariable=self.dyn_readback_var, style="Subtle.TLabel", wraplength=520).grid(row=10, column=0, columnspan=2, sticky="w", pady=(10, 0))

        right_col = ttk.Frame(page, style="App.TFrame")
        right_col.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right_col.columnconfigure(0, weight=1)

        batt = ttk.LabelFrame(right_col, text="Battery Discharge (:BATT)", style="Panel.TLabelframe", padding=14)
        batt.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        batt.columnconfigure(0, weight=1)
        batt.columnconfigure(1, weight=1)

        ttk.Label(batt, text="Slot (1-7)").grid(row=0, column=0, sticky="w")
        ttk.Entry(batt, textvariable=self.batt_slot_var, style="Field.TEntry").grid(row=1, column=0, sticky="ew", pady=(2, 6))
        ttk.Label(batt, text="Current Range (A)").grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Entry(batt, textvariable=self.batt_range_var, style="Field.TEntry").grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(2, 6))

        ttk.Label(batt, text="Discharge Current (A)").grid(row=2, column=0, sticky="w")
        ttk.Entry(batt, textvariable=self.batt_curr_var, style="Field.TEntry").grid(row=3, column=0, sticky="ew", pady=(2, 6))
        ttk.Label(batt, text="Cutoff Voltage (V)").grid(row=2, column=1, sticky="w", padx=(10, 0))
        ttk.Entry(batt, textvariable=self.batt_vcut_var, style="Field.TEntry").grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=(2, 6))

        ttk.Label(batt, text="Cutoff Capacity (Ah)").grid(row=4, column=0, sticky="w")
        ttk.Entry(batt, textvariable=self.batt_capcut_var, style="Field.TEntry").grid(row=5, column=0, sticky="ew", pady=(2, 8))
        ttk.Label(batt, text="Time Limit (min)").grid(row=4, column=1, sticky="w", padx=(10, 0))
        ttk.Entry(batt, textvariable=self.batt_timelimit_var, style="Field.TEntry").grid(row=5, column=1, sticky="ew", padx=(10, 0), pady=(2, 8))

        ttk.Button(batt, text="Save :BATT", style="Primary.TButton", command=lambda: self.run_action(self._apply_battery, "Battery profile saved")).grid(row=6, column=0, sticky="ew")
        ttk.Button(batt, text="Recall & Read Tim/Cap", style="Secondary.TButton", command=lambda: self.run_action(self._query_battery, "Battery status read")).grid(row=6, column=1, sticky="ew", padx=(10, 0))
        ttk.Label(batt, textvariable=self.batt_readback_var, style="Subtle.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

        sysf = ttk.LabelFrame(right_col, text="System & Memory (*SAV / *RCL)", style="Panel.TLabelframe", padding=14)
        sysf.grid(row=1, column=0, sticky="ew")
        sysf.columnconfigure(0, weight=1)
        sysf.columnconfigure(1, weight=1)

        ttk.Checkbutton(sysf, text="Beep", variable=self.beep_var, style="Field.TCheckbutton", command=lambda: self.run_action(lambda: self._dev().set_beep(self.beep_var.get()), "Beep updated")).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(sysf, text="Front Panel Key Lock", variable=self.lock_var, style="Field.TCheckbutton", command=lambda: self.run_action(lambda: self._dev().set_lock(self.lock_var.get()), "Key lock updated")).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(sysf, text="Remote Sense (COMP)", variable=self.comp_var, style="Field.TCheckbutton", command=lambda: self.run_action(lambda: self._dev().set_remote_comp(self.comp_var.get()), "Remote sense updated")).grid(row=1, column=0, sticky="w", pady=(6, 10))

        mem_row = ttk.Frame(sysf, style="Card.TFrame")
        mem_row.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Label(mem_row, text="Memory Slot (1–100):").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Entry(mem_row, textvariable=self.mem_slot_var, width=8, style="Field.TEntry").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(mem_row, text="*SAV", style="Secondary.TButton", command=lambda: self.run_action(lambda: self._dev().save_memory(int(self.mem_slot_var.get())), "Saved memory slot")).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(mem_row, text="*RCL", style="Primary.TButton", command=lambda: self.run_action(self._recall_memory, "Recalled memory slot")).pack(side=tk.LEFT)
        return page

    def _build_sequence_page(self, parent: tk.Widget) -> ttk.Frame:
        page = ttk.Frame(parent, style="App.TFrame")
        page.columnconfigure(0, weight=3)
        page.columnconfigure(1, weight=2)
        page.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(page, text="Programmed Steps", style="Panel.TLabelframe", padding=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        cols = ("step", "mode", "setpoint", "duration")
        self.seq_tree = ttk.Treeview(left, columns=cols, show="headings", style="Seq.Treeview", selectmode="browse")
        self.seq_tree.heading("step", text="#")
        self.seq_tree.heading("mode", text="Mode")
        self.seq_tree.heading("setpoint", text="Setpoint")
        self.seq_tree.heading("duration", text="Duration (s)")
        self.seq_tree.column("step", width=48, anchor="center")
        self.seq_tree.column("mode", width=110, anchor="center")
        self.seq_tree.column("setpoint", width=150, anchor="center")
        self.seq_tree.column("duration", width=120, anchor="center")
        self.seq_tree.grid(row=0, column=0, sticky="nsew")
        self.seq_tree.bind("<<TreeviewSelect>>", self._on_seq_select)

        seq_scroll = ttk.Scrollbar(left, orient="vertical", command=self.seq_tree.yview)
        seq_scroll.grid(row=0, column=1, sticky="ns")
        self.seq_tree.configure(yscrollcommand=seq_scroll.set)

        tree_btns = ttk.Frame(left, style="Card.TFrame")
        tree_btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(tree_btns, text="Move Up", style="Secondary.TButton", command=lambda: self._move_seq_step(-1)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(tree_btns, text="Move Down", style="Secondary.TButton", command=lambda: self._move_seq_step(1)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(tree_btns, text="Remove", style="Danger.TButton", command=self._remove_seq_step).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(tree_btns, text="Clear All", style="Secondary.TButton", command=self._clear_seq_steps).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(tree_btns, text="Save JSON...", style="Secondary.TButton", command=self._save_seq_json).pack(side=tk.RIGHT)
        ttk.Button(tree_btns, text="Load JSON...", style="Secondary.TButton", command=self._load_seq_json).pack(side=tk.RIGHT, padx=(0, 6))

        right = ttk.Frame(page, style="App.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right.columnconfigure(0, weight=1)

        editor = ttk.LabelFrame(right, text="Step Editor", style="Panel.TLabelframe", padding=12)
        editor.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        editor.columnconfigure(0, weight=1)
        editor.columnconfigure(1, weight=1)

        ttk.Label(editor, text="Mode").grid(row=0, column=0, sticky="w")
        ttk.Combobox(editor, textvariable=self.seq_mode_var, values=["CC", "CV", "CR", "CW", "SHORT", "OFF"], state="readonly", style="Field.TCombobox").grid(row=1, column=0, sticky="ew", pady=(4, 8))
        ttk.Label(editor, text="Duration (s)").grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Entry(editor, textvariable=self.seq_dur_var, style="Field.TEntry").grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(4, 8))

        ttk.Label(editor, text="Setpoint (ignored for SHORT / OFF)").grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Entry(editor, textvariable=self.seq_val_var, style="Field.TEntry").grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 10))

        ttk.Button(editor, text="Add Step", style="Primary.TButton", command=self._add_seq_step).grid(row=4, column=0, sticky="ew")
        ttk.Button(editor, text="Update Selected", style="Secondary.TButton", command=self._update_seq_step).grid(row=4, column=1, sticky="ew", padx=(10, 0))

        runner = ttk.LabelFrame(right, text="Timer Execution", style="Panel.TLabelframe", padding=12)
        runner.grid(row=1, column=0, sticky="ew")
        runner.columnconfigure(0, weight=1)
        runner.columnconfigure(1, weight=1)

        ttk.Label(runner, text="Repeat Cycles (0 = infinite)").grid(row=0, column=0, sticky="w")
        ttk.Entry(runner, textvariable=self.seq_repeat_var, style="Field.TEntry", width=10).grid(row=0, column=1, sticky="ew", pady=(0, 6))
        ttk.Label(runner, text="Safety OFF Dwell between steps (s)").grid(row=1, column=0, sticky="w")
        ttk.Entry(runner, textvariable=self.seq_dwell_var, style="Field.TEntry", width=10).grid(row=1, column=1, sticky="ew", pady=(0, 6))
        ttk.Checkbutton(runner, text="Safe Stop (force Input OFF on finish/stop)", variable=self.seq_safe_stop_var, style="Field.TCheckbutton").grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 10))

        btn_row = ttk.Frame(runner, style="Card.TFrame")
        btn_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        for i in range(3):
            btn_row.columnconfigure(i, weight=1)
        self.seq_start_btn = ttk.Button(btn_row, text="Start", style="Primary.TButton", command=self._start_sequence)
        self.seq_start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.seq_pause_btn = ttk.Button(btn_row, text="Pause", style="Secondary.TButton", command=self._toggle_pause_sequence, state="disabled")
        self.seq_pause_btn.grid(row=0, column=1, sticky="ew", padx=4)
        self.seq_stop_btn = ttk.Button(btn_row, text="Stop", style="Danger.TButton", command=self._stop_sequence, state="disabled")
        self.seq_stop_btn.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        ttk.Label(runner, textvariable=self.seq_status_var, style="Section.TLabel").grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(runner, textvariable=self.seq_remaining_var, style="SmallValue.TLabel").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 6))
        ttk.Progressbar(runner, variable=self.seq_progress_var, maximum=100.0, style="Seq.Horizontal.TProgressbar").grid(row=6, column=0, columnspan=2, sticky="ew")

        self._refresh_seq_tree()
        return page

    def _build_advanced_page(self, parent: tk.Widget) -> ttk.Frame:
        page = ttk.Frame(parent, style="App.TFrame")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        cmd_frame = ttk.LabelFrame(page, text="Manual SCPI Console", style="Panel.TLabelframe", padding=12)
        cmd_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        cmd_frame.columnconfigure(0, weight=1)

        entry = ttk.Entry(cmd_frame, textvariable=self.manual_cmd_var, style="Field.TEntry")
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        entry.bind("<Return>", lambda _e: self.run_action(self._send_manual_cmd, "Manual SCPI sent"))
        ttk.Checkbutton(cmd_frame, text="Expect Response (Query)", variable=self.manual_query_var, style="Field.TCheckbutton").grid(row=0, column=1, padx=(0, 8))
        ttk.Button(cmd_frame, text="Send", style="Primary.TButton", command=lambda: self.run_action(self._send_manual_cmd, "Manual SCPI sent")).grid(row=0, column=2)

        log_frame = ttk.LabelFrame(page, text="TX / RX Communication Log", style="Panel.TLabelframe", padding=12)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        c = self._theme_colors
        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            font=_ui_font(10, mono=True),
            background=c["text_bg"],
            foreground=c["text_fg"],
            insertbackground=c["insert_fg"],
            relief="flat",
            padx=8,
            pady=8,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        return page

    # --- Navigation & Ports ---
    def show_page(self, name: str) -> None:
        for page_name, frame in self.pages.items():
            if page_name == name:
                frame.tkraise()
                self.nav_buttons[page_name].configure(style="NavActive.TButton")
            else:
                self.nav_buttons[page_name].configure(style="Nav.TButton")

    def refresh_ports(self) -> None:
        self.ports_cache = list_serial_ports()
        labels = [p.label for p in self.ports_cache]
        if self.port_combo is not None:
            self.port_combo.configure(values=labels)
        if not labels:
            self.port_var.set("")
            return
        current = self.port_var.get()
        if current:
            for p in self.ports_cache:
                if p.device == current or p.label == current:
                    self.port_var.set(p.label)
                    return
        korad = next((p for p in self.ports_cache if p.is_korad), self.ports_cache[0])
        self.port_var.set(korad.label)

    def _selected_port_device(self) -> str:
        val = self.port_var.get().strip()
        for p in self.ports_cache:
            if p.label == val or p.device == val:
                return p.device
        return val.split(" - ")[0].strip()

    def _selected_line_ending(self) -> str:
        mapping = {"LF (\\n)": "\n", "CR (\\r)": "\r", "CRLF (\\r\\n)": "\r\n"}
        return mapping.get(self.line_end_var.get(), "\n")

    # --- Connection & Polling ---
    def toggle_connection(self) -> None:
        if self.device is not None and self.device.is_open:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        port = self._selected_port_device()
        if not port:
            messagebox.showwarning("No Port", "Select a valid serial port first.")
            return
        baud = int(self.baud_var.get())
        ending = self._selected_line_ending()
        client = KEL2050Client(
            port=port,
            baudrate=baud,
            line_ending=ending,
            io_logger=lambda d, p: self.ui_queue.put(("io", (d, p))),
        )
        try:
            client.open()
            idn = client.idn()
            limits = client.get_limits()
            snap = client.read_snapshot()
        except Exception as exc:
            client.close()
            messagebox.showerror("Connection Error", f"Failed to connect on {port}:\n{exc}")
            return

        self.device = client
        self.limits = limits
        display_idn = re.sub(r",?\s*SN:[^,]*", "", idn, flags=re.IGNORECASE).strip(" ,")
        self.idn_var.set(display_idn or idn)
        self.connection_var.set(f"Connected ({port})")
        self.limits_info_var.set(
            f"Limits: {limits.v_min:.2f}–{limits.v_max:.1f} V | "
            f"{limits.i_min:.2f}–{limits.i_max:.1f} A | "
            f"{limits.r_min:.2f}–{limits.r_max:.1f} Ω | "
            f"{limits.p_min:.2f}–{limits.p_max:.1f} W"
        )
        if self.connect_btn is not None:
            self.connect_btn.configure(text="Disconnect", style="Danger.TButton")
        self._apply_snapshot_to_ui(snap, sync_inputs=True)
        self.footer_var.set(f"Connected to {idn} on {port} @ {baud} baud.")

        self.stop_event.clear()
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()

    def _disconnect(self) -> None:
        self._stop_sequence(wait=True)
        self.stop_event.set()
        if self.poll_thread is not None and self.poll_thread.is_alive():
            self.poll_thread.join(timeout=1.0)
        self.poll_thread = None
        if self.device is not None:
            self.device.close()
            self.device = None
        self.connection_var.set("Disconnected")
        self.idn_var.set("--")
        if self.connect_btn is not None:
            self.connect_btn.configure(text="Connect", style="Primary.TButton")
        self.footer_var.set("Disconnected.")

    def _poll_loop(self) -> None:
        tick = 0
        while not self.stop_event.is_set():
            if self.device is None or not self.device.is_open:
                break
            gen_before = self._action_gen
            try:
                snap = self.device.read_snapshot(silent=True, include_setpoints=(tick % 8 == 0))
                if gen_before == self._action_gen:
                    self.ui_queue.put(("snapshot", (gen_before, snap)))
                tick += 1
            except Exception as exc:
                self.ui_queue.put(("poll_error", str(exc)))
                time.sleep(0.5)
            self.stop_event.wait(0.35)

    def _process_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "snapshot":
                    gen, snap_obj = payload  # type: ignore[misc]
                    if gen == self._action_gen:
                        self._apply_snapshot_to_ui(snap_obj, sync_inputs=False)
                elif kind == "snapshot_sync":
                    snap_obj = payload  # type: ignore[assignment]
                    self._apply_snapshot_to_ui(snap_obj, sync_inputs=True)
                elif kind == "io":
                    direction, text = payload  # type: ignore[misc]
                    self._append_log(direction, text)
                elif kind == "status":
                    self.footer_var.set(str(payload))
                elif kind == "error":
                    self.footer_var.set(f"Error: {payload}")
                    messagebox.showerror("Device Error", str(payload))
                elif kind == "poll_error":
                    self.footer_var.set(f"Polling warning: {payload}")
                elif kind == "dyn_readback":
                    self.dyn_readback_var.set(f"Dynamic: {payload}")
                elif kind == "batt_readback":
                    self.batt_readback_var.set(str(payload))
                elif kind == "seq_progress":
                    status_txt, rem_txt, pct = payload  # type: ignore[misc]
                    self.seq_status_var.set(status_txt)
                    self.seq_remaining_var.set(rem_txt)
                    self.seq_progress_var.set(pct)
                elif kind == "seq_done":
                    self.seq_status_var.set(str(payload))
                    self.seq_remaining_var.set("Remaining: --")
                    self.seq_progress_var.set(0.0)
                    self._set_seq_running_ui(False)
        except queue.Empty:
            pass
        finally:
            self.after(80, self._process_ui_queue)

    # --- Snapshot & Actions ---
    def _dev(self) -> KEL2050Client:
        if self.device is None or not self.device.is_open:
            raise RuntimeError("Not connected to RS-KEL2050.")
        return self.device

    def run_action(self, fn: Callable[[], None], ok_msg: str) -> None:
        self._action_gen += 1
        def worker() -> None:
            with self._action_lock:
                try:
                    fn()
                    self.ui_queue.put(("status", ok_msg))
                except Exception as exc:
                    self.ui_queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_snapshot_to_ui(self, snap: Snapshot, sync_inputs: bool = False) -> None:
        self.v_meas_var.set(f"{snap.v_meas:06.3f} V")
        self.i_meas_var.set(f"{snap.i_meas:07.4f} A")
        self.p_meas_var.set(f"{snap.p_meas:06.3f} W")
        self.temp_var.set(f"{snap.temp_c:.1f} °C")

        if snap.v_set == snap.v_set:
            self.v_set_read_var.set(f"{snap.v_set:.3f} V")
        if snap.i_set == snap.i_set:
            self.i_set_read_var.set(f"{snap.i_set:.4f} A")
        if snap.r_set == snap.r_set:
            self.r_set_read_var.set(f"{snap.r_set:.3f} Ω")
        if snap.p_set == snap.p_set:
            self.p_set_read_var.set(f"{snap.p_set:.3f} W")

        self.mode_status_var.set(snap.mode)
        self.input_status_var.set("ON" if snap.input_on else "OFF")
        if self.input_badge is not None:
            self.input_badge.configure(style="StateBadgeOn.TLabel" if snap.input_on else "StateBadgeOff.TLabel")

        rev = snap.status.reverse_polarity
        self.polarity_var.set("REVERSE!" if rev else "OK")
        if self.polarity_badge is not None:
            self.polarity_badge.configure(style="StateBadgeOff.TLabel" if rev else "StateBadgeOn.TLabel")

        for m, btn in self.mode_buttons.items():
            btn.configure(style="ModeActive.TButton" if m == snap.mode else "Mode.TButton")

        if sync_inputs or self.active_mode_var.get() != snap.mode:
            self.active_mode_var.set(snap.mode)
            self._update_mode_fields(snap)

        self.beep_var.set(snap.status.beep)
        self.lock_var.set(snap.status.key_lock)
        self.comp_var.set(snap.status.remote_comp)

        if self._csv_writer is not None:
            self._csv_writer.writerow([
                datetime.now().isoformat(timespec="milliseconds"),
                snap.mode,
                int(snap.input_on),
                f"{snap.v_meas:.4f}",
                f"{snap.i_meas:.4f}",
                f"{snap.p_meas:.4f}",
                f"{snap.temp_c:.1f}",
            ])
            if self._csv_file is not None:
                self._csv_file.flush()

    def _update_mode_fields(self, snap: Snapshot) -> None:
        mode = snap.mode
        if mode in MODE_META:
            label, unit, attr, min_attr, max_attr = MODE_META[mode]
            self.setpoint_label_var.set(f"Setpoint {label}")
            val = getattr(snap, attr)
            if val == val:
                self.setpoint_var.set(f"{val:.4f}")
            lo = getattr(self.limits, min_attr)
            hi = getattr(self.limits, max_attr)
            self.hw_limit_var.set(f"{lo:.3f} – {hi:.3f} {unit} (read-only)")
        else:
            self.setpoint_label_var.set("SHORT Mode Active")
            self.hw_limit_var.set(f"Max {self.limits.i_max:.1f} A / {self.limits.p_max:.1f} W")
        self._refresh_preset_buttons()

    def _refresh_snapshot_now(self) -> None:
        snap = self._dev().read_snapshot(silent=True, include_setpoints=True)
        self.ui_queue.put(("snapshot_sync", snap))

    def _set_input(self, on: bool) -> None:
        self._dev().set_input(on)
        self._refresh_snapshot_now()

    def _apply_mode(self, mode: str) -> None:
        dev = self._dev()
        dev.set_mode(mode)
        time.sleep(0.08)
        self._refresh_snapshot_now()

    def _apply_setpoint(self) -> None:
        dev = self._dev()
        mode = self.active_mode_var.get()
        val = float(self.setpoint_var.get())
        if mode not in MODE_META:
            raise ValueError("Cannot apply setpoint in SHORT mode.")
        _lbl, unit, _attr, min_attr, max_attr = MODE_META[mode]
        lo = getattr(self.limits, min_attr)
        hi = getattr(self.limits, max_attr)
        if not (lo <= val <= hi):
            raise ValueError(f"Value {val} {unit} is outside hardware limits ({lo:.3f} – {hi:.3f} {unit}).")
        if mode == "CC":
            dev.set_current(val)
        elif mode == "CV":
            dev.set_voltage(val)
        elif mode == "CR":
            dev.set_resistance(val)
        elif mode == "CW":
            dev.set_power(val)
        self._refresh_snapshot_now()


    def _apply_dynamic(self) -> None:
        dev = self._dev()
        mode = self.dyn_mode_var.get()
        v1 = float(self.dyn_val1_var.get())
        v2 = float(self.dyn_val2_var.get())
        freq = float(self.dyn_freq_var.get())
        duty = float(self.dyn_duty_var.get())
        if mode == "CC":
            rise = float(self.dyn_rise_var.get())
            fall = float(self.dyn_fall_var.get())
            dev.set_dynamic_cc(rise, fall, v1, v2, freq, duty)
        elif mode == "CV":
            dev.set_dynamic_cv(v1, v2, freq, duty)
        elif mode == "CR":
            dev.set_dynamic_cr(v1, v2, freq, duty)
        elif mode == "CW":
            dev.set_dynamic_cw(v1, v2, freq, duty)

    def _query_dynamic(self) -> None:
        resp = self._dev().get_dynamic()
        self.ui_queue.put(("dyn_readback", resp))

    def _apply_battery(self) -> None:
        self._dev().set_battery(
            slot=int(self.batt_slot_var.get()),
            i_range=float(self.batt_range_var.get()),
            i_discharge=float(self.batt_curr_var.get()),
            v_cutoff=float(self.batt_vcut_var.get()),
            cap_cutoff_ah=float(self.batt_capcut_var.get()),
            time_limit_min=int(self.batt_timelimit_var.get()),
        )

    def _query_battery(self) -> None:
        dev = self._dev()
        recalled = dev.recall_battery(int(self.batt_slot_var.get()))
        t = dev.get_battery_time()
        cap = dev.get_battery_capacity()
        self.ui_queue.put(("batt_readback", f"Slot: {recalled} | Time: {t} | Cap: {cap}"))

    def _recall_memory(self) -> None:
        self._dev().recall_memory(int(self.mem_slot_var.get()))
        self._refresh_snapshot_now()

    def _send_manual_cmd(self) -> None:
        cmd = self.manual_cmd_var.get().strip()
        if not cmd:
            return
        dev = self._dev()
        if self.manual_query_var.get() or cmd.endswith("?"):
            dev.query_text(cmd)
        else:
            dev.write(cmd)

    # --- Presets & CSV ---
    def _load_presets(self) -> dict[str, list[dict[str, object]]]:
        if PRESET_FILE.exists():
            try:
                data = json.loads(PRESET_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return json.loads(json.dumps(DEFAULT_PRESETS))

    def _save_presets(self) -> None:
        PRESET_FILE.write_text(json.dumps(self.presets, indent=2), encoding="utf-8")

    def _refresh_preset_buttons(self) -> None:
        mode = self.active_mode_var.get()
        items = self.presets.get(mode, [])
        for idx, btn in enumerate(self.preset_buttons):
            if idx < len(items):
                item = items[idx]
                val = float(item["value"])
                btn.configure(
                    text=str(item["label"]),
                    state="normal",
                    command=lambda v=val: self._apply_preset_value(v),
                )
            else:
                btn.configure(text="-", state="disabled", command=lambda: None)

    def _apply_preset_value(self, value: float) -> None:
        self.setpoint_var.set(f"{value:.4f}")
        self.run_action(self._apply_setpoint, f"Preset {value} applied")

    def _save_current_to_preset_dialog(self) -> None:
        mode = self.active_mode_var.get()
        if mode not in MODE_META:
            messagebox.showinfo("Presets", "Switch to CC, CV, CR, or CW to save a preset.")
            return
        val = float(self.setpoint_var.get())
        win = tk.Toplevel(self)
        win.title(f"Save {mode} Preset")
        win.transient(self)
        win.grab_set()
        slot_var = tk.StringVar(value="1")
        label_var = tk.StringVar(value=f"{val:g} {MODE_META[mode][1]}")
        ttk.Label(win, text="Preset Slot (1–4):").grid(row=0, column=0, padx=10, pady=8, sticky="w")
        ttk.Combobox(win, textvariable=slot_var, values=["1", "2", "3", "4"], state="readonly", width=6).grid(row=0, column=1, padx=10, pady=8)
        ttk.Label(win, text="Button Label:").grid(row=1, column=0, padx=10, pady=8, sticky="w")
        ttk.Entry(win, textvariable=label_var, width=18).grid(row=1, column=1, padx=10, pady=8)

        def save_action() -> None:
            idx = int(slot_var.get()) - 1
            self.presets.setdefault(mode, list(DEFAULT_PRESETS[mode]))[idx] = {
                "label": label_var.get().strip() or f"{val:g}",
                "value": val,
            }
            self._save_presets()
            self._refresh_preset_buttons()
            win.destroy()

        ttk.Button(win, text="Save", command=save_action).grid(row=2, column=0, columnspan=2, pady=10)

    def _toggle_csv_logging(self) -> None:
        if self._csv_writer is not None:
            self._stop_csv_logging()
            return
        path_str = filedialog.asksaveasfilename(
            title="Save CSV Log",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"kel2050_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
        if not path_str:
            return
        self._csv_path = Path(path_str)
        self._csv_file = open(self._csv_path, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(["timestamp", "mode", "input_on", "voltage_V", "current_A", "power_W", "temp_C"])
        self.csv_status_var.set(f"Logging -> {self._csv_path.name}")
        if self.csv_btn is not None:
            self.csv_btn.configure(text="Stop CSV Logging", style="Danger.TButton")

    def _stop_csv_logging(self) -> None:
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except Exception:
                pass
        self._csv_file = None
        self._csv_writer = None
        self.csv_status_var.set("CSV Logger: Idle")
        if self.csv_btn is not None:
            self.csv_btn.configure(text="Start CSV Logging...", style="Secondary.TButton")

    # --- Sequence / Timer ---
    @staticmethod
    def _format_step_setpoint(mode: str, val: float) -> str:
        if mode in ("OFF", "SHORT"):
            return "-"
        unit = MODE_META[mode][1] if mode in MODE_META else ""
        return f"{val:g} {unit}"

    def _refresh_seq_tree(self) -> None:
        if self.seq_tree is None:
            return
        for item in self.seq_tree.get_children():
            self.seq_tree.delete(item)
        for idx, step in enumerate(self.seq_steps, start=1):
            mode = str(step["mode"])
            val = float(step["value"])
            dur = float(step["duration"])
            self.seq_tree.insert("", "end", iid=str(idx - 1), values=(idx, mode, self._format_step_setpoint(mode, val), f"{dur:g}"))

    def _on_seq_select(self, _event: object = None) -> None:
        if self.seq_tree is None:
            return
        sel = self.seq_tree.selection()
        if not sel:
            return
        step = self.seq_steps[int(sel[0])]
        self.seq_mode_var.set(str(step["mode"]))
        self.seq_val_var.set(f"{float(step['value']):g}")
        self.seq_dur_var.set(f"{float(step['duration']):g}")

    def _parse_seq_editor(self) -> dict[str, object]:
        mode = self.seq_mode_var.get().strip().upper()
        dur = float(self.seq_dur_var.get())
        if dur <= 0:
            raise ValueError("Duration must be greater than 0 seconds.")
        val = 0.0 if mode in ("OFF", "SHORT") else float(self.seq_val_var.get())
        return {"mode": mode, "value": val, "duration": dur}

    def _add_seq_step(self) -> None:
        try:
            self.seq_steps.append(self._parse_seq_editor())
            self._refresh_seq_tree()
        except Exception as exc:
            messagebox.showerror("Invalid Step", str(exc))

    def _update_seq_step(self) -> None:
        if self.seq_tree is None:
            return
        sel = self.seq_tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            self.seq_steps[idx] = self._parse_seq_editor()
            self._refresh_seq_tree()
            self.seq_tree.selection_set(str(idx))
        except Exception as exc:
            messagebox.showerror("Invalid Step", str(exc))

    def _remove_seq_step(self) -> None:
        if self.seq_tree is None:
            return
        sel = self.seq_tree.selection()
        if not sel:
            return
        del self.seq_steps[int(sel[0])]
        self._refresh_seq_tree()

    def _move_seq_step(self, delta: int) -> None:
        if self.seq_tree is None:
            return
        sel = self.seq_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        new_idx = idx + delta
        if 0 <= new_idx < len(self.seq_steps):
            self.seq_steps[idx], self.seq_steps[new_idx] = self.seq_steps[new_idx], self.seq_steps[idx]
            self._refresh_seq_tree()
            self.seq_tree.selection_set(str(new_idx))

    def _clear_seq_steps(self) -> None:
        self.seq_steps.clear()
        self._refresh_seq_tree()

    def _save_seq_json(self) -> None:
        path_str = filedialog.asksaveasfilename(title="Save Sequence", defaultextension=".json", filetypes=[("JSON files", "*.json")])
        if path_str:
            Path(path_str).write_text(json.dumps(self.seq_steps, indent=2), encoding="utf-8")

    def _load_seq_json(self) -> None:
        path_str = filedialog.askopenfilename(title="Load Sequence", filetypes=[("JSON files", "*.json")])
        if not path_str:
            return
        try:
            data = json.loads(Path(path_str).read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.seq_steps = [{"mode": str(s["mode"]).upper(), "value": float(s.get("value", 0.0)), "duration": float(s["duration"])} for s in data]
                self._refresh_seq_tree()
        except Exception as exc:
            messagebox.showerror("Load Error", str(exc))

    def _set_seq_running_ui(self, running: bool) -> None:
        if self.seq_start_btn is not None:
            self.seq_start_btn.configure(state="disabled" if running else "normal")
        if self.seq_pause_btn is not None:
            self.seq_pause_btn.configure(state="normal" if running else "disabled", text="Pause")
        if self.seq_stop_btn is not None:
            self.seq_stop_btn.configure(state="normal" if running else "disabled")

    def _start_sequence(self) -> None:
        if self.device is None or not self.device.is_open:
            messagebox.showwarning("Not Connected", "Connect to RS-KEL2050 first.")
            return
        if not self.seq_steps:
            messagebox.showwarning("Empty Sequence", "Add at least one step first.")
            return
        if self.seq_thread is not None and self.seq_thread.is_alive():
            return
        try:
            repeats = int(self.seq_repeat_var.get())
            if repeats < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Repeat", "Repeat cycles must be 0 (infinite) or a positive integer.")
            return

        try:
            dwell = max(0.2, float(self.seq_dwell_var.get()))
        except ValueError:
            messagebox.showerror("Invalid Dwell", "Safety OFF dwell must be a positive number (e.g. 1.0).")
            return

        steps = [dict(s) for s in self.seq_steps]
        safe_stop = self.seq_safe_stop_var.get()
        self.seq_stop_event.clear()
        self.seq_pause_event.clear()
        self._set_seq_running_ui(True)

        def runner() -> None:
            dev = self._dev()
            cycle = 0
            try:
                while not self.seq_stop_event.is_set() and (repeats == 0 or cycle < repeats):
                    cycle += 1
                    cycle_lbl = f"Cycle {cycle}" + (f"/{repeats}" if repeats > 0 else "/∞")
                    for idx, step in enumerate(steps, start=1):
                        if self.seq_stop_event.is_set():
                            break
                        mode = str(step["mode"])
                        val = float(step["value"])
                        dur = float(step["duration"])

                        dev.set_input(False)
                        self.ui_queue.put((
                            "seq_progress",
                            (f"{cycle_lbl} | Step {idx}/{len(steps)}: Safety OFF Dwell ({dwell:.1f}s)...",
                             f"Switching to {mode}...", 0.0),
                        ))
                        if self.seq_stop_event.wait(dwell):
                            break

                        if mode != "OFF":
                            dev.set_mode(mode)
                            time.sleep(0.08)
                            if mode == "CC":
                                dev.set_current(val)
                            elif mode == "CV":
                                dev.set_voltage(val)
                            elif mode == "CR":
                                dev.set_resistance(val)
                            elif mode == "CW":
                                dev.set_power(val)
                            time.sleep(0.05)
                            dev.set_input(True)

                        elapsed = 0.0
                        last_t = time.monotonic()
                        while elapsed < dur and not self.seq_stop_event.is_set():
                            now = time.monotonic()
                            dt = now - last_t
                            last_t = now
                            if not self.seq_pause_event.is_set():
                                elapsed += dt
                            rem = max(0.0, dur - elapsed)
                            pct = min(100.0, (elapsed / dur) * 100.0)
                            state_tag = " [PAUSED]" if self.seq_pause_event.is_set() else ""
                            self.ui_queue.put((
                                "seq_progress",
                                (f"{cycle_lbl} | Step {idx}/{len(steps)}: {mode} {self._format_step_setpoint(mode, val)}{state_tag}",
                                 f"Remaining: {rem:.1f} s / {dur:g} s", pct),
                            ))
                            self.seq_stop_event.wait(0.1)
                final_msg = "Sequence: Stopped" if self.seq_stop_event.is_set() else "Sequence: Completed"
            except Exception as exc:
                final_msg = f"Sequence Error: {exc}"
            finally:
                if safe_stop and self.device is not None and self.device.is_open:
                    try:
                        self.device.set_input(False)
                    except Exception:
                        pass
                self.ui_queue.put(("seq_done", final_msg))

        self.seq_thread = threading.Thread(target=runner, daemon=True)
        self.seq_thread.start()

    def _toggle_pause_sequence(self) -> None:
        if self.seq_pause_event.is_set():
            self.seq_pause_event.clear()
            if self.seq_pause_btn is not None:
                self.seq_pause_btn.configure(text="Pause")
        else:
            self.seq_pause_event.set()
            if self.seq_pause_btn is not None:
                self.seq_pause_btn.configure(text="Resume")

    def _stop_sequence(self, wait: bool = False) -> None:
        self.seq_stop_event.set()
        self.seq_pause_event.clear()
        if wait and self.seq_thread is not None and self.seq_thread.is_alive():
            self.seq_thread.join(timeout=1.5)

    # --- Logging & Lifecycle ---
    def _append_log(self, direction: str, text: str) -> None:
        if direction == "TX":
            self.tx_last_var.set(f"TX: {text}")
        else:
            self.rx_last_var.set(f"RX: {text}")
        if self.log_text is None:
            return
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.insert("end", f"[{stamp}] {direction}: {text}\n")
        self.log_text.see("end")

    def clear_log(self) -> None:
        if self.log_text is not None:
            self.log_text.delete("1.0", "end")

    def _on_close(self) -> None:
        self._stop_csv_logging()
        self._disconnect()
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="RS PRO RS-KEL2050 DC Electronic Load GUI Controller")
    parser.add_argument("--port", default="", help="Preselected serial port (e.g. COM8 or /dev/cu.usbmodem...)")
    args = parser.parse_args()
    app = RSKEL2050App(preselected_port=args.port)
    app.mainloop()


if __name__ == "__main__":
    main()
