# Auction Bidding Bot

Real-time auction sniper bot for easyliveauction.com. Captures live audio from the auction stream, transcribes auctioneer speech in real-time using OpenAI Whisper, and uses GPT-4o to make intelligent bidding decisions.

## Strategy

The bot implements a "sniper" strategy:
1. **Wait** as the auctioneer drops the price
2. **Listen** for closing signals ("going once", "going twice", "final call")
3. **React** to competitor bids when the item is about to close
4. **Bid** only at the last moment, up to your configured maximum

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```

Required environment variables:
- `OPENAI_API_KEY` - Your OpenAI API key (for Whisper + GPT-4o)
- `AUCTION_EMAIL` - Your easyliveauction.com email (if login needed)
- `AUCTION_PASSWORD` - Your easyliveauction.com password

## Configuration

Edit `config.json`:

| Field | Description |
|-------|-------------|
| `auction_url` | The live auction URL |
| `max_bid_gbp` | Maximum bid in GBP |
| `target_lots` | List of lot numbers to bid on (empty = all) |
| `bid_strategy.snipe_on_going_once` | Bid on "going once" |
| `bid_strategy.snipe_on_going_twice` | Bid on "going twice" |
| `bid_strategy.reaction_delay_ms` | Delay before placing bid |
| `headless` | Run browser in headless mode |

## Usage

```bash
python auction_bot.py
```

The bot will:
1. Open the auction page in a Chromium browser
2. Capture the Dolby/WebRTC audio stream
3. Transcribe audio in 3-second chunks via Whisper
4. Monitor the page for bid state changes
5. Use AI to decide when to place bids

## How It Works

### Audio Capture
The bot injects JavaScript into the page that taps into the `<audio id="dolbyVideo">` element's MediaStream via the Web Audio API, capturing PCM audio data at 16kHz.

### Speech-to-Text
Audio chunks are sent to OpenAI's Whisper API with auction-specific vocabulary prompting for better recognition of terms like "going once", "going twice", lot numbers, etc.

### Decision Engine
Three layers of decision-making:
1. **Fast triggers** - Pattern matching for immediate signals ("going twice" = instant bid)
2. **AI analysis** - GPT-4o analyzes transcript context for ambiguous situations
3. **DOM monitoring** - Watches bid amount changes to detect competitor activity

### Bid Placement
Clicks the appropriate bid button (`#bid-live-get-ready` or `#bid-live-bidding-soon`) with a configurable reaction delay to appear more human.

## Safety

- Never bids above your configured maximum
- Stops bidding if it detects it's already winning
- Configurable target lots (won't bid on lots you don't want)
- Non-headless mode by default so you can watch and intervene
- Reaction delay to avoid appearing bot-like

## Limitations

- Requires the auction page to be using Dolby/WebRTC audio streaming
- Whisper transcription has ~3 second latency per chunk
- Requires an active OpenAI API key with Whisper and GPT-4o access
- Browser must remain in focus for audio capture to work reliably
