#!/usr/bin/env python3
"""
Dynamic Stock List Manager
Scans YouTube and X (Twitter) for expert stock recommendations,
maintains a dynamic stocks.json that your trading script reads from.

Adds stocks on bullish signals, removes on bearish signals.
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import time
import anthropic
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scanner.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
YOUTUBE_CHANNELS_FILE = Path("youtube_channels.txt")
X_ACCOUNTS_FILE       = Path("x_accounts.txt")
STOCKS_FILE           = Path("stocks.json")
SEEN_FILE             = Path("seen_content.json")
MACRO_FILE            = Path("macro_signals.json")
MAX_TRANSCRIPT        = 8000
MAX_VIDEOS_PER_CHAN   = 5
MACRO_RETENTION_DAYS  = 14


# ── stocks.json helpers ───────────────────────────────────────────────────────

def load_stocks() -> dict:
    """
    stocks.json structure:
    {
      "AAPL": {
        "added": "2025-01-15",
        "sources": ["YouTube: Investing with Tom", "X: @jimcramer"],
        "mentions": 3
      },
      ...
    }
    """
    if STOCKS_FILE.exists():
        return json.loads(STOCKS_FILE.read_text())
    return {}


def save_stocks(stocks: dict):
    STOCKS_FILE.write_text(json.dumps(stocks, indent=2))
    log.info(f"stocks.json updated — {len(stocks)} stock(s) in list: {sorted(stocks.keys())}")


def add_stock(stocks: dict, ticker: str, source: str):
    today = datetime.now().strftime("%Y-%m-%d")
    # Deduplicate known aliases — always use the canonical ticker
    aliases = {"GOOG": "GOOGL", "GOOGL": "GOOGL"}
    ticker = aliases.get(ticker, ticker)
    # Also merge any existing GOOG entry into GOOGL
    if "GOOG" in stocks and ticker == "GOOGL":
        stocks["GOOGL"] = stocks.pop("GOOG")
    if ticker in stocks:
        stocks[ticker]["mentions"] += 1
        if source not in stocks[ticker]["sources"]:
            stocks[ticker]["sources"].append(source)
        log.info(f"  ↑ {ticker} mention count now {stocks[ticker]['mentions']} (source: {source})")
    else:
        stocks[ticker] = {
            "added":    today,
            "sources":  [source],
            "mentions": 1,
        }
        log.info(f"  ✅ ADDED {ticker} to stock list (source: {source})")


def remove_stock(stocks: dict, ticker: str, source: str, reason: str):
    aliases = {"GOOG": "GOOGL", "GOOGL": "GOOGL"}
    ticker = aliases.get(ticker, ticker)
    if ticker in stocks:
        del stocks[ticker]
        log.info(f"  ❌ REMOVED {ticker} from stock list (reason: {reason}, source: {source})")
    else:
        log.debug(f"  {ticker} not in list, nothing to remove")


# ── Seen content tracking ─────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


# ── Claude AI analysis ────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are a financial analyst. Given content from a financial expert (YouTube video or social media post),
extract ALL stock signals — both bullish (buy) and bearish (sell).

Return ONLY a JSON object in this exact format, no other text:
{
  "bullish": ["TICKER1", "TICKER2"],
  "bearish": ["TICKER3"]
}

Rules:
- bullish: explicit buy/long recommendations ("I'm buying", "strong buy", "great entry", "adding to portfolio", "love this stock")
- bearish: explicit sell/short signals ("I'm selling", "avoid", "getting out", "bearish on", "this is a sell", "cut your losses")
- Neutral mentions, comparisons, or examples = exclude entirely
- Uppercase tickers only, 1-5 letters, US stocks
- Empty arrays if nothing qualifies"""

MACRO_PROMPT = """You are a market analyst. Given content from a financial YouTube video or podcast,
extract macro/market-level signals. Only populate fields where the content explicitly addresses them.

Return ONLY a JSON object in this exact format, no other text:
{
  "is_macro": true,
  "market_bias": "bullish",
  "vix_outlook": "stable",
  "sectors": {"tech": "bullish", "energy": "neutral"},
  "risks": ["Fed rate decision", "earnings season"],
  "summary": "One sentence capturing the analyst's key market view."
}

Rules:
- is_macro: true only if the content discusses broad market direction, macro conditions, or sector rotation. false if it is purely stock picks with no macro context.
- market_bias: "bullish", "bearish", or "neutral". null if not discussed.
- vix_outlook: "rising", "falling", or "stable". null if not discussed.
- sectors: object mapping sector names to "bullish"/"bearish"/"neutral". Omit sectors not discussed.
- risks: list of specific risks named (e.g. "Fed rate hike", "recession fears", "earnings miss"). Empty array if none named.
- summary: one sentence. Empty string if is_macro is false.
If is_macro is false, return: {"is_macro": false, "market_bias": null, "vix_outlook": null, "sectors": {}, "risks": [], "summary": ""}"""


def _extract_json_object(text: str) -> dict:
    """
    Defensively pull a JSON object out of Claude's response, even if it's
    wrapped in markdown fences or has leading/trailing commentary.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if text.count("```") >= 2 else text.lstrip("`")
        text = text.removeprefix("json").strip()
    start = text.find("{")
    end   = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in response")
    return json.loads(text[start:end + 1])


def load_macro_signals() -> list:
    if MACRO_FILE.exists():
        return json.loads(MACRO_FILE.read_text())
    return []


def save_macro_signals(signals: list):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MACRO_RETENTION_DAYS)).strftime("%Y-%m-%d")
    signals = [s for s in signals if s.get("date", "9999") >= cutoff]
    MACRO_FILE.write_text(json.dumps(signals, indent=2))
    log.info(f"macro_signals.json updated — {len(signals)} signal(s) retained")


def analyze_macro(client: anthropic.Anthropic, content: str, source_label: str, date_str: str) -> dict | None:
    """Returns a macro signal dict, or None if not macro content or on error."""
    raw = ""
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Source: {source_label}\n\nContent:\n{content[:6000]}"
            }],
            system=MACRO_PROMPT,
        )
        raw = msg.content[0].text
        data = _extract_json_object(raw)
        if not data.get("is_macro"):
            return None
        return {
            "date":          date_str,
            "source":        source_label,
            "market_bias":   data.get("market_bias"),
            "vix_outlook":   data.get("vix_outlook"),
            "sectors":       data.get("sectors", {}),
            "risks":         data.get("risks", []),
            "summary":       data.get("summary", ""),
        }
    except Exception as e:
        log.warning(f"Macro analysis failed for '{source_label}': {e} | raw={raw[:200]!r}")
        return None


def analyze_content(client: anthropic.Anthropic, content: str, source_label: str) -> tuple[list, list]:
    """Returns (bullish_tickers, bearish_tickers)."""
    raw = ""
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Source: {source_label}\n\nContent:\n{content[:6000]}"
            }],
            system=ANALYSIS_PROMPT,
        )
        raw = msg.content[0].text
        data = _extract_json_object(raw)
        bullish = [t.upper() for t in data.get("bullish", []) if isinstance(t, str) and t.isalpha() and 1 <= len(t) <= 5]
        bearish = [t.upper() for t in data.get("bearish", []) if isinstance(t, str) and t.isalpha() and 1 <= len(t) <= 5]
        log.info(f"    -> bullish={bullish} bearish={bearish}")
        return bullish, bearish
    except Exception as e:
        log.warning(f"Claude analysis failed for '{source_label}': {e} | raw_response={raw[:200]!r}")
        return None, None


# ── YouTube scanner ───────────────────────────────────────────────────────────

def load_youtube_channels() -> list:
    if not YOUTUBE_CHANNELS_FILE.exists():
        log.warning("youtube_channels.txt not found — skipping YouTube scan")
        return []
    return [l.strip() for l in YOUTUBE_CHANNELS_FILE.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


CHANNEL_CACHE_FILE    = Path("channel_id_cache.json")


def _load_channel_cache() -> dict:
    if CHANNEL_CACHE_FILE.exists():
        return json.loads(CHANNEL_CACHE_FILE.read_text())
    return {}


def _save_channel_cache(cache: dict):
    CHANNEL_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def resolve_channel_id(youtube, identifier: str) -> str | None:
    if identifier.startswith("UC") and len(identifier) == 24:
        return identifier

    cache = _load_channel_cache()
    if identifier in cache:
        return cache[identifier]

    resolved = None
    for prefix in ["https://www.youtube.com/@", "https://youtube.com/@", "@"]:
        if identifier.startswith(prefix):
            handle = identifier[len(prefix):].split("/")[0]
            try:
                resp = youtube.search().list(q=handle, type="channel", part="id", maxResults=1).execute()
                items = resp.get("items", [])
                if items:
                    resolved = items[0]["id"]["channelId"]
            except Exception as e:
                log.warning(f"Could not resolve handle '{handle}': {e}")
            break
    else:
        try:
            resp = youtube.search().list(q=identifier, type="channel", part="id", maxResults=1).execute()
            items = resp.get("items", [])
            if items:
                resolved = items[0]["id"]["channelId"]
        except Exception as e:
            log.warning(f"YouTube search fallback failed for '{identifier}': {e}")

    if resolved:
        cache[identifier] = resolved
        _save_channel_cache(cache)

    return resolved


def get_transcript(video_id: str) -> str:
    try:
        ytt_api = YouTubeTranscriptApi()
        fetched = ytt_api.fetch(video_id, languages=["en", "en-US"])
        text = " ".join(snippet.text for snippet in fetched)[:MAX_TRANSCRIPT]
        time.sleep(5)   # be polite to YouTube — avoid triggering rate limits
        return text
    except (NoTranscriptFound, TranscriptsDisabled) as e:
        log.warning(f"    Transcript unavailable for {video_id}: {type(e).__name__}")
        return ""
    except Exception as e:
        log.warning(f"    Transcript fetch error for {video_id}: {type(e).__name__}: {e}")
        return ""


def scan_youtube(client: anthropic.Anthropic, stocks: dict, seen: set, since: datetime) -> list:
    """Returns list of macro signal dicts collected during this scan."""
    yt_api_key = os.environ.get("YOUTUBE_API_KEY")
    if not yt_api_key:
        log.warning("YOUTUBE_API_KEY not set — skipping YouTube")
        return []

    youtube      = build("youtube", "v3", developerKey=yt_api_key)
    channels     = load_youtube_channels()
    macro_new    = []
    log.info(f"YouTube: scanning {len(channels)} channel(s)")

    for identifier in channels:
        channel_id = resolve_channel_id(youtube, identifier)
        if not channel_id:
            log.warning(f"  ❌ Could not resolve channel: {identifier}")
            continue
        log.info(f"  ✓ Resolved {identifier} -> {channel_id}")

        try:
            resp = youtube.search().list(
                channelId=channel_id, part="id,snippet", order="date",
                type="video", publishedAfter=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                maxResults=MAX_VIDEOS_PER_CHAN,
            ).execute()
        except Exception as e:
            log.warning(f"  Error fetching videos for {identifier}: {e}")
            continue

        items = resp.get("items", [])
        log.info(f"  {identifier}: {len(items)} video(s) found since {since.strftime('%Y-%m-%d')}")

        for item in items:
            vid_id    = item["id"]["videoId"]
            title     = item["snippet"]["title"]
            channel   = item["snippet"]["channelTitle"]
            pub_date  = item["snippet"]["publishedAt"][:10]

            if vid_id in seen:
                log.info(f"  Skipping (already processed): [{channel}] {title}")
                continue

            log.info(f"  Analyzing: [{channel}] {title}")
            transcript = get_transcript(vid_id)
            if not transcript:
                log.warning(f"    ⚠ No transcript available — Claude only sees title/description")
            else:
                log.info(f"    Transcript fetched: {len(transcript)} chars")
            content = f"Title: {title}\n\nDescription: {item['snippet'].get('description','')[:300]}\n\nTranscript: {transcript}"
            source  = f"YouTube: {channel}"

            bullish, bearish = analyze_content(client, content, source)

            macro = analyze_macro(client, content, source, pub_date)
            if macro:
                log.info(f"    -> macro: bias={macro['market_bias']} vix={macro['vix_outlook']} risks={macro['risks']}")
                macro_new.append(macro)

            # Only mark as seen if analysis actually ran (not an API error)
            if bullish is not None and bearish is not None:
                seen.add(vid_id)

            for ticker in (bullish or []):
                add_stock(stocks, ticker, source)
            for ticker in (bearish or []):
                remove_stock(stocks, ticker, source, reason="expert bearish signal")

            seen.add(vid_id)

    return macro_new



# ── Alpha Vantage News Sentiment scanner ──────────────────────────────────────

# Tickers to scan for news sentiment — pulls from your core watchlist
# plus dynamic picks in stocks.json
AV_WATCHLIST = [
    "NVDA", "META", "GOOGL", "MSFT", "AMD", "AVGO", "CRM", "NOW", "SNOW", "INTU",
    "IONQ", "RGTI", "PLTR", "COIN", "HOOD", "MSTR", "NBIS", "RKLB",
]
AV_MIN_RELEVANCE  = 0.5    # only use articles with relevance score >= 0.5
AV_MIN_CONFIDENCE = 0.6    # only use sentiment scores >= 0.6 confidence


def scan_alphavantage(client: anthropic.Anthropic, stocks: dict, seen: set, since: datetime):
    """
    Scan Alpha Vantage News & Sentiment API for bullish/bearish signals.
    Free API key at https://www.alphavantage.co/support/#api-key
    Docs: https://www.alphavantage.co/documentation/#news-sentiment
    """
    av_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not av_key:
        log.warning("ALPHAVANTAGE_API_KEY not set — skipping Alpha Vantage news scan")
        return

    import urllib.request

    # Build ticker list: core watchlist + current dynamic picks
    tickers = list(set(AV_WATCHLIST + list(stocks.keys())))
    tickers_str = ",".join(tickers[:20])   # API allows up to 20 tickers

    time_from = since.strftime("%Y%m%dT%H%M")
    url = (
        f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
        f"&tickers={tickers_str}"
        f"&time_from={time_from}"
        f"&limit=50"
        f"&apikey={av_key}"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning(f"Alpha Vantage fetch failed: {e}")
        return

    articles = data.get("feed", [])
    if not articles:
        log.warning(f"Alpha Vantage: no articles returned (check API key or free tier limit)")
        return

    log.info(f"Alpha Vantage: {len(articles)} article(s) found")

    for article in articles:
        article_id = f"av_{article.get('url', '')[-40:]}"
        if article_id in seen:
            continue

        title      = article.get("title", "")
        ticker_sentiments = article.get("ticker_sentiment", [])

        for ts in ticker_sentiments:
            ticker    = ts.get("ticker", "").upper()
            relevance = float(ts.get("relevance_score", 0))
            score     = float(ts.get("ticker_sentiment_score", 0))
            label     = ts.get("ticker_sentiment_label", "").lower()

            # Skip low relevance or weak signals
            if relevance < AV_MIN_RELEVANCE:
                continue
            if abs(score) < AV_MIN_CONFIDENCE:
                continue
            if not ticker.isalpha() or not (1 <= len(ticker) <= 5):
                continue

            source = f"AlphaVantage News"

            if "bullish" in label:
                add_stock(stocks, ticker, source)
                log.info(f"    📰 Bullish: {ticker} (score={score:.2f}, relevance={relevance:.2f}) — {title[:60]}")
            elif "bearish" in label:
                remove_stock(stocks, ticker, source, reason="AlphaVantage bearish news sentiment")
                log.info(f"    📰 Bearish: {ticker} (score={score:.2f}, relevance={relevance:.2f}) — {title[:60]}")

        seen.add(article_id)
        time.sleep(0.5)   # stay well within free tier rate limits




# ── X (Twitter) scanner ───────────────────────────────────────────────────────

def load_x_accounts() -> list:
    if not X_ACCOUNTS_FILE.exists():
        log.warning("x_accounts.txt not found — skipping X scan")
        return []
    return [l.strip().lstrip("@") for l in X_ACCOUNTS_FILE.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def scrape_x_posts(username: str, since: datetime) -> list[dict]:
    """
    Scrape recent posts from an X account using nitter (free, no API key needed).
    Returns list of {id, text} dicts.
    """
    try:
        import httpx
        from bs4 import BeautifulSoup

        # Try multiple nitter instances in case one is down
        nitter_hosts = [
            "https://nitter.privacydev.net",
            "https://nitter.poast.org",
            "https://nitter.1d4.us",
        ]

        cutoff_str = since.strftime("%b %d, %Y")
        posts = []

        for host in nitter_hosts:
            try:
                url  = f"{host}/{username}"
                resp = httpx.get(url, timeout=10, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    continue

                soup      = BeautifulSoup(resp.text, "html.parser")
                timeline  = soup.select(".timeline-item")

                for item in timeline[:20]:
                    tweet_text = item.select_one(".tweet-content")
                    tweet_date = item.select_one(".tweet-date a")
                    tweet_link = item.select_one(".tweet-link")

                    if not tweet_text or not tweet_date:
                        continue

                    # Basic recency check — nitter shows relative or absolute dates
                    date_str = tweet_date.get("title", "")
                    post_id  = tweet_link["href"] if tweet_link else f"{username}_{len(posts)}"

                    if post_id in [p["id"] for p in posts]:
                        continue

                    posts.append({"id": post_id, "text": tweet_text.get_text(strip=True)})

                if posts:
                    log.info(f"  X/@{username}: fetched {len(posts)} post(s) via {host}")
                    return posts

            except Exception as e:
                log.debug(f"  Nitter host {host} failed: {e}")
                continue

        log.warning(f"  X/@{username}: all nitter hosts failed")
        return []

    except ImportError:
        log.warning("httpx or beautifulsoup4 not installed — skipping X scraping")
        return []


def scan_x(client: anthropic.Anthropic, stocks: dict, seen: set, since: datetime):
    accounts = load_x_accounts()
    if not accounts:
        return

    log.info(f"X: scanning {len(accounts)} account(s)")

    for username in accounts:
        posts = scrape_x_posts(username, since)
        for post in posts:
            post_id = f"x_{post['id']}"
            if post_id in seen:
                continue

            source   = f"X: @{username}"
            bullish, bearish = analyze_content(client, post["text"], source)

            for ticker in bullish:
                add_stock(stocks, ticker, source)
            for ticker in bearish:
                remove_stock(stocks, ticker, source, reason="expert bearish signal on X")

            seen.add(post_id)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Don't save changes to stocks.json")
    parser.add_argument("--days",    type=int, default=2,  help="How many days back to scan (default: 2)")
    args = parser.parse_args()

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        log.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=anthropic_key)
    since  = datetime.now(timezone.utc) - timedelta(days=args.days)
    stocks = load_stocks()
    seen   = load_seen()

    before = set(stocks.keys())

    log.info(f"=== Scan started | lookback={args.days}d | existing stocks={sorted(before)} ===")

    macro_new = scan_youtube(client, stocks, seen, since)
    scan_alphavantage(client, stocks, seen, since)
    # X/Twitter scanning disabled — free scraping (nitter) is no longer reliable.
    # Re-enable once a paid X API or alternative source is set up.
    # scan_x(client, stocks, seen, since)

    after   = set(stocks.keys())
    added   = after - before
    removed = before - after

    log.info(f"=== Scan complete | added={sorted(added)} | removed={sorted(removed)} | total={sorted(after)} ===")
    log.info(f"=== Macro signals collected this run: {len(macro_new)} ===")

    if not args.dry_run:
        save_stocks(stocks)
        save_seen(seen)
        existing_macro = load_macro_signals()
        save_macro_signals(existing_macro + macro_new)
    else:
        log.info("[DRY RUN] No files were modified")


if __name__ == "__main__":
    main()
