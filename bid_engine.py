"""Bid decision engine — the per-lot state machine.

    WAITING  — price descending, nobody has bid. DO NOTHING. Every ask
               reduction is money saved.
    SNIPE    — audio says the auctioneer is about to pass the lot UNSOLD
               (PASS_IMMINENT). Place the FIRST bid at the floor price.
    BID_WAR  — real bids exist (someone else bid, or our snipe was
               countered). Counter every competitor bid up to our max.
    OUT      — next bid would exceed our max. Let it go.
"""

import asyncio

from models import ACCENT_GREEN, ACCENT_AMBER, ACCENT_RED


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
        """Max bid for the current lot: per-lot target if set, else global."""
        lot = self.state.lot
        if self.target_lots:
            for tgt, tgt_max in self.target_lots.items():
                if tgt in (lot.lot_number or ""):
                    return tgt_max
            return None  # lot not targeted
        return int(self.max_bid_var.get() or 500)

    def evaluate(self, trigger="") -> dict:
        """Decide whether to bid right now. Returns {action, reason, amount?}."""
        lot = self.state.lot
        budget = int(self.budget_var.get() or 0)

        if lot.register_required:
            return {"action": "PASS", "reason": "Not registered on this auction"}
        if lot.bidding_ended:
            return {"action": "PASS", "reason": "Bidding ended"}
        if lot.we_are_winning:
            return {"action": "PASS", "reason": "Already winning — no need to bid"}

        max_bid = self.get_lot_max()
        if max_bid is None:
            return {"action": "PASS",
                    "reason": f"Lot #{lot.lot_number} not in target list"}

        if budget > 0 and self.state.total_spent >= budget:
            return {"action": "PASS", "reason": "Budget exhausted"}

        if lot.current_bid <= 0:
            return {"action": "WAIT", "reason": "No price visible yet"}

        # SNIPE: first bid at the floor — clicking the button accepts the
        # current asking price, no increment involved.
        # BID_WAR: countering — the button accepts the next ask up.
        if trigger == "SNIPE":
            bid_amount = lot.current_bid
        else:
            increment = self.config["bid_strategy"].get("bid_increment", 10)
            bid_amount = lot.current_bid + increment

        if bid_amount > max_bid:
            self.state.lot_phase = "OUT"
            return {"action": "PASS",
                    "reason": f"Bid £{bid_amount:,} would exceed "
                              f"max £{max_bid:,} — OUT"}

        if not lot.bid_button_visible:
            return {"action": "WAIT",
                    "reason": "Bid button not visible on page"}

        now = asyncio.get_event_loop().time()
        since_last = now - self.state.last_bid_placed_at
        if since_last < 2:
            return {"action": "WAIT",
                    "reason": f"Cooldown ({since_last:.1f}s since last bid)"}

        return {"action": "BID", "amount": bid_amount,
                "reason": f"{trigger} @ ask £{lot.current_bid:,}, "
                          f"max £{max_bid:,}"}

    async def place_bid(self, decision: dict):
        """Execute the bid (LIVE) or log what we would do (observer)."""
        lot = self.state.lot
        reason = decision.get("reason", "")

        max_bid = self.get_lot_max() or int(self.max_bid_var.get() or 500)

        if not self.live_var.get():
            self.ui.log_decision(
                f">>> WOULD CLICK BID £{decision['amount']:,} <<<  "
                f"{reason}  lot=#{lot.lot_number}", "bid")
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
                        f"on Lot #{lot.lot_number} ({reason})", "bid")
                    self.ui.set_status("BID PLACED", ACCENT_GREEN)
                else:
                    self.ui.log_decision(
                        "BID FAILED — no button visible", "error")
                    return
            except Exception as e:
                self.ui.log_decision(f"BID ERROR: {e}", "error")
                return

        # Our bid is a real bid: waiting game permanently over for this lot
        self.state.we_have_bid_this_lot = True
        self.state.any_bids_this_lot = True
        if self.state.lot_phase != "OUT":
            self.state.lot_phase = "BID_WAR"
        self.state.last_bid_placed_at = asyncio.get_event_loop().time()
        self.state.bids_placed_this_lot += 1
        self.state.total_bids_placed += 1
        self.ui.log_decision(
            f"[DEBUG] phase=BID_WAR — we are in, will counter "
            f"competitor bids up to £{max_bid:,}", "debug")

    async def run_loop(self, running_check):
        """Decision loop — reacts to competitor bids and audio signals."""
        while running_check():
            await asyncio.sleep(0.2)

            trigger = None

            # 1. Competitor bid on the lot -> counter immediately (BID_WAR)
            if self.state.competitor_bid_active:
                self.state.competitor_bid_active = False
                if self.state.lot_phase == "OUT":
                    self.ui.log_decision(
                        "[DEBUG] competitor bid but phase=OUT — "
                        "price beyond our max, ignoring", "debug")
                else:
                    trigger = "COMPETITOR_BID"

            # 2. Audio signal
            elif self.state.closing_signal_active:
                now = asyncio.get_event_loop().time()
                if now - self.state.closing_signal_time > 15:
                    self.state.closing_signal_active = False
                    self.ui.log_decision(
                        "[DEBUG] closing signal expired (15s), no action taken",
                        "debug")
                    self.ui.set_status("RUNNING", ACCENT_GREEN)
                    continue

                sig = self.state.closing_signal_type

                if sig == "PASS_IMMINENT":
                    if self.state.any_bids_this_lot:
                        # Bids exist — pass-imminent doesn't apply, stale info
                        self.ui.log_decision(
                            "[DEBUG] PASS_IMMINENT but bids exist — "
                            "treating as SALE_CLOSING", "debug")
                        trigger = "SALE_CLOSING"
                    else:
                        # THE snipe moment: about to go unsold at the floor
                        self.state.lot_phase = "SNIPE"
                        trigger = "SNIPE"
                        self.ui.log_decision(
                            "[DEBUG] phase=SNIPE — lot about to pass unsold, "
                            "placing first bid at floor price", "debug")

                elif sig == "SALE_CLOSING":
                    if self.state.we_have_bid_this_lot and \
                            not self.state.lot.we_are_winning:
                        # We're in this fight and about to lose it
                        trigger = "SALE_CLOSING"
                    elif not self.state.any_bids_this_lot:
                        self.ui.log_decision(
                            "[DEBUG] SALE_CLOSING but no bids seen — "
                            "treating as snipe moment", "debug")
                        self.state.lot_phase = "SNIPE"
                        trigger = "SNIPE"
                    else:
                        # Someone else's sale closing; we never entered.
                        # The strategy allows entering here too if under max.
                        trigger = "SALE_CLOSING"

            if not trigger:
                continue

            decision = self.evaluate(trigger)

            self.ui.log_decision(
                f"[DEBUG] trigger={trigger} -> {decision['action']}: "
                f"{decision['reason']}", "debug")

            if decision["action"] == "BID":
                await self.place_bid(decision)
                self.state.closing_signal_active = False
                self.ui.set_status("RUNNING", ACCENT_GREEN)
            elif decision["action"] == "PASS":
                self.ui.log_decision(f"PASS: {decision['reason']}", "pass")
                self.state.closing_signal_active = False
                self.ui.set_status("RUNNING", ACCENT_GREEN)
            # WAIT: keep the signal alive, retry next tick
