"""Bid decision evaluation and bid placement."""

import asyncio

from models import ACCENT_GREEN, ACCENT_AMBER


class BidEngine:
    """Evaluates whether to bid and places bids."""

    def __init__(self, config, state, ui):
        self.config = config
        self.state = state
        self.ui = ui
        self.page = None
        self.target_lots = {}
        self.live_var = None
        self.max_bid_var = None
        self.budget_var = None

    def set_page(self, page):
        self.page = page

    def get_lot_max(self):
        """Return the max bid for the current lot, or the global default."""
        lot = self.state.lot
        if self.target_lots:
            for tgt, tgt_max in self.target_lots.items():
                if tgt in (lot.lot_number or ""):
                    return tgt_max
            return None
        return int(self.max_bid_var.get() or 500)

    def evaluate(self, trigger="") -> dict:
        """Decide whether to bid. Returns {action, reason, amount?}."""
        lot = self.state.lot
        default_max = int(self.max_bid_var.get() or 500)
        budget = int(self.budget_var.get() or 0)

        if lot.register_required:
            return {"action": "PASS", "reason": "Not registered"}
        if lot.bidding_ended:
            return {"action": "PASS", "reason": "Bidding ended"}
        if lot.we_are_winning:
            return {"action": "PASS", "reason": "Already winning"}

        lot_max = self.get_lot_max()
        if self.target_lots and lot_max is None:
            return {"action": "PASS",
                    "reason": f"Lot {lot.lot_number} not targeted"}
        max_bid = lot_max if lot_max is not None else default_max

        if budget > 0 and self.state.total_spent >= budget:
            return {"action": "PASS", "reason": "Budget exhausted"}

        effective_max = self.ui.update_strategy_display(override_max=max_bid)

        if lot.current_bid >= effective_max:
            return {"action": "PASS",
                    "reason": f"£{lot.current_bid:,} >= max £{effective_max:,}"}

        increment = self.config["bid_strategy"].get("bid_increment", 10)
        next_bid = lot.current_bid + increment
        if next_bid > effective_max:
            return {"action": "PASS",
                    "reason": f"Next £{next_bid:,} > max £{effective_max:,}"}

        if not lot.bid_button_visible:
            return {"action": "WAIT", "reason": "Button not visible"}

        now = asyncio.get_event_loop().time()
        if now - self.state.last_bid_placed_at < 3:
            return {"action": "WAIT", "reason": "Cooldown"}

        return {"action": "BID", "amount": next_bid,
                "reason": f"{trigger} @ £{lot.current_bid:,}"}

    async def place_bid(self, decision: dict):
        """Execute or log a bid."""
        lot = self.state.lot
        reason = decision.get("reason", "")

        lot_max = self.get_lot_max()
        display_max = lot_max if lot_max is not None else int(
            self.max_bid_var.get() or 500)

        if not self.live_var.get():
            self.ui.log_decision(
                f">>> WOULD CLICK BID £{decision['amount']:,} <<<  "
                f"reason={reason}  lot=#{lot.lot_number}  "
                f"current=£{lot.current_bid:,}  max=£{display_max:,}", "bid")
            self.ui.set_status(
                f"WOULD BID £{decision['amount']:,}", ACCENT_AMBER)
            self.ui.flash_alert("MEDIUM")
        else:
            delay = self.config["bid_strategy"].get(
                "reaction_delay_ms", 500) / 1000
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
                    self.ui.log_decision(
                        f"BID PLACED £{decision['amount']:,} "
                        f"on Lot {lot.lot_number}", "bid")
                    self.ui.set_status("BID PLACED", ACCENT_GREEN)
                    self.state.total_spent += decision["amount"]
                    self.state.items_won += 1
                    self.ui.update_stats()
                    self.ui.update_strategy_display()
                else:
                    self.ui.log_decision(
                        "BID FAILED — no button visible", "error")
                    return
            except Exception as e:
                self.ui.log_decision(f"BID ERROR: {e}", "error")

        self.state.last_bid_placed_at = asyncio.get_event_loop().time()
        self.state.bids_placed_this_lot += 1
        self.state.total_bids_placed += 1

    async def run_loop(self, running_check):
        """Decision loop — responds to competitor bids and closing signals."""
        while running_check():
            await asyncio.sleep(0.2)

            trigger = None
            if self.state.competitor_bid_active:
                trigger = "COMPETITOR_BID"
                self.state.competitor_bid_active = False
            elif self.state.closing_signal_active:
                now = asyncio.get_event_loop().time()
                if now - self.state.closing_signal_time > 15:
                    self.state.closing_signal_active = False
                    self.ui.set_status("RUNNING", ACCENT_GREEN)
                    continue
                trigger = self.state.closing_signal_type or "CLOSING"

            if not trigger:
                continue

            decision = self.evaluate(trigger)

            if decision["action"] == "BID":
                await self.place_bid(decision)
                self.state.closing_signal_active = False
                self.ui.set_status("RUNNING", ACCENT_GREEN)
            elif decision["action"] == "PASS":
                self.ui.log_decision(
                    f"PASS: {decision['reason']}", "pass")
                self.state.closing_signal_active = False
                self.ui.set_status("RUNNING", ACCENT_GREEN)
