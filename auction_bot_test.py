"""
Auction Bot - TEST/OBSERVER MODE
No bidding capability. Watches the auction and prints what it sees and when it would bid.
"""

import asyncio
import json
import os
import struct
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import async_playwright, Page

load_dotenv()


@dataclass
class BidState:
    current_lot: str = ""
    current_bid: int = 0
    lot_description: str = ""
    lot_estimate: str = ""
    auctioneer_message: str = ""
    transcript_buffer: list = field(default_factory=list)
    bid_active: bool = False
    going_once_detected: bool = False
    going_twice_detected: bool = False
    competitor_bid_detected: bool = False
    last_bid_amount: int = 0


class AuctionBotTest:
    def __init__(self, config_path: str = "config.json"):
        with open(config_path) as f:
            self.config = json.load(f)

        self.openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.state = BidState()
        self.max_bid = self.config["max_bid_gbp"]
        self.running = False
        self.page: Page = None

    async def start(self):
        print("=" * 60)
        print("  AUCTION BOT - OBSERVER MODE (no bidding)")
        print(f"  Max bid limit: £{self.max_bid}")
        print("=" * 60)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--autoplay-policy=no-user-gesture-required"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            self.page = await context.new_page()

            print("[BOT] Navigating to auction...")
            await self.page.goto(self.config["auction_url"], wait_until="networkidle")
            print("[BOT] Page loaded.")

            await self._setup_audio_capture()
            await self._unmute_audio()

            self.running = True
            await asyncio.gather(
                self._audio_transcription_loop(),
                self._page_state_monitor_loop(),
            )

    async def _setup_audio_capture(self):
        await self.page.evaluate("""
        () => {
            window.__auctionAudioChunks = [];
            window.__auctionCapturing = false;

            const captureAudio = () => {
                const audioEl = document.getElementById('dolbyVideo');
                if (!audioEl || !audioEl.srcObject) {
                    console.log('No audio stream yet, retrying...');
                    setTimeout(captureAudio, 1000);
                    return;
                }

                const audioCtx = new (window.AudioContext || window.webkitAudioContext)({
                    sampleRate: 16000
                });
                const source = audioCtx.createMediaStreamSource(audioEl.srcObject);
                const processor = audioCtx.createScriptProcessor(4096, 1, 1);

                processor.onaudioprocess = (e) => {
                    if (!window.__auctionCapturing) return;
                    const data = e.inputBuffer.getChannelData(0);
                    const pcm16 = new Int16Array(data.length);
                    for (let i = 0; i < data.length; i++) {
                        pcm16[i] = Math.max(-32768, Math.min(32767, Math.round(data[i] * 32767)));
                    }
                    window.__auctionAudioChunks.push(Array.from(pcm16));
                };

                source.connect(processor);
                processor.connect(audioCtx.destination);
                window.__auctionCapturing = true;
                console.log('Audio capture started');
            };

            captureAudio();
        }
        """)
        print("[BOT] Audio capture injected, waiting for stream...")

    async def _unmute_audio(self):
        try:
            unmute_btn = self.page.locator("#bid-live-controls-unmute")
            if await unmute_btn.is_visible():
                await unmute_btn.click()
                print("[BOT] Audio unmuted")
        except Exception:
            pass

    async def _get_audio_chunk(self) -> bytes | None:
        chunks = await self.page.evaluate("""
        () => {
            const chunks = window.__auctionAudioChunks;
            window.__auctionAudioChunks = [];
            return chunks;
        }
        """)
        if not chunks:
            return None
        pcm_data = []
        for chunk in chunks:
            pcm_data.extend(chunk)
        if not pcm_data:
            return None
        return struct.pack(f"<{len(pcm_data)}h", *pcm_data)

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        sample_rate = self.config["audio_sample_rate"]
        data_size = len(pcm_data)
        byte_rate = sample_rate * 2
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16,
            1, 1, sample_rate, byte_rate, 2, 16, b"data", data_size,
        )
        return header + pcm_data

    async def _audio_transcription_loop(self):
        chunk_duration = self.config["speech_to_text"]["chunk_duration_seconds"]
        print("[BOT] Listening for auctioneer...\n")

        while self.running:
            await asyncio.sleep(chunk_duration)
            try:
                audio_data = await self._get_audio_chunk()
                if not audio_data or len(audio_data) < 1000:
                    continue

                wav_buffer = self._pcm_to_wav(audio_data)
                response = self.openai.audio.transcriptions.create(
                    model=self.config["speech_to_text"]["model"],
                    file=("audio.wav", wav_buffer, "audio/wav"),
                    language=self.config["speech_to_text"]["language"],
                    prompt="auction bidding going once going twice sold hammer lot number bid increment"
                )
                transcript = response.text.strip()
                if not transcript:
                    continue

                print(f"  [HEARD] {transcript}")
                self.state.transcript_buffer.append(transcript)
                if len(self.state.transcript_buffer) > 20:
                    self.state.transcript_buffer = self.state.transcript_buffer[-15:]

                self._check_triggers(transcript)

            except Exception as e:
                print(f"  [ERROR] Transcription: {e}")

    def _check_triggers(self, transcript: str):
        lower = transcript.lower()

        closing_signals = {
            "going twice": "GOING TWICE",
            "going once": "GOING ONCE",
            "final call": "FINAL CALL",
            "fair warning": "FAIR WARNING",
            "last chance": "LAST CHANCE",
            "about to sell": "ABOUT TO SELL",
            "selling now": "SELLING NOW",
            "any more": "ANY MORE BIDS?",
            "anymore": "ANY MORE BIDS?",
        }

        for phrase, label in closing_signals.items():
            if phrase in lower:
                bid_ok = self.state.current_bid < self.max_bid
                action = ">>> WOULD BID NOW <<<" if bid_ok else f">>> OVER LIMIT (£{self.state.current_bid} >= £{self.max_bid}) - PASS <<<"
                print(f"\n  *** TRIGGER: {label} detected! Current bid: £{self.state.current_bid}")
                print(f"  *** {action}\n")
                return

        sold_phrases = ["sold", "hammer", "knocked down"]
        for phrase in sold_phrases:
            if phrase in lower:
                print(f"\n  --- LOT SOLD at £{self.state.current_bid} ---\n")
                self.state.going_once_detected = False
                self.state.going_twice_detected = False
                return

    async def _page_state_monitor_loop(self):
        prev_lot = ""
        prev_bid = 0
        prev_msg = ""

        while self.running:
            await asyncio.sleep(0.5)
            try:
                state_data = await self.page.evaluate("""
                () => {
                    const t = (sel) => {
                        const el = document.querySelector(sel);
                        return el ? el.textContent.trim() : '';
                    };
                    return {
                        currentBid: t('.bid-live-current-bid .current-bid'),
                        lotNo: t('#bid-live-lot-no'),
                        lotDesc: t('#bid-live-lot-desc'),
                        lotEst: t('#bid-live-lot-est-small'),
                        auctioneerMsg: t('#auctioneer-message'),
                    };
                }
                """)

                lot = state_data.get("lotNo", "")
                desc = state_data.get("lotDesc", "")
                est = state_data.get("lotEst", "")
                msg = state_data.get("auctioneerMsg", "")
                bid_text = state_data.get("currentBid", "")

                match = re.search(r"[\£\$]?\s*([0-9,]+)", bid_text)
                bid_amount = int(match.group(1).replace(",", "")) if match else 0

                if lot != prev_lot and lot:
                    print(f"\n{'=' * 60}")
                    print(f"  LOT {lot}: {desc}")
                    print(f"  Estimate: {est}")
                    print(f"{'=' * 60}")
                    prev_lot = lot
                    self.state.current_lot = lot
                    self.state.lot_description = desc
                    self.state.lot_estimate = est

                if bid_amount != prev_bid and bid_amount > 0:
                    direction = "UP" if bid_amount > prev_bid and prev_bid > 0 else "START" if prev_bid == 0 else "DOWN"
                    if direction == "UP" and prev_bid > 0:
                        print(f"  [BID CHANGE] £{prev_bid} -> £{bid_amount} (competitor bid!)")
                        if bid_amount < self.max_bid:
                            print(f"  *** Would consider counter-bidding (under £{self.max_bid} limit) ***")
                        else:
                            print(f"  *** Over limit - would NOT bid ***")
                    else:
                        print(f"  [PRICE] £{bid_amount} ({direction})")
                    prev_bid = bid_amount
                    self.state.current_bid = bid_amount

                if msg != prev_msg and msg:
                    print(f"  [AUCTIONEER MSG] {msg}")
                    prev_msg = msg

            except Exception as e:
                print(f"  [ERROR] Page monitor: {e}")


async def main():
    bot = AuctionBotTest()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
