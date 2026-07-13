"""
Real-time Auction Bidding Bot for easyliveauction.com

Captures live audio from the auction stream, transcribes it in real-time,
and uses AI to make bidding decisions based on auctioneer speech patterns.
"""

import asyncio
import json
import io
import os
import time
import tempfile
import struct
from pathlib import Path
from dataclasses import dataclass, field

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import async_playwright, Page

load_dotenv()


@dataclass
class BidState:
    current_lot: str = ""
    current_bid: int = 0
    my_last_bid: int = 0
    i_am_winning: bool = False
    lot_description: str = ""
    lot_estimate: str = ""
    auctioneer_message: str = ""
    transcript_buffer: list = field(default_factory=list)
    bid_active: bool = False
    going_once_detected: bool = False
    going_twice_detected: bool = False
    competitor_bid_detected: bool = False


class AuctionBot:
    def __init__(self, config_path: str = "config.json"):
        with open(config_path) as f:
            self.config = json.load(f)

        self.openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.state = BidState()
        self.max_bid = self.config["max_bid_gbp"]
        self.target_lots = self.config.get("target_lots", [])
        self.running = False
        self.page: Page = None
        self.transcript_history: list[str] = []

    async def start(self):
        """Launch browser, navigate to auction, and start monitoring."""
        print("[BOT] Starting auction bot...")
        print(f"[BOT] Max bid: £{self.max_bid}")
        print(f"[BOT] Target lots: {self.target_lots or 'ALL'}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.config.get("headless", False),
                args=["--autoplay-policy=no-user-gesture-required"]
            )
            context = await browser.new_context(
                permissions=["microphone"],
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            self.page = await context.new_page()

            await self.page.goto(self.config["auction_url"], wait_until="networkidle")
            print("[BOT] Page loaded, waiting for auction stream...")

            await self._setup_audio_capture()
            await self._unmute_audio()

            self.running = True
            await asyncio.gather(
                self._audio_transcription_loop(),
                self._page_state_monitor_loop(),
                self._decision_loop()
            )

    async def _setup_audio_capture(self):
        """Inject JS to capture audio from the Dolby/WebRTC stream and pipe PCM data out."""
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
        print("[BOT] Audio capture JS injected")

    async def _unmute_audio(self):
        """Click the unmute button to start receiving audio."""
        try:
            unmute_btn = self.page.locator("#bid-live-controls-unmute")
            if await unmute_btn.is_visible():
                await unmute_btn.click()
                print("[BOT] Audio unmuted")
        except Exception:
            pass

    async def _get_audio_chunk(self) -> bytes | None:
        """Pull accumulated PCM audio data from the browser."""
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

    async def _audio_transcription_loop(self):
        """Continuously capture audio and transcribe it."""
        chunk_duration = self.config["speech_to_text"]["chunk_duration_seconds"]
        print("[BOT] Transcription loop started")

        while self.running:
            await asyncio.sleep(chunk_duration)

            try:
                audio_data = await self._get_audio_chunk()
                if not audio_data or len(audio_data) < 1000:
                    continue

                transcript = await self._transcribe_audio(audio_data)
                if transcript and transcript.strip():
                    print(f"[AUDIO] {transcript}")
                    self.transcript_history.append(transcript)
                    if len(self.transcript_history) > 50:
                        self.transcript_history = self.transcript_history[-30:]
                    self.state.transcript_buffer.append(transcript)

            except Exception as e:
                print(f"[ERROR] Transcription error: {e}")

    async def _transcribe_audio(self, pcm_data: bytes) -> str:
        """Send audio to OpenAI Whisper for transcription."""
        wav_buffer = self._pcm_to_wav(pcm_data)

        response = self.openai.audio.transcriptions.create(
            model=self.config["speech_to_text"]["model"],
            file=("audio.wav", wav_buffer, "audio/wav"),
            language=self.config["speech_to_text"]["language"],
            prompt="auction bidding going once going twice sold hammer lot number bid increment"
        )
        return response.text

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """Convert raw PCM16 data to WAV format."""
        sample_rate = self.config["audio_sample_rate"]
        num_channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        data_size = len(pcm_data)

        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + data_size,
            b"WAVE",
            b"fmt ",
            16,
            1,  # PCM
            num_channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b"data",
            data_size,
        )
        return header + pcm_data

    async def _page_state_monitor_loop(self):
        """Monitor the page DOM for bid state changes."""
        print("[BOT] Page monitor loop started")

        while self.running:
            await asyncio.sleep(0.5)

            try:
                state_data = await self.page.evaluate("""
                () => {
                    const getBidText = (sel) => {
                        const el = document.querySelector(sel);
                        return el ? el.textContent.trim() : '';
                    };
                    return {
                        currentBid: getBidText('.bid-live-current-bid .current-bid'),
                        lotNo: getBidText('#bid-live-lot-no'),
                        lotDesc: getBidText('#bid-live-lot-desc'),
                        lotEst: getBidText('#bid-live-lot-est-small'),
                        auctioneerMsg: getBidText('#auctioneer-message'),
                        bidBtnVisible: !document.querySelector('#bid-live-get-ready')?.classList.contains('hidden'),
                        biddingEnded: !document.querySelector('#bid-live-bidding-ended')?.classList.contains('hidden'),
                    };
                }
                """)

                bid_text = state_data.get("currentBid", "")
                bid_amount = self._parse_bid_amount(bid_text)

                if bid_amount != self.state.current_bid:
                    old_bid = self.state.current_bid
                    self.state.current_bid = bid_amount
                    if bid_amount > old_bid and old_bid > 0:
                        self.state.competitor_bid_detected = True
                        print(f"[BID] Competitor bid detected! £{old_bid} -> £{bid_amount}")

                self.state.current_lot = state_data.get("lotNo", "")
                self.state.lot_description = state_data.get("lotDesc", "")
                self.state.lot_estimate = state_data.get("lotEst", "")
                self.state.auctioneer_message = state_data.get("auctioneerMsg", "")
                self.state.bid_active = state_data.get("bidBtnVisible", False)

                if state_data.get("biddingEnded"):
                    self._reset_lot_state()

            except Exception as e:
                print(f"[ERROR] Page monitor error: {e}")

    def _parse_bid_amount(self, text: str) -> int:
        """Extract numeric bid amount from text like '£280'."""
        import re
        match = re.search(r"[\£\$]?\s*([0-9,]+)", text)
        if match:
            return int(match.group(1).replace(",", ""))
        return 0

    def _reset_lot_state(self):
        """Reset state between lots."""
        self.state.going_once_detected = False
        self.state.going_twice_detected = False
        self.state.competitor_bid_detected = False
        self.state.my_last_bid = 0
        self.state.i_am_winning = False
        self.state.transcript_buffer.clear()

    async def _decision_loop(self):
        """AI-powered decision engine that determines when to bid."""
        print("[BOT] Decision loop started")

        while self.running:
            await asyncio.sleep(1)

            if not self.state.bid_active:
                continue

            if not self.state.transcript_buffer and not self.state.competitor_bid_detected:
                continue

            if self.target_lots and self.state.current_lot not in self.target_lots:
                continue

            if self.state.current_bid >= self.max_bid:
                continue

            if self.state.i_am_winning:
                self.state.transcript_buffer.clear()
                self.state.competitor_bid_detected = False
                continue

            should_bid = await self._should_bid()

            if should_bid:
                await self._place_bid()

            self.state.transcript_buffer.clear()
            self.state.competitor_bid_detected = False

    async def _should_bid(self) -> bool:
        """Use AI to analyze transcript and decide whether to bid now."""
        recent_transcript = " ".join(self.state.transcript_buffer[-5:])

        quick_triggers = [
            "going once", "going twice", "going once", "going twice",
            "any more", "anymore", "last chance", "final call",
            "about to sell", "selling now", "fair warning"
        ]
        lower_transcript = recent_transcript.lower()
        for trigger in quick_triggers:
            if trigger in lower_transcript:
                if "going twice" in lower_transcript or "going twice" in lower_transcript:
                    self.state.going_twice_detected = True
                    print("[DECISION] GOING TWICE detected - bidding immediately!")
                    return True
                if "going once" in lower_transcript or "going once" in lower_transcript:
                    self.state.going_once_detected = True
                    if self.config["bid_strategy"]["snipe_on_going_once"]:
                        print("[DECISION] GOING ONCE detected - sniping!")
                        return True

        if self.state.competitor_bid_detected and self.state.current_bid < self.max_bid:
            response = self._ask_ai_decision(recent_transcript)
            return response

        return False

    def _ask_ai_decision(self, transcript: str) -> bool:
        """Ask GPT to make a bidding decision based on context."""
        prompt = f"""You are an auction bidding assistant. Analyze the situation and decide if we should bid NOW.

Current state:
- Lot: {self.state.current_lot} - {self.state.lot_description}
- Estimate: {self.state.lot_estimate}
- Current bid: £{self.state.current_bid}
- Our maximum: £{self.max_bid}
- Our last bid: £{self.state.my_last_bid}
- Competitor just bid: {self.state.competitor_bid_detected}
- Going once heard: {self.state.going_once_detected}
- Going twice heard: {self.state.going_twice_detected}

Recent auctioneer transcript: "{transcript}"

Strategy: We want to SNIPE - only bid at the last possible moment. Wait for the price to drop.
- If the auctioneer is still looking for bids and price is dropping, WAIT.
- If going once/twice or someone else bid and it's about to close, BID.
- If current bid is already at or above our max, DO NOT BID.
- If we are already winning, DO NOT BID.

Respond with ONLY "BID" or "WAIT" and a brief reason."""

        try:
            response = self.openai.chat.completions.create(
                model=self.config["openai_model"],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=50,
                temperature=0.1
            )
            answer = response.choices[0].message.content.strip().upper()
            print(f"[AI] Decision: {answer}")
            return answer.startswith("BID")
        except Exception as e:
            print(f"[ERROR] AI decision error: {e}")
            return False

    async def _place_bid(self):
        """Click the bid button on the page."""
        if self.state.current_bid >= self.max_bid:
            print(f"[BID] Cannot bid - current £{self.state.current_bid} >= max £{self.max_bid}")
            return

        try:
            delay = self.config["bid_strategy"]["reaction_delay_ms"] / 1000
            await asyncio.sleep(delay)

            bid_btn = self.page.locator("#bid-live-get-ready")
            if await bid_btn.is_visible():
                await bid_btn.click()
                self.state.my_last_bid = self.state.current_bid + self.config["bid_strategy"]["bid_increment"]
                self.state.i_am_winning = True
                print(f"[BID] *** BID PLACED at ~£{self.state.my_last_bid} ***")
            else:
                bidding_soon = self.page.locator("#bid-live-bidding-soon")
                if await bidding_soon.is_visible():
                    await bidding_soon.click()
                    self.state.my_last_bid = self.state.current_bid + self.config["bid_strategy"]["bid_increment"]
                    self.state.i_am_winning = True
                    print(f"[BID] *** BID PLACED (soon btn) at ~£{self.state.my_last_bid} ***")
                else:
                    print("[BID] No bid button available")

        except Exception as e:
            print(f"[ERROR] Bid placement error: {e}")


async def main():
    bot = AuctionBot()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
