"""
Auction Sniper Bot - DOM-based bidding with audio triggers.

Architecture:
- Audio transcription detects closing signals (going once, going twice, etc.)
- ALL bidding decisions based on DOM state (current bid, button visibility, lot info)
- Closing signals are just the TRIGGER to start evaluating whether to bid
- Observer mode by default (set LIVE_MODE=true in config to enable actual clicks)
"""

import asyncio
import json
import os
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import async_playwright, Page

load_dotenv()


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


class AuctionSniper:
    def __init__(self, config_path: str = "config.json"):
        with open(config_path) as f:
            self.config = json.load(f)

        self.openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.state = BotState()
        self.max_bid = self.config["max_bid_gbp"]
        self.target_lots = self.config.get("target_lots", [])
        self.live_mode = self.config.get("live_mode", False)
        self.running = False
        self.page: Page = None

    async def start(self):
        mode = "LIVE" if self.live_mode else "OBSERVER"
        print("=" * 60)
        print(f"  AUCTION SNIPER - {mode} MODE")
        print(f"  Max bid: £{self.max_bid}")
        if self.target_lots:
            print(f"  Target lots: {self.target_lots}")
        else:
            print(f"  Target lots: ALL")
        print("=" * 60)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.config.get("headless", False),
                args=["--autoplay-policy=no-user-gesture-required"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            self.page = await context.new_page()

            print("[SNIPER] Opening auction page...")
            await self.page.goto(self.config["auction_url"], wait_until="networkidle")
            print("[SNIPER] Page loaded.")

            await self._setup_audio_capture()
            await self._unmute_audio()

            self.running = True
            print("[SNIPER] Running. Audio = trigger only. Decisions = DOM state.\n")

            await asyncio.gather(
                self._audio_trigger_loop(),
                self._dom_monitor_loop(),
                self._decision_loop(),
            )

    async def _setup_audio_capture(self):
        await self.page.evaluate("""
        () => {
            window.__audioChunks = [];
            window.__audioCapturing = false;

            const startCapture = () => {
                // Try video element first (Dolby uses <video>), then audio
                let el = document.getElementById('dolbyVideo');
                if (!el) el = document.querySelector('video[id*="dolby"]');
                if (!el) el = document.querySelector('audio');
                if (!el || !el.srcObject) {
                    setTimeout(startCapture, 1000);
                    return;
                }

                const ctx = new (window.AudioContext || window.webkitAudioContext)({
                    sampleRate: 16000
                });
                const src = ctx.createMediaStreamSource(el.srcObject);
                const proc = ctx.createScriptProcessor(4096, 1, 1);

                proc.onaudioprocess = (e) => {
                    if (!window.__audioCapturing) return;
                    const data = e.inputBuffer.getChannelData(0);
                    const pcm = new Int16Array(data.length);
                    for (let i = 0; i < data.length; i++)
                        pcm[i] = Math.max(-32768, Math.min(32767, Math.round(data[i] * 32767)));
                    window.__audioChunks.push(Array.from(pcm));
                };

                src.connect(proc);
                proc.connect(ctx.destination);
                window.__audioCapturing = true;
            };

            startCapture();
        }
        """)
        print("[SNIPER] Audio capture injected.")

    async def _unmute_audio(self):
        try:
            btn = self.page.locator("#bid-live-controls-unmute")
            if await btn.is_visible():
                await btn.click()
                print("[SNIPER] Audio unmuted.")
        except Exception:
            pass

    # ─── AUDIO TRIGGER LOOP ────────────────────────────────────────────
    # Only job: detect closing signals from auctioneer speech.
    # Does NOT make any bidding decisions.

    async def _audio_trigger_loop(self):
        chunk_seconds = self.config["speech_to_text"]["chunk_duration_seconds"]

        closing_signals = {
            "going twice": "GOING_TWICE",
            "going once": "GOING_ONCE",
            "final call": "FINAL_CALL",
            "fair warning": "FAIR_WARNING",
            "last chance": "LAST_CHANCE",
            "about to sell": "ABOUT_TO_SELL",
            "selling now": "SELLING_NOW",
            "any more": "ANY_MORE",
            "anymore": "ANY_MORE",
        }

        sold_phrases = ["sold", "hammer", "knocked down"]

        while self.running:
            await asyncio.sleep(chunk_seconds)
            try:
                chunks = await self.page.evaluate("""
                () => { const c = window.__audioChunks; window.__audioChunks = []; return c; }
                """)
                if not chunks:
                    continue

                pcm_data = []
                for chunk in chunks:
                    pcm_data.extend(chunk)
                if len(pcm_data) < 500:
                    continue

                raw = struct.pack(f"<{len(pcm_data)}h", *pcm_data)
                wav = self._pcm_to_wav(raw)

                resp = self.openai.audio.transcriptions.create(
                    model=self.config["speech_to_text"]["model"],
                    file=("audio.wav", wav, "audio/wav"),
                    language=self.config["speech_to_text"]["language"],
                    prompt="auction bidding going once going twice sold hammer lot number bid increment fair warning"
                )
                text = resp.text.strip()
                if not text:
                    continue

                print(f"  [AUDIO] {text}")

                lower = text.lower()

                for phrase, signal_type in closing_signals.items():
                    if phrase in lower:
                        self.state.closing_signal_active = True
                        self.state.closing_signal_type = signal_type
                        self.state.closing_signal_time = asyncio.get_event_loop().time()
                        print(f"  [TRIGGER] >>> {signal_type} detected! Decision engine activated. <<<")
                        break

                for phrase in sold_phrases:
                    if phrase in lower:
                        self.state.closing_signal_active = False
                        self.state.bids_placed_this_lot = 0
                        print(f"  [AUDIO] --- Item sold ---")
                        break

            except Exception as e:
                print(f"  [ERROR] Audio: {e}")

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        sr = self.config["audio_sample_rate"]
        size = len(pcm_data)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + size, b"WAVE", b"fmt ", 16,
            1, 1, sr, sr * 2, 2, 16, b"data", size,
        )
        return header + pcm_data

    # ─── DOM MONITOR LOOP ──────────────────────────────────────────────
    # Continuously reads the page state. This is the SOURCE OF TRUTH.

    async def _dom_monitor_loop(self):
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

                lot = data.get("lotNo", "")
                desc = data.get("lotDesc", "")
                est = data.get("lotEst", "")
                msg = data.get("auctioneerMsg", "")
                bid_text = data.get("currentBid", "")

                match = re.search(r"[\£\$]?\s*([0-9,]+)", bid_text)
                bid_amount = int(match.group(1).replace(",", "")) if match else 0

                self.state.lot.lot_number = lot
                self.state.lot.description = desc
                self.state.lot.estimate = est
                self.state.lot.current_bid = bid_amount
                self.state.lot.auctioneer_message = msg
                self.state.lot.bid_button_visible = data.get("bidButtonVisible", False)
                self.state.lot.bidding_ended = data.get("biddingEnded", False)
                self.state.lot.we_are_winning = data.get("winningBadge", False)
                self.state.lot.register_required = data.get("registerVisible", False)

                if lot != prev_lot and lot:
                    print(f"\n{'=' * 60}")
                    print(f"  NEW LOT: {lot} - {desc}")
                    print(f"  Estimate: {est}")
                    target_match = not self.target_lots or any(t in lot for t in self.target_lots)
                    print(f"  Targeting: {'YES' if target_match else 'NO (skipping)'}")
                    print(f"{'=' * 60}")
                    prev_lot = lot
                    self.state.closing_signal_active = False
                    self.state.bids_placed_this_lot = 0

                if bid_amount != prev_bid and bid_amount > 0:
                    if prev_bid > 0 and bid_amount > prev_bid:
                        print(f"  [DOM] Bid increased: £{prev_bid} -> £{bid_amount}")
                    elif prev_bid > 0 and bid_amount < prev_bid:
                        print(f"  [DOM] Bid dropped: £{prev_bid} -> £{bid_amount}")
                    else:
                        print(f"  [DOM] Current bid: £{bid_amount}")
                    prev_bid = bid_amount

                if msg != prev_msg and msg:
                    print(f"  [DOM] Auctioneer: {msg}")
                    prev_msg = msg

            except Exception as e:
                print(f"  [ERROR] DOM: {e}")

    # ─── DECISION LOOP ─────────────────────────────────────────────────
    # Evaluates whether to bid based ENTIRELY on DOM state.
    # Only activates when audio trigger fires a closing signal.

    async def _decision_loop(self):
        while self.running:
            await asyncio.sleep(0.2)

            if not self.state.closing_signal_active:
                continue

            now = asyncio.get_event_loop().time()
            signal_age = now - self.state.closing_signal_time
            if signal_age > 15:
                self.state.closing_signal_active = False
                continue

            decision = self._evaluate_bid()

            if decision["action"] == "BID":
                await self._place_bid(decision)
                self.state.closing_signal_active = False
            elif decision["action"] == "PASS":
                print(f"  [DECISION] PASS - {decision['reason']}")
                self.state.closing_signal_active = False
            elif decision["action"] == "WAIT":
                pass

    def _evaluate_bid(self) -> dict:
        lot = self.state.lot

        if lot.register_required:
            return {"action": "PASS", "reason": "Not registered to bid"}

        if lot.bidding_ended:
            return {"action": "PASS", "reason": "Bidding has ended"}

        if lot.we_are_winning:
            return {"action": "PASS", "reason": "Already winning - no need to bid"}

        if self.target_lots and not any(t in lot.lot_number for t in self.target_lots):
            return {"action": "PASS", "reason": f"Lot {lot.lot_number} not in target list"}

        if lot.current_bid >= self.max_bid:
            return {"action": "PASS", "reason": f"Current bid £{lot.current_bid} >= max £{self.max_bid}"}

        next_bid = lot.current_bid + self.config["bid_strategy"]["bid_increment"]
        if next_bid > self.max_bid:
            return {"action": "PASS", "reason": f"Next bid £{next_bid} would exceed max £{self.max_bid}"}

        if not lot.bid_button_visible:
            return {"action": "WAIT", "reason": "Bid button not visible yet"}

        strategy = self.config["bid_strategy"]
        signal = self.state.closing_signal_type

        should_bid = False
        if signal == "GOING_ONCE" and strategy.get("snipe_on_going_once", True):
            should_bid = True
        elif signal == "GOING_TWICE" and strategy.get("snipe_on_going_twice", True):
            should_bid = True
        elif signal in ("FINAL_CALL", "FAIR_WARNING", "LAST_CHANCE", "ABOUT_TO_SELL", "SELLING_NOW", "ANY_MORE"):
            should_bid = True

        if not should_bid:
            return {"action": "PASS", "reason": f"Strategy says skip {signal}"}

        now = asyncio.get_event_loop().time()
        cooldown = 5
        if now - self.state.last_bid_placed_at < cooldown:
            return {"action": "WAIT", "reason": "Bid cooldown active"}

        return {
            "action": "BID",
            "reason": f"Trigger={signal}, bid=£{lot.current_bid}, under max=£{self.max_bid}",
            "amount": next_bid,
        }

    async def _place_bid(self, decision: dict):
        lot = self.state.lot
        signal = self.state.closing_signal_type

        if not self.live_mode:
            print(f"\n  {'*' * 50}")
            print(f"  [WOULD BID] Trigger: {signal}")
            print(f"  [WOULD BID] Lot: {lot.lot_number} - {lot.description}")
            print(f"  [WOULD BID] Current: £{lot.current_bid} | Next: £{decision['amount']} | Max: £{self.max_bid}")
            print(f"  [WOULD BID] Button visible: {lot.bid_button_visible}")
            print(f"  {'*' * 50}\n")
            self.state.last_bid_placed_at = asyncio.get_event_loop().time()
            self.state.bids_placed_this_lot += 1
            self.state.total_bids_placed += 1
            return

        # LIVE MODE - actually click the bid button
        delay_ms = self.config["bid_strategy"].get("reaction_delay_ms", 500)
        await asyncio.sleep(delay_ms / 1000)

        try:
            ready_btn = self.page.locator("#bid-live-get-ready")
            soon_btn = self.page.locator("#bid-live-bidding-soon")

            if await ready_btn.is_visible():
                await ready_btn.click()
                print(f"  [BID PLACED] Clicked 'Get Ready' - £{decision['amount']}")
            elif await soon_btn.is_visible():
                await soon_btn.click()
                print(f"  [BID PLACED] Clicked 'Bidding Soon' - £{decision['amount']}")
            else:
                print(f"  [BID FAILED] No bid button found!")
                return

            self.state.last_bid_placed_at = asyncio.get_event_loop().time()
            self.state.bids_placed_this_lot += 1
            self.state.total_bids_placed += 1

        except Exception as e:
            print(f"  [BID ERROR] {e}")


async def main():
    bot = AuctionSniper()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
