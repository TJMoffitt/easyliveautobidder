"""DOM monitoring — reads lot info, bid amounts, and button state from the page."""

import asyncio
import re

from models import ACCENT_GREEN


DOM_SCRAPE_JS = """
() => {
    const txt = s => { try { return document.querySelector(s)?.innerText?.trim() || ''; } catch { return ''; } };
    const vis = s => { try { const e = document.querySelector(s); return e && e.offsetParent !== null; } catch { return false; } };
    return {
        lotNo: txt('#bid-live-lot-no'),
        lotDesc: txt('#bid-live-lot-desc, .bid-live-lot-desc'),
        lotEst: txt('#bid-live-lot-est, .bid-live-lot-est'),
        currentBid: txt('.bid-live-current-bid .current-bid, #bid-live-current-bid'),
        auctioneerMsg: txt('#auctioneer-message'),
        bidButtonVisible: vis('#bid-live-get-ready') || vis('#bid-live-bidding-soon'),
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

    def set_page(self, page):
        self.page = page

    async def run_loop(self, running_check):
        while running_check():
            await asyncio.sleep(0.5)
            try:
                data = await self.page.evaluate(DOM_SCRAPE_JS)

                lot_no = data.get("lotNo", "")
                bid_text = data.get("currentBid", "")
                match = re.search(r"[\£\$]?\s*([0-9,]+)", bid_text)
                bid_amount = int(match.group(1).replace(",", "")) if match else 0

                lot = self.state.lot
                lot.lot_number = lot_no
                lot.description = data.get("lotDesc", "")
                lot.estimate = data.get("lotEst", "")
                lot.current_bid = bid_amount
                lot.auctioneer_message = data.get("auctioneerMsg", "")
                lot.bid_button_visible = data.get("bidButtonVisible", False)
                lot.bidding_ended = data.get("biddingEnded", False)
                lot.we_are_winning = data.get("winningBadge", False)
                lot.register_required = data.get("registerVisible", False)

                if lot_no != self.prev_lot and lot_no:
                    self.ui.log_decision(
                        f"NEW LOT: #{lot_no} — {lot.description}", "trigger")
                    self.prev_lot = lot_no
                    self.state.closing_signal_active = False
                    self.state.bids_placed_this_lot = 0
                    self.ui.refresh_target_list()

                if bid_amount != self.prev_bid and bid_amount > 0:
                    direction = None
                    if self.prev_bid > 0 and bid_amount > self.prev_bid:
                        direction = "up"
                        self.ui.log_decision(
                            f"BID UP: £{self.prev_bid:,} → £{bid_amount:,}")
                        on_target = not self.target_lots or any(
                            t in (lot_no or "") for t in self.target_lots)
                        if on_target and not lot.we_are_winning:
                            self.state.competitor_bid_active = True
                            self.ui.log_decision(
                                f"COMPETITOR BID on #{lot_no} — responding",
                                "trigger")
                    elif self.prev_bid > 0 and bid_amount < self.prev_bid:
                        direction = "down"
                    self.ui.update_price(bid_amount, direction)
                    self.prev_bid = bid_amount

                msg = data.get("auctioneerMsg", "")
                if msg != self.prev_msg and msg:
                    self.prev_msg = msg

                self.ui.update_lot()

            except Exception:
                pass
