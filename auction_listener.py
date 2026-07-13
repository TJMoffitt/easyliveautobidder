"""
Auction Listener - just transcribes the auctioneer and alerts on closing signals.
No bidding, no DOM monitoring, just audio -> text -> alerts.
"""

import asyncio
import os
import struct

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import async_playwright

load_dotenv()

AUCTION_URL = "https://www.easyliveauction.com/catalogue/cf29ea3fbdee98fa08c922f6734707a0/0af8d24542e81eb9357e7ef448a6646f/saturday-homes-antiques/bid-live/"
OPENAI_KEY = os.getenv("OPENAI_API_KEY")


async def main():
    client = OpenAI(api_key=OPENAI_KEY)

    print("=" * 50)
    print("  AUCTION LISTENER - transcribe only")
    print("=" * 50)

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

        # inject audio capture
        await page.evaluate("""
        () => {
            window.__chunks = [];
            window.__capturing = false;
            const go = () => {
                const el = document.getElementById('dolbyVideo');
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

        # unmute
        try:
            btn = page.locator("#bid-live-controls-unmute")
            if await btn.is_visible():
                await btn.click()
                print("Audio unmuted.")
        except Exception:
            pass

        print("Listening...\n")

        closing_phrases = [
            "going once", "going twice", "final call", "fair warning",
            "last chance", "about to sell", "selling now", "any more",
            "anymore", "sold", "hammer",
        ]

        while True:
            await asyncio.sleep(3)

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

            # build wav header
            size = len(raw)
            wav = struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF", 36 + size, b"WAVE", b"fmt ", 16,
                1, 1, 16000, 32000, 2, 16, b"data", size,
            ) + raw

            try:
                resp = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=("audio.wav", wav, "audio/wav"),
                    language="en",
                    prompt="auction bidding going once going twice sold hammer lot number"
                )
                text = resp.text.strip()
                if not text:
                    continue

                print(f"  {text}")

                lower = text.lower()
                for phrase in closing_phrases:
                    if phrase in lower:
                        print(f"\n  *** CLOSING SIGNAL: \"{phrase}\" ***\n")
                        break

            except Exception as e:
                print(f"  [error] {e}")


if __name__ == "__main__":
    asyncio.run(main())
