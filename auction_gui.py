"""
Auction Sniper - Control Room GUI

Bloomberg-terminal-style dark interface for real-time auction monitoring.
Audio = trigger only. DOM = source of truth. Decision chaining across lots.
"""

import asyncio
import json
import os
import re
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from dataclasses import dataclass, field
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import async_playwright, Page

load_dotenv()

# If .env doesn't load on Windows, the key can be set directly here:
# os.environ["OPENAI_API_KEY"] = "your-key-here"

# ── Colour palette ──────────────────────────────────────────────────────
BG_DARK = "#0a0e17"
BG_PANEL = "#111827"
BG_CARD = "#1a2234"
BG_INPUT = "#1e293b"
BORDER = "#2a3a52"
TEXT = "#e2e8f0"
TEXT_DIM = "#64748b"
TEXT_BRIGHT = "#f8fafc"
ACCENT_BLUE = "#3b82f6"
ACCENT_GREEN = "#22c55e"
ACCENT_RED = "#ef4444"
ACCENT_AMBER = "#f59e0b"
ACCENT_PURPLE = "#a78bfa"
PRICE_GREEN = "#4ade80"
PRICE_RED = "#f87171"


@dataclass
class SoldItem:
    lot_number: str
    description: str
    estimate: str
    sold_price: int
    timestamp: str
    won_by_us: bool = False


@dataclass
class LotState:
    lot_number: str = ""
    description: str = ""
    estimate: str = ""
    current_bid: int = 0
    auctioneer_message: str = ""
    bid_button_visible: bool = False
    bidding_ended: bool = False
    we_are_winning: bool = False
    register_required: bool = False


@dataclass
class BotState:
    lot: LotState = field(default_factory=LotState)
    closing_signal_active: bool = False
    closing_signal_type: str = ""
    closing_signal_time: float = 0
    last_bid_placed_at: float = 0
    bids_placed_this_lot: int = 0
    total_bids_placed: int = 0
    auction_history: list = field(default_factory=list)
    total_spent: int = 0
    items_won: int = 0
    whisper_latency_ms: int = 0
    audio_level: float = 0.0


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

        self._build_ui()

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
                "speech_to_text": {"model": "whisper-1", "language": "en", "chunk_duration_seconds": 3},
                "audio_sample_rate": 16000,
            }

    # ── UI Construction ─────────────────────────────────────────────────

    def _make_panel(self, parent, **kw):
        f = tk.Frame(parent, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
        f.pack(**kw)
        return f

    def _make_label(self, parent, text="", font=("Consolas", 10), fg=TEXT, **kw):
        l = tk.Label(parent, text=text, font=font, fg=fg, bg=parent["bg"], anchor="w", **kw)
        return l

    def _make_entry(self, parent, var, width=10):
        e = tk.Entry(parent, textvariable=var, width=width, font=("Consolas", 10),
                     bg=BG_INPUT, fg=TEXT_BRIGHT, insertbackground=TEXT,
                     relief="flat", highlightbackground=BORDER, highlightthickness=1)
        return e

    def _make_text_box(self, parent, height=10):
        t = tk.Text(parent, height=height, bg=BG_CARD, fg=TEXT, font=("Consolas", 9),
                    wrap=tk.WORD, relief="flat", highlightthickness=0,
                    insertbackground=TEXT, state=tk.DISABLED, padx=8, pady=6)
        return t

    def _build_ui(self):
        # ── Top control bar ─────────────────────────────────────────────
        top_bar = tk.Frame(self.root, bg=BG_PANEL, pady=6, padx=10)
        top_bar.pack(fill=tk.X)

        self._make_label(top_bar, "AUCTION SNIPER", font=("Consolas", 13, "bold"),
                         fg=ACCENT_BLUE).pack(side=tk.LEFT)

        # Status light
        self.status_canvas = tk.Canvas(top_bar, width=14, height=14, bg=BG_PANEL,
                                       highlightthickness=0)
        self.status_canvas.pack(side=tk.LEFT, padx=(12, 4))
        self.status_dot = self.status_canvas.create_oval(2, 2, 12, 12, fill=TEXT_DIM, outline="")

        self.status_text = self._make_label(top_bar, "IDLE", font=("Consolas", 10, "bold"), fg=TEXT_DIM)
        self.status_text.pack(side=tk.LEFT)

        # Right side controls
        right_ctrl = tk.Frame(top_bar, bg=BG_PANEL)
        right_ctrl.pack(side=tk.RIGHT)

        self.live_var = tk.BooleanVar(value=False)
        self.live_cb = tk.Checkbutton(right_ctrl, text="LIVE", variable=self.live_var,
                                      font=("Consolas", 10, "bold"), fg=ACCENT_RED, bg=BG_PANEL,
                                      selectcolor=BG_CARD, activebackground=BG_PANEL,
                                      activeforeground=ACCENT_RED, command=self._on_live_toggle)
        self.live_cb.pack(side=tk.RIGHT, padx=5)

        self.start_btn = tk.Button(right_ctrl, text="  START  ", font=("Consolas", 11, "bold"),
                                   bg=ACCENT_GREEN, fg=BG_DARK, relief="flat", cursor="hand2",
                                   command=self._on_start, activebackground="#16a34a")
        self.start_btn.pack(side=tk.RIGHT, padx=10)

        # ── Config row ──────────────────────────────────────────────────
        cfg_bar = tk.Frame(self.root, bg=BG_DARK, pady=4, padx=10)
        cfg_bar.pack(fill=tk.X)

        self._make_label(cfg_bar, "URL", fg=TEXT_DIM, font=("Consolas", 9)).pack(side=tk.LEFT)
        self.url_var = tk.StringVar(value=self.config.get("auction_url", ""))
        self._make_entry(cfg_bar, self.url_var, width=55).pack(side=tk.LEFT, padx=(4, 12))

        self._make_label(cfg_bar, "MAX £", fg=TEXT_DIM, font=("Consolas", 9)).pack(side=tk.LEFT)
        self.max_bid_var = tk.StringVar(value=str(self.config["max_bid_gbp"]))
        self._make_entry(cfg_bar, self.max_bid_var, width=6).pack(side=tk.LEFT, padx=(4, 12))

        self._make_label(cfg_bar, "BUDGET £", fg=TEXT_DIM, font=("Consolas", 9)).pack(side=tk.LEFT)
        self.budget_var = tk.StringVar(value=str(self.config["bid_strategy"].get("budget_limit", 0)))
        self._make_entry(cfg_bar, self.budget_var, width=6).pack(side=tk.LEFT, padx=(4, 12))

        self._make_label(cfg_bar, "LOTS", fg=TEXT_DIM, font=("Consolas", 9)).pack(side=tk.LEFT)
        self.targets_var = tk.StringVar(value=",".join(self.config.get("target_lots", [])))
        self._make_entry(cfg_bar, self.targets_var, width=18).pack(side=tk.LEFT, padx=(4, 0))

        # ── Main 3-column layout ────────────────────────────────────────
        main = tk.Frame(self.root, bg=BG_DARK)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=3)
        main.columnconfigure(2, weight=2)

        self._build_left_panel(main)
        self._build_center_panel(main)
        self._build_right_panel(main)

        # ── Bottom status strip ─────────────────────────────────────────
        bottom = tk.Frame(self.root, bg=BG_PANEL, pady=4, padx=10)
        bottom.pack(fill=tk.X)

        self.conn_label = self._make_label(bottom, "DISCONNECTED", font=("Consolas", 9), fg=TEXT_DIM)
        self.conn_label.pack(side=tk.LEFT)

        # Audio level bar
        self._make_label(bottom, "  AUDIO ", font=("Consolas", 9), fg=TEXT_DIM).pack(side=tk.LEFT, padx=(20, 0))
        self.audio_canvas = tk.Canvas(bottom, width=80, height=10, bg=BG_CARD, highlightthickness=0)
        self.audio_canvas.pack(side=tk.LEFT, padx=4)
        self.audio_bar = self.audio_canvas.create_rectangle(0, 0, 0, 10, fill=ACCENT_GREEN, outline="")

        self.latency_label = self._make_label(bottom, "WHISPER: --ms", font=("Consolas", 9), fg=TEXT_DIM)
        self.latency_label.pack(side=tk.RIGHT)

    def _build_left_panel(self, parent):
        left = tk.Frame(parent, bg=BG_DARK)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        left.rowconfigure(2, weight=1)

        # Current lot card
        lot_card = tk.Frame(left, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
        lot_card.pack(fill=tk.X, pady=(0, 4))

        lot_header = tk.Frame(lot_card, bg=BG_PANEL, padx=12, pady=6)
        lot_header.pack(fill=tk.X)
        self._make_label(lot_header, "CURRENT LOT", font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(side=tk.LEFT)
        self.lot_number_label = self._make_label(lot_header, "", font=("Consolas", 11, "bold"),
                                                  fg=ACCENT_BLUE)
        self.lot_number_label.pack(side=tk.RIGHT)

        lot_body = tk.Frame(lot_card, bg=BG_PANEL, padx=12, pady=4)
        lot_body.pack(fill=tk.X, pady=(0, 4))
        self.lot_desc_label = self._make_label(lot_body, "--", font=("Consolas", 10),
                                                fg=TEXT, wraplength=350)
        self.lot_desc_label.pack(anchor="w")
        self.lot_est_label = self._make_label(lot_body, "", font=("Consolas", 9), fg=TEXT_DIM)
        self.lot_est_label.pack(anchor="w", pady=(2, 0))

        # Price display
        price_card = tk.Frame(left, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
        price_card.pack(fill=tk.X, pady=(0, 4))

        price_inner = tk.Frame(price_card, bg=BG_PANEL, padx=12, pady=10)
        price_inner.pack(fill=tk.X)

        self._make_label(price_inner, "CURRENT BID", font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(anchor="w")
        self.price_label = self._make_label(price_inner, "£ --",
                                            font=("Consolas", 36, "bold"), fg=TEXT_BRIGHT)
        self.price_label.pack(anchor="w", pady=(2, 4))

        self.auctioneer_label = self._make_label(price_inner, "", font=("Consolas", 9),
                                                  fg=ACCENT_AMBER)
        self.auctioneer_label.pack(anchor="w")

        # Transcription feed
        tx_header = tk.Frame(left, bg=BG_DARK)
        tx_header.pack(fill=tk.X, pady=(4, 2))
        self._make_label(tx_header, "LIVE TRANSCRIPTION", font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(side=tk.LEFT)

        self.transcript_box = self._make_text_box(left, height=8)
        self.transcript_box.pack(fill=tk.BOTH, expand=True)

    def _build_center_panel(self, parent):
        center = tk.Frame(parent, bg=BG_DARK)
        center.grid(row=0, column=1, sticky="nsew", padx=4)
        center.rowconfigure(1, weight=1)

        # Strategy card
        strat_card = tk.Frame(center, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
        strat_card.pack(fill=tk.X, pady=(0, 4))

        strat_inner = tk.Frame(strat_card, bg=BG_PANEL, padx=12, pady=8)
        strat_inner.pack(fill=tk.X)

        self._make_label(strat_inner, "STRATEGY ENGINE", font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(anchor="w")

        strat_row = tk.Frame(strat_inner, bg=BG_PANEL)
        strat_row.pack(fill=tk.X, pady=(6, 0))

        # Effective max display
        eff_frame = tk.Frame(strat_row, bg=BG_CARD, padx=10, pady=6)
        eff_frame.pack(side=tk.LEFT, padx=(0, 8))
        self._make_label(eff_frame, "EFF. MAX", font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.eff_max_label = self._make_label(eff_frame, "£500",
                                               font=("Consolas", 16, "bold"), fg=ACCENT_GREEN)
        self.eff_max_label.pack(anchor="w")

        # Trend display
        trend_frame = tk.Frame(strat_row, bg=BG_CARD, padx=10, pady=6)
        trend_frame.pack(side=tk.LEFT, padx=(0, 8))
        self._make_label(trend_frame, "TREND", font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.trend_label = self._make_label(trend_frame, "—",
                                             font=("Consolas", 16, "bold"), fg=TEXT)
        self.trend_label.pack(anchor="w")

        # Mode display
        mode_frame = tk.Frame(strat_row, bg=BG_CARD, padx=10, pady=6)
        mode_frame.pack(side=tk.LEFT)
        self._make_label(mode_frame, "MODE", font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.mode_label = self._make_label(mode_frame, "NORMAL",
                                            font=("Consolas", 14, "bold"), fg=TEXT)
        self.mode_label.pack(anchor="w")

        # Chain reasoning
        self.chain_label = self._make_label(strat_inner, "Waiting for auction data...",
                                             font=("Consolas", 9), fg=TEXT_DIM, wraplength=380)
        self.chain_label.pack(anchor="w", pady=(8, 0))

        # Price trend mini chart
        chart_frame = tk.Frame(strat_inner, bg=BG_PANEL)
        chart_frame.pack(fill=tk.X, pady=(8, 0))
        self._make_label(chart_frame, "RECENT PRICES", font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.chart_canvas = tk.Canvas(chart_frame, height=50, bg=BG_CARD, highlightthickness=0)
        self.chart_canvas.pack(fill=tk.X, pady=(2, 0))

        # Decision log
        dec_header = tk.Frame(center, bg=BG_DARK)
        dec_header.pack(fill=tk.X, pady=(4, 2))
        self._make_label(dec_header, "DECISION LOG", font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(side=tk.LEFT)

        self.decision_box = self._make_text_box(center, height=10)
        self.decision_box.pack(fill=tk.BOTH, expand=True)
        self.decision_box.tag_configure("trigger", foreground=ACCENT_AMBER)
        self.decision_box.tag_configure("bid", foreground=ACCENT_GREEN)
        self.decision_box.tag_configure("pass", foreground=TEXT_DIM)
        self.decision_box.tag_configure("sold", foreground=ACCENT_PURPLE)
        self.decision_box.tag_configure("error", foreground=ACCENT_RED)

    def _build_right_panel(self, parent):
        right = tk.Frame(parent, bg=BG_DARK)
        right.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        right.rowconfigure(1, weight=1)

        # Stats cards
        stats_frame = tk.Frame(right, bg=BG_DARK)
        stats_frame.pack(fill=tk.X, pady=(0, 4))

        # Won / Spent
        won_card = tk.Frame(stats_frame, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
        won_card.pack(fill=tk.X, pady=(0, 4))
        won_inner = tk.Frame(won_card, bg=BG_PANEL, padx=12, pady=8)
        won_inner.pack(fill=tk.X)

        row1 = tk.Frame(won_inner, bg=BG_PANEL)
        row1.pack(fill=tk.X)

        w_left = tk.Frame(row1, bg=BG_PANEL)
        w_left.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self._make_label(w_left, "WON", font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.won_label = self._make_label(w_left, "0", font=("Consolas", 20, "bold"), fg=ACCENT_GREEN)
        self.won_label.pack(anchor="w")

        w_right = tk.Frame(row1, bg=BG_PANEL)
        w_right.pack(side=tk.RIGHT, expand=True, fill=tk.X)
        self._make_label(w_right, "SPENT", font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.spent_label = self._make_label(w_right, "£0", font=("Consolas", 20, "bold"), fg=TEXT)
        self.spent_label.pack(anchor="w")

        # Budget bar
        budget_frame = tk.Frame(won_inner, bg=BG_PANEL)
        budget_frame.pack(fill=tk.X, pady=(6, 0))
        self._make_label(budget_frame, "BUDGET", font=("Consolas", 8), fg=TEXT_DIM).pack(anchor="w")
        self.budget_canvas = tk.Canvas(budget_frame, height=8, bg=BG_CARD, highlightthickness=0)
        self.budget_canvas.pack(fill=tk.X, pady=(2, 0))
        self.budget_bar = self.budget_canvas.create_rectangle(0, 0, 0, 8, fill=ACCENT_BLUE, outline="")
        self.budget_pct_label = self._make_label(budget_frame, "0%", font=("Consolas", 8), fg=TEXT_DIM)
        self.budget_pct_label.pack(anchor="e")

        # History list
        hist_header = tk.Frame(right, bg=BG_DARK)
        hist_header.pack(fill=tk.X, pady=(0, 2))
        self._make_label(hist_header, "AUCTION HISTORY", font=("Consolas", 9, "bold"),
                         fg=TEXT_DIM).pack(side=tk.LEFT)
        self.hist_count_label = self._make_label(hist_header, "0 items", font=("Consolas", 9), fg=TEXT_DIM)
        self.hist_count_label.pack(side=tk.RIGHT)

        # History listbox
        hist_frame = tk.Frame(right, bg=BG_CARD)
        hist_frame.pack(fill=tk.BOTH, expand=True)

        self.history_listbox = tk.Listbox(hist_frame, bg=BG_CARD, fg=TEXT, font=("Consolas", 9),
                                          relief="flat", highlightthickness=0, selectbackground=BG_INPUT,
                                          activestyle="none", borderwidth=0)
        scrollbar = tk.Scrollbar(hist_frame, command=self.history_listbox.yview)
        self.history_listbox.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_listbox.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ── UI Update Helpers ───────────────────────────────────────────────

    def _set_status(self, text, color):
        self.root.after(0, lambda: (
            self.status_text.config(text=text, fg=color),
            self.status_canvas.itemconfig(self.status_dot, fill=color),
        ))

    def _set_connection(self, text, color=TEXT_DIM):
        self.root.after(0, lambda: self.conn_label.config(text=text, fg=color))

    def _log_transcript(self, text):
        def do():
            self.transcript_box.config(state=tk.NORMAL)
            self.transcript_box.insert(tk.END, text + "\n")
            self.transcript_box.see(tk.END)
            lines = int(self.transcript_box.index("end-1c").split(".")[0])
            if lines > 200:
                self.transcript_box.delete("1.0", "50.0")
            self.transcript_box.config(state=tk.DISABLED)
        self.root.after(0, do)

    def _log_decision(self, text, tag=None):
        def do():
            ts = datetime.now().strftime("%H:%M:%S")
            self.decision_box.config(state=tk.NORMAL)
            if tag:
                self.decision_box.insert(tk.END, f"[{ts}] {text}\n", tag)
            else:
                self.decision_box.insert(tk.END, f"[{ts}] {text}\n")
            self.decision_box.see(tk.END)
            lines = int(self.decision_box.index("end-1c").split(".")[0])
            if lines > 300:
                self.decision_box.delete("1.0", "100.0")
            self.decision_box.config(state=tk.DISABLED)
        self.root.after(0, do)

    def _update_lot(self):
        lot = self.state.lot
        def do():
            self.lot_number_label.config(text=f"#{lot.lot_number}" if lot.lot_number else "")
            self.lot_desc_label.config(text=lot.description or "--")
            self.lot_est_label.config(text=f"Est: {lot.estimate}" if lot.estimate else "")
            self.auctioneer_label.config(text=lot.auctioneer_message)
        self.root.after(0, do)

    def _update_price(self, amount, direction=None):
        def do():
            self.price_label.config(text=f"£{amount:,}" if amount else "£ --")
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
                    1500, lambda: self.price_label.config(fg=TEXT_BRIGHT))
        self.root.after(0, do)

    def _update_audio_level(self, level):
        def do():
            w = min(80, int(level * 80))
            self.audio_canvas.coords(self.audio_bar, 0, 0, w, 10)
            color = ACCENT_GREEN if level < 0.6 else ACCENT_AMBER if level < 0.85 else ACCENT_RED
            self.audio_canvas.itemconfig(self.audio_bar, fill=color)
        self.root.after(0, do)

    def _update_latency(self, ms):
        def do():
            color = ACCENT_GREEN if ms < 2000 else ACCENT_AMBER if ms < 4000 else ACCENT_RED
            self.latency_label.config(text=f"WHISPER: {ms}ms", fg=color)
        self.root.after(0, do)

    def _flash_alert(self, urgency):
        """Flash the price area and beep. urgency = 'HIGH', 'MEDIUM', or 'LOW'."""
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

    def _update_stats(self):
        def do():
            self.won_label.config(text=str(self.state.items_won))
            self.spent_label.config(text=f"£{self.state.total_spent:,}")

            budget = int(self.budget_var.get() or 0)
            if budget > 0:
                pct = min(1.0, self.state.total_spent / budget)
                w = self.budget_canvas.winfo_width()
                self.budget_canvas.coords(self.budget_bar, 0, 0, int(w * pct), 8)
                color = ACCENT_GREEN if pct < 0.7 else ACCENT_AMBER if pct < 0.9 else ACCENT_RED
                self.budget_canvas.itemconfig(self.budget_bar, fill=color)
                self.budget_pct_label.config(text=f"{int(pct * 100)}% of £{budget:,}")
            else:
                self.budget_pct_label.config(text="No limit set")

            self.hist_count_label.config(text=f"{len(self.state.auction_history)} items")
        self.root.after(0, do)

    def _update_history_list(self):
        def do():
            self.history_listbox.delete(0, tk.END)
            for item in reversed(self.state.auction_history[-50:]):
                marker = " *" if item.won_by_us else ""
                self.history_listbox.insert(tk.END,
                    f"  {item.timestamp}  Lot {item.lot_number:<6}  £{item.sold_price:>5,}{marker}")
        self.root.after(0, do)

    def _update_chart(self):
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
            max_y = h - int((max_bid - min_p) / rng * (h - 10)) - 5
            max_y = max(2, min(h - 2, max_y))
            self.chart_canvas.create_line(0, max_y, w, max_y, fill=ACCENT_RED, dash=(4, 4))

            for i, p in enumerate(prices):
                x = 2 + i * (bar_w + 2)
                bar_h = int((p - min_p) / rng * (h - 14)) + 4
                y = h - bar_h - 2
                color = ACCENT_GREEN if p < max_bid else ACCENT_RED
                self.chart_canvas.create_rectangle(x, y, x + bar_w, h - 2, fill=color, outline="")
        self.root.after(0, do)

    def _update_strategy_display(self):
        history = self.state.auction_history
        max_bid = int(self.max_bid_var.get() or 500)
        budget = int(self.budget_var.get() or 0)

        effective_max = max_bid
        trend_text = "—"
        mode_text = "NORMAL"
        mode_color = TEXT
        reasoning = []

        if len(history) >= 3:
            recent = history[-5:]
            avg = sum(s.sold_price for s in recent) / len(recent)
            prev_avg = sum(s.sold_price for s in history[-10:-5]) / len(history[-10:-5]) if len(history) > 5 else avg

            if avg > prev_avg * 1.1:
                trend_text = "UP"
                trend_color = PRICE_RED
            elif avg < prev_avg * 0.9:
                trend_text = "DOWN"
                trend_color = PRICE_GREEN
            else:
                trend_text = "FLAT"
                trend_color = TEXT

            if avg > max_bid * 0.9:
                effective_max = int(max_bid * 0.8)
                mode_text = "CONSERV"
                mode_color = ACCENT_AMBER
                reasoning.append(f"Avg £{avg:.0f} > 90% of max — reduced to £{effective_max}")
            elif avg < max_bid * 0.3:
                effective_max = int(max_bid * 1.1)
                mode_text = "AGGRESSIVE"
                mode_color = ACCENT_GREEN
                reasoning.append(f"Avg £{avg:.0f} < 30% of max — raised to £{effective_max}")
            else:
                reasoning.append(f"Avg recent price: £{avg:.0f}")
        else:
            trend_color = TEXT
            reasoning.append("Collecting price data...")

        if budget > 0:
            remaining = budget - self.state.total_spent
            reasoning.append(f"Budget: £{remaining:,} remaining")
            if remaining <= 0:
                mode_text = "STOPPED"
                mode_color = ACCENT_RED
                reasoning.append("Budget exhausted — will not bid")

        if self.state.items_won > 0:
            reasoning.append(f"Won {self.state.items_won} items for £{self.state.total_spent:,}")

        def do():
            self.eff_max_label.config(text=f"£{effective_max:,}")
            self.trend_label.config(text=trend_text, fg=trend_color)
            self.mode_label.config(text=mode_text, fg=mode_color)
            self.chain_label.config(text="\n".join(reasoning))
        self.root.after(0, do)

        return effective_max

    # ── Controls ────────────────────────────────────────────────────────

    def _on_live_toggle(self):
        if self.live_var.get():
            if not messagebox.askyesno("LIVE MODE",
                    "This will place REAL BIDS with real money.\n\nAre you sure?"):
                self.live_var.set(False)

    def _on_start(self):
        if self.running:
            self.running = False
            self.start_btn.config(text="  START  ", bg=ACCENT_GREEN)
            self._set_status("STOPPED", ACCENT_RED)
            return

        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Error", "Enter an auction URL")
            return

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            messagebox.showerror("Error", "OPENAI_API_KEY not set.\nCreate a .env file or set it in your environment.")
            return

        self.openai_client = OpenAI(api_key=api_key)
        self.running = True
        self.start_btn.config(text="  STOP  ", bg=ACCENT_RED)
        self._set_status("CONNECTING", ACCENT_AMBER)

        thread = threading.Thread(target=lambda: asyncio.run(self._bot_main()), daemon=True)
        thread.start()

    # ── Bot Logic ───────────────────────────────────────────────────────

    async def _bot_main(self):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=False,
                    args=["--autoplay-policy=no-user-gesture-required"]
                )
                context = await browser.new_context()
                self.page = await context.new_page()

                self._log_decision("Connecting to auction...", "trigger")
                self._set_connection("CONNECTING...", ACCENT_AMBER)

                await self.page.goto(self.url_var.get().strip(), wait_until="networkidle")

                self._log_decision("Page loaded", "bid")
                self._set_status("RUNNING", ACCENT_GREEN)
                self._set_connection("CONNECTED", ACCENT_GREEN)

                await self._inject_audio()
                await self._unmute()

                await asyncio.gather(
                    self._audio_loop(),
                    self._dom_loop(),
                    self._decision_loop(),
                )
        except Exception as e:
            self._log_decision(f"Fatal: {e}", "error")
            self._set_status("ERROR", ACCENT_RED)
            self._set_connection(f"ERROR: {e}", ACCENT_RED)
            self.running = False
            self.root.after(0, lambda: self.start_btn.config(text="  START  ", bg=ACCENT_GREEN))

    async def _inject_audio(self):
        await self.page.evaluate("""
        () => {
            window.__chunks = [];
            window.__capturing = false;
            window.__audioLevel = 0;
            const go = () => {
                let el = document.getElementById('dolbyVideo');
                if (!el) el = document.querySelector('video');
                if (!el) el = document.querySelector('audio');
                if (!el || !el.srcObject) { setTimeout(go, 1000); return; }
                const ctx = new AudioContext({ sampleRate: 16000 });
                const src = ctx.createMediaStreamSource(el.srcObject);
                const analyser = ctx.createAnalyser();
                analyser.fftSize = 256;
                const proc = ctx.createScriptProcessor(4096, 1, 1);
                proc.onaudioprocess = (e) => {
                    if (!window.__capturing) return;
                    const d = e.inputBuffer.getChannelData(0);
                    let sum = 0;
                    const pcm = new Int16Array(d.length);
                    for (let i = 0; i < d.length; i++) {
                        pcm[i] = Math.max(-32768, Math.min(32767, Math.round(d[i] * 32767)));
                        sum += Math.abs(d[i]);
                    }
                    window.__audioLevel = sum / d.length;
                    window.__chunks.push(Array.from(pcm));
                };
                src.connect(analyser);
                src.connect(proc);
                proc.connect(ctx.destination);
                window.__capturing = true;
            };
            go();
        }
        """)
        self._log_decision("Audio capture started")

    async def _unmute(self):
        try:
            btn = self.page.locator("#bid-live-controls-unmute")
            if await btn.is_visible():
                await btn.click()
                self._log_decision("Audio unmuted")
        except Exception:
            pass

    def _analyze_transcript(self, text):
        """Use GPT-4o to classify auctioneer speech as closing/sold/normal."""
        try:
            resp = self.openai_client.chat.completions.create(
                model=self.config.get("openai_model", "gpt-4o"),
                max_tokens=60,
                temperature=0,
                messages=[
                    {"role": "system", "content": (
                        "You analyse live auction transcripts. Classify the auctioneer's intent. "
                        "Reply with EXACTLY one JSON object, no other text.\n"
                        "Fields:\n"
                        '  "status": one of "CLOSING", "SOLD", "NORMAL"\n'
                        '  "urgency": one of "HIGH", "MEDIUM", "LOW"\n'
                        '  "reason": very short phrase (max 5 words)\n\n'
                        "CLOSING = auctioneer is warning the lot is about to sell "
                        "(going once, going twice, last call, fair warning, any more bids, "
                        "lowest I'll go, about to sell, shall I sell, final chance, "
                        "all done, selling now, any takers, last time, etc.)\n"
                        "SOLD = the lot has been sold (sold, hammer down, knocked down, "
                        "congratulations, sold to, etc.)\n"
                        "NORMAL = anything else (describing items, taking bids, price drops, chatter)"
                    )},
                    {"role": "user", "content": text}
                ]
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
            return json.loads(raw)
        except Exception as e:
            self._log_decision(f"AI classify error: {e}", "error")
            return {"status": "NORMAL", "urgency": "LOW", "reason": "error"}

    async def _audio_loop(self):
        self._transcript_buffer = []

        while self.running:
            await asyncio.sleep(3)
            try:
                result = await self.page.evaluate("""
                () => {
                    const c = window.__chunks;
                    window.__chunks = [];
                    return { chunks: c, level: window.__audioLevel || 0 };
                }
                """)
                chunks = result.get("chunks", [])
                level = result.get("level", 0)
                self._update_audio_level(min(1.0, level * 10))

                if not chunks:
                    continue
                pcm = []
                for c in chunks:
                    pcm.extend(c)
                if len(pcm) < 500:
                    continue

                raw = struct.pack(f"<{len(pcm)}h", *pcm)
                size = len(raw)
                wav = struct.pack(
                    "<4sI4s4sIHHIIHH4sI",
                    b"RIFF", 36 + size, b"WAVE", b"fmt ", 16,
                    1, 1, 16000, 32000, 2, 16, b"data", size,
                ) + raw

                t0 = time.time()
                resp = self.openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=("audio.wav", wav, "audio/wav"),
                    language="en",
                    prompt="auction bidding going once going twice sold hammer lot number fair warning"
                )
                latency = int((time.time() - t0) * 1000)
                self._update_latency(latency)

                text = resp.text.strip()
                if not text:
                    continue

                self._log_transcript(text)

                self._transcript_buffer.append(text)
                if len(self._transcript_buffer) > 5:
                    self._transcript_buffer = self._transcript_buffer[-5:]

                context = " | ".join(self._transcript_buffer[-3:])
                analysis = await asyncio.get_event_loop().run_in_executor(
                    None, self._analyze_transcript, context
                )

                status = analysis.get("status", "NORMAL")
                urgency = analysis.get("urgency", "LOW")
                reason = analysis.get("reason", "")

                if status == "CLOSING":
                    self.state.closing_signal_active = True
                    self.state.closing_signal_type = reason.upper()
                    self.state.closing_signal_time = asyncio.get_event_loop().time()
                    color = ACCENT_RED if urgency == "HIGH" else ACCENT_AMBER
                    self._log_decision(f">>> CLOSING: {reason} [{urgency}] <<<", "trigger")
                    self._set_status(f"CLOSING: {reason}", color)
                    self._flash_alert(urgency)

                elif status == "SOLD":
                    self._log_decision(f"SOLD DETECTED: {reason}", "sold")
                    self._record_sale()

            except Exception as e:
                self._log_decision(f"Audio error: {e}", "error")

    def _record_sale(self):
        lot = self.state.lot
        if lot.lot_number and lot.current_bid > 0:
            sold = SoldItem(
                lot_number=lot.lot_number,
                description=lot.description,
                estimate=lot.estimate,
                sold_price=lot.current_bid,
                timestamp=datetime.now().strftime("%H:%M:%S"),
            )
            self.state.auction_history.append(sold)
            self._log_decision(f"SOLD: Lot {lot.lot_number} — £{lot.current_bid:,}", "sold")
            self._update_history_list()
            self._update_stats()
            self._update_chart()
            self._update_strategy_display()

        self.state.closing_signal_active = False
        self.state.bids_placed_this_lot = 0
        self._set_status("RUNNING", ACCENT_GREEN)

    async def _dom_loop(self):
        prev_lot = ""
        prev_bid = 0
        prev_msg = ""

        while self.running:
            await asyncio.sleep(0.4)
            try:
                data = await self.page.evaluate("""
                () => {
                    const txt = (sel) => {
                        const el = document.querySelector(sel);
                        return el ? el.textContent.trim() : '';
                    };
                    const vis = (sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null;
                    };
                    return {
                        lotNo: txt('#bid-live-lot-no'),
                        lotDesc: txt('#bid-live-lot-desc'),
                        lotEst: txt('#bid-live-lot-est-small'),
                        currentBid: txt('.bid-live-current-bid .current-bid'),
                        auctioneerMsg: txt('#auctioneer-message'),
                        bidButtonVisible: vis('#bid-live-get-ready') || vis('#bid-live-bidding-soon'),
                        biddingEnded: vis('#bid-live-bidding-ended'),
                        registerVisible: vis('#bid-live-reg-btn'),
                        winningBadge: txt('.bid-live-current-bid').includes('winning') ||
                                      txt('.bid-live-current-bid').includes('Winning'),
                    };
                }
                """)

                lot_no = data.get("lotNo", "")
                bid_text = data.get("currentBid", "")
                match = re.search(r"[\£\$]?\s*([0-9,]+)", bid_text)
                bid_amount = int(match.group(1).replace(",", "")) if match else 0

                self.state.lot.lot_number = lot_no
                self.state.lot.description = data.get("lotDesc", "")
                self.state.lot.estimate = data.get("lotEst", "")
                self.state.lot.current_bid = bid_amount
                self.state.lot.auctioneer_message = data.get("auctioneerMsg", "")
                self.state.lot.bid_button_visible = data.get("bidButtonVisible", False)
                self.state.lot.bidding_ended = data.get("biddingEnded", False)
                self.state.lot.we_are_winning = data.get("winningBadge", False)
                self.state.lot.register_required = data.get("registerVisible", False)

                if lot_no != prev_lot and lot_no:
                    self._log_decision(f"NEW LOT: #{lot_no} — {self.state.lot.description}", "trigger")
                    prev_lot = lot_no
                    self.state.closing_signal_active = False
                    self.state.bids_placed_this_lot = 0

                if bid_amount != prev_bid and bid_amount > 0:
                    direction = None
                    if prev_bid > 0 and bid_amount > prev_bid:
                        direction = "up"
                        self._log_decision(f"BID UP: £{prev_bid:,} → £{bid_amount:,}")
                    elif prev_bid > 0 and bid_amount < prev_bid:
                        direction = "down"
                    self._update_price(bid_amount, direction)
                    prev_bid = bid_amount

                msg = data.get("auctioneerMsg", "")
                if msg != prev_msg and msg:
                    prev_msg = msg

                self._update_lot()

            except Exception:
                pass

    async def _decision_loop(self):
        while self.running:
            await asyncio.sleep(0.2)

            if not self.state.closing_signal_active:
                continue

            now = asyncio.get_event_loop().time()
            if now - self.state.closing_signal_time > 15:
                self.state.closing_signal_active = False
                self._set_status("RUNNING", ACCENT_GREEN)
                continue

            decision = self._evaluate_bid()

            if decision["action"] == "BID":
                await self._place_bid(decision)
                self.state.closing_signal_active = False
                self._set_status("RUNNING", ACCENT_GREEN)
            elif decision["action"] == "PASS":
                self._log_decision(f"PASS: {decision['reason']}", "pass")
                self.state.closing_signal_active = False
                self._set_status("RUNNING", ACCENT_GREEN)

    def _evaluate_bid(self) -> dict:
        lot = self.state.lot
        max_bid = int(self.max_bid_var.get() or 500)
        budget = int(self.budget_var.get() or 0)
        targets = [t.strip() for t in self.targets_var.get().split(",") if t.strip()]

        if lot.register_required:
            return {"action": "PASS", "reason": "Not registered"}
        if lot.bidding_ended:
            return {"action": "PASS", "reason": "Bidding ended"}
        if lot.we_are_winning:
            return {"action": "PASS", "reason": "Already winning"}
        if targets and not any(t in lot.lot_number for t in targets):
            return {"action": "PASS", "reason": f"Lot {lot.lot_number} not targeted"}
        if budget > 0 and self.state.total_spent >= budget:
            return {"action": "PASS", "reason": "Budget exhausted"}

        effective_max = self._update_strategy_display()

        if lot.current_bid >= effective_max:
            return {"action": "PASS", "reason": f"£{lot.current_bid:,} >= max £{effective_max:,}"}

        increment = self.config["bid_strategy"].get("bid_increment", 10)
        next_bid = lot.current_bid + increment
        if next_bid > effective_max:
            return {"action": "PASS", "reason": f"Next £{next_bid:,} > max £{effective_max:,}"}

        if not lot.bid_button_visible:
            return {"action": "WAIT", "reason": "Button not visible"}

        signal = self.state.closing_signal_type
        strategy = self.config["bid_strategy"]
        if signal == "GOING_ONCE" and not strategy.get("snipe_on_going_once", True):
            return {"action": "PASS", "reason": "Strategy: skip going once"}
        if signal == "GOING_TWICE" and not strategy.get("snipe_on_going_twice", True):
            return {"action": "PASS", "reason": "Strategy: skip going twice"}

        now = asyncio.get_event_loop().time()
        if now - self.state.last_bid_placed_at < 5:
            return {"action": "WAIT", "reason": "Cooldown"}

        return {"action": "BID", "amount": next_bid, "reason": f"{signal} @ £{lot.current_bid:,}"}

    async def _place_bid(self, decision: dict):
        lot = self.state.lot
        signal = self.state.closing_signal_type

        if not self.live_var.get():
            self._log_decision(
                f"WOULD BID £{decision['amount']:,}  |  trigger={signal}  lot={lot.lot_number}  "
                f"current=£{lot.current_bid:,}  max=£{int(self.max_bid_var.get() or 500):,}", "bid")
            self._set_status(f"WOULD BID £{decision['amount']:,}", ACCENT_AMBER)
        else:
            delay = self.config["bid_strategy"].get("reaction_delay_ms", 500) / 1000
            await asyncio.sleep(delay)
            try:
                ready = self.page.locator("#bid-live-get-ready")
                soon = self.page.locator("#bid-live-bidding-soon")
                clicked = False
                if await ready.is_visible():
                    await ready.click()
                    clicked = True
                elif await soon.is_visible():
                    await soon.click()
                    clicked = True

                if clicked:
                    self._log_decision(
                        f"BID PLACED £{decision['amount']:,} on Lot {lot.lot_number}", "bid")
                    self._set_status("BID PLACED", ACCENT_GREEN)
                    self.state.total_spent += decision["amount"]
                    self.state.items_won += 1
                    self._update_stats()
                    self._update_strategy_display()
                else:
                    self._log_decision("BID FAILED — no button visible", "error")
                    return
            except Exception as e:
                self._log_decision(f"BID ERROR: {e}", "error")

        self.state.last_bid_placed_at = asyncio.get_event_loop().time()
        self.state.bids_placed_this_lot += 1
        self.state.total_bids_placed += 1

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = ControlRoom()
    app.run()
