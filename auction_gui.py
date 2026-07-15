"""
Auction Sniper - Control Room GUI

Bloomberg-terminal-style dark interface for real-time auction monitoring.
Audio = trigger only (AI-analysed). DOM = source of truth. Decision chaining across lots.

Modules:
  models.py      — data classes and colour constants
  audio_engine.py — Whisper transcription + GPT-4o closing signal analysis
  dom_monitor.py  — page scraping for lot/bid/button state
  bid_engine.py   — bid evaluation and placement
"""

import asyncio
import json
import os
import threading
import tkinter as tk
from tkinter import messagebox
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import async_playwright

from models import (
    BG_DARK, BG_PANEL, BG_CARD, BG_INPUT, BORDER,
    TEXT, TEXT_DIM, TEXT_BRIGHT,
    ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED, ACCENT_AMBER, ACCENT_PURPLE,
    PRICE_GREEN, PRICE_RED,
    SoldItem, LotState, BotState,
)
from audio_engine import AudioEngine
from dom_monitor import DomMonitor
from bid_engine import BidEngine

load_dotenv()


# ════════════════════════════════════════════════════════════════════════
#  UI CALLBACK INTERFACE
#  Engines call these methods on the ControlRoom via self.ui.*
# ════════════════════════════════════════════════════════════════════════

class ControlRoom:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AUCTION SNIPER — Control Room")
        self.root.geometry("1280x820")
        self.root.configure(bg=BG_DARK)
        self.root.minsize(1000, 650)

        self.config = self._load_config()
        self.state = BotState()
        self.running = False
        self.page = None
        self.openai_client = None
        self.prev_bid = 0
        self.price_flash_job = None
        self.target_lots = {}

        self.audio_engine = None
        self.dom_monitor = None
        self.bid_engine = None
        self.debug_window = None

        self._build_ui()
        self._open_debug_window()

    # ── Config ──────────────────────────────────────────────────────────

    def _load_config(self):
        try:
            with open("config.json") as f:
                return json.load(f)
        except FileNotFoundError:
            return {
                "auction_url": "",
                "max_bid_gbp": 500,
                "target_lots": [],
                "live_mode": False,
                "bid_strategy": {
                    "snipe_on_going_once": True,
                    "snipe_on_going_twice": True,
                    "bid_increment": 10,
                    "reaction_delay_ms": 500,
                    "budget_limit": 0,
                },
                "speech_to_text": {
                    "model": "whisper-1",
                    "language": "en",
                    "chunk_duration_seconds": 3,
                },
                "audio_sample_rate": 16000,
            }

    # ── Widget helpers ──────────────────────────────────────────────────

    def _make_panel(self, parent, **kw):
        f = tk.Frame(parent, bg=BG_PANEL,
                     highlightbackground=BORDER, highlightthickness=1)
        f.pack(**kw)
        return f

    def _make_label(self, parent, text="", font=("Consolas", 10),
                    fg=TEXT, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg,
                        bg=parent["bg"], anchor="w", **kw)

    def _make_entry(self, parent, var, width=10):
        return tk.Entry(parent, textvariable=var, width=width,
                        font=("Consolas", 10), bg=BG_INPUT, fg=TEXT_BRIGHT,
                        insertbackground=TEXT, relief="flat",
                        highlightbackground=BORDER, highlightthickness=1)

    def _make_text_box(self, parent, height=10):
        return tk.Text(parent, height=height, bg=BG_CARD, fg=TEXT,
                       font=("Consolas", 9), wrap=tk.WORD, relief="flat",
                       highlightthickness=0, insertbackground=TEXT,
                       state=tk.DISABLED, padx=8, pady=6)

    # ── UI Construction ─────────────────────────────────────────────────

    def _build_ui(self):
        # Top control bar
        top_bar = tk.Frame(self.root, bg=BG_PANEL, pady=6, padx=10)
        top_bar.pack(fill=tk.X)

        self._make_label(top_bar, "AUCTION SNIPER",
                         font=("Consolas", 13, "bold"),
                         fg=ACCENT_BLUE).pack(side=tk.LEFT)

        self.status_canvas = tk.Canvas(top_bar, width=14, height=14,
                                       bg=BG_PANEL, highlightthickness=0)
        self.status_canvas.pack(side=tk.LEFT, padx=(12, 4))
        self.status_dot = self.status_canvas.create_oval(
            2, 2, 12, 12, fill=TEXT_DIM, outline="")

        self.status_text = self._make_label(
            top_bar, "IDLE", font=("Consolas", 10, "bold"), fg=TEXT_DIM)
        self.status_text.pack(side=tk.LEFT)

        right_ctrl = tk.Frame(top_bar, bg=BG_PANEL)
        right_ctrl.pack(side=tk.RIGHT)

        self.live_var = tk.BooleanVar(value=False)
        self.live_cb = tk.Checkbutton(
            right_ctrl, text="LIVE", variable=self.live_var,
            font=("Consolas", 10, "bold"), fg=ACCENT_RED, bg=BG_PANEL,
            selectcolor=BG_CARD, activebackground=BG_PANEL,
            activeforeground=ACCENT_RED, command=self._on_live_toggle)
        self.live_cb.pack(side=tk.RIGHT, padx=5)

        tk.Button(right_ctrl, text="DEBUG", font=("Consolas", 9, "bold"),
                  bg=BG_CARD, fg=ACCENT_PURPLE, relief="flat", cursor="hand2",
                  command=self._toggle_debug_window, padx=8).pack(
                      side=tk.RIGHT, padx=5)

        self.start_btn = tk.Button(
            right_ctrl, text="  START  ", font=("Consolas", 11, "bold"),
            bg=ACCENT_GREEN, fg=BG_DARK, relief="flat", cursor="hand2",
            command=self._on_start, activebackground="#16a34a")
        self.start_btn.pack(side=tk.RIGHT, padx=10)

        # Config row
        cfg_bar = tk.Frame(self.root, bg=BG_DARK, pady=4, padx=10)
        cfg_bar.pack(fill=tk.X)

        self._make_label(cfg_bar, "URL", fg=TEXT_DIM,
                         font=("Consolas", 9)).pack(side=tk.LEFT)
        self.url_var = tk.StringVar(
            value=self.config.get("auction_url", ""))
        self._make_entry(cfg_bar, self.url_var, width=55).pack(
            side=tk.LEFT, padx=(4, 12))

        self._make_label(cfg_bar, "MAX £", fg=TEXT_DIM,
                         font=("Consolas", 9)).pack(side=tk.LEFT)
        self.max_bid_var = tk.StringVar(
            value=str(self.config["max_bid_gbp"]))
        self._make_entry(cfg_bar, self.max_bid_var, width=6).pack(
            side=tk.LEFT, padx=(4, 12))

        self._make_label(cfg_bar, "BUDGET £", fg=TEXT_DIM,
                         font=("Consolas", 9)).pack(side=tk.LEFT)
        self.budget_var = tk.StringVar(
            value=str(self.config["bid_strategy"].get("budget_limit", 0)))
        self._make_entry(cfg_bar, self.budget_var, width=6).pack(
            side=tk.LEFT, padx=(4, 12))

        self.targets_var = tk.StringVar(value="")

        # Main 3-column layout
        main = tk.Frame(self.root, bg=BG_DARK)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=3)
        main.columnconfigure(2, weight=2)

        self._build_left_panel(main)
        self._build_center_panel(main)
        self._build_right_panel(main)

        # Bottom status strip
        bottom = tk.Frame(self.root, bg=BG_PANEL, pady=4, padx=10)
        bottom.pack(fill=tk.X)

        self.conn_label = self._make_label(
            bottom, "DISCONNECTED", font=("Consolas", 9), fg=TEXT_DIM)
        self.conn_label.pack(side=tk.LEFT)

        self._make_label(bottom, "  AUDIO ", font=("Consolas", 9),
                         fg=TEXT_DIM).pack(side=tk.LEFT, padx=(20, 0))
        self.audio_canvas = tk.Canvas(bottom, width=80, height=10,
                                      bg=BG_CARD, highlightthickness=0)
        self.audio_canvas.pack(side=tk.LEFT, padx=4)
        self.audio_bar = self.audio_canvas.create_rectangle(
            0, 0, 0, 10, fill=ACCENT_GREEN, outline="")

        self.latency_label = self._make_label(
            bottom, "WHISPER: --ms", font=("Consolas", 9), fg=TEXT_DIM)
        self.latency_label.pack(side=tk.RIGHT)

    def _build_left_panel(self, parent):
        left = tk.Frame(parent, bg=BG_DARK)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        left.rowconfigure(2, weight=1)

        # Current lot card
        lot_card = tk.Frame(left, bg=BG_PANEL,
                            highlightbackground=BORDER, highlightthickness=1)
        lot_card.pack(fill=tk.X, pady=(0, 4))

        lot_header = tk.Frame(lot_card, bg=BG_PANEL, padx=12, pady=6)
        lot_header.pack(fill=tk.X)
        self._make_label(lot_header, "CURRENT LOT",
                         font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(side=tk.LEFT)
        self.lot_number_label = self._make_label(
            lot_header, "", font=("Consolas", 11, "bold"), fg=ACCENT_BLUE)
        self.lot_number_label.pack(side=tk.RIGHT)

        lot_body = tk.Frame(lot_card, bg=BG_PANEL, padx=12, pady=4)
        lot_body.pack(fill=tk.X, pady=(0, 4))
        self.lot_desc_label = self._make_label(
            lot_body, "--", font=("Consolas", 10), fg=TEXT, wraplength=350)
        self.lot_desc_label.pack(anchor="w")
        self.lot_est_label = self._make_label(
            lot_body, "", font=("Consolas", 9), fg=TEXT_DIM)
        self.lot_est_label.pack(anchor="w", pady=(2, 0))

        # Price display
        price_card = tk.Frame(left, bg=BG_PANEL,
                              highlightbackground=BORDER, highlightthickness=1)
        price_card.pack(fill=tk.X, pady=(0, 4))

        price_inner = tk.Frame(price_card, bg=BG_PANEL, padx=12, pady=10)
        price_inner.pack(fill=tk.X)

        self._make_label(price_inner, "CURRENT BID",
                         font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(anchor="w")
        self.price_label = self._make_label(
            price_inner, "£ --",
            font=("Consolas", 36, "bold"), fg=TEXT_BRIGHT)
        self.price_label.pack(anchor="w", pady=(2, 4))

        self.auctioneer_label = self._make_label(
            price_inner, "", font=("Consolas", 9), fg=ACCENT_AMBER)
        self.auctioneer_label.pack(anchor="w")

        # Transcription feed
        tx_header = tk.Frame(left, bg=BG_DARK)
        tx_header.pack(fill=tk.X, pady=(4, 2))
        self._make_label(tx_header, "LIVE TRANSCRIPTION",
                         font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(side=tk.LEFT)

        self.transcript_box = self._make_text_box(left, height=8)
        self.transcript_box.pack(fill=tk.BOTH, expand=True)

    def _build_center_panel(self, parent):
        center = tk.Frame(parent, bg=BG_DARK)
        center.grid(row=0, column=1, sticky="nsew", padx=4)
        center.rowconfigure(1, weight=1)

        # Strategy engine card
        strat_card = tk.Frame(center, bg=BG_PANEL,
                              highlightbackground=BORDER, highlightthickness=1)
        strat_card.pack(fill=tk.X, pady=(0, 4))

        strat_inner = tk.Frame(strat_card, bg=BG_PANEL, padx=12, pady=8)
        strat_inner.pack(fill=tk.X)

        self._make_label(strat_inner, "STRATEGY ENGINE",
                         font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(anchor="w")

        strat_row = tk.Frame(strat_inner, bg=BG_PANEL)
        strat_row.pack(fill=tk.X, pady=(6, 0))

        eff_frame = tk.Frame(strat_row, bg=BG_CARD, padx=10, pady=6)
        eff_frame.pack(side=tk.LEFT, padx=(0, 8))
        self._make_label(eff_frame, "EFF. MAX",
                         font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.eff_max_label = self._make_label(
            eff_frame, "£500",
            font=("Consolas", 16, "bold"), fg=ACCENT_GREEN)
        self.eff_max_label.pack(anchor="w")

        trend_frame = tk.Frame(strat_row, bg=BG_CARD, padx=10, pady=6)
        trend_frame.pack(side=tk.LEFT, padx=(0, 8))
        self._make_label(trend_frame, "TREND",
                         font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.trend_label = self._make_label(
            trend_frame, "—",
            font=("Consolas", 16, "bold"), fg=TEXT)
        self.trend_label.pack(anchor="w")

        mode_frame = tk.Frame(strat_row, bg=BG_CARD, padx=10, pady=6)
        mode_frame.pack(side=tk.LEFT)
        self._make_label(mode_frame, "MODE",
                         font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.mode_label = self._make_label(
            mode_frame, "NORMAL",
            font=("Consolas", 14, "bold"), fg=TEXT)
        self.mode_label.pack(anchor="w")

        self.chain_label = self._make_label(
            strat_inner, "Waiting for auction data...",
            font=("Consolas", 9), fg=TEXT_DIM, wraplength=380)
        self.chain_label.pack(anchor="w", pady=(8, 0))

        # Price trend mini chart
        chart_frame = tk.Frame(strat_inner, bg=BG_PANEL)
        chart_frame.pack(fill=tk.X, pady=(8, 0))
        self._make_label(chart_frame, "RECENT PRICES",
                         font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.chart_canvas = tk.Canvas(chart_frame, height=50, bg=BG_CARD,
                                      highlightthickness=0)
        self.chart_canvas.pack(fill=tk.X, pady=(2, 0))

        # Decision log
        dec_header = tk.Frame(center, bg=BG_DARK)
        dec_header.pack(fill=tk.X, pady=(4, 2))
        self._make_label(dec_header, "DECISION LOG",
                         font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(side=tk.LEFT)

        self.decision_box = self._make_text_box(center, height=10)
        self.decision_box.pack(fill=tk.BOTH, expand=True)
        self.decision_box.tag_configure("trigger", foreground=ACCENT_AMBER)
        self.decision_box.tag_configure("bid", foreground=ACCENT_GREEN)
        self.decision_box.tag_configure("pass", foreground=TEXT_DIM)
        self.decision_box.tag_configure("sold", foreground=ACCENT_PURPLE)
        self.decision_box.tag_configure("error", foreground=ACCENT_RED)
        self.decision_box.tag_configure("debug", foreground="#475569")

    def _build_right_panel(self, parent):
        right = tk.Frame(parent, bg=BG_DARK)
        right.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        right.rowconfigure(2, weight=1)

        # Stats
        stats_frame = tk.Frame(right, bg=BG_DARK)
        stats_frame.pack(fill=tk.X, pady=(0, 4))

        won_card = tk.Frame(stats_frame, bg=BG_PANEL,
                            highlightbackground=BORDER, highlightthickness=1)
        won_card.pack(fill=tk.X, pady=(0, 4))
        won_inner = tk.Frame(won_card, bg=BG_PANEL, padx=12, pady=8)
        won_inner.pack(fill=tk.X)

        row1 = tk.Frame(won_inner, bg=BG_PANEL)
        row1.pack(fill=tk.X)

        w_left = tk.Frame(row1, bg=BG_PANEL)
        w_left.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self._make_label(w_left, "WON", font=("Consolas", 8),
                         fg=TEXT_DIM).pack(anchor="w")
        self.won_label = self._make_label(
            w_left, "0", font=("Consolas", 20, "bold"), fg=ACCENT_GREEN)
        self.won_label.pack(anchor="w")

        w_right = tk.Frame(row1, bg=BG_PANEL)
        w_right.pack(side=tk.RIGHT, expand=True, fill=tk.X)
        self._make_label(w_right, "SPENT", font=("Consolas", 8),
                         fg=TEXT_DIM).pack(anchor="w")
        self.spent_label = self._make_label(
            w_right, "£0", font=("Consolas", 20, "bold"), fg=TEXT)
        self.spent_label.pack(anchor="w")

        budget_frame = tk.Frame(won_inner, bg=BG_PANEL)
        budget_frame.pack(fill=tk.X, pady=(6, 0))
        self._make_label(budget_frame, "BUDGET", font=("Consolas", 8),
                         fg=TEXT_DIM).pack(anchor="w")
        self.budget_canvas = tk.Canvas(budget_frame, height=8, bg=BG_CARD,
                                       highlightthickness=0)
        self.budget_canvas.pack(fill=tk.X, pady=(2, 0))
        self.budget_bar = self.budget_canvas.create_rectangle(
            0, 0, 0, 8, fill=ACCENT_BLUE, outline="")
        self.budget_pct_label = self._make_label(
            budget_frame, "0%", font=("Consolas", 8), fg=TEXT_DIM)
        self.budget_pct_label.pack(anchor="e")

        # Target lots
        target_card = tk.Frame(right, bg=BG_PANEL,
                               highlightbackground=BORDER, highlightthickness=1)
        target_card.pack(fill=tk.X, pady=(0, 4))

        target_header = tk.Frame(target_card, bg=BG_PANEL, padx=12, pady=6)
        target_header.pack(fill=tk.X)
        self._make_label(target_header, "TARGET LOTS",
                         font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(side=tk.LEFT)
        self.target_count_label = self._make_label(
            target_header, "0 lots", font=("Consolas", 9), fg=TEXT_DIM)
        self.target_count_label.pack(side=tk.RIGHT)

        add_row = tk.Frame(target_card, bg=BG_PANEL, padx=12, pady=4)
        add_row.pack(fill=tk.X)

        self._make_label(add_row, "Lot", font=("Consolas", 9),
                         fg=TEXT_DIM).pack(side=tk.LEFT)
        self.target_lot_var = tk.StringVar()
        self._make_entry(add_row, self.target_lot_var, width=8).pack(
            side=tk.LEFT, padx=(4, 8))

        self._make_label(add_row, "Max £", font=("Consolas", 9),
                         fg=TEXT_DIM).pack(side=tk.LEFT)
        self.target_max_var = tk.StringVar()
        self._make_entry(add_row, self.target_max_var, width=7).pack(
            side=tk.LEFT, padx=(4, 8))

        tk.Button(add_row, text="ADD", font=("Consolas", 9, "bold"),
                  bg=ACCENT_BLUE, fg=BG_DARK, relief="flat", cursor="hand2",
                  command=self._add_target_lot, padx=8).pack(side=tk.LEFT)

        tk.Button(add_row, text="DEL", font=("Consolas", 9, "bold"),
                  bg=ACCENT_RED, fg=BG_DARK, relief="flat", cursor="hand2",
                  command=self._remove_target_lot, padx=8).pack(
                      side=tk.LEFT, padx=(4, 0))

        tgt_list_frame = tk.Frame(target_card, bg=BG_CARD, padx=4, pady=4)
        tgt_list_frame.pack(fill=tk.X, padx=12, pady=(0, 8))

        self.target_listbox = tk.Listbox(
            tgt_list_frame, bg=BG_CARD, fg=TEXT, font=("Consolas", 9),
            height=6, relief="flat", highlightthickness=0,
            selectbackground=BG_INPUT, activestyle="none", borderwidth=0)
        tgt_scroll = tk.Scrollbar(tgt_list_frame,
                                  command=self.target_listbox.yview)
        self.target_listbox.configure(yscrollcommand=tgt_scroll.set)
        tgt_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.target_listbox.pack(fill=tk.X, expand=True)

        # History list
        hist_header = tk.Frame(right, bg=BG_DARK)
        hist_header.pack(fill=tk.X, pady=(0, 2))
        self._make_label(hist_header, "AUCTION HISTORY",
                         font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(side=tk.LEFT)
        self.hist_count_label = self._make_label(
            hist_header, "0 items", font=("Consolas", 9), fg=TEXT_DIM)
        self.hist_count_label.pack(side=tk.RIGHT)

        hist_frame = tk.Frame(right, bg=BG_CARD)
        hist_frame.pack(fill=tk.BOTH, expand=True)

        self.history_listbox = tk.Listbox(
            hist_frame, bg=BG_CARD, fg=TEXT, font=("Consolas", 9),
            relief="flat", highlightthickness=0, selectbackground=BG_INPUT,
            activestyle="none", borderwidth=0)
        scrollbar = tk.Scrollbar(hist_frame,
                                 command=self.history_listbox.yview)
        self.history_listbox.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_listbox.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ── Target lot management ───────────────────────────────────────────

    def _add_target_lot(self):
        lot = self.target_lot_var.get().strip()
        max_str = self.target_max_var.get().strip()
        if not lot:
            return
        try:
            max_bid = int(max_str) if max_str else int(
                self.max_bid_var.get() or 500)
        except ValueError:
            return
        self.target_lots[lot] = max_bid
        self.target_lot_var.set("")
        self.target_max_var.set("")
        self._sync_targets()
        self.refresh_target_list()

    def _remove_target_lot(self):
        sel = self.target_listbox.curselection()
        if not sel:
            return
        text = self.target_listbox.get(sel[0])
        lot_num = text.split()[0].lstrip("#")
        self.target_lots.pop(lot_num, None)
        self._sync_targets()
        self.refresh_target_list()

    def _sync_targets(self):
        """Push target_lots dict to all engines."""
        if self.audio_engine:
            self.audio_engine.target_lots = self.target_lots
        if self.dom_monitor:
            self.dom_monitor.target_lots = self.target_lots
        if self.bid_engine:
            self.bid_engine.target_lots = self.target_lots

    def refresh_target_list(self):
        def do():
            self.target_listbox.delete(0, tk.END)
            for lot, maxb in sorted(self.target_lots.items(),
                                     key=lambda x: x[0]):
                status = ""
                current_lot = self.state.lot.lot_number or ""
                if lot in current_lot:
                    status = "  << ACTIVE"
                for item in self.state.auction_history:
                    if lot in item.lot_number:
                        status = ("  [WON]" if item.won_by_us
                                  else "  [SOLD]")
                self.target_listbox.insert(
                    tk.END, f"  #{lot:<8} Max £{maxb:>6,}{status}")
            n = len(self.target_lots)
            self.target_count_label.config(
                text=f"{n} lot{'s' if n != 1 else ''}")
        self.root.after(0, do)

    # ── Debug window ────────────────────────────────────────────────────

    def _toggle_debug_window(self):
        if getattr(self, "debug_window", None) and \
                self.debug_window.winfo_exists():
            self.debug_window.destroy()
            self.debug_window = None
            return
        self._open_debug_window()

    def _open_debug_window(self):
        self.debug_window = tk.Toplevel(self.root)
        self.debug_window.title("DEBUG — DOM change feed")
        self.debug_window.geometry("720x480")
        self.debug_window.configure(bg=BG_DARK)

        header = tk.Frame(self.debug_window, bg=BG_PANEL, padx=10, pady=6)
        header.pack(fill=tk.X)
        tk.Label(header,
                 text="Timestamped feed of every DOM change "
                      "(lot / bid / screen message)",
                 font=("Consolas", 9), fg=TEXT_DIM, bg=BG_PANEL,
                 anchor="w").pack(side=tk.LEFT)

        box_frame = tk.Frame(self.debug_window, bg=BG_DARK)
        box_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.debug_box = tk.Text(
            box_frame, bg=BG_CARD, fg=TEXT, font=("Consolas", 9),
            wrap=tk.NONE, relief="flat", highlightthickness=0,
            state=tk.DISABLED, padx=8, pady=6)
        dbg_scroll = tk.Scrollbar(box_frame, command=self.debug_box.yview)
        self.debug_box.configure(yscrollcommand=dbg_scroll.set)
        dbg_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.debug_box.pack(fill=tk.BOTH, expand=True)

        self.debug_box.tag_configure("lot", foreground=ACCENT_BLUE)
        self.debug_box.tag_configure("bid", foreground=ACCENT_GREEN)
        self.debug_box.tag_configure("msg", foreground=ACCENT_AMBER)
        self.debug_box.tag_configure("btn", foreground=ACCENT_PURPLE)

    def log_debug_screen(self, kind, text):
        """Append a timestamped line to the debug window (if open).
        kind: 'lot' | 'bid' | 'msg' — colours the line."""
        def do():
            if not getattr(self, "debug_window", None) or \
                    not self.debug_window.winfo_exists():
                return
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self.debug_box.config(state=tk.NORMAL)
            self.debug_box.insert(
                tk.END, f"[{ts}] {text}\n", kind)
            self.debug_box.see(tk.END)
            lines = int(self.debug_box.index("end-1c").split(".")[0])
            if lines > 500:
                self.debug_box.delete("1.0", "100.0")
            self.debug_box.config(state=tk.DISABLED)
        self.root.after(0, do)

    # ════════════════════════════════════════════════════════════════════
    #  UI UPDATE METHODS — called by engines via self.ui.*
    # ════════════════════════════════════════════════════════════════════

    def set_status(self, text, color):
        self.root.after(0, lambda: (
            self.status_text.config(text=text, fg=color),
            self.status_canvas.itemconfig(self.status_dot, fill=color),
        ))

    def set_connection(self, text, color=TEXT_DIM):
        self.root.after(0, lambda: self.conn_label.config(
            text=text, fg=color))

    def log_transcript(self, text):
        def do():
            self.transcript_box.config(state=tk.NORMAL)
            self.transcript_box.insert(tk.END, text + "\n")
            self.transcript_box.see(tk.END)
            lines = int(
                self.transcript_box.index("end-1c").split(".")[0])
            if lines > 200:
                self.transcript_box.delete("1.0", "50.0")
            self.transcript_box.config(state=tk.DISABLED)
        self.root.after(0, do)

    def log_decision(self, text, tag=None):
        def do():
            ts = datetime.now().strftime("%H:%M:%S")
            self.decision_box.config(state=tk.NORMAL)
            if tag:
                self.decision_box.insert(
                    tk.END, f"[{ts}] {text}\n", tag)
            else:
                self.decision_box.insert(tk.END, f"[{ts}] {text}\n")
            self.decision_box.see(tk.END)
            lines = int(
                self.decision_box.index("end-1c").split(".")[0])
            if lines > 300:
                self.decision_box.delete("1.0", "100.0")
            self.decision_box.config(state=tk.DISABLED)
        self.root.after(0, do)

    def update_lot(self):
        lot = self.state.lot
        def do():
            self.lot_number_label.config(
                text=f"#{lot.lot_number}" if lot.lot_number else "")
            self.lot_desc_label.config(text=lot.description or "--")
            self.lot_est_label.config(
                text=f"Est: {lot.estimate}" if lot.estimate else "")
            self.auctioneer_label.config(text=lot.auctioneer_message)
        self.root.after(0, do)

    def update_price(self, amount, direction=None):
        def do():
            self.price_label.config(
                text=f"£{amount:,}" if amount else "£ --")
            if direction == "up":
                self.price_label.config(fg=PRICE_RED)
            elif direction == "down":
                self.price_label.config(fg=PRICE_GREEN)
            else:
                self.price_label.config(fg=TEXT_BRIGHT)

            if self.price_flash_job:
                self.root.after_cancel(self.price_flash_job)
            if direction:
                self.price_flash_job = self.root.after(
                    1500,
                    lambda: self.price_label.config(fg=TEXT_BRIGHT))
        self.root.after(0, do)

    def update_audio_level(self, level):
        def do():
            w = min(80, int(level * 80))
            self.audio_canvas.coords(self.audio_bar, 0, 0, w, 10)
            color = (ACCENT_GREEN if level < 0.6
                     else ACCENT_AMBER if level < 0.85
                     else ACCENT_RED)
            self.audio_canvas.itemconfig(self.audio_bar, fill=color)
        self.root.after(0, do)

    def update_latency(self, ms):
        def do():
            color = (ACCENT_GREEN if ms < 2000
                     else ACCENT_AMBER if ms < 4000
                     else ACCENT_RED)
            self.latency_label.config(
                text=f"WHISPER: {ms}ms", fg=color)
        self.root.after(0, do)

    def flash_alert(self, urgency):
        is_high = urgency == "HIGH"
        flash_color = ACCENT_RED if is_high else ACCENT_AMBER
        flash_count = 10 if is_high else 6

        def beep():
            try:
                import winsound
                if is_high:
                    winsound.Beep(2200, 200)
                    import time as _t
                    _t.sleep(0.05)
                    winsound.Beep(2200, 200)
                else:
                    winsound.Beep(1800, 150)
            except Exception:
                print("\a", end="", flush=True)

        threading.Thread(target=beep, daemon=True).start()

        def do_flash(n):
            if n <= 0:
                self.price_label.config(bg=BG_CARD)
                return
            bg = flash_color if n % 2 == 0 else BG_CARD
            self.price_label.config(bg=bg)
            self.root.after(120, lambda: do_flash(n - 1))

        self.root.after(0, lambda: do_flash(flash_count))

    def update_stats(self):
        def do():
            self.won_label.config(text=str(self.state.items_won))
            self.spent_label.config(
                text=f"£{self.state.total_spent:,}")

            budget = int(self.budget_var.get() or 0)
            if budget > 0:
                pct = min(1.0, self.state.total_spent / budget)
                w = self.budget_canvas.winfo_width()
                self.budget_canvas.coords(
                    self.budget_bar, 0, 0, int(w * pct), 8)
                color = (ACCENT_GREEN if pct < 0.7
                         else ACCENT_AMBER if pct < 0.9
                         else ACCENT_RED)
                self.budget_canvas.itemconfig(
                    self.budget_bar, fill=color)
                self.budget_pct_label.config(
                    text=f"{int(pct * 100)}% of £{budget:,}")
            else:
                self.budget_pct_label.config(text="No limit set")

            self.hist_count_label.config(
                text=f"{len(self.state.auction_history)} items")
        self.root.after(0, do)

    def update_history_list(self):
        def do():
            self.history_listbox.delete(0, tk.END)
            for item in reversed(self.state.auction_history[-50:]):
                marker = " *" if item.won_by_us else ""
                self.history_listbox.insert(tk.END,
                    f"  {item.timestamp}  Lot {item.lot_number:<6}"
                    f"  £{item.sold_price:>5,}{marker}")
        self.root.after(0, do)

    def update_chart(self):
        def do():
            self.chart_canvas.delete("all")
            history = self.state.auction_history
            if len(history) < 2:
                return
            prices = [s.sold_price for s in history[-15:]]
            w = self.chart_canvas.winfo_width()
            h = 50
            if w < 10:
                return
            max_p = max(prices) or 1
            min_p = min(prices)
            rng = max_p - min_p or 1
            bar_w = max(4, (w - 4) // len(prices) - 2)

            max_bid = int(self.max_bid_var.get() or 500)
            max_y = h - int(
                (max_bid - min_p) / rng * (h - 10)) - 5
            max_y = max(2, min(h - 2, max_y))
            self.chart_canvas.create_line(
                0, max_y, w, max_y, fill=ACCENT_RED, dash=(4, 4))

            for i, p in enumerate(prices):
                x = 2 + i * (bar_w + 2)
                bar_h = int((p - min_p) / rng * (h - 14)) + 4
                y = h - bar_h - 2
                color = ACCENT_GREEN if p < max_bid else ACCENT_RED
                self.chart_canvas.create_rectangle(
                    x, y, x + bar_w, h - 2, fill=color, outline="")
        self.root.after(0, do)

    def update_strategy_display(self, override_max=None):
        """Informational ONLY — nothing here changes bid decisions.
        The bid engine uses the per-lot max from TARGET LOTS directly;
        this panel just shows market context and budget state."""
        history = self.state.auction_history
        max_bid = (override_max if override_max is not None
                   else int(self.max_bid_var.get() or 500))
        budget = int(self.budget_var.get() or 0)

        trend_text = "—"
        trend_color = TEXT
        mode_text = "OBSERVE" if not self.target_lots else "TARGETED"
        mode_color = TEXT if not self.target_lots else ACCENT_BLUE
        reasoning = []

        if not self.target_lots:
            reasoning.append(
                "No target lots — will never bid on anything")
        else:
            reasoning.append(
                f"Bidding ONLY on {len(self.target_lots)} targeted "
                f"lot(s), each to its own max")

        if len(history) >= 3:
            recent = history[-5:]
            avg = sum(s.sold_price for s in recent) / len(recent)
            prev_avg = (
                sum(s.sold_price for s in history[-10:-5])
                / len(history[-10:-5])
                if len(history) > 5 else avg)

            if avg > prev_avg * 1.1:
                trend_text = "UP"
                trend_color = PRICE_RED
            elif avg < prev_avg * 0.9:
                trend_text = "DOWN"
                trend_color = PRICE_GREEN
            else:
                trend_text = "FLAT"
            reasoning.append(f"Avg recent sold price: £{avg:.0f}")

        if budget > 0:
            remaining = budget - self.state.total_spent
            reasoning.append(f"Budget: £{remaining:,} remaining")
            if remaining <= 0:
                mode_text = "STOPPED"
                mode_color = ACCENT_RED
                reasoning.append("Budget exhausted — will not bid")

        if self.state.items_won > 0:
            reasoning.append(
                f"Won {self.state.items_won} items"
                f" for £{self.state.total_spent:,}")

        def do():
            self.eff_max_label.config(text=f"£{max_bid:,}")
            self.trend_label.config(text=trend_text, fg=trend_color)
            self.mode_label.config(text=mode_text, fg=mode_color)
            self.chain_label.config(text="\n".join(reasoning))
        self.root.after(0, do)

        return max_bid

    def record_sale(self, lot=None):
        lot = lot or self.state.lot
        if lot.lot_number and lot.current_bid > 0:
            won = lot.we_are_winning and self.state.we_have_bid_this_lot
            sold = SoldItem(
                lot_number=lot.lot_number,
                description=lot.description,
                estimate=lot.estimate,
                sold_price=lot.current_bid,
                timestamp=datetime.now().strftime("%H:%M:%S"),
                won_by_us=won,
            )
            self.state.auction_history.append(sold)
            if won:
                self.state.items_won += 1
                self.state.total_spent += lot.current_bid
                self.log_decision(
                    f"*** WE WON Lot {lot.lot_number}"
                    f" at £{lot.current_bid:,} ***", "bid")
            else:
                self.log_decision(
                    f"SOLD: Lot {lot.lot_number}"
                    f" — £{lot.current_bid:,}", "sold")
            self.update_history_list()
            self.update_stats()
            self.update_chart()
            self.update_strategy_display()
            self.refresh_target_list()

        self.state.closing_signal_active = False
        self.state.bids_placed_this_lot = 0
        self.state.lot_phase = "WAITING"
        self.state.any_bids_this_lot = False
        self.state.we_have_bid_this_lot = False
        self.set_status("RUNNING", ACCENT_GREEN)

    def undo_sale(self):
        """Bidding re-opened after a SOLD — remove the last history entry."""
        if not self.state.auction_history:
            return
        item = self.state.auction_history.pop()
        if item.won_by_us:
            self.state.items_won -= 1
            self.state.total_spent -= item.sold_price
        self.log_decision(
            f"SALE UNDONE: Lot {item.lot_number} £{item.sold_price:,} "
            f"(bidding re-opened)", "sold")
        self.update_history_list()
        self.update_stats()
        self.update_chart()

    # ── Controls ────────────────────────────────────────────────────────

    def _on_live_toggle(self):
        if self.live_var.get():
            if not self.config.get("live_mode", False):
                messagebox.showinfo(
                    "SAFETY LOCK",
                    "live_mode is false in config.json — the bot CANNOT "
                    "place bids.\n\nIt will run in observer mode and log "
                    "WOULD CLICK BID instead.\n\nTo arm real bidding, set "
                    "\"live_mode\": true in config.json AND tick LIVE.")
                self.live_var.set(False)
                return
            if not messagebox.askyesno(
                "LIVE MODE",
                "live_mode is enabled in config.json and you are arming "
                "LIVE bidding.\n\nThis will place REAL BIDS with real "
                "money.\n\nAre you sure?"):
                self.live_var.set(False)

    def _on_start(self):
        if self.running:
            self.running = False
            self.start_btn.config(text="  START  ", bg=ACCENT_GREEN)
            self.set_status("STOPPED", ACCENT_RED)
            return

        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Error", "Enter an auction URL")
            return

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            messagebox.showerror(
                "Error",
                "OPENAI_API_KEY not set.\n"
                "Create a .env file or set it in your environment.")
            return

        self.openai_client = OpenAI(api_key=api_key)

        # Create engines
        self.audio_engine = AudioEngine(
            self.openai_client, self.config, self.state, ui=self)
        self.dom_monitor = DomMonitor(self.state, ui=self,
                                      config=self.config)
        self.bid_engine = BidEngine(self.config, self.state, ui=self)
        self.bid_engine.live_var = self.live_var
        self.bid_engine.max_bid_var = self.max_bid_var
        self.bid_engine.budget_var = self.budget_var
        self._sync_targets()

        self.running = True
        self.start_btn.config(text="  STOP  ", bg=ACCENT_RED)
        self.set_status("CONNECTING", ACCENT_AMBER)

        thread = threading.Thread(
            target=lambda: asyncio.run(self._bot_main()), daemon=True)
        thread.start()

    # ── Bot orchestration ───────────────────────────────────────────────

    async def _bot_main(self):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=False,
                    args=["--autoplay-policy=no-user-gesture-required"])
                context = await browser.new_context()
                page = await context.new_page()
                self.page = page

                self.audio_engine.set_page(page)
                self.dom_monitor.set_page(page)
                self.bid_engine.set_page(page)

                self.log_decision("Connecting to auction...", "trigger")
                self.set_connection("CONNECTING...", ACCENT_AMBER)

                await page.goto(
                    self.url_var.get().strip(),
                    wait_until="networkidle")

                self.log_decision("Page loaded", "bid")
                self.set_status("RUNNING", ACCENT_GREEN)
                self.set_connection("CONNECTED", ACCENT_GREEN)

                await self.audio_engine.inject_audio()
                await self.audio_engine.unmute()

                running = lambda: self.running
                await asyncio.gather(
                    self.audio_engine.run_loop(running),
                    self.dom_monitor.run_loop(running),
                    self.bid_engine.run_loop(running),
                )
        except Exception as e:
            self.log_decision(f"Fatal error: {e}", "error")
            self.set_status("ERROR", ACCENT_RED)
            self.running = False
            self.root.after(0, lambda: self.start_btn.config(
                text="  START  ", bg=ACCENT_GREEN))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = ControlRoom()
    app.run()
