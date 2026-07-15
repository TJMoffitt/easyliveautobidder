"""Audio capture, Whisper transcription, and GPT-4o closing signal analysis."""

import asyncio
import json
import struct
import time

from models import ACCENT_RED, ACCENT_AMBER


AUDIO_INJECT_JS = """
() => {
    if (window.__audioHooked) return;
    window.__audioHooked = true;
    window.__chunks = [];
    window.__audioLevel = 0;
    function go() {
        const el = document.querySelector('#dolbyVideo') || document.querySelector('video') || document.querySelector('audio');
        if (!el) { setTimeout(go, 500); return; }
        const ctx = new AudioContext({sampleRate: 16000});
        let src;
        if (el.captureStream) src = ctx.createMediaStreamSource(el.captureStream());
        else if (el.srcObject) src = ctx.createMediaStreamSource(el.srcObject);
        else { setTimeout(go, 500); return; }
        const proc = ctx.createScriptProcessor(4096, 1, 1);
        proc.onaudioprocess = e => {
            const d = e.inputBuffer.getChannelData(0);
            let sum = 0;
            const arr = [];
            for (let i = 0; i < d.length; i++) {
                const s = Math.max(-1, Math.min(1, d[i]));
                arr.push(Math.floor(s * 32767));
                sum += Math.abs(s);
            }
            window.__audioLevel = sum / d.length;
            window.__chunks.push(arr);
        };
        src.connect(proc);
        proc.connect(ctx.destination);
    }
    go();
}
"""


def build_wav(pcm_samples):
    """Convert list of PCM16 samples to WAV bytes."""
    raw = struct.pack(f"<{len(pcm_samples)}h", *pcm_samples)
    size = len(raw)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + size, b"WAVE", b"fmt ", 16,
        1, 1, 16000, 32000, 2, 16, b"data", size,
    ) + raw


class AudioEngine:
    """Handles audio capture, transcription, and AI-based closing signal detection."""

    def __init__(self, openai_client, config, state, ui):
        self.client = openai_client
        self.config = config
        self.state = state
        self.ui = ui
        self.page = None
        self.transcript_buffer = []
        self.target_lots = {}

    def set_page(self, page):
        self.page = page

    async def inject_audio(self):
        await self.page.evaluate(AUDIO_INJECT_JS)
        self.ui.log_decision("Audio capture started")

    async def unmute(self):
        try:
            btn = self.page.locator("#bid-live-controls-unmute")
            if await btn.is_visible():
                await btn.click()
                self.ui.log_decision("Audio unmuted")
        except Exception:
            pass

    def analyze_transcript(self, text):
        """Use GPT-4o to classify auctioneer speech."""
        try:
            resp = self.client.chat.completions.create(
                model=self.config.get("openai_model", "gpt-4o"),
                max_tokens=60,
                temperature=0,
                messages=[
                    {"role": "system", "content": (
                        "You analyse live auction transcripts. Classify the auctioneer's intent. "
                        "Reply with EXACTLY one JSON object, no other text.\n"
                        "Fields:\n"
                        '  "status": one of "CLOSING", "SOLD", "NORMAL"\n'
                        '  "urgency": one of "HIGH", "MEDIUM", "LOW"\n'
                        '  "reason": very short phrase (max 5 words)\n\n'
                        "CLOSING = auctioneer is warning the lot is about to sell "
                        "(going once, going twice, last call, fair warning, any more bids, "
                        "lowest I'll go, about to sell, shall I sell, final chance, "
                        "all done, selling now, any takers, last time, etc.)\n"
                        "SOLD = the lot has been sold (sold, hammer down, knocked down, "
                        "congratulations, sold to, etc.)\n"
                        "NORMAL = anything else (describing items, taking bids, price drops, chatter)"
                    )},
                    {"role": "user", "content": text}
                ]
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
            return json.loads(raw)
        except Exception as e:
            self.ui.log_decision(f"AI classify error: {e}", "error")
            return {"status": "NORMAL", "urgency": "LOW", "reason": "error"}

    async def run_loop(self, running_check):
        """Main audio loop — captures, transcribes, analyses for closing signals."""
        while running_check():
            await asyncio.sleep(3)
            try:
                result = await self.page.evaluate("""
                () => {
                    const c = window.__chunks;
                    window.__chunks = [];
                    return { chunks: c, level: window.__audioLevel || 0 };
                }
                """)
                chunks = result.get("chunks", [])
                level = result.get("level", 0)
                self.ui.update_audio_level(min(1.0, level * 10))

                if not chunks:
                    continue
                pcm = []
                for c in chunks:
                    pcm.extend(c)
                if len(pcm) < 500:
                    continue

                wav = build_wav(pcm)

                t0 = time.time()
                resp = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=("audio.wav", wav, "audio/wav"),
                    language="en",
                    prompt="auction bidding going once going twice sold hammer lot number fair warning"
                )
                latency = int((time.time() - t0) * 1000)
                self.ui.update_latency(latency)

                text = resp.text.strip()
                if not text:
                    continue

                self.ui.log_transcript(text)

                self.transcript_buffer.append(text)
                if len(self.transcript_buffer) > 5:
                    self.transcript_buffer = self.transcript_buffer[-5:]

                current_lot = self.state.lot.lot_number or ""
                on_target = not self.target_lots or any(
                    t in current_lot for t in self.target_lots)

                if not on_target:
                    continue

                context = " | ".join(self.transcript_buffer[-3:])
                analysis = await asyncio.get_event_loop().run_in_executor(
                    None, self.analyze_transcript, context
                )

                status = analysis.get("status", "NORMAL")
                urgency = analysis.get("urgency", "LOW")
                reason = analysis.get("reason", "")

                if status == "CLOSING":
                    self.state.closing_signal_active = True
                    self.state.closing_signal_type = reason.upper()
                    self.state.closing_signal_time = asyncio.get_event_loop().time()
                    color = ACCENT_RED if urgency == "HIGH" else ACCENT_AMBER
                    self.ui.log_decision(f">>> CLOSING: {reason} [{urgency}] <<<", "trigger")
                    self.ui.set_status(f"CLOSING: {reason}", color)
                    self.ui.flash_alert(urgency)

                elif status == "SOLD":
                    self.ui.log_decision(f"SOLD DETECTED: {reason}", "sold")
                    self.ui.record_sale()

            except Exception as e:
                self.ui.log_decision(f"Audio error: {e}", "error")
