"""
Auction Test - prints transcription + DOM state side by side.
No bidding logic, just shows what it hears and what it sees on screen.
"""

import asyncio
import os
import re
import struct

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import async_playwright

load_dotenv()

AUCTION_URL = "https://www.easyliveauction.com/catalogue/cf29ea3fbdee98fa08c922f6734707a0/0af8d24542e81eb9357e7ef448a6646f/saturday-homes-antiques/bid-live/"
OPENAI_KEY = os.getenv("OPENAI_API_KEY")


async def main():
    client = OpenAI(api_key=OPENAI_KEY)

    print("=" * 60)
    print("  AUCTION TEST - transcription + DOM state")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context()
        page = await context.new_page()

        print("Opening auction page...")
        await page.goto(AUCTION_URL, wait_until="networkidle")
        print("Page loaded.")

        await page.evaluate("""
        () => {
            window.__chunks = [];
            window.__capturing = false;
            const go = () => {
                let el = document.getElementById('dolbyVideo');
                if (!el) el = document.querySelector('video');
                if (!el) el = document.querySelector('audio');
                if (!el || !el.srcObject) { setTimeout(go, 1000); return; }
                const ctx = new AudioContext({ sampleRate: 16000 });
                const src = ctx.createMediaStreamSource(el.srcObject);
                const proc = ctx.createScriptProcessor(4096, 1, 1);
                proc.onaudioprocess = (e) => {
                    if (!window.__capturing) return;
                    const d = e.inputBuffer.getChannelData(0);
                    const pcm = new Int16Array(d.length);
                    for (let i = 0; i < d.length; i++)
                        pcm[i] = Math.max(-32768, Math.min(32767, Math.round(d[i] * 32767)));
                    window.__chunks.push(Array.from(pcm));
                };
                src.connect(proc);
                proc.connect(ctx.destination);
                window.__capturing = true;
            };
            go();
        }
        """)

        try:
            btn = page.locator("#bid-live-controls-unmute")
            if await btn.is_visible():
                await btn.click()
                print("Audio unmuted.")
        except Exception:
            pass

        print("Listening...\n")

        prev_lot = ""
        prev_bid = 0

        while True:
            await asyncio.sleep(3)

            # Read DOM state
            try:
                data = await page.evaluate("""
                () => {
                    const txt = (sel) => {
                        const el = document.querySelector(sel);
                        return el ? el.textContent.trim() : '';
                    };
                    return {
                        lotNo: txt('#bid-live-lot-no'),
                        lotDesc: txt('#bid-live-lot-desc'),
                        lotEst: txt('#bid-live-lot-est-small'),
                        currentBid: txt('.bid-live-current-bid .current-bid'),
                        auctioneerMsg: txt('#auctioneer-message'),
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

                if lot != prev_lot and lot:
                    print(f"\n{'=' * 60}")
                    print(f"  LOT: {lot} - {desc}")
                    print(f"  Estimate: {est}")
                    print(f"{'=' * 60}")
                    prev_lot = lot

                if bid_amount != prev_bid and bid_amount > 0:
                    print(f"  [SCREEN] Current bid: £{bid_amount}")
                    prev_bid = bid_amount

                if msg:
                    print(f"  [SCREEN] Auctioneer says: {msg}")

            except Exception as e:
                print(f"  [DOM ERROR] {e}")

            # Transcribe audio
            try:
                chunks = await page.evaluate("""
                () => { const c = window.__chunks; window.__chunks = []; return c; }
                """)
                if not chunks:
                    continue

                pcm = []
                for c in chunks:
                    pcm.extend(c)
                if len(pcm) < 500:
                    continue

                raw = struct.pack(f"<{len(pcm)}h", *pcm)
                size = len(raw)
                wav = struct.pack(
                    "<4sI4s4sIHHIIHH4sI",
                    b"RIFF", 36 + size, b"WAVE", b"fmt ", 16,
                    1, 1, 16000, 32000, 2, 16, b"data", size,
                ) + raw

                resp = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=("audio.wav", wav, "audio/wav"),
                    language="en",
                    prompt="auction bidding going once going twice sold hammer lot number"
                )
                text = resp.text.strip()
                if text:
                    print(f"  [HEARD] {text}")

            except Exception as e:
                print(f"  [AUDIO ERROR] {e}")


if __name__ == "__main__":
    asyncio.run(main())
