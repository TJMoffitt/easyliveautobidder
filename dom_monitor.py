"""DOM monitoring — reads lot info, bid amounts, and button state from the page.

Key job besides scraping: classify price movement.
  price DOWN = auctioneer reducing the ask (nobody wants it yet) -> stay WAITING
  price UP   = a real bid was placed -> the waiting game is over -> BID_WAR
"""

import asyncio
import re


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
        bidButtonText: txt('#bid-live-get-ready') || txt('#bid-live-bidding-soon'),
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
        self._debug_tick = 0

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

                # ── New lot: reset the state machine ────────────────────
                if lot_no != self.prev_lot and lot_no:
                    self.ui.log_decision(
                        f"NEW LOT: #{lot_no} — {lot.description}", "trigger")
                    self.prev_lot = lot_no
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

                # ── Price movement classification ───────────────────────
                if bid_amount != self.prev_bid and bid_amount > 0:
                    direction = None
                    if self.prev_bid > 0 and bid_amount > self.prev_bid:
                        # Price UP = someone placed a real bid
                        direction = "up"
                        self.state.any_bids_this_lot = True
                        self.ui.log_decision(
                            f"[DOM] REAL BID: £{self.prev_bid:,} → £{bid_amount:,} "
                            f"(winning={lot.we_are_winning})")
                        if self.state.lot_phase in ("WAITING", "SNIPE"):
                            self.state.lot_phase = "BID_WAR"
                            self.ui.log_decision(
                                "[DEBUG] phase=BID_WAR — real bids exist, "
                                "waiting game over", "debug")
                        if not lot.we_are_winning:
                            self.state.competitor_bid_active = True
                            self.ui.log_decision(
                                f"[DEBUG] competitor bid on #{lot_no} — "
                                f"decision loop will evaluate counter-bid",
                                "debug")
                    elif self.prev_bid > 0 and bid_amount < self.prev_bid:
                        # Price DOWN = auctioneer reducing the ask, no bids
                        direction = "down"
                        self.ui.log_decision(
                            f"[DOM] ASK REDUCED: £{self.prev_bid:,} → £{bid_amount:,} "
                            f"(auctioneer dropping, phase={self.state.lot_phase})",
                            "debug")
                    self.ui.update_price(bid_amount, direction)
                    self.prev_bid = bid_amount

                msg = data.get("auctioneerMsg", "")
                if msg != self.prev_msg and msg:
                    self.prev_msg = msg
                    self.ui.log_decision(f"[DOM] auctioneer msg: {msg}", "debug")

                # ── Periodic debug snapshot every ~10s ──────────────────
                self._debug_tick += 1
                if self._debug_tick >= 20:
                    self._debug_tick = 0
                    self.ui.log_decision(
                        f"[DOM] lot=#{lot_no} ask=£{bid_amount:,} "
                        f"phase={self.state.lot_phase} "
                        f"bids_exist={self.state.any_bids_this_lot} "
                        f"btn={'Y' if lot.bid_button_visible else 'N'} "
                        f"btn_text='{data.get('bidButtonText', '')}' "
                        f"winning={'Y' if lot.we_are_winning else 'N'} "
                        f"ended={'Y' if lot.bidding_ended else 'N'}",
                        "debug")

                self.ui.update_lot()

            except Exception:
                pass
