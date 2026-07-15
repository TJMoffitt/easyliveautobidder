"""Auction simulator — replay scripted scenarios through the REAL bot logic.

No login, no browser, no audio. Feeds scripted DOM snapshots and
auctioneer speech through the same DomMonitor / AudioEngine / BidEngine
code the live bot uses, and prints every decision — including exactly
when it WOULD CLICK BID.

Usage:
    python simulator.py                     # run all built-in scenarios
    python simulator.py scenarios/snipe.json  # run one scenario file

Scenario JSON:
{
  "name": "descent then snipe",
  "target_lots": {"416": 100},          // lot -> max bid (empty = all lots)
  "default_max": 500,                   // global max when no targets
  "events": [
    {"wait": 1, "dom": {"lotNo": "Lot 416", "bidLabel": "ASKING BID",
                         "currentBid": "£200", "bidButtonText": "BID NOW £200",
                         "bidButtonVisible": true}},
    {"wait": 2, "audio": "eighty pounds, lowest I'll go, last chance",
     "fallback_signal": {"status": "PASS_IMMINENT", "urgency": "HIGH",
                          "reason": "lowest I'll go"}}
  ]
}

"audio" events are classified by GPT-4o when OPENAI_API_KEY is set;
otherwise the fallback_signal is used so scenarios run fully offline.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from models import BotState
from dom_monitor import DomMonitor
from bid_engine import BidEngine
from audio_engine import AudioEngine

load_dotenv()


# ── Console UI (replaces the tkinter ControlRoom) ───────────────────────

class ConsoleUI:
    """Implements the ui callback interface engines expect, as prints."""

    def __init__(self, state, default_max=500):
        self.state = state
        self.default_max = default_max

    @staticmethod
    def _ts():
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def log_decision(self, text, tag=None):
        marker = {"bid": "★", "trigger": "!", "error": "✗",
                  "sold": "◆", "pass": "·", "debug": " "}.get(tag, " ")
        print(f"[{self._ts()}] {marker} {text}")

    def log_debug_screen(self, kind, text):
        print(f"[{self._ts()}]   [{kind.upper()}] {text}")

    def log_transcript(self, text):
        print(f"[{self._ts()}]   (heard) {text}")

    def set_status(self, text, color=None):
        print(f"[{self._ts()}]   STATUS: {text}")

    def flash_alert(self, urgency):
        print(f"[{self._ts()}]   *** ALERT [{urgency}] ***")

    def record_sale(self, lot=None):
        lot = lot or self.state.lot
        won = lot.we_are_winning and self.state.we_have_bid_this_lot
        if lot.lot_number and lot.current_bid > 0:
            if won:
                print(f"[{self._ts()}] ◆ *** WE WON Lot {lot.lot_number} "
                      f"at £{lot.current_bid:,} ***")
            else:
                print(f"[{self._ts()}] ◆ SOLD: Lot {lot.lot_number} "
                      f"— £{lot.current_bid:,}")
        self.state.closing_signal_active = False
        self.state.bids_placed_this_lot = 0
        self.state.lot_phase = "WAITING"
        self.state.any_bids_this_lot = False
        self.state.we_have_bid_this_lot = False

    def undo_sale(self):
        print(f"[{self._ts()}] ◆ SALE UNDONE (bidding re-opened)")

    def update_strategy_display(self, override_max=None):
        # No decision-chaining history in the simulator — effective max
        # is simply the requested max.
        return override_max if override_max is not None else self.default_max

    # No-ops (GUI-only concerns)
    def set_connection(self, *a, **k): pass
    def update_price(self, *a, **k): pass
    def update_lot(self, *a, **k): pass
    def update_audio_level(self, *a, **k): pass
    def update_latency(self, *a, **k): pass
    def update_stats(self, *a, **k): pass
    def update_history_list(self, *a, **k): pass
    def update_chart(self, *a, **k): pass
    def refresh_target_list(self, *a, **k): pass


class StubVar:
    """Stands in for a tkinter StringVar/BooleanVar."""
    def __init__(self, value):
        self._v = value
    def get(self):
        return self._v


# ── Snapshot defaults ────────────────────────────────────────────────────

DOM_DEFAULTS = {
    "lotNo": "", "lotDesc": "", "lotEst": "", "currentBid": "",
    "bidLabel": "", "auctioneerMsg": "", "bidButtonVisible": False,
    "bidButtonText": "", "getReadyVisible": False, "biddingEnded": False,
    "registerVisible": False, "winningBadge": False,
}


# ── Built-in scenarios ───────────────────────────────────────────────────

BUILTIN_SCENARIOS = [
    {
        "name": "1. SNIPE — descent, nobody bids, auctioneer about to pass",
        "target_lots": {"416": 100},
        "events": [
            {"dom": {"lotNo": "Lot 416", "lotDesc": "OAK SIDE TABLE",
                     "bidLabel": "ASKING BID", "currentBid": "£200",
                     "bidButtonText": "BID NOW £200", "bidButtonVisible": True}},
            {"dom": {"lotNo": "Lot 416", "bidLabel": "ASKING BID",
                     "currentBid": "£150", "bidButtonText": "BID NOW £150",
                     "bidButtonVisible": True}},
            {"dom": {"lotNo": "Lot 416", "bidLabel": "ASKING BID",
                     "currentBid": "£100", "bidButtonText": "BID NOW £100",
                     "bidButtonVisible": True}},
            {"dom": {"lotNo": "Lot 416", "bidLabel": "ASKING BID",
                     "currentBid": "£80", "bidButtonText": "BID NOW £80",
                     "bidButtonVisible": True}},
            {"audio": "eighty pounds then, that's the lowest I'll go, "
                      "last chance or I'll pass it and move on",
             "fallback_signal": {"status": "PASS_IMMINENT",
                                  "urgency": "HIGH",
                                  "reason": "lowest I'll go, or pass"}},
            {"wait": 2, "note": "expect: WOULD CLICK BID £80 (snipe at floor)"},
        ],
    },
    {
        "name": "2. BID WAR — competitor bids on our lot, counter to max, then OUT",
        "target_lots": {"500": 60},
        "events": [
            {"dom": {"lotNo": "Lot 500", "lotDesc": "BRASS LAMP",
                     "bidLabel": "ASKING BID", "currentBid": "£50",
                     "bidButtonText": "BID NOW £50", "bidButtonVisible": True}},
            {"dom": {"lotNo": "Lot 500", "bidLabel": "ASKING BID",
                     "currentBid": "£20", "bidButtonText": "BID NOW £20",
                     "bidButtonVisible": True},
             "note": "descent — no action expected"},
            {"dom": {"lotNo": "Lot 500", "bidLabel": "CURRENT ROOM BID",
                     "currentBid": "£20", "bidButtonText": "BID NOW £25",
                     "bidButtonVisible": True},
             "note": "room bidder takes the £20 ask — expect counter £25"},
            {"wait": 3,
             "dom": {"lotNo": "Lot 500", "bidLabel": "CURRENT ROOM BID",
                     "currentBid": "£30", "bidButtonText": "BID NOW £35",
                     "bidButtonVisible": True},
             "note": "room bidder again at £30 — expect counter £35"},
            {"wait": 3,
             "dom": {"lotNo": "Lot 500", "bidLabel": "CURRENT ROOM BID",
                     "currentBid": "£60", "bidButtonText": "BID NOW £65",
                     "bidButtonVisible": True},
             "note": "room at £60, next is £65 > max £60 — expect OUT"},
            {"wait": 2},
        ],
    },
    {
        "name": "3. NOT TARGETED — closing signal on a lot we don't want",
        "target_lots": {"999": 50},
        "events": [
            {"dom": {"lotNo": "Lot 700", "lotDesc": "CHINA SET",
                     "bidLabel": "ASKING BID", "currentBid": "£10",
                     "bidButtonText": "BID NOW £10", "bidButtonVisible": True}},
            {"audio": "ten pounds anywhere, last chance, all done, "
                      "I'll pass it",
             "fallback_signal": {"status": "PASS_IMMINENT",
                                  "urgency": "HIGH",
                                  "reason": "last chance, pass it"}},
            {"wait": 2, "note": "expect: PASS (not in target list)"},
        ],
    },
    {
        "name": "5. SOLD LABEL + RE-OPEN — sale confirmed by DOM, then re-opened",
        "target_lots": {"1120": 500},
        "events": [
            {"dom": {"lotNo": "Lot 1120", "lotDesc": "BOX OF TOOLS",
                     "bidLabel": "CURRENT ROOM BID", "currentBid": "£8",
                     "bidButtonText": "BID NOW £10", "bidButtonVisible": True}},
            {"dom": {"lotNo": "Lot 1120", "bidLabel": "SOLD TO THE INTERNET",
                     "currentBid": "£10", "bidButtonVisible": False},
             "note": "expect: SOLD recorded from label"},
            {"dom": {"lotNo": "Lot 1120", "bidLabel": "CURRENT ROOM BID",
                     "currentBid": "£12", "bidButtonText": "BID NOW £15",
                     "bidButtonVisible": True,
                     "auctioneerMsg": "Bidding has been re-opened!"},
             "note": "expect: sale UNDONE, bidding live again"},
            {"wait": 2},
        ],
    },
    {
        "name": "4. DOM CLOSING MESSAGE — Fair Warning shown while we wait",
        "target_lots": {"810": 40},
        "events": [
            {"dom": {"lotNo": "Lot 810", "lotDesc": "GARDEN TOOLS",
                     "bidLabel": "ASKING BID", "currentBid": "£30",
                     "bidButtonText": "BID NOW £30", "bidButtonVisible": True}},
            {"dom": {"lotNo": "Lot 810", "bidLabel": "ASKING BID",
                     "currentBid": "£15", "bidButtonText": "BID NOW £15",
                     "bidButtonVisible": True}},
            {"dom": {"lotNo": "Lot 810", "bidLabel": "ASKING BID",
                     "currentBid": "£15", "bidButtonText": "BID NOW £15",
                     "bidButtonVisible": True,
                     "auctioneerMsg":
                         "Fair Warning!! This lot is about to close..."},
             "note": "site shows Fair Warning, no bids — expect snipe £15"},
            {"wait": 2},
        ],
    },
]


# ── Runner ───────────────────────────────────────────────────────────────

async def run_scenario(scn, openai_client=None):
    print("\n" + "═" * 70)
    print(f"  SCENARIO: {scn['name']}")
    targets = {str(k): int(v)
               for k, v in scn.get("target_lots", {}).items()}
    default_max = int(scn.get("default_max", 500))
    if targets:
        tgt_str = ", ".join(f"#{k} max £{v}" for k, v in targets.items())
        print(f"  TARGETS:  {tgt_str}")
    else:
        print(f"  TARGETS:  all lots, max £{default_max}")
    print("═" * 70)

    config = {"bid_strategy": {"bid_increment": 10, "reaction_delay_ms": 0},
              "openai_model": "gpt-4o"}
    state = BotState()
    ui = ConsoleUI(state, default_max)

    dom = DomMonitor(state, ui)
    dom.target_lots = targets

    bid = BidEngine(config, state, ui)
    bid.target_lots = targets
    bid.live_var = StubVar(False)      # observer mode: WOULD CLICK BID
    bid.max_bid_var = StubVar(str(default_max))
    bid.budget_var = StubVar("0")

    audio = None
    if openai_client is not None:
        audio = AudioEngine(openai_client, config, state, ui)
        audio.target_lots = targets

    running = True
    decision_task = asyncio.create_task(
        bid.run_loop(lambda: running))

    for ev in scn["events"]:
        await asyncio.sleep(float(ev.get("wait", 1.0)))
        if "note" in ev:
            print(f"          --- {ev['note']} ---")

        if "dom" in ev:
            dom.process_snapshot({**DOM_DEFAULTS, **ev["dom"]})

        if "audio" in ev:
            ui.log_transcript(ev["audio"])
            analysis = None
            if audio is not None:
                analysis = await asyncio.get_event_loop().run_in_executor(
                    None, audio.analyze_transcript, ev["audio"])
                ui.log_decision(
                    f"[AI] status={analysis.get('status')} "
                    f"urgency={analysis.get('urgency')} "
                    f"reason='{analysis.get('reason')}'", "debug")
            elif "fallback_signal" in ev:
                analysis = ev["fallback_signal"]
                ui.log_decision(
                    f"[AI-OFFLINE] using fallback: {analysis}", "debug")
            else:
                ui.log_decision(
                    "[AI] skipped — no OPENAI_API_KEY and no "
                    "fallback_signal", "error")
            if analysis:
                target = audio if audio is not None else \
                    AudioEngine(None, config, state, ui)
                target.apply_analysis(analysis)

        if "signal" in ev:
            AudioEngine(None, config, state, ui).apply_analysis(ev["signal"])

    # let the decision loop drain any pending trigger
    await asyncio.sleep(2.5)
    running = False
    await decision_task

    print("─" * 70)
    print(f"  END STATE: phase={state.lot_phase}  "
          f"bids_we_placed={state.bids_placed_this_lot}  "
          f"total_would_bids={state.total_bids_placed}")


def main():
    openai_client = None
    api_key = os.getenv("OPENAI_API_KEY")
    use_ai = "--no-ai" not in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if api_key and use_ai:
        from openai import OpenAI
        openai_client = OpenAI(api_key=api_key)
        print("AI classification: ON (GPT-4o will classify audio events)")
    else:
        print("AI classification: OFF — using fallback_signal from scenarios")

    if args:
        scenarios = []
        for path in args:
            with open(path) as f:
                scenarios.append(json.load(f))
    else:
        scenarios = BUILTIN_SCENARIOS

    for scn in scenarios:
        asyncio.run(run_scenario(scn, openai_client))

    print("\nDone.")


if __name__ == "__main__":
    main()
