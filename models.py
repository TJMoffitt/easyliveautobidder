"""Data models and shared constants."""

from dataclasses import dataclass, field


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
    bid_label: str = ""  # h4 above the price, e.g. "CURRENT ROOM BID"
    auctioneer_message: str = ""
    bid_button_visible: bool = False
    bidding_ended: bool = False
    we_are_winning: bool = False
    register_required: bool = False


@dataclass
class BotState:
    lot: LotState = field(default_factory=LotState)
    # Per-lot state machine: WAITING -> SNIPE / BID_WAR -> OUT
    #   WAITING  = price descending, nobody has bid, we do nothing
    #   SNIPE    = auctioneer about to pass unsold -> we place first bid
    #   BID_WAR  = real bids exist (ours or theirs) -> counter up to max
    #   OUT      = price exceeded our max, we let it go
    lot_phase: str = "WAITING"
    any_bids_this_lot: bool = False
    we_have_bid_this_lot: bool = False
    closing_signal_active: bool = False
    closing_signal_type: str = ""  # PASS_IMMINENT or SALE_CLOSING
    closing_signal_time: float = 0
    competitor_bid_active: bool = False
    last_bid_placed_at: float = 0
    bids_placed_this_lot: int = 0
    total_bids_placed: int = 0
    auction_history: list = field(default_factory=list)
    total_spent: int = 0
    items_won: int = 0
    whisper_latency_ms: int = 0
    audio_level: float = 0.0
