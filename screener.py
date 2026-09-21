"""
screener.py — Dynamic Stock Universe Builder
=============================================
Five parallel pipelines feed into one deduplicated, scored candidate list.

  Pipeline 1 — Momentum Screener       (yFinance, FREE, no key)
  Pipeline 2 — Alpaca Top Movers       (FREE with your Alpaca account)
  Pipeline 3 — News Sentiment          (NewsAPI.org, FREE tier optional)
  Pipeline 4 — Congressional Trades    (Quiver Quantitative, FREE optional)
  Pipeline 5 — Reddit VADER Sentiment  (Reddit API, FREE optional)
      Scrapes r/stocks, r/investing, r/wallstreetbets, r/dividends for ticker
      mentions, scores each post title with VADER NLP, and ranks by weighted
      sentiment — inspired directly by the Colab notebook approach but applied
      to quality stocks rather than penny stocks.

Bucket-aware: get_universe() accepts an optional bucket config so that
dividend-focused buckets get a dividend-weighted candidate pool, and
price-range buckets get a pre-filtered set — all before the AI agents run.
"""

import os
import re
import time
import logging
import requests
import yfinance as yf
from datetime import datetime, timedelta

import screener_cache

# Load keys before module-level constants are read. agent.py imports screener.py
# before it copies config.json into os.environ, so screener must hydrate itself.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except Exception:
    pass
try:
    from config_server import load_config, sanitize_config_value
    for _k, _v in load_config().items():
        if _v and not os.environ.get(_k):
            os.environ[_k] = sanitize_config_value(_k, _v)
except Exception:
    pass

log = logging.getLogger(__name__)

# ── Config from config.json / .env ────────────────────────────────────────────
NEWS_API_KEY      = os.getenv("NEWS_API_KEY", "")
QUIVER_API_KEY    = os.getenv("QUIVER_API_KEY", "")
REDDIT_CLIENT_ID  = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_SECRET     = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "trading_agent_bot/1.0")
TRADINGVIEW_SCREENER_REGION = os.getenv("TRADINGVIEW_SCREENER_REGION", "america")
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET     = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_DATA_URL   = "https://data.alpaca.markets"

# Default filters (overridden per-bucket)
MIN_PRICE   = 5.0
MAX_PRICE   = 500.0
MIN_AVG_VOL = 500_000

EXCLUSIONS = {
    "SPY","QQQ","IWM","DIA","VXX","UVXY","SQQQ","TQQQ","SPXU","UPRO",
    "SOXS","SOXL","LABD","LABU","ARKK","GLD","SLV","USO","TLT","HYG",
}

# ── Base universe ─────────────────────────────────────────────────────────────
SP500_SAMPLE = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","BRK-B","JPM","V",
    "UNH","XOM","LLY","JNJ","AVGO","PG","MA","HD","MRK","ABBV",
    "CVX","COST","ORCL","PEP","ADBE","CSCO","ACN","MCD","BAC","CRM",
    "NFLX","TMO","ABT","WMT","LIN","AMD","QCOM","DHR","TXN","INTU",
    "PM","MS","GS","ISRG","SPGI","RTX","BLK","NOW","AMGN","SYK",
    "AXP","BKNG","PLD","ADI","GILD","MO","CB","ZTS","MDLZ","REGN",
    "MMC","VRTX","TJX","ELV","SCHW","BSX","AON","HCA","C","SHW",
    "UBER","ABNB","SNOW","PLTR","CRWD","PANW","DDOG","ZS","NET","MDB",
    "SHOP","COIN","RBLX","SOFI","RIVN","ENPH","FSLR","CELH","MNST","KDP",
    "DXCM","PODD","ALGN","FTNT","CYBR","DT","ESTC","APPN","PEGA","PCTY",
]

RUSSELL_EXTRA = [
    "SMCI","SEDG","RUN","ARRY","NOVA","STEM","HOLX","XRAY","HSIC",
    "S","VRNS","TENB","QLYS","RDWR","CERT","JAMF","SUMO","NEWR",
    "EXR","CUBE","NSA","REXR","EQR","UDR","CPT","ESS","MAA",
    "WOLF","LAZR","LIDR","IONQ","RGTI","QBTS","SOUN","BBAI","GTLB","PATH",
]

# High-yield dividend stocks — used when bucket mode = "dividend"
DIVIDEND_UNIVERSE = [
    # Dividend Aristocrats & high-yield blue chips
    "JNJ","PG","KO","PEP","MCD","MMM","T","VZ","XOM","CVX",
    "MO","PM","ABBV","BMY","PFE","GILD","AMGN","AVGO","TXN","QCOM",
    # REITs (high yield)
    "O","MAIN","STAG","EPR","WPC","NNN","ADC","VICI","IIPR","MPW",
    # Business Development Companies
    "ARCC","HTGC","GBDC","PSEC","GAIN","TPVG","FDUS","GLAD","SLRC","CSWC",
    # Utilities
    "NEE","DUK","SO","D","AEP","EXC","SRE","XEL","WEC","ES",
    # MLPs & Energy income
    "ENB","ET","EPD","MMP","PAA","MPLX","WMB","KMI","OKE","LNG",
]

ALL_CANDIDATES = list(set(SP500_SAMPLE + RUSSELL_EXTRA) - EXCLUSIONS)

# Alpaca crypto symbols to consider when bucket mode = crypto.
DEFAULT_CRYPTO_SYMBOLS = [
    s.strip().upper() for s in os.getenv("CRYPTO_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD,LINK/USD").split(",") if s.strip()
]

def crypto_universe_screen(symbols: list = None, top_n: int = 20) -> list:
    """Simple 24/7 crypto universe builder. Uses yFinance daily bars for free momentum ranking.
    Trading symbols remain Alpaca-style BTC/USD, ETH/USD, etc.
    """
    symbols = symbols or DEFAULT_CRYPTO_SYMBOLS
    scored = []
    for sym in symbols:
        yf_sym = sym.replace("/", "-")
        try:
            hist = yf.Ticker(yf_sym).history(period="60d", interval="1d")
            if hist.empty or len(hist) < 10:
                scored.append((sym, 0.0))
                continue
            closes = hist["Close"].dropna().tolist()
            vols = hist["Volume"].dropna().tolist() if "Volume" in hist else []
            mom_7 = (closes[-1] - closes[-8]) / closes[-8] if len(closes) >= 8 and closes[-8] else 0
            mom_30 = (closes[-1] - closes[-31]) / closes[-31] if len(closes) >= 31 and closes[-31] else mom_7
            vol_score = 0.0
            if len(vols) >= 15 and sum(vols[-15:-1]) > 0:
                vol_score = min(vols[-1] / (sum(vols[-15:-1]) / 14), 3.0) / 3.0
            scored.append((sym, mom_7 * 0.55 + mom_30 * 0.30 + vol_score * 0.15))
        except Exception as e:
            log.warning(f"[Screener/Crypto] {sym}: {e}")
            scored.append((sym, 0.0))
    scored.sort(key=lambda x: x[1], reverse=True)
    result = [s for s, _ in scored[:top_n]]
    log.info(f"[Screener/Crypto] Universe: {result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline 1: Momentum Screen
# ─────────────────────────────────────────────────────────────────────────────

def momentum_screen(candidates: list, top_n: int = 30,
                    min_price: float = MIN_PRICE,
                    max_price: float = MAX_PRICE) -> list:
    """
    Batch-score candidates by 20d momentum + volume spike + RSI neutrality.
    Now accepts a candidates list so bucket-specific pools can be passed in.
    """
    log.info(f"[Screener/Momentum] Scoring {len(candidates)} candidates...")

    try:
        raw = yf.download(
            candidates, period="3mo", interval="1d",
            group_by="ticker", auto_adjust=True,
            progress=False, threads=True,
        )
    except Exception as e:
        log.warning(f"[Screener/Momentum] Batch download failed: {e}")
        return candidates[:top_n]

    scores = []
    multi  = len(candidates) > 1

    for ticker in candidates:
        try:
            closes  = (raw[ticker]["Close"] if multi else raw["Close"]).dropna().tolist()
            volumes = (raw[ticker]["Volume"] if multi else raw["Volume"]).dropna().tolist()

            if len(closes) < 25:
                continue

            price = closes[-1]
            if not (min_price <= price <= max_price):
                continue

            avg_vol = sum(volumes[-20:]) / 20
            if avg_vol < MIN_AVG_VOL:
                continue

            mom       = (closes[-1] - closes[-20]) / closes[-20]
            vol_spike = min(volumes[-1] / avg_vol if avg_vol > 0 else 1.0, 3.0) / 3.0

            deltas   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains    = [d for d in deltas[-14:] if d > 0]
            losses   = [-d for d in deltas[-14:] if d < 0]
            avg_gain = sum(gains)  / 14 if gains  else 0
            avg_loss = sum(losses) / 14 if losses else 0
            rsi      = (100 - 100 / (1 + avg_gain / avg_loss)) if avg_loss else 100.0
            rsi_score = max(0.0, 1.0 - abs(rsi - 52.5) / 47.5)

            composite = mom * 0.5 + vol_spike * 0.3 + rsi_score * 0.2
            scores.append((ticker, composite))

        except Exception:
            continue

    scores.sort(key=lambda x: x[1], reverse=True)
    result = [t for t, _ in scores[:top_n]]
    log.info(f"[Screener/Momentum] Top {len(result)}: {result[:8]}...")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline 2: Alpaca Top Movers
# ─────────────────────────────────────────────────────────────────────────────

def alpaca_movers_screen(top_n: int = 15,
                         min_price: float = MIN_PRICE,
                         max_price: float = MAX_PRICE) -> list:
    if not ALPACA_API_KEY or not ALPACA_SECRET:
        log.info("[Screener/Alpaca] Keys not set — skipping.")
        return []

    log.info("[Screener/Alpaca] Fetching top movers...")
    try:
        r = requests.get(
            f"{ALPACA_DATA_URL}/v1beta1/screener/stocks/movers",
            params={"top": top_n, "market_type": "stocks"},
            headers={
                "APCA-API-KEY-ID":     ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            timeout=10,
        )
        r.raise_for_status()
        gainers = r.json().get("gainers", [])
        result  = [
            g["symbol"] for g in gainers
            if g["symbol"] not in EXCLUSIONS
            and min_price <= g.get("price", 0) <= max_price
        ]
        log.info(f"[Screener/Alpaca] Movers: {result}")
        return result[:top_n]
    except Exception as e:
        log.warning(f"[Screener/Alpaca] Error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline 3: News Sentiment
# ─────────────────────────────────────────────────────────────────────────────

TICKER_RE    = re.compile(r'\b([A-Z]{2,5})\b')
BULLISH_WORDS = {
    "surge","surges","surging","soar","soars","soaring","rally","rallies",
    "beat","beats","beating","record","upgrade","upgrades","outperform",
    "breakout","buy","strong","gains","gain","rises","rise","jumps","jump",
    "lifted","boost","boosts","positive","profit","growth","high","highest",
    "dividend","yield","payout","income",
}
BEARISH_WORDS = {
    "crash","crashes","plunge","plunges","drop","drops","miss","misses",
    "downgrade","downgrades","underperform","sell","weak","loss","losses",
    "falls","fall","tumbles","tumble","negative","cut","cuts","layoff","fired",
    "suspend","suspended","halted",
}
NON_TICKERS = {
    "THE","AND","FOR","ARE","BUT","NOT","YOU","ALL","CAN","HER","WAS","ONE",
    "OUR","OUT","DAY","GET","HAS","HIM","HIS","HOW","ITS","NEW","NOW","OLD",
    "SEE","TWO","WAY","WHO","BOY","DID","OIL","GAS","CEO","CFO","IPO","GDP",
    "CPI","FED","ETF","SEC","FDA","IMF","CNN","BBC","NYT","WSJ","AI","US",
    "UK","EU","UN","USD","EUR","JPY","USA","NYSE","DOW","LAW","TAX","ESG",
    "COVID","NASDAQ","HTTP","HTTPS","LLC","INC","CORP","LTD","REIT","MLP",
}


def news_sentiment_screen(known_tickers: set, top_n: int = 20) -> list:
    if not NEWS_API_KEY:
        log.info("[Screener/News] NEWS_API_KEY not set — skipping.")
        return []

    log.info("[Screener/News] Fetching headlines...")
    ticker_scores = {}

    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q":        "stock OR earnings OR dividend OR market OR trading",
                "from":     yesterday,
                "language": "en",
                "sortBy":   "popularity",
                "pageSize": 100,
                "apiKey":   NEWS_API_KEY,
            },
            timeout=10,
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])
        log.info(f"[Screener/News] Fetched {len(articles)} articles.")

        for article in articles:
            text  = f"{article.get('title','')} {article.get('description','')}".upper()
            words = set(text.split())
            bullish = bool(words & {w.upper() for w in BULLISH_WORDS})
            bearish = bool(words & {w.upper() for w in BEARISH_WORDS})
            if not bullish or bearish:
                continue
            for match in TICKER_RE.finditer(text):
                tok = match.group(1)
                if tok in NON_TICKERS or len(tok) < 2:
                    continue
                if tok in known_tickers:
                    ticker_scores[tok] = ticker_scores.get(tok, 0) + 1

    except Exception as e:
        log.warning(f"[Screener/News] Error: {e}")
        return []

    ranked = sorted(ticker_scores.items(), key=lambda x: x[1], reverse=True)
    result = [t for t, _ in ranked[:top_n]]
    log.info(f"[Screener/News] Bullish mentions: {result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline 4: Congressional Trades
# ─────────────────────────────────────────────────────────────────────────────

def congress_trades_screen(top_n: int = 10, lookback_days: int = 14) -> list:
    if not QUIVER_API_KEY:
        log.info("[Screener/Congress] QUIVER_API_KEY not set — skipping.")
        return []

    log.info("[Screener/Congress] Fetching Congressional trades...")
    try:
        r = requests.get(
            "https://api.quiverquant.com/beta/live/congresstrading",
            headers={"Authorization": f"Token {QUIVER_API_KEY}"},
            timeout=10,
        )
        r.raise_for_status()
        trades = r.json()
        cutoff = datetime.now() - timedelta(days=lookback_days)
        counts = {}

        for trade in trades:
            if trade.get("Transaction", "").upper() != "PURCHASE":
                continue
            try:
                trade_date = datetime.strptime(trade["TransactionDate"], "%Y-%m-%d")
                if trade_date < cutoff:
                    continue
            except Exception:
                continue

            ticker = trade.get("Ticker", "").upper().strip()
            if not ticker or ticker in EXCLUSIONS or len(ticker) > 5:
                continue
            counts[ticker] = counts.get(ticker, 0) + 1

        ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        result = [t for t, _ in ranked[:top_n]]
        log.info(f"[Screener/Congress] Buys (last {lookback_days}d): {result}")
        return result

    except Exception as e:
        log.warning(f"[Screener/Congress] Error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline 5: Reddit VADER Sentiment  ← NEW (from Colab notebook, upgraded)
# ─────────────────────────────────────────────────────────────────────────────

# Subreddits searched — dynamically extended for dividend bucket
REDDIT_SUBS_DEFAULT  = ["stocks", "investing", "wallstreetbets", "StockMarket"]
REDDIT_SUBS_DIVIDEND = ["dividends", "dividendinvesting", "Bogleheads", "investing"]

# VADER compound score threshold — only count posts with net positive sentiment
VADER_MIN_SCORE = 0.05
# Minimum Reddit post score (upvotes) before we trust the mention
MIN_POST_SCORE  = 10


def _get_vader():
    """Lazy-import VADER so the whole module doesn't crash if vaderSentiment isn't installed."""
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        return SentimentIntensityAnalyzer()
    except ImportError:
        return None


def _get_reddit_token() -> str:
    """Get a Reddit OAuth2 bearer token using client credentials flow."""
    # Re-read config at call time so settings saved after process startup are honored.
    client_id = REDDIT_CLIENT_ID
    secret = REDDIT_SECRET
    user_agent = REDDIT_USER_AGENT
    try:
        from config_server import load_config, sanitize_config_value
        cfg = load_config()
        client_id = sanitize_config_value("REDDIT_CLIENT_ID", cfg.get("REDDIT_CLIENT_ID", client_id)) or client_id
        secret = sanitize_config_value("REDDIT_CLIENT_SECRET", cfg.get("REDDIT_CLIENT_SECRET", secret)) or secret
        user_agent = sanitize_config_value("REDDIT_USER_AGENT", cfg.get("REDDIT_USER_AGENT", user_agent)) or user_agent
    except Exception:
        pass
    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(client_id, secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": user_agent},
        timeout=10,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Reddit OAuth HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["access_token"]


def reddit_vader_screen(known_tickers: set,
                        subreddits: list = None,
                        top_n: int = 20,
                        post_limit: int = 100) -> list:
    """Return top tickers ranked by weighted VADER sentiment."""
    tickers, _meta = reddit_vader_screen_detailed(known_tickers, subreddits, top_n, post_limit)
    return tickers


def reddit_vader_screen_detailed(known_tickers: set,
                               subreddits: list = None,
                               top_n: int = 20,
                               post_limit: int = 100) -> tuple[list, dict]:
    """
    Scrape Reddit, score mentions with VADER + mention quality.
    Returns (ticker_list, intelligence_dict keyed by symbol).
    """
    from signal_attribution import score_reddit_mention

    empty_meta = {}
    if not REDDIT_CLIENT_ID or not REDDIT_SECRET:
        log.info("[Screener/Reddit] REDDIT_CLIENT_ID/SECRET not set — skipping.")
        return [], empty_meta

    vader = _get_vader()
    if not vader:
        log.info("[Screener/Reddit] vaderSentiment not installed — run: pip install vaderSentiment")
        return [], empty_meta

    subs = subreddits or REDDIT_SUBS_DEFAULT
    log.info(f"[Screener/Reddit] Scanning r/{', r/'.join(subs)} with VADER...")

    ticker_data = {}
    mentions_by_ticker = {}
    now_ts = datetime.now().timestamp()

    try:
        token   = _get_reddit_token()
        headers = {
            "Authorization": f"bearer {token}",
            "User-Agent":    REDDIT_USER_AGENT,
        }

        for sub in subs:
            try:
                r = requests.get(
                    f"https://oauth.reddit.com/r/{sub}/hot",
                    params={"limit": post_limit},
                    headers=headers,
                    timeout=10,
                )
                r.raise_for_status()
                posts = r.json().get("data", {}).get("children", [])

                for post in posts:
                    d = post.get("data", {})
                    post_score = d.get("score", 0)
                    if post_score < MIN_POST_SCORE:
                        continue

                    text = f"{d.get('title','')} {d.get('selftext','')}".upper()
                    compound = vader.polarity_scores(text)["compound"]
                    if compound < VADER_MIN_SCORE:
                        continue

                    created = d.get("created_utc") or now_ts
                    post_age_hours = max((now_ts - float(created)) / 3600.0, 0.25)
                    comment_count = int(d.get("num_comments") or 0)
                    author_karma = float(d.get("author_karma") or 0)

                    for match in TICKER_RE.finditer(text):
                        tok = match.group(1)
                        if tok in NON_TICKERS or len(tok) < 2:
                            continue
                        if tok not in known_tickers:
                            continue

                        q = score_reddit_mention(
                            upvotes=post_score,
                            comment_count=comment_count,
                            post_age_hours=post_age_hours,
                            subreddit=sub,
                            vader_compound=compound,
                            author_karma=author_karma,
                        )
                        weight = (1 + __import__('math').log10(max(post_score, 1))) * q["quality_score"]
                        if tok not in ticker_data:
                            ticker_data[tok] = {"score_sum": 0.0, "weight_sum": 0.0, "quality_sum": 0.0, "mentions": 0}
                        ticker_data[tok]["score_sum"] += compound * weight
                        ticker_data[tok]["weight_sum"] += weight
                        ticker_data[tok]["quality_sum"] += q["quality_score"]
                        ticker_data[tok]["mentions"] += 1

                        mentions_by_ticker.setdefault(tok, []).append({
                            "symbol": tok,
                            "subreddit": sub,
                            "post_id": d.get("id"),
                            "upvotes": post_score,
                            "comment_count": comment_count,
                            "post_age_hours": round(post_age_hours, 2),
                            "author_karma": author_karma,
                            "vader_compound": compound,
                            **q,
                        })

                time.sleep(1)

            except Exception as e:
                log.warning(f"[Screener/Reddit] Error fetching r/{sub}: {e}")
                continue

    except Exception as e:
        log.warning(f"[Screener/Reddit] Auth or fetch error: {e}")
        return [], empty_meta

    intelligence = {}
    scored = []
    for ticker, data in ticker_data.items():
        if data["weight_sum"] > 0:
            avg = data["score_sum"] / data["weight_sum"]
            scored.append((ticker, avg))
            intelligence[ticker] = {
                "avg_sentiment": round(avg, 4),
                "quality_score": round(data["quality_sum"] / max(data["mentions"], 1), 3),
                "mention_count": data["mentions"],
                "mentions": mentions_by_ticker.get(ticker, []),
                "subreddits": list({m["subreddit"] for m in mentions_by_ticker.get(ticker, [])}),
            }

    scored.sort(key=lambda x: x[1], reverse=True)
    result = [t for t, _ in scored[:top_n]]
    log.info(f"[Screener/Reddit] VADER top picks: {result}")
    return result, intelligence


# ─────────────────────────────────────────────────────────────────────────────
# Dividend Quality Screen  (used by dividend bucket)
# ─────────────────────────────────────────────────────────────────────────────

def dividend_quality_screen(candidates: list, top_n: int = 25) -> list:
    """
    Filter and rank dividend-focused candidates by:
      - Dividend yield ≥ 2%
      - Payout ratio < 90% (sustainable)
      - 5-year dividend growth (positive)
      - Positive trailing EPS
    Returns top_n sorted by composite dividend quality score.
    """
    log.info(f"[Screener/Dividend] Evaluating {len(candidates)} dividend candidates...")
    scored = []

    for ticker in candidates:
        try:
            info = yf.Ticker(ticker).info
            yield_    = info.get("dividendYield") or 0
            payout    = info.get("payoutRatio")   or 1.0
            eps       = info.get("trailingEps")   or 0
            div_rate  = info.get("dividendRate")  or 0
            ex_date   = info.get("exDividendDate")

            # Hard filters
            if yield_ < 0.02:    # must yield at least 2%
                continue
            if payout > 0.90:    # payout ratio must be sustainable
                continue
            if eps <= 0:         # must be profitable
                continue

            # Score: higher yield × lower payout ratio = better
            # Bonus if ex-dividend date is coming up within 45 days
            days_to_ex = 999
            if ex_date:
                try:
                    ex_dt = datetime.fromtimestamp(ex_date)
                    days_to_ex = (ex_dt - datetime.now()).days
                except Exception:
                    pass

            ex_bonus   = 0.3 if 0 < days_to_ex <= 45 else 0.0
            composite  = (yield_ * 10) * (1 - payout) + ex_bonus
            scored.append((ticker, composite, yield_, payout, days_to_ex))
            time.sleep(0.2)

        except Exception:
            continue

    scored.sort(key=lambda x: x[1], reverse=True)
    result = [t for t, *_ in scored[:top_n]]

    if scored:
        log.info(f"[Screener/Dividend] Top picks with yields:")
        for t, score, y, p, ex in scored[:5]:
            log.info(f"  {t}: yield={y*100:.1f}%  payout={p*100:.0f}%  days_to_ex={ex}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Universe Builder — bucket-aware
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline 6: TradingView Technical Rating  (no API key needed)
# ─────────────────────────────────────────────────────────────────────────────
# Replicates TradingView's Recommend.All composite signal using the same
# 17-indicator formula computed locally from yFinance data.
# Oscillators (7): RSI(14), Stoch %K, CCI(20), MACD, ADX, AO, Momentum
# Moving Averages (10): SMA 5/10/20/50/100/200 + EMA 5/10/20/50

TRADINGVIEW_SCREENER_REGION = os.getenv("TRADINGVIEW_SCREENER_REGION", "america")


def _tv_single_rating(ticker: str) -> tuple:
    """Returns (rating_str, score -1.0 to +1.0) for a single ticker."""
    try:
        t  = yf.Ticker(ticker)
        df = t.history(period="6mo", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 55:
            return "NEUTRAL", 0.0
        close = df["Close"]; high = df["High"]; low = df["Low"]
        signals = []
        # RSI(14)
        d = close.diff(); g = d.clip(lower=0).rolling(14).mean()
        ls = (-d.clip(upper=0)).rolling(14).mean()
        rsi = (100 - (100/(1+g/ls))).iloc[-1]
        signals.append(1 if rsi < 30 else -1 if rsi > 70 else 0)
        # Stoch %K
        k = (100*(close-low.rolling(14).min())/(high.rolling(14).max()-low.rolling(14).min())).rolling(3).mean()
        kv, dv = k.iloc[-1], k.rolling(3).mean().iloc[-1]
        signals.append(1 if kv < 20 and kv > dv else -1 if kv > 80 and kv < dv else 0)
        # CCI(20)
        tp = (high+low+close)/3
        cci = ((tp-tp.rolling(20).mean())/(0.015*tp.rolling(20).std())).iloc[-1]
        signals.append(1 if cci < -100 else -1 if cci > 100 else 0)
        # MACD
        ml = close.ewm(span=12).mean()-close.ewm(span=26).mean()
        signals.append(1 if ml.iloc[-1] > ml.ewm(span=9).mean().iloc[-1] else -1)
        # ADX proxy
        tr = (high-low).combine((high-close.shift()).abs(),max).combine((low-close.shift()).abs(),max)
        ap = (tr.rolling(14).mean()/tr.rolling(14).mean()*100).iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        signals.append(1 if ap>25 and close.iloc[-1]>sma50 else -1 if ap>25 else 0)
        # Awesome Oscillator
        mp = (high+low)/2
        ao = mp.rolling(5).mean()-mp.rolling(34).mean()
        signals.append(1 if ao.iloc[-1]>0 and ao.iloc[-1]>ao.iloc[-2] else
                       -1 if ao.iloc[-1]<0 and ao.iloc[-1]<ao.iloc[-2] else 0)
        # Momentum(10)
        signals.append(1 if close.iloc[-1]-close.iloc[-11]>0 else -1)
        # SMA 5/10/20/50/100/200
        p = close.iloc[-1]
        for per in [5,10,20,50,100,200]:
            if len(close)>per:
                signals.append(1 if p>close.rolling(per).mean().iloc[-1] else -1)
        # EMA 5/10/20/50
        for per in [5,10,20,50]:
            signals.append(1 if p>close.ewm(span=per).mean().iloc[-1] else -1)
        score = sum(signals)/len(signals)
        rating = ("STRONG_BUY" if score>0.5 else "BUY" if score>0.1 else
                  "NEUTRAL" if score>-0.1 else "SELL" if score>-0.5 else "STRONG_SELL")
        return rating, round(score, 3)
    except Exception as e:
        log.debug(f"[Screener/TV] {ticker}: {e}")
        return "NEUTRAL", 0.0


def tradingview_screen(candidates: list, top_n: int = 25,
                       min_rating: str = "BUY") -> list:
    """Pipeline 6: Score candidates with TradingView's 17-indicator formula."""
    log.info(f"[Screener/TV] Rating {len(candidates)} tickers...")
    min_score = {"STRONG_BUY": 0.5, "BUY": 0.1, "NEUTRAL": -0.1}.get(min_rating, 0.1)
    scored = []
    for i, ticker in enumerate(candidates):
        rating, score = _tv_single_rating(ticker)
        if score >= min_score:
            scored.append((ticker, score, rating))
        time.sleep(0.2)
        if (i+1) % 10 == 0:
            log.info(f"[Screener/TV] {i+1}/{len(candidates)}")
    scored.sort(key=lambda x: x[1], reverse=True)
    result = [t for t,s,r in scored[:top_n]]
    if scored:
        log.info(f"[Screener/TV] Top results: {[(t,r) for t,s,r in scored[:5]]}")
    return result

def get_universe(
    bucket_config:       dict = None,
    use_news_filter:     bool = True,
    use_congress_filter: bool = True,
    use_reddit_filter:   bool = True,
    max_universe:        int  = 40,
    force_refresh:       bool = False,
) -> tuple:
    """
    Entry point called by agent.py for each bucket each cycle.

    Results are cached on disk when SCREENER_CACHE=true (default) for
    SCREENER_CACHE_TTL_MINUTES (default 240). Cache key includes bucket mode,
    price band, custom tickers, and filter flags.

    Returns: (universe: list[str], sources: dict)
    """
    cache_key = screener_cache.make_cache_key(
        bucket_config, use_news_filter, use_congress_filter, use_reddit_filter, max_universe,
    )

    if not force_refresh:
        hit = screener_cache.get(cache_key)
        if hit:
            universe, sources, age_sec = hit
            sources = dict(sources)
            sources["cached"] = True
            sources["cache_age_min"] = round(age_sec / 60, 1)
            sources["cache_key"] = cache_key[:12]
            log.info(
                f"[Screener] Cache HIT ({sources['cache_age_min']}m old, "
                f"mode={sources.get('mode', '?')}) — {len(universe)} tickers"
            )
            return universe, sources

    universe, sources = _build_universe(
        bucket_config=bucket_config,
        use_news_filter=use_news_filter,
        use_congress_filter=use_congress_filter,
        use_reddit_filter=use_reddit_filter,
        max_universe=max_universe,
    )

    screener_cache.set(cache_key, universe, sources)
    sources = dict(sources)
    sources["cached"] = False
    sources["cache_key"] = cache_key[:12]
    return universe, sources


def _build_universe(
    bucket_config:       dict = None,
    use_news_filter:     bool = True,
    use_congress_filter: bool = True,
    use_reddit_filter:   bool = True,
    max_universe:        int  = 40,
) -> tuple:
    """
    Run all screener pipelines and merge into a ranked universe.

    bucket_config examples:
      None                          → default growth universe
      {"mode": "dividend"}          → dividend-quality focused pool
      {"mode": "price_range",
       "min_price": 5, "max_price": 50}  → low-priced stock bucket
      {"mode": "custom",
       "tickers": ["AAPL","MSFT"]}  → fixed custom list as the seed
      {"mode": "crypto",
       "symbols": ["BTC/USD","ETH/USD"]} → Alpaca crypto symbols
    """
    cfg = bucket_config or {}
    mode      = cfg.get("mode", "growth")
    min_price = float(cfg.get("min_price", MIN_PRICE))
    max_price = float(cfg.get("max_price", MAX_PRICE))

    log.info("=" * 55)
    log.info(f"[Screener] Building universe — mode={mode}  ${min_price}–${max_price}")

    # ── Choose candidate pool based on bucket mode ──
    if mode == "dividend":
        pool          = DIVIDEND_UNIVERSE
        reddit_subs   = REDDIT_SUBS_DIVIDEND
        # For dividend bucket, lower max price to focus on accessible names
        max_price     = min(max_price, 200.0)
    elif mode == "custom":
        pool          = cfg.get("tickers", ALL_CANDIDATES)
        reddit_subs   = REDDIT_SUBS_DEFAULT
    elif mode == "crypto":
        pool          = cfg.get("symbols", DEFAULT_CRYPTO_SYMBOLS)
        reddit_subs   = []
    elif mode == "price_range":
        # Filter base universe to the price band
        pool          = ALL_CANDIDATES
        reddit_subs   = REDDIT_SUBS_DEFAULT
    else:
        pool          = ALL_CANDIDATES
        reddit_subs   = REDDIT_SUBS_DEFAULT

    if mode == "crypto":
        universe = crypto_universe_screen(pool, top_n=min(max_universe, len(pool)))
        sources = {"crypto": len(universe), "mode": mode}
        log.info(
            "[Screener] Crypto pipeline: %d symbols from pool of %d (no equity pipelines in crypto mode)",
            len(universe),
            len(pool),
        )
        log.info(f"[Screener] Final crypto universe ({len(universe)}): {universe}")
        log.info("=" * 55)
        return universe, sources

    known = set(t.replace("-", "") for t in pool)
    scored = {}
    pipeline_membership = {}
    tv_ratings = {}

    def merge(tickers: list, weight: float, label: str):
        for i, raw_t in enumerate(tickers):
            t = raw_t.upper().replace("-", "")
            if t in EXCLUSIONS:
                continue
            pos_bonus = 1.0 - (i / max(len(tickers), 1)) * 0.5
            scored[t] = scored.get(t, 0) + weight * pos_bonus
            pipeline_membership.setdefault(t, {})[label] = {
                "rank": i,
                "list_size": len(tickers),
                "weight": weight,
            }
        log.info(f"[Screener]   {label}: {len(tickers)} tickers")

    # ── Run all pipelines ──
    if mode == "dividend":
        # For dividend buckets: run dividend quality screen first, then momentum on results
        div_picks = dividend_quality_screen(pool, top_n=30)
        momentum  = momentum_screen(div_picks or pool, top_n=20, min_price=min_price, max_price=max_price)
    else:
        momentum  = momentum_screen(pool, top_n=30, min_price=min_price, max_price=max_price)

    movers   = alpaca_movers_screen(top_n=15, min_price=min_price, max_price=max_price)
    news     = news_sentiment_screen(known, top_n=20) if use_news_filter     else []
    congress = congress_trades_screen(top_n=10)       if use_congress_filter else []
    reddit, reddit_intelligence = (
        reddit_vader_screen_detailed(known, subreddits=reddit_subs, top_n=20)
        if use_reddit_filter else ([], {})
    )

    # ── Merge with weights ──
    merge(momentum, 1.0, "Momentum")
    merge(movers,   2.0, "Alpaca Movers")
    merge(news,     2.0, "News Sentiment")
    merge(congress, 3.0, "Congress Trades")
    merge(reddit,   2.5, "Reddit VADER")
    tradingview = tradingview_screen(pool, top_n=25)
    for i, raw_t in enumerate(tradingview):
        t = raw_t.upper().replace("-", "")
        rating, tv_score = _tv_single_rating(t)
        tv_ratings[t] = tv_score
    merge(tradingview, 2.0, "TradingView")

    # For dividend mode, additionally boost any ticker that passed quality screen
    if mode == "dividend":
        div_set = set(t.replace("-","") for t in (div_picks or []))
        for t in div_set:
            if t in scored:
                scored[t] *= 1.5   # 50% boost for confirmed dividend quality

    ranked   = sorted(scored.items(), key=lambda x: x[1], reverse=True)
    universe = [t for t, _ in ranked[:max_universe]]

    from signal_attribution import build_universe_attributions
    attribution = build_universe_attributions(
        universe,
        pipeline_membership,
        reddit_intelligence=reddit_intelligence,
        tv_ratings=tv_ratings,
    )

    sources = {
        "momentum": len(momentum),
        "movers":   len(movers),
        "news":     len(news),
        "congress": len(congress),
        "reddit":   len(reddit),
        "tradingview": len(tradingview),
        "mode":     mode,
        "attribution": attribution,
        "reddit_intelligence": reddit_intelligence,
        "pipeline_membership": pipeline_membership,
    }

    log.info(f"[Screener] Final universe ({len(universe)}): {universe}")
    log.info("=" * 55)
    return universe, sources


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from dotenv import load_dotenv
    load_dotenv()

    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "growth"
    cfg  = {"mode": mode}
    if mode == "price_range":
        cfg.update({"min_price": 5, "max_price": 50})

    print(f"\n🔍 Running screener standalone — mode={mode}\n")
    universe, sources = get_universe(bucket_config=cfg)
    print(f"\n✅ Universe ({len(universe)} tickers):\n{universe}")
    print(f"\n📊 Sources: {sources}\n")
