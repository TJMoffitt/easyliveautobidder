"""DOM monitoring — reads lot info, bid amounts, and button state from the page.

The h4 label above the price is the ground truth for bid state:
  "ASKING BID"                = no live bid, auctioneer descending
  "CURRENT ROOM/INTERNET BID" = a REAL bid is held at this price
Price direction is never used to infer bids.
"""

import asyncio
import re
from dataclasses import replace

from models import ACCENT_RED

# Phrases the site puts in #auctioneer-message when a lot is closing.
# NOT a single source of truth — the message often never appears; the
# audio AI runs in parallel and catches what the DOM doesn't show.
DOM_CLOSING_PATTERNS = (
    "about to close", "fair warning", "final call",
    "last chance", "going once", "going twice", "closing",
)


DOM_SCRAPE_JS = """
() => {
    const txt = s => { try { return document.querySelector(s)?.innerText?.trim() || ''; } catch { return ''; } };
    const vis = s => { try { const e = document.querySelector(s); return e && e.offsetParent !== null; } catch { return false; } };
    return {
        lotNo: txt('#bid-live-lot-no'),
        lotDesc: txt('#bid-live-lot-desc, .bid-live-lot-desc'),
        lotEst: txt('#bid-live-lot-est, .bid-live-lot-est'),
        currentBid: txt('.bid-live-current-bid .current-bid, #bid-live-current-bid'),
        bidLabel: txt('.bid-live-current-bid .h4'),
        auctioneerMsg: (() => {
            // duplicate ids possible (mobile/desktop layouts) and innerText
            // returns '' for hidden elements — check all, fall back to textContent
            const els = document.querySelectorAll('#auctioneer-message');
            for (const e of els) {
                const t = (e.innerText || '').trim() || (e.textContent || '').trim();
                if (t) return t;
            }
            return '';
        })(),
        bidButtonVisible: vis('#bid-live-bid-btn'),
        bidButtonText: txt('#bid-live-bid-btn'),
        getReadyVisible: vis('#bid-live-get-ready') || vis('#bid-live-bidding-soon'),
        biddingEnded: vis('#bid-live-bidding-ended'),
        registerVisible: vis('#bid-live-reg-btn'),
        winningBadge: txt('.bid-live-current-bid').includes('winning') ||
                      txt('.bid-live-current-bid').includes('Winning'),
    };
}
"""


class DomMonitor:
    """Watches the auction page DOM for state changes."""

    def __init__(self, state, ui):
        self.state = state
        self.ui = ui
        self.page = None
        self.target_lots = {}
        self.prev_lot = ""
        self.prev_bid = 0
        self.prev_msg = ""
        self._sale_recorded = False
        self._debug_tick = 0

    def set_page(self, page):
        self.page = page

    async def run_loop(self, running_check):
        while running_check():
            await asyncio.sleep(0.5)
            try:
                data = await self.page.evaluate(DOM_SCRAPE_JS)
                self.process_snapshot(data)
            except Exception:
                pass

    def process_snapshot(self, data):
        """Apply one DOM snapshot (dict in DOM_SCRAPE_JS shape) to the bot
        state. Called by run_loop with live page data, and by the
        simulator with scripted test data — same logic either way."""
        lot_no = data.get("lotNo", "")
        bid_text = data.get("currentBid", "")
        match = re.search(r"[\£\$]?\s*([0-9,]+)", bid_text)
        bid_amount = int(match.group(1).replace(",", "")) if match else 0

        btn_text = data.get("bidButtonText", "")
        btn_match = re.search(r"[\£\$]\s*([0-9,]+)", btn_text)
        btn_amount = (int(btn_match.group(1).replace(",", ""))
                      if btn_match else 0)

        lot = self.state.lot
        # keep a copy of the outgoing lot so an unrecorded sale can be
        # written to history when the lot changes under us
        prev_lot_state = replace(lot)

        lot.lot_number = lot_no
        lot.description = data.get("lotDesc", "")
        lot.estimate = data.get("lotEst", "")
        lot.current_bid = bid_amount
        lot.bid_label = data.get("bidLabel", "")
        lot.auctioneer_message = data.get("auctioneerMsg", "")
        lot.bid_button_visible = data.get("bidButtonVisible", False)
        lot.bid_button_amount = btn_amount
        lot.bidding_ended = data.get("biddingEnded", False)
        lot.we_are_winning = data.get("winningBadge", False)
        lot.register_required = data.get("registerVisible", False)

        # ── New lot: reset the state machine ────────────────────────────
        if lot_no != self.prev_lot and lot_no:
            # Record the outgoing lot as sold if it had a live bid and
            # the SOLD label never appeared (implicit sale on lot change)
            if (self.prev_lot and not self._sale_recorded
                    and self.state.any_bids_this_lot
                    and prev_lot_state.current_bid > 0):
                self.ui.log_decision(
                    f"[DOM] lot changed with live bid held — recording "
                    f"#{prev_lot_state.lot_number} as sold at "
                    f"£{prev_lot_state.current_bid:,}", "debug")
                self.ui.record_sale(prev_lot_state)
            self._sale_recorded = False
            self.ui.log_decision(
                f"NEW LOT: #{lot_no} — {lot.description}", "trigger")
            self.ui.log_debug_screen(
                "lot",
                f"LOT   #{self.prev_lot or '--'} → #{lot_no}  "
                f"({lot.description[:60]})")
            self.prev_lot = lot_no
            # prev_bid=0: the first price of the new lot is a baseline,
            # NOT a movement vs the old lot's price
            self.prev_bid = 0
            self.prev_msg = ""
            self.state.lot_phase = "WAITING"
            self.state.any_bids_this_lot = False
            self.state.we_have_bid_this_lot = False
            self.state.closing_signal_active = False
            self.state.competitor_bid_active = False
            self.state.bids_placed_this_lot = 0
            self.ui.log_decision(
                f"[DEBUG] phase=WAITING — watching price descend, "
                f"will not bid until pass-imminent signal or a real bid",
                "debug")
            self.ui.refresh_target_list()

        # ── Bid state from the label (ground truth) ─────────────────────
        #   "ASKING BID"              = no live bid
        #   "CURRENT ROOM/INTERNET BID" = real bid held
        #   "SOLD TO THE ROOM/INTERNET" = hammer fell — sale confirmed
        label = (lot.bid_label or "").upper()
        has_live_bid = "CURRENT" in label and "BID" in label

        if "SOLD" in label and not self._sale_recorded \
                and lot.current_bid > 0:
            self._sale_recorded = True
            self.ui.log_decision(
                f"[DOM] SOLD label: '{lot.bid_label}' at "
                f"£{lot.current_bid:,} — sale confirmed", "sold")
            self.ui.log_debug_screen(
                "msg", f"SOLD  '{lot.bid_label}' at £{lot.current_bid:,}")
            self.ui.record_sale()

        if bid_amount != self.prev_bid and bid_amount > 0:
            arrow = ("↑" if bid_amount > self.prev_bid > 0
                     else "↓" if 0 < bid_amount < self.prev_bid
                     else "=")
            self.ui.log_debug_screen(
                "bid",
                f"BID   £{self.prev_bid:,} → £{bid_amount:,} {arrow}  "
                f"label='{lot.bid_label}'  btn='{btn_text}'  "
                f"(lot #{lot_no}, phase={self.state.lot_phase})")

            direction = None
            if self.prev_bid > 0 and bid_amount > self.prev_bid:
                direction = "up"
            elif self.prev_bid > 0 and bid_amount < self.prev_bid:
                direction = "down"

            if has_live_bid:
                # Real bid at this price (room or internet)
                if not self.state.any_bids_this_lot:
                    self.ui.log_decision(
                        f"[DOM] FIRST REAL BID: £{bid_amount:,} "
                        f"({lot.bid_label})", "debug")
                self.state.any_bids_this_lot = True
                if self.state.lot_phase in ("WAITING", "SNIPE"):
                    self.state.lot_phase = "BID_WAR"
                    self.ui.log_decision(
                        f"[DEBUG] phase=BID_WAR — live bid held "
                        f"({lot.bid_label} £{bid_amount:,})", "debug")
                if not lot.we_are_winning:
                    self.state.competitor_bid_active = True
            else:
                # ASKING BID: auctioneer adjusting the ask — not a bid
                self.ui.log_decision(
                    f"[DOM] ASK {'RAISED' if direction == 'up' else 'REDUCED'}: "
                    f"£{self.prev_bid:,} → £{bid_amount:,} "
                    f"(label='{lot.bid_label}', not a bid)", "debug")
                if not self.state.we_have_bid_this_lot:
                    if self.state.lot_phase == "BID_WAR":
                        self.ui.log_decision(
                            "[DEBUG] phase=BID_WAR → WAITING — "
                            "label says ASKING, no live bid held", "debug")
                        self.state.lot_phase = "WAITING"
                    self.state.any_bids_this_lot = False
                    self.state.competitor_bid_active = False

            self.ui.update_price(bid_amount, direction)
            self.prev_bid = bid_amount

        # Label can flip ASKING -> CURRENT at the SAME price
        # (someone takes the ask exactly) — catch that too
        elif has_live_bid and not self.state.any_bids_this_lot \
                and bid_amount > 0:
            self.state.any_bids_this_lot = True
            self.ui.log_decision(
                f"[DOM] REAL BID at ask: £{bid_amount:,} "
                f"({lot.bid_label})", "debug")
            self.ui.log_debug_screen(
                "bid",
                f"BID   ask taken at £{bid_amount:,}  "
                f"label='{lot.bid_label}'  "
                f"(lot #{lot_no}, phase={self.state.lot_phase})")
            if self.state.lot_phase in ("WAITING", "SNIPE"):
                self.state.lot_phase = "BID_WAR"
            if not lot.we_are_winning:
                self.state.competitor_bid_active = True

        # ── Auctioneer message ──────────────────────────────────────────
        msg = data.get("auctioneerMsg", "")
        if msg != self.prev_msg and msg:
            self.ui.log_debug_screen(
                "msg", f"MSG   '{self.prev_msg}' → '{msg}'")
            self.prev_msg = msg
            self.ui.log_decision(f"[DOM] auctioneer msg: {msg}", "debug")

            lower_msg = msg.lower()

            # Re-opened = the closing (or even a recorded sale) is off
            if "re-opened" in lower_msg or "reopened" in lower_msg:
                if self.state.closing_signal_active:
                    self.state.closing_signal_active = False
                    self.ui.log_decision(
                        "[DEBUG] bidding re-opened — closing signal "
                        "cleared", "debug")
                if self._sale_recorded:
                    self._sale_recorded = False
                    self.ui.undo_sale()
                    self.ui.log_decision(
                        "[DEBUG] bidding re-opened after SOLD — sale "
                        "un-recorded, lot is live again", "debug")

            # Instant closing trigger when the site shows it.
            # (Bonus signal only — audio AI is the primary source
            # because this message frequently never appears.)
            elif any(k in lower_msg for k in DOM_CLOSING_PATTERNS):
                sig_type = ("SALE_CLOSING"
                            if self.state.any_bids_this_lot
                            else "PASS_IMMINENT")
                self.state.closing_signal_active = True
                self.state.closing_signal_type = sig_type
                self.state.closing_signal_time = \
                    asyncio.get_event_loop().time()
                self.ui.log_decision(
                    f">>> DOM CLOSING SIGNAL ({sig_type}): "
                    f"'{msg}' <<<", "trigger")
                self.ui.set_status(f"CLOSING (DOM): {msg[:30]}",
                                   ACCENT_RED)
                self.ui.flash_alert("HIGH")

        # ── Periodic debug snapshot every ~10s ──────────────────────────
        self._debug_tick += 1
        if self._debug_tick >= 20:
            self._debug_tick = 0
            self.ui.log_decision(
                f"[DOM] lot=#{lot_no} ask=£{bid_amount:,} "
                f"label='{lot.bid_label}' "
                f"phase={self.state.lot_phase} "
                f"bids_exist={self.state.any_bids_this_lot} "
                f"btn={'Y' if lot.bid_button_visible else 'N'} "
                f"btn_text='{btn_text}' "
                f"winning={'Y' if lot.we_are_winning else 'N'} "
                f"ended={'Y' if lot.bidding_ended else 'N'}",
                "debug")

        self.ui.update_lot()
