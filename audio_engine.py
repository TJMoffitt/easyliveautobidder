"""Audio capture, Whisper transcription, and GPT-4o auctioneer intent analysis.

The AI classifies each transcript window into:
  PASS_IMMINENT — no bids, auctioneer about to give up and pass the lot
                  unsold ("lowest I'll go", "last chance or I move on").
                  THIS is the snipe trigger: bid now at the floor price.
  SALE_CLOSING  — bids exist, auctioneer closing the sale to the current
                  bidder (going once/twice, fair warning, hammer up).
  SOLD          — the hammer fell.
  NORMAL        — everything else.
"""

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

CLASSIFY_SYSTEM_PROMPT = """You analyse live auction house transcripts to time bids for a sniper strategy.

The strategy: when nobody bids, the auctioneer keeps LOWERING the asking price. \
Eventually he gives a signal that he is about to give up and pass the lot UNSOLD. \
That signal is the moment to bid — the lot is at its minimum possible price.

You get the recent transcript and the current DOM state (asking price, whether any \
real bids have been placed yet).

Reply with EXACTLY one JSON object, no other text:
{"status": "...", "urgency": "...", "reason": "..."}

status must be one of:
  "PASS_IMMINENT" — NO bids yet and the auctioneer signals he's done dropping and \
will pass/withdraw the lot or move on without a sale. Examples: "80 pounds lowest \
I'll go", "last chance at 50 or I'll pass it", "no interest? moving on", "anyone at \
all... no? I'll withdraw it", "are we all done at 40, last time", "or it goes to \
the next lot", "I can't sell it any cheaper". Also counts if he offers the absolute \
floor: "come on, someone start me, 30 pounds anywhere".
  "SALE_CLOSING" — bids EXIST and the auctioneer is about to hammer the sale to the \
current bidder: "going once", "going twice", "fair warning", "hammer's up", \
"selling at 120 to the room", "all done at 120?".
  "SOLD" — the hammer fell: "sold", "knocked down", "sold to bidder 5", "yours sir".
  "NORMAL" — anything else: describing the item, taking active bids, routine price \
drops with no give-up signal, chatter.

urgency: "HIGH" if it will happen within seconds, "MEDIUM" if within ~10s, "LOW" otherwise.
reason: max 6 words quoting/paraphrasing the trigger phrase.

If bids exist, prefer SALE_CLOSING over PASS_IMMINENT. If no bids exist, closing \
language means PASS_IMMINENT."""


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
    """Handles audio capture, transcription, and AI intent detection."""

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
        """Send transcript + DOM context to GPT-4o for intent classification."""
        dom_context = (
            f"DOM state: lot #{self.state.lot.lot_number}, "
            f"asking price £{self.state.lot.current_bid}, "
            f"real bids placed this lot: "
            f"{'YES' if self.state.any_bids_this_lot else 'NO'}, "
            f"phase: {self.state.lot_phase}"
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.config.get("openai_model", "gpt-4o"),
                max_tokens=60,
                temperature=0,
                messages=[
                    {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                    {"role": "user",
                     "content": f"{dom_context}\n\nTranscript:\n{text}"}
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
        """Main audio loop — capture, transcribe, classify EVERY lot."""
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

                # Audio has 3-6s of lag (chunking + transcription). If the
                # lot changes while this chunk is in flight, the words
                # belong to the PREVIOUS lot — discard the classification.
                lot_at_capture = self.state.lot.lot_number

                t0 = time.time()
                # Run in executor — a blocking call here would freeze the
                # DOM and decision loops for the whole API round-trip.
                resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.client.audio.transcriptions.create(
                        model="whisper-1",
                        file=("audio.wav", wav, "audio/wav"),
                        language="en",
                        prompt=("auction bidding going once going twice sold "
                                "hammer lot number fair warning last chance "
                                "pass it withdraw lowest moving on")
                    )
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

                # Run AI classification on EVERY lot (debug mode)
                context = " | ".join(self.transcript_buffer[-3:])
                analysis = await asyncio.get_event_loop().run_in_executor(
                    None, self.analyze_transcript, context
                )

                status = analysis.get("status", "NORMAL")
                urgency = analysis.get("urgency", "LOW")
                reason = analysis.get("reason", "")

                # Debug: log every AI verdict so we can see what it thinks
                self.ui.log_decision(
                    f"[AI] status={status} urgency={urgency} "
                    f"reason='{reason}' "
                    f"(bids={'Y' if self.state.any_bids_this_lot else 'N'} "
                    f"phase={self.state.lot_phase})",
                    "debug")

                if self.state.lot.lot_number != lot_at_capture:
                    if status != "NORMAL":
                        self.ui.log_decision(
                            f"[DEBUG] discarding {status} — audio was from "
                            f"lot #{lot_at_capture}, now on "
                            f"#{self.state.lot.lot_number}", "debug")
                    continue

                self.apply_analysis(analysis)

            except Exception as e:
                self.ui.log_decision(f"Audio error: {e}", "error")

    def apply_analysis(self, analysis):
        """Turn an AI classification into bot signals. Called by run_loop
        with live results, and by the simulator with scripted/AI results."""
        status = analysis.get("status", "NORMAL")
        urgency = analysis.get("urgency", "LOW")
        reason = analysis.get("reason", "")

        if status == "PASS_IMMINENT":
            self.state.closing_signal_active = True
            self.state.closing_signal_type = "PASS_IMMINENT"
            self.state.closing_signal_time = \
                asyncio.get_event_loop().time()
            self.ui.log_decision(
                f">>> PASS IMMINENT: {reason} [{urgency}] — "
                f"lot about to go unsold, snipe window open <<<",
                "trigger")
            self.ui.set_status(f"PASS IMMINENT: {reason}", ACCENT_RED)
            self.ui.flash_alert("HIGH")

        elif status == "SALE_CLOSING":
            self.state.closing_signal_active = True
            self.state.closing_signal_type = "SALE_CLOSING"
            self.state.closing_signal_time = \
                asyncio.get_event_loop().time()
            color = ACCENT_RED if urgency == "HIGH" else ACCENT_AMBER
            self.ui.log_decision(
                f">>> SALE CLOSING: {reason} [{urgency}] — "
                f"hammer about to fall on current bidder <<<",
                "trigger")
            self.ui.set_status(f"SALE CLOSING: {reason}", color)
            self.ui.flash_alert(urgency)

        elif status == "SOLD":
            # Log only — do NOT touch the state machine. Auctioneers say
            # "sold" constantly and it was wiping live BID_WAR state
            # mid-lot. Actual sales are recorded from the DOM: the
            # "SOLD TO THE ..." label or the lot changing.
            self.ui.log_decision(
                f"SOLD heard (audio): {reason} — awaiting DOM confirmation",
                "sold")
