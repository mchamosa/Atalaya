"""
ChamosaNews Cripto - actualizador de datos
Autor: Marcos Chamosa

Genera data/crypto.json y data/crypto_news.json con fuentes gratuitas:
  - CoinGecko: panorama global, top de monedas, máximos históricos y sectores
    (sin clave; opcional COINGECKO_API_KEY de su plan Demo para más margen)
  - alternative.me: índice de Miedo y Codicia cripto
  - DefiLlama: oferta total de stablecoins (liquidez que entra o sale)
  - Hyperliquid: funding y open interest de futuros perpetuos
  - Binance (espejo público data-api.binance.vision): velas diarias para RSI,
    medias y volumen relativo
  - RSS: CoinDesk, Cointelegraph, Decrypt, Bitcoin Magazine, Cointelegraph ES

Cada bloque es independiente: si una fuente falla, se conserva su último dato.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
ROOT = _HERE if (_HERE / "index.html").exists() else _HERE.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
NOW_DT = datetime.now(timezone.utc)
NOW = NOW_DT.isoformat(timespec="seconds")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json"}
CG_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
CG = "https://api.coingecko.com/api/v3"
BINANCE = ["https://data-api.binance.vision", "https://api.binance.com"]

STABLE_SYMBOLS = {
    "usdt", "usdc", "dai", "fdusd", "tusd", "usde", "usds", "pyusd", "usd1", "usdd", "gusd",
    "busd", "frax", "lusd", "susd", "eurc", "eurt", "usdp", "usdy", "usd0", "rlusd", "susde",
    "usdx", "usdb", "crvusd", "gho", "bfusd", "usdtb", "usdf", "usual", "buidl", "ustb",
}
SKIP_NAME = re.compile(r"\b(wrapped|staked|bridged|restaked|liquid staked|binance-peg|"
                       r"tokenized|xaut|pax gold|\bgold\b)\b", re.I)


def _jsonable(o):
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def get(url, params=None, headers=None, tries=3, timeout=30):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout)
            if r.status_code == 429:
                time.sleep(30 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(3)
    raise RuntimeError(f"{url}: {last}")


def cg(path, **params):
    h = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    out = get(CG + path, params=params, headers=h)
    time.sleep(2.5 if CG_KEY else 6)   # respeta el límite por minuto del plan gratuito
    return out


def rsi(closes, n=14):
    if len(closes) < n + 2:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


_KCACHE: dict[str, list] = {}


def klines(symbol: str, limit: int = 1000):
    """Velas diarias de Binance: lista de (fecha, high, close, volumen en USDT). Se guardan en memoria."""
    if symbol in _KCACHE and len(_KCACHE[symbol]) >= min(limit, len(_KCACHE[symbol])):
        return _KCACHE[symbol][-limit:]
    for host in BINANCE:
        try:
            r = requests.get(f"{host}/api/v3/klines", params={"symbol": symbol, "interval": "1d", "limit": 1000},
                             headers=UA, timeout=20)
            if r.status_code != 200:
                continue
            rows = [(datetime.fromtimestamp(k[0] / 1000, timezone.utc), float(k[2]), float(k[4]), float(k[7]))
                    for k in r.json()]
            _KCACHE[symbol] = rows
            time.sleep(0.12)
            return rows[-limit:]
        except Exception:
            continue
    _KCACHE[symbol] = []
    return None


def binance_usdt_pairs() -> list[str]:
    """Pares spot contra USDT en negociación (sin tokens apalancados ni stablecoins)."""
    for host in BINANCE:
        try:
            info = requests.get(f"{host}/api/v3/exchangeInfo", params={"permissions": "SPOT"},
                                headers=UA, timeout=30).json()
            out = []
            for x in info.get("symbols", []):
                b = x.get("baseAsset", "")
                if (x.get("quoteAsset") == "USDT" and x.get("status") == "TRADING"
                        and b.lower() not in STABLE_SYMBOLS and not re.search(r"(UP|DOWN|BULL|BEAR)$", b)):
                    out.append(b)
            if out:
                return out
        except Exception:
            continue
    return []


def is_stable_or_wrapped(c: dict) -> bool:
    sym = (c.get("symbol") or "").lower()
    if sym in STABLE_SYMBOLS or SKIP_NAME.search(c.get("name") or ""):
        return True
    p = c.get("current_price") or 0
    ch = abs(c.get("price_change_percentage_30d_in_currency") or 0)
    return 0.97 <= p <= 1.03 and ch < 2           # parece un dólar digital


def bias_score(chg7, chg30, r, above50, above200):
    s = (25 if above200 else -25) + (20 if above50 else -20)
    if r is not None:
        s += max(-25, min(25, (r - 50) * 1.25))
    if chg7 is not None:
        s += max(-15, min(15, chg7 * 2))
    if chg30 is not None:
        s += max(-15, min(15, chg30 * 0.8))
    return int(round(max(-100, min(100, s))))


# --------------------------------------------------------------------------
def block_global():
    g = cg("/global")["data"]
    return {
        "mcap": g["total_market_cap"]["usd"],
        "volume": g["total_volume"]["usd"],
        "mcap_chg24": g.get("market_cap_change_percentage_24h_usd"),
        "btc_dom": g["market_cap_percentage"].get("btc"),
        "eth_dom": g["market_cap_percentage"].get("eth"),
        "coins": g.get("active_cryptocurrencies"),
    }


def block_fear_greed():
    d = get("https://api.alternative.me/fng/", params={"limit": 90})["data"]
    es = {"extreme fear": "Miedo extremo", "fear": "Miedo", "neutral": "Neutral",
          "greed": "Codicia", "extreme greed": "Codicia extrema"}
    return {
        "value": int(d[0]["value"]),
        "label": es.get(d[0]["value_classification"].lower(), d[0]["value_classification"]),
        "prev_week": int(d[7]["value"]) if len(d) > 7 else None,
        "prev_month": int(d[30]["value"]) if len(d) > 30 else None,
        "history": [int(x["value"]) for x in reversed(d)],
    }


def block_stables():
    rows = get("https://stablecoins.llama.fi/stablecoincharts/all")
    pts = [(int(r["date"]), float((r.get("totalCirculatingUSD") or {}).get("peggedUSD") or 0)) for r in rows]
    pts = [p for p in pts if p[1] > 0]
    vals = [v for _, v in pts]
    last = vals[-1]
    return {
        "total": last,
        "chg7": round((last / vals[-8] - 1) * 100, 2) if len(vals) > 8 else None,
        "chg30": round((last / vals[-31] - 1) * 100, 2) if len(vals) > 31 else None,
        "history": [round(v / 1e9, 2) for v in vals[-120:]],
    }


def block_markets():
    rows = []
    for page in (1, 2, 3, 4):
        rows += cg("/coins/markets", vs_currency="usd", order="market_cap_desc", per_page=250, page=page,
                   price_change_percentage="1h,24h,7d,30d,1y", sparkline="false")
    return rows


def block_coins(markets: list[dict]) -> list[dict]:
    coins = []
    for c in markets:
        if is_stable_or_wrapped(c):
            continue
        coins.append({
            "id": c["id"], "symbol": c["symbol"].upper(), "name": c["name"], "rank": c.get("market_cap_rank"),
            "price": c.get("current_price"), "mcap": c.get("market_cap"), "volume": c.get("total_volume"),
            "chg1h": c.get("price_change_percentage_1h_in_currency"),
            "chg24h": c.get("price_change_percentage_24h_in_currency"),
            "chg7d": c.get("price_change_percentage_7d_in_currency"),
            "chg30d": c.get("price_change_percentage_30d_in_currency"),
            "chg1y": c.get("price_change_percentage_1y_in_currency"),
            "ath": c.get("ath"), "ath_date": (c.get("ath_date") or "")[:10],
            "from_ath": c.get("ath_change_percentage"),
        })
    # análisis técnico con velas diarias para las 100 primeras
    for c in coins[:100]:
        k = klines(c["symbol"] + "USDT", 400)
        if not k or len(k) < 60:
            continue
        closes = [x[2] for x in k]
        last = closes[-1]
        sma50 = sum(closes[-50:]) / 50
        sma200 = sum(closes[-200:]) / min(200, len(closes))
        r = rsi(closes)
        vols = [x[3] for x in k]
        avg = sum(vols[-31:-1]) / 30 if len(vols) > 31 else None
        c.update({
            "rsi": r, "above_sma50": last > sma50, "above_sma200": last > sma200,
            "vol_ratio": round(vols[-1] / avg, 2) if avg else None,
            "bias": bias_score(c["chg7d"], c["chg30d"], r, last > sma50, last > sma200),
        })
    return coins


ATH_MIN_VOLUME = 1e6          # volumen diario mínimo en dólares para entrar en la lista


def _signals(row: dict, closes: list[float], vol_ratio, base_days, nh) -> list[str]:
    sig = []
    if vol_ratio and vol_ratio >= 1.5:
        sig.append(f"Volumen x{vol_ratio:.1f}".replace(".", ","))
    if base_days is not None:
        if base_days >= 365:
            sig.append(f"Base de {base_days / 365:.1f} años".replace(".", ","))
        elif base_days >= 90:
            sig.append(f"Base de {base_days // 30} meses")
    if nh and nh >= 4:
        sig.append(f"{nh} máximos en 20 días")
    if closes and len(closes) >= 50:
        ext = (closes[-1] / (sum(closes[-50:]) / 50) - 1) * 100
        row["ext50"] = round(ext, 1)
        if ext >= 30:
            sig.append(f"Extendida +{ext:.0f} %")
    if row.get("status") != "cerca" and vol_ratio is not None and vol_ratio < 0.8:
        sig.append("Ruptura con poco volumen")
    return sig


def _history_stats(k, status: str):
    """Máximo anterior, base, volumen relativo, racha y gráfico con las velas de Binance."""
    highs = [x[1] for x in k]
    closes = [x[2] for x in k]
    dates = [x[0] for x in k]
    vols = [x[3] for x in k]
    cut = len(k) - 1
    if status != "cerca":
        for i in range(max(len(k) - 10, 1), len(k)):            # primera vela de la racha de máximos
            if highs[i] > max(highs[:i]):
                cut = i
                break
    res = {}
    prev = max(highs[:cut]) if cut > 0 else None
    if prev:
        j = highs.index(prev)
        res.update(prev_ath=prev, prev_ath_date=dates[j].strftime("%Y-%m-%d"),
                   base_days=None if (len(k) >= 1000 and j == 0) else (dates[cut] - dates[j]).days,
                   closed_above=closes[-1] > prev)
    avg = sum(vols[-31:-1]) / 30 if len(vols) > 31 else 0
    res["vol_ratio"] = round(vols[-1] / avg, 2) if avg else None
    run = 0
    for i in range(max(len(k) - 20, 1), len(k)):
        if highs[i] >= max(highs[:i]):
            run += 1
    res["nh20"] = run
    res["rsi"] = rsi(closes)
    step = max(1, 365 // 50)
    res["spark"] = [round(v, 8) for v in closes[-365::step]] + [round(closes[-1], 8)]
    return res, closes


def block_ath(markets: list[dict]) -> list[dict]:
    out = []
    for c in markets:
        if is_stable_or_wrapped(c) or (c.get("total_volume") or 0) < ATH_MIN_VOLUME:
            continue
        fa = c.get("ath_change_percentage")
        if fa is None or fa < -10:
            continue
        try:
            ath_dt = datetime.fromisoformat(c["ath_date"].replace("Z", "+00:00"))
        except Exception:
            continue
        age_h = (NOW_DT - ath_dt).total_seconds() / 3600
        if age_h <= 36 and fa >= -3:
            status = "hoy"
        elif age_h <= 240:
            status = "reciente"
        else:
            status = "cerca"
        row = {"id": c["id"], "symbol": c["symbol"].upper(), "name": c["name"], "rank": c.get("market_cap_rank"),
               "status": status, "price": c.get("current_price"), "ath": c.get("ath"),
               "ath_date": c["ath_date"][:10], "from_ath": fa, "volume": c.get("total_volume"),
               "mcap": c.get("market_cap"), "chg24h": c.get("price_change_percentage_24h_in_currency"),
               "chg7d": c.get("price_change_percentage_7d_in_currency"),
               "chg30d": c.get("price_change_percentage_30d_in_currency"),
               "base_days": None, "prev_ath": None, "prev_ath_date": None, "vol_ratio": None, "spark": None,
               "on_binance": False}
        k = klines(c["symbol"].upper() + "USDT")
        closes = []
        if k and len(k) > 60:
            st, closes = _history_stats(k, status)
            row.update(st)
            row["on_binance"] = True
        row["signals"] = _signals(row, closes, row.get("vol_ratio"), row.get("base_days"), row.get("nh20"))
        out.append(row)
    order = {"hoy": 0, "reciente": 1, "cerca": 2}
    out.sort(key=lambda r: (order[r["status"]], -(r["vol_ratio"] or 0), -(r["volume"] or 0)))
    return out


def block_ath_breadth(markets: list[dict]) -> dict:
    top = [c for c in markets if not is_stable_or_wrapped(c)][:100]
    near = [c for c in top if (c.get("ath_change_percentage") or -100) >= -10]
    near25 = [c for c in top if (c.get("ath_change_percentage") or -100) >= -25]
    return {"top": len(top), "near10": len(near), "near25": len(near25),
            "median_from_ath": sorted(c.get("ath_change_percentage") or -100 for c in top)[len(top) // 2] if top else None}


def block_high_1y(markets: list[dict]) -> list[dict]:
    """Máximos de 1 año (365 días) en todos los pares de Binance contra USDT."""
    names = {}
    for c in markets:
        names.setdefault(c["symbol"].upper(), c)
    out = []
    for base in binance_usdt_pairs():
        k = klines(base + "USDT")
        if not k or len(k) < 120:
            continue
        highs = [x[1] for x in k]
        closes = [x[2] for x in k]
        vols = [x[3] for x in k]
        avgv = sum(vols[-31:-1]) / 30
        if avgv < 2e6:                                         # poca liquidez
            continue
        win = 365
        prior = max(highs[-win - 1:-1]) if len(highs) > win else max(highs[:-1])
        hi1y = max(highs[-win:])
        dist = (closes[-1] / hi1y - 1) * 100
        new = [closes[i] > max(highs[max(0, i - win):i]) for i in range(len(k) - 7, len(k))]
        if new[-1]:
            status = "hoy"
        elif any(new) and dist >= -5:
            status = "reciente"
        elif dist >= -5:
            status = "cerca"
        else:
            continue
        j = len(highs) - win - 1 + highs[-win - 1:-1].index(prior) if len(highs) > win else highs.index(prior)
        c = names.get(base, {})
        all_time = c.get("ath_change_percentage")
        row = {"symbol": base, "name": c.get("name", base), "rank": c.get("market_cap_rank"), "status": status,
               "price": closes[-1], "high_1y": hi1y, "prev_high": prior, "prev_high_date": k[j][0].strftime("%Y-%m-%d"),
               "base_days": (k[-1][0] - k[j][0]).days, "dist": round(dist, 2),
               "over_prev": round((closes[-1] / prior - 1) * 100, 2),
               "vol_ratio": round(vols[-1] / avgv, 2) if avgv else None, "volume": vols[-1],
               "chg24h": round((closes[-1] / closes[-2] - 1) * 100, 2),
               "chg30d": round((closes[-1] / closes[-31] - 1) * 100, 2) if len(closes) > 31 else None,
               "from_ath": all_time, "rsi": rsi(closes),
               "spark": [round(v, 8) for v in closes[-365::7]] + [round(closes[-1], 8)]}
        row["signals"] = _signals(row, closes, row["vol_ratio"], row["base_days"],
                                  sum(1 for i in range(len(k) - 20, len(k)) if closes[i] > max(highs[max(0, i - win):i])))
        if all_time is not None and all_time >= -3:
            row["signals"].insert(0, "También en máximo histórico")
        out.append(row)
    order = {"hoy": 0, "reciente": 1, "cerca": 2}
    out.sort(key=lambda r: (order[r["status"]], -(r["vol_ratio"] or 0)))
    return out


def block_derivs() -> list[dict]:
    r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"},
                      headers={"Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    meta, ctxs = r.json()
    rows = []
    for a, x in zip(meta["universe"], ctxs):
        try:
            px = float(x.get("markPx") or 0)
            oi = float(x.get("openInterest") or 0) * px
            prev = float(x.get("prevDayPx") or 0)
            if oi < 5e6:
                continue
            f = float(x.get("funding") or 0)                           # tasa por hora
            rows.append({"symbol": a["name"], "price": px, "oi": oi,
                         "volume": float(x.get("dayNtlVlm") or 0),
                         "chg24h": round((px / prev - 1) * 100, 2) if prev else None,
                         "funding_8h": round(f * 8 * 100, 4),
                         "funding_apr": round(f * 24 * 365 * 100, 1),
                         "premium": round(float(x.get("premium") or 0) * 100, 3)})
        except Exception:
            continue
    rows.sort(key=lambda r: -r["oi"])
    return rows[:30]


def block_categories() -> list[dict]:
    cats = cg("/coins/categories")
    out = []
    for c in cats:
        if not c.get("market_cap") or c["market_cap"] < 1e9:
            continue
        if re.search(r"stablecoin|exchange-based|ecosystem|portfolio|wrapped|bridged|binance|coinbase|"
                     r"alleged|fan token|made in|world liberty|grayscale|pantera|a16z|delphi|galaxy|"
                     r"multicoin|dwf|paradigm|polychain|andreessen|launchpool|launchpad|holdings", c["name"], re.I):
            continue
        out.append({"id": c.get("id"), "name": c["name"], "mcap": c["market_cap"],
                    "chg24h": c.get("market_cap_change_24h"), "volume": c.get("volume_24h"),
                    "top3": c.get("top_3_coins_id") or []})
    out.sort(key=lambda r: -r["mcap"])
    return out[:36]


MEMBERS_MAX_AGE_H = 6      # la composición de los sectores cambia poco: se refresca cada 6 h


def block_category_members(cats: list[dict], prev: dict | None, prev_when: str | None) -> dict:
    """Principales monedas de cada sector (para la ventana emergente de la web)."""
    prev = prev or {}
    fresh = False
    if prev_when:
        try:
            fresh = (NOW_DT - datetime.fromisoformat(prev_when)).total_seconds() < MEMBERS_MAX_AGE_H * 3600
        except Exception:
            fresh = False
    out = {}
    for c in cats:
        cid = c.get("id")
        if not cid:
            continue
        if fresh and cid in prev:
            out[cid] = prev[cid]
            continue
        try:
            rows = cg("/coins/markets", vs_currency="usd", category=cid, order="market_cap_desc",
                      per_page=12, page=1, price_change_percentage="24h,7d", sparkline="false")
            out[cid] = [{"symbol": (r.get("symbol") or "").upper(), "name": r.get("name"),
                         "price": r.get("current_price"), "mcap": r.get("market_cap"),
                         "chg24h": r.get("price_change_percentage_24h_in_currency"),
                         "chg7d": r.get("price_change_percentage_7d_in_currency")} for r in rows]
        except Exception as e:
            print(f"  aviso sector {cid}: {e}")
            if cid in prev:
                out[cid] = prev[cid]
    return out


# --------------------------------------------------------------------------
FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Cointelegraph ES", "https://es.cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/.rss/full/"),
]
TAGS = {
    "Bitcoin": r"\b(bitcoin|btc)\b", "Ethereum": r"\b(ethereum|ether|eth)\b", "Solana": r"\b(solana|sol)\b",
    "XRP": r"\b(xrp|ripple)\b", "ETF": r"\betfs?\b", "Regulación": r"\b(sec|regulat|regulación|ley|law|mica|cftc|congress|congreso)",
    "Stablecoins": r"\b(stablecoin|usdt|usdc|tether)", "DeFi": r"\b(defi|dex|lending|tvl)\b",
    "Hackeos": r"\b(hack|exploit|hackeo|robo|drain)", "Fed": r"\b(fed|powell|fomc|tipos|rates?)\b",
    "Memecoins": r"\b(meme|doge|dogecoin|shib|pepe|bonk)\b",
}
POS = r"(sube|suben|alza|repunta|rebota|r[eé]cord|m[aá]ximos?|gana|entradas|inflows?|rises?|rall(y|ies)|gains?|jumps?|surges?|soars?|climbs?|record high|bullish|approv|aprueba)"
NEG = r"(cae|caen|ca[ií]da|baja|desploma|retrocede|pierde|salidas|outflows?|falls?|drops?|slides?|slumps?|plunges?|tumbles?|declines?|bearish|hack|exploit|liquidat|fears?|crash)"


def tone(t: str) -> int:
    t = t.lower()
    return len(re.findall(POS, t)) - len(re.findall(NEG, t))


def update_news():
    import feedparser
    items, seen = [], set()
    for src, url in FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=25)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:
            print(f"  aviso RSS {src}: {e}")
            continue
        for en in feed.entries[:40]:
            title = re.sub(r"\s+", " ", en.get("title", "")).strip()
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            ts = en.get("published_parsed") or en.get("updated_parsed")
            pub = datetime(*ts[:6], tzinfo=timezone.utc).isoformat() if ts else NOW
            blob = (title + " " + re.sub("<[^>]+>", " ", en.get("summary", ""))[:400]).lower()
            items.append({"title": title, "link": en.get("link", ""), "source": src, "published": pub,
                          "tags": [k for k, rx in TAGS.items() if re.search(rx, blob)], "score": tone(title)})
    if not items:
        raise RuntimeError("sin titulares")
    items.sort(key=lambda x: x["published"], reverse=True)
    (DATA / "crypto_news.json").write_text(json.dumps(
        {"updated": NOW, "sample": False, "source": ", ".join(s for s, _ in FEEDS), "items": items[:150]},
        ensure_ascii=False, indent=1, default=_jsonable), encoding="utf-8")
    print("  ok  crypto_news.json")


def main():
    path = DATA / "crypto.json"
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
        if out.get("sample"):
            out = {}
    except Exception:
        out = {}
    ok = 0

    def run(key, fn, *a):
        nonlocal ok
        print(f"Actualizando {key}...")
        try:
            out[key] = fn(*a)
            ok += 1
            return out[key]
        except Exception as e:
            print(f"  ERROR {key}: {e} (se conserva el dato anterior)")
            return None

    run("global", block_global)
    run("fear_greed", block_fear_greed)
    run("stables", block_stables)
    markets = run("_markets", block_markets) or []
    out.pop("_markets", None)
    if markets:
        run("coins", block_coins, markets)
        run("ath", block_ath, markets)
        run("ath_breadth", block_ath_breadth, markets)
        run("high_1y", block_high_1y, markets)
    run("derivs", block_derivs)
    cats = run("categories", block_categories)
    if cats:
        prev_when = out.get("category_members_updated")
        before = out.get("category_members")
        members = run("category_members", block_category_members, cats, before, prev_when)
        if members is not None and members != before:
            out["category_members_updated"] = NOW
    out.update({"updated": NOW, "sample": False,
                "source": "CoinGecko, alternative.me, DefiLlama, Hyperliquid y Binance"})
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=_jsonable), encoding="utf-8")
    print("  ok  crypto.json")
    try:
        update_news()
        ok += 1
    except Exception as e:
        print(f"  ERROR titulares: {e}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
