"""
ChamosaNews - actualizador de datos
Autor: Marcos Chamosa

Descarga de fuentes gratuitas (sin API key) y guarda JSON en /data:
  - calendar.json   calendario económico con consenso y anterior (feed público de Forex Factory)
  - sentiment.json  Miedo y Codicia (CNN y cripto) + sesgo técnico por activo (Yahoo Finance)
  - news.json       titulares RSS con tono estimado por palabras clave
  - ath.json        todas las acciones de EE. UU. y grandes europeas en ruptura de máximos
                    históricos (se ejecuta aparte: python scripts/update_data.py --ath [--refresh])
  - stocks.json     acciones a vigilar: PER, analistas, momento y noticias (cada 6 h)
  - indicators.json indicadores clave (NFP, paro, IPC, PCE, PIB, ISM, tipos...) con histórico

Cada fuente es independiente: si una falla, se conserva el último JSON bueno.
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
# Funciona tanto si el script está en la raíz del repositorio como dentro de scripts/
ROOT = _HERE if (_HERE / "index.html").exists() else _HERE.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Fuentes de cotizaciones
#   Yahoo Finance: principal (sin clave).
#   Stooq: respaldo para series sueltas. Necesita STOOQ_API_KEY (gratuita, se
#          obtiene en stooq.com/q/d/?s=spy.us&get_apikey) y tiene cuota diaria.
#   Massive (antes Polygon.io): barras diarias de todo EE. UU. en una llamada.
#          Necesita MASSIVE_API_KEY (plan gratuito Basic).
# Las claves se leen de variables de entorno (Secrets de GitHub).
# --------------------------------------------------------------------------
import os
import time

STOOQ_KEY = os.environ.get("STOOQ_API_KEY", "").strip()
MASSIVE_KEY = os.environ.get("MASSIVE_API_KEY", "").strip()
SOURCES_USED: set[str] = set()

# Equivalencias Yahoo → Stooq. Si alguna no responde, se ignora sin romper nada.
STOOQ_MAP = {
    "GC=F": "gc.f", "SI=F": "si.f", "CL=F": "cl.f", "EURUSD=X": "eurusd", "GBPUSD=X": "gbpusd",
    "USDJPY=X": "usdjpy", "DX-Y.NYB": "dx.f", "^GSPC": "^spx", "^NDX": "^ndx", "^GDAXI": "^dax",
    "^IBEX": "^ibex", "^TNX": "10usy.b", "^VIX": "vi.f", "BTC-USD": "btcusd",
}


def stooq_symbol(sym: str) -> str | None:
    if sym in STOOQ_MAP:
        return STOOQ_MAP[sym]
    if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", sym):          # acción de EE. UU.
        return sym.lower().replace("-", ".") + ".us"
    if sym.endswith(".DE"):
        return sym[:-3].lower() + ".de"
    if sym.endswith(".L"):
        return sym[:-2].lower() + ".uk"
    return None


def stooq_history(sym: str, days: int = 400):
    """Serie diaria de Stooq como DataFrame (Close, High, Volume) o None."""
    import io
    import pandas as pd
    st = stooq_symbol(sym)
    if not STOOQ_KEY or not st:
        return None
    d1 = (datetime.now(timezone.utc) - pd.Timedelta(days=days)).strftime("%Y%m%d")
    url = f"https://stooq.com/q/d/l/?s={st}&i=d&d1={d1}&apikey={STOOQ_KEY}"
    try:
        r = requests.get(url, headers=UA, timeout=25)
        if r.status_code != 200 or "Exceeded" in r.text or not r.text.startswith("Date"):
            return None
        df = pd.read_csv(io.StringIO(r.text), parse_dates=["Date"], index_col="Date")
        if df.empty:
            return None
        if "Volume" not in df:
            df["Volume"] = 0
        SOURCES_USED.add("Stooq")
        return df[["Close", "High", "Volume"]].dropna(subset=["Close"])
    except Exception:
        return None


def price_histories(tickers: list[str], period: str = "1y") -> dict:
    """Cierres ajustados por ticker: Yahoo y, para los que falten, Stooq."""
    out = {}
    try:
        import yfinance as yf
        df = yf.download(tickers, period=period, interval="1d", group_by="ticker",
                         auto_adjust=True, progress=False, threads=True)
        for t in tickers:
            try:
                c = (df[t] if len(tickers) > 1 else df)["Close"].dropna()
                if len(c) >= 30:
                    out[t] = c
            except Exception:
                pass
        if out:
            SOURCES_USED.add("Yahoo Finance")
    except Exception as e:
        print(f"  aviso Yahoo: {e}")
    missing = [t for t in tickers if t not in out]
    for t in missing:
        h = stooq_history(t)
        if h is not None and len(h) >= 30:
            out[t] = h["Close"]
            print(f"  {t}: Yahoo sin datos, se usa Stooq")
    return out


def save(name: str, payload: dict) -> None:
    payload["updated"] = NOW
    payload["sample"] = False
    (DATA / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  ok  {name}")


def get_json(url: str, **kw):
    r = requests.get(url, headers=UA, timeout=25, **kw)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# 1. Calendario económico
# --------------------------------------------------------------------------
FF_FEEDS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]


def update_calendar() -> None:
    events, seen = [], set()
    for url in FF_FEEDS:
        try:
            rows = get_json(url)
        except Exception as e:  # la semana siguiente no siempre está publicada
            print(f"  aviso calendario {url}: {e}")
            continue
        for r in rows:
            key = (r.get("date"), r.get("country"), r.get("title"))
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "date": r.get("date"),
                "country": r.get("country", ""),
                "title": r.get("title", ""),
                "impact": r.get("impact", "Low"),
                "forecast": r.get("forecast", "") or "",
                "previous": r.get("previous", "") or "",
                "actual": r.get("actual", "") or "",
            })
    if not events:
        raise RuntimeError("calendario vacío")
    events.sort(key=lambda e: datetime.fromisoformat(e["date"]).astimezone(timezone.utc) if e["date"] else datetime.max.replace(tzinfo=timezone.utc))
    save("calendar.json", {"source": "Forex Factory (feed público)", "events": events})


# --------------------------------------------------------------------------
# 2. Sentimiento
# --------------------------------------------------------------------------
RATING_ES = {
    "extreme fear": "Miedo extremo", "fear": "Miedo", "neutral": "Neutral",
    "greed": "Codicia", "extreme greed": "Codicia extrema",
}

ASSETS = [  # (ticker Yahoo, nombre visible, grupo)
    ("GC=F", "Oro", "Materias primas"),
    ("SI=F", "Plata", "Materias primas"),
    ("CL=F", "Petróleo WTI", "Materias primas"),
    ("EURUSD=X", "EUR/USD", "Divisas"),
    ("GBPUSD=X", "GBP/USD", "Divisas"),
    ("USDJPY=X", "USD/JPY", "Divisas"),
    ("DX-Y.NYB", "Índice dólar", "Divisas"),
    ("^GSPC", "S&P 500", "Índices"),
    ("^NDX", "Nasdaq 100", "Índices"),
    ("^GDAXI", "DAX", "Índices"),
    ("^IBEX", "IBEX 35", "Índices"),
    ("^TNX", "Bono EE. UU. 10a", "Tipos"),
    ("^VIX", "VIX", "Volatilidad"),
    ("BTC-USD", "Bitcoin", "Cripto"),
]


def rsi(series, n: int = 14) -> float:
    delta = series.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, math.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def pct(a: float, b: float) -> float | None:
    if b in (0, None) or a is None or math.isnan(a) or math.isnan(b):
        return None
    return round((a / b - 1) * 100, 2)


def bias_score(chg5, chg20, r, above50, above200) -> int:
    """Sesgo técnico simple entre -100 y +100. Orientativo, no es una señal."""
    s = 0.0
    s += 25 if above200 else -25
    s += 20 if above50 else -20
    if r is not None and not math.isnan(r):
        s += max(-25, min(25, (r - 50) * 1.25))
    if chg5 is not None:
        s += max(-15, min(15, chg5 * 5))
    if chg20 is not None:
        s += max(-15, min(15, chg20 * 2))
    return int(round(max(-100, min(100, s))))


def update_sentiment() -> None:
    out: dict = {"source": "CNN, alternative.me, Yahoo Finance", "fear_greed": {}, "assets": []}

    try:
        j = requests.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                         headers={**UA, "Referer": "https://edition.cnn.com/"}, timeout=25).json()
        fg = j["fear_and_greed"]
        out["fear_greed"]["stocks"] = {
            "value": round(fg["score"]),
            "label": RATING_ES.get(str(fg["rating"]).lower(), fg["rating"]),
            "prev_week": round(fg.get("previous_1_week") or fg["score"]),
            "prev_month": round(fg.get("previous_1_month") or fg["score"]),
        }
    except Exception as e:
        print(f"  aviso CNN: {e}")

    try:
        j = get_json("https://api.alternative.me/fng/?limit=31")
        d = j["data"]
        out["fear_greed"]["crypto"] = {
            "value": int(d[0]["value"]),
            "label": RATING_ES.get(d[0]["value_classification"].lower(), d[0]["value_classification"]),
            "prev_week": int(d[7]["value"]) if len(d) > 7 else None,
            "prev_month": int(d[30]["value"]) if len(d) > 30 else None,
        }
    except Exception as e:
        print(f"  aviso cripto F&G: {e}")

    try:
        closes = price_histories([a[0] for a in ASSETS])
        for tk, name, group in ASSETS:
            try:
                c = closes.get(tk)
                if c is None or len(c) < 30:
                    continue
                last = float(c.iloc[-1])
                sma50 = float(c.tail(50).mean())
                sma200 = float(c.tail(200).mean())
                r = rsi(c)
                chg1 = pct(last, float(c.iloc[-2]))
                chg5 = pct(last, float(c.iloc[-6]))
                chg20 = pct(last, float(c.iloc[-21]))
                out["assets"].append({
                    "symbol": tk, "name": name, "group": group,
                    "price": round(last, 4 if last < 10 else 2),
                    "chg1d": chg1, "chg5d": chg5, "chg1m": chg20,
                    "rsi": round(r, 1),
                    "above_sma50": last > sma50, "above_sma200": last > sma200,
                    "bias": bias_score(chg5, chg20, r, last > sma50, last > sma200),
                })
            except Exception as e:
                print(f"  aviso {tk}: {e}")
    except Exception as e:
        print(f"  aviso precios: {e}")

    out["source"] = "CNN, alternative.me, " + ", ".join(sorted(SOURCES_USED) or ["Yahoo Finance"])
    if not out["fear_greed"] and not out["assets"]:
        raise RuntimeError("sin datos de sentimiento")
    save("sentiment.json", out)


# --------------------------------------------------------------------------
# 3. Noticias
# --------------------------------------------------------------------------
FEEDS = [
    ("FXStreet", "https://www.fxstreet.es/rss/news"),
    ("Investing.com", "https://es.investing.com/rss/news_14.rss"),
    ("Investing.com", "https://es.investing.com/rss/news_1.rss"),
    ("ForexLive", "https://www.forexlive.com/feed/news"),
    ("Expansión", "https://e00-expansion.uecdn.es/rss/mercados.xml"),
]

ASSET_TAGS = {
    "Oro": r"\b(oro|gold|xau)",
    "Dólar": r"\b(d[oó]lar|dollar|usd|dxy|greenback)",
    "Euro": r"\b(euro|eur/|eurusd|eur usd)",
    "Libra": r"\b(libra|pound|sterling|gbp)",
    "Yen": r"\b(yen|jpy)",
    "Fed": r"\b(fed|fomc|powell|reserva federal)",
    "BCE": r"\b(bce|ecb|lagarde)",
    "Petróleo": r"\b(petr[oó]leo|crude|oil|brent|wti|opep|opec)",
    "Bolsa": r"\b(s&p|nasdaq|dow|ibex|dax|stocks|acciones|wall street|bolsa)",
    "Cripto": r"\b(bitcoin|btc|ethereum|cripto|crypto)",
    "Bonos": r"\b(bono|bonds?|treasur|yield|rentabilidad)",
}

POS = r"(sube|suben|subida|alza|al alza|repunta|rebota|r[eé]cord|m[aá]ximos?|gana|ganancias|optimis|fuerte|supera|mejor de lo esperado|rises?|rall(y|ies)|gains?|jumps?|surges?|soars?|climbs?|beats?|strong|higher|bullish|record high)"
NEG = r"(cae|caen|ca[ií]da|baja|a la baja|desploma|retrocede|pierde|p[eé]rdidas|m[ií]nimos?|temor|miedo|recesi[oó]n|peor de lo esperado|d[eé]bil|falls?|drops?|slides?|slumps?|plunges?|tumbles?|declines?|loses?|weak|lower|bearish|fears?|misses?|recession)"


def tone(text: str) -> int:
    t = text.lower()
    return len(re.findall(POS, t)) - len(re.findall(NEG, t))


def update_news() -> None:
    import feedparser
    items, seen = [], set()
    for source, url in FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=25)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:
            print(f"  aviso RSS {source}: {e}")
            continue
        for en in feed.entries[:40]:
            title = re.sub(r"\s+", " ", en.get("title", "")).strip()
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            ts = en.get("published_parsed") or en.get("updated_parsed")
            published = (datetime(*ts[:6], tzinfo=timezone.utc).isoformat()
                         if ts else NOW)
            summary = re.sub("<[^>]+>", " ", en.get("summary", ""))[:400]
            blob = f"{title} {summary}".lower()
            items.append({
                "title": title,
                "link": en.get("link", ""),
                "source": source,
                "published": published,
                "tags": [k for k, rx in ASSET_TAGS.items() if re.search(rx, blob)],
                "score": tone(title),
            })
    if not items:
        raise RuntimeError("sin noticias")
    items.sort(key=lambda x: x["published"], reverse=True)
    save("news.json", {"source": ", ".join(sorted({s for s, _ in FEEDS})), "items": items[:150]})


# --------------------------------------------------------------------------
# 4. Indicadores clave (series históricas oficiales)
#    FRED (Reserva Federal de St. Louis) y portal de datos del BCE, sin clave.
#    "cal" es la expresión que enlaza cada indicador con su evento del calendario.
# --------------------------------------------------------------------------
INDICATORS = [
    # EE. UU.: empleo
    dict(id="nfp", region="EE. UU.", name="Nóminas no agrícolas (NFP)", src="fred", key="PAYEMS",
         tf="diff", unit="K", dec=0, cal=r"^Non-Farm Employment Change", cur="USD"),
    dict(id="unrate", region="EE. UU.", name="Tasa de paro", src="fred", key="UNRATE",
         tf="level", unit="%", dec=1, cal=r"^Unemployment Rate", cur="USD"),
    dict(id="ahe", region="EE. UU.", name="Salario medio por hora (mensual)", src="fred", key="CES0500000003",
         tf="mom", unit="%", dec=1, cal=r"^Average Hourly Earnings m/m", cur="USD"),
    dict(id="claims", region="EE. UU.", name="Peticiones semanales de paro", src="fred", key="ICSA",
         tf="level", unit="K", scale=0.001, dec=0, freq="W", cal=r"^Unemployment Claims", cur="USD"),
    # EE. UU.: inflación
    dict(id="cpi", region="EE. UU.", name="IPC (interanual)", src="fred", key="CPIAUCSL",
         tf="yoy", unit="%", dec=1, cal=r"^CPI y/y", cur="USD"),
    dict(id="core_cpi", region="EE. UU.", name="IPC subyacente (interanual)", src="fred", key="CPILFESL",
         tf="yoy", unit="%", dec=1, cal=r"^Core CPI", cur="USD"),
    dict(id="core_pce", region="EE. UU.", name="PCE subyacente (mensual)", src="fred", key="PCEPILFE",
         tf="mom", unit="%", dec=1, cal=r"^Core PCE Price Index m/m", cur="USD"),
    # EE. UU.: actividad y confianza
    dict(id="gdp", region="EE. UU.", name="PIB (trimestral anualizado)", src="fred", key="A191RL1Q225SBEA",
         tf="level", unit="%", dec=1, freq="Q", cal=r"GDP q/q", cur="USD"),
    dict(id="ism_m", region="EE. UU.", name="PMI manufacturero ISM", src="cal",
         unit="", dec=1, cal=r"^ISM Manufacturing PMI", cur="USD"),
    dict(id="ism_s", region="EE. UU.", name="PMI de servicios ISM", src="cal",
         unit="", dec=1, cal=r"^ISM Services PMI", cur="USD"),
    dict(id="umich", region="EE. UU.", name="Confianza del consumidor (Michigan)", src="fred", key="UMCSENT",
         tf="level", unit="", dec=1, cal=r"UoM Consumer Sentiment", cur="USD"),
    dict(id="fed", region="EE. UU.", name="Tipos de la Fed (techo)", src="fred", key="DFEDTARU",
         tf="level", unit="%", dec=2, freq="D", cal=r"^Federal Funds Rate", cur="USD"),
    # Eurozona
    dict(id="ez_hicp", region="Eurozona", name="IPC armonizado (interanual)", src="ecb",
         key=["ICP/M.U2.N.000000.4.ANR"], tf="level", unit="%", dec=1, cal=r"^CPI Flash Estimate y/y", cur="EUR"),
    dict(id="ez_core", region="Eurozona", name="IPC subyacente (interanual)", src="ecb",
         key=["ICP/M.U2.N.XEF000.4.ANR"], tf="level", unit="%", dec=1, cal=r"^Core CPI Flash Estimate y/y", cur="EUR"),
    dict(id="ez_unemp", region="Eurozona", name="Tasa de paro", src="ecb",
         key=["LFSI/M.I9.S.UNEHRT.TOTAL0.15_74.T", "LFSI/M.I8.S.UNEHRT.TOTAL0.15_74.T"],
         tf="level", unit="%", dec=1, cal=r"^Unemployment Rate", cur="EUR"),
    dict(id="ez_gdp", region="Eurozona", name="PIB (trimestral)", src="cal",
         unit="%", dec=1, cal=r"GDP q/q", cur="EUR"),
    dict(id="ez_pmi", region="Eurozona", name="PMI compuesto", src="cal",
         unit="", dec=1, cal=r"^(Flash |Final )?Composite PMI|^Composite PMI", cur="EUR"),
    dict(id="ecb_dfr", region="Eurozona", name="Tipo de depósito del BCE", src="ecb",
         key=["FM/D.U2.EUR.4F.KR.DFR.LEV"], tf="level", unit="%", dec=2, freq="D",
         cal=r"^(Main Refinancing Rate|Deposit Facility Rate)", cur="EUR"),
    # España
    dict(id="es_hicp", region="España", name="IPC armonizado (interanual)", src="ecb",
         key=["ICP/M.ES.N.000000.4.ANR"], tf="level", unit="%", dec=1, cal=r"^Spanish.*CPI", cur="EUR"),
    dict(id="es_unemp", region="España", name="Variación del paro registrado", src="cal",
         unit="K", dec=1, cal=r"^Spanish Unemployment Change", cur="EUR"),
]


def _series_from_csv(text: str):
    import io
    import pandas as pd
    df = pd.read_csv(io.StringIO(text))
    cols = {c.lower(): c for c in df.columns}
    dcol = cols.get("time_period") or cols.get("observation_date") or cols.get("date") or df.columns[0]
    vcol = cols.get("obs_value") or [c for c in df.columns if c != dcol][-1]
    s = pd.Series(pd.to_numeric(df[vcol], errors="coerce").values,
                  index=pd.to_datetime(df[dcol].astype(str), errors="coerce"))
    return s.dropna().sort_index()


def fetch_series(ind: dict):
    if ind["src"] == "fred":
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={ind['key']}&cosd=2018-01-01"
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        return _series_from_csv(r.text)
    last = None
    for k in ind["key"]:
        flow, key = k.split("/", 1)
        url = (f"https://data-api.ecb.europa.eu/service/data/{flow}/{key}"
               f"?format=csvdata&startPeriod=2018-01")
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            s = _series_from_csv(r.text)
            if len(s):
                return s
        except Exception as e:
            last = e
    raise RuntimeError(f"sin datos BCE ({last})")


def transform(s, ind: dict):
    freq = ind.get("freq", "M")
    if freq == "D":  # tipos diarios → último valor de cada mes
        s = s.resample("MS").last().dropna()
        freq = "M"
    s = s * ind.get("scale", 1)
    tf = ind.get("tf", "level")
    if tf == "diff":
        s = s.diff()
    elif tf == "mom":
        s = s.pct_change() * 100
    elif tf == "yoy":
        s = s.pct_change(52 if freq == "W" else 4 if freq == "Q" else 12) * 100
    return s.dropna(), freq


def update_indicators() -> None:
    out = []
    for ind in INDICATORS:
        row = {k: ind[k] for k in ("id", "region", "name", "unit", "dec", "cal", "cur", "src")}
        if ind["src"] != "cal":
            try:
                s, freq = transform(fetch_series(ind), ind)
                tail = s.tail(24)
                row.update({
                    "freq": freq,
                    "last": {"period": tail.index[-1].strftime("%Y-%m-%d"), "value": round(float(tail.iloc[-1]), 3)},
                    "prev": round(float(tail.iloc[-2]), 3) if len(tail) > 1 else None,
                    "history": [[d.strftime("%Y-%m-%d"), round(float(v), 3)] for d, v in tail.items()],
                })
            except Exception as e:
                print(f"  aviso indicador {ind['id']}: {e}")
                row["error"] = True
        out.append(row)
    if all(r.get("error") for r in out if r["src"] != "cal"):
        raise RuntimeError("no se pudo descargar ningún indicador")
    save("indicators.json", {"source": "FRED (Fed de St. Louis), BCE y calendario", "indicators": out})


# --------------------------------------------------------------------------
# 5. Acciones a vigilar (filtro por valoración, analistas, momento y noticias)
#    Se recalcula como mucho cada 6 h para no saturar Yahoo Finance.
#    Es un filtro para decidir qué mirar, no una recomendación de compra.
# --------------------------------------------------------------------------
STOCKS = {
    "España": ["SAN.MC", "BBVA.MC", "ITX.MC", "IBE.MC", "TEF.MC", "REP.MC", "CABK.MC", "AMS.MC",
               "FER.MC", "ACS.MC", "AENA.MC", "IAG.MC", "ELE.MC", "NTGY.MC", "RED.MC", "ENG.MC",
               "GRF.MC", "MAP.MC", "SAB.MC", "BKT.MC", "MTS.MC", "ACX.MC", "CLNX.MC", "IDR.MC",
               "MRL.MC", "LOG.MC", "SCYR.MC", "ANA.MC", "ANE.MC", "COL.MC", "FDR.MC", "PUIG.MC",
               "ROVI.MC", "SLR.MC", "UNI.MC"],
    "EE. UU.": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V",
                "UNH", "LLY", "XOM", "CVX", "JNJ", "PG", "KO", "PFE", "INTC", "AMD", "NFLX",
                "DIS", "BA", "WMT", "BRK-B"],
}
STOCKS_MAX_AGE_H = 6


def _num(x):
    try:
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def _news_items(tk) -> list[dict]:
    out = []
    try:
        raw = tk.news or []
    except Exception:
        return out
    for n in raw[:20]:
        c = n.get("content", n)  # yfinance cambió el formato: admite ambos
        title = c.get("title") or ""
        if not title:
            continue
        ts = c.get("pubDate") or c.get("displayTime") or n.get("providerPublishTime")
        if isinstance(ts, (int, float)):
            pub = datetime.fromtimestamp(ts, timezone.utc)
        else:
            try:
                pub = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                pub = datetime.now(timezone.utc)
        link = (c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else n.get("link")
        prov = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else n.get("publisher")
        out.append({"title": title, "link": link or "", "source": prov or "", "published": pub.isoformat(),
                    "score": tone(title)})
    return out


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def score_rows(rows: list[dict]) -> None:
    """Puntuación 0-100: valoración 30, analistas 25, calidad 15, momento 15, noticias 15."""
    # PER de referencia: mediana del sector dentro del propio universo
    from statistics import median
    by_sector: dict[str, list[float]] = {}
    for r in rows:
        v = r["fpe"] or r["pe"]
        if v and 0 < v < 200:
            by_sector.setdefault(r["sector"], []).append(v)
    all_pe = [v for vs in by_sector.values() for v in vs]
    med_all = median(all_pe) if all_pe else 15

    for r in rows:
        why = []
        pe = r["fpe"] or r["pe"]
        ref = median(by_sector[r["sector"]]) if len(by_sector.get(r["sector"], [])) >= 3 else med_all
        r["pe_ref"] = round(ref, 1)
        # Valoración (0-30)
        v = 15.0
        if pe and pe > 0:
            rel = pe / ref
            v = _clip(30 - (rel - 0.5) * 20, 0, 30)
            if rel <= 0.8:
                why.append(f"PER {pe:.1f} frente a {ref:.1f} del sector")
        elif pe is not None:
            v = 3
        if r["peg"] and 0 < r["peg"] < 1:
            v = min(30, v + 5); why.append(f"PEG {r['peg']:.2f}")
        # Analistas (0-25)
        a = 10.0
        if r["upside"] is not None and r["analysts"] >= 3:
            a = _clip(5 + r["upside"] * 0.5, 0, 20)
            if r["upside"] >= 15:
                why.append(f"Precio objetivo +{r['upside']:.0f} %")
        if r["rec"] and r["analysts"] >= 3:
            a += _clip((3 - r["rec"]) * 3.5, -5, 7)
            if r["rec"] <= 2:
                why.append("Consenso de compra")
        a = _clip(a, 0, 25)
        # Calidad (0-15)
        q = 7.5
        if r["roe"] is not None:
            q = _clip(r["roe"] * 50, 0, 10)
        if r["eps_growth"] is not None:
            q += _clip(r["eps_growth"] * 20, -3, 5)
            if r["eps_growth"] >= 0.15:
                why.append(f"Beneficio +{r['eps_growth']*100:.0f} %")
        q = _clip(q, 0, 15)
        # Momento (0-15)
        m = (6 if r["above_sma200"] else 1) + _clip((r["chg3m"] or 0) / 3, -3, 6)
        if r["rsi"] >= 75:
            m -= 3
        elif r["rsi"] <= 30:
            why.append(f"RSI {r['rsi']:.0f} en sobreventa")
        m = _clip(m, 0, 15)
        # Noticias (0-15)
        nt = _clip(7.5 + r["news_tone"] * 1.5 + min(r["news7d"], 10) * 0.2, 0, 15)
        if r["news_tone"] >= 2:
            why.append("Noticias positivas")
        elif r["news_tone"] <= -2:
            why.append("Noticias negativas")
        if r["earnings"]:
            try:
                days = (datetime.fromisoformat(r["earnings"]).date() - datetime.now(timezone.utc).date()).days
                if 0 <= days <= 14:
                    why.append("Resultados próximos")
            except Exception:
                pass
        r["sub"] = {"valor": round(v, 1), "analistas": round(a, 1), "calidad": round(q, 1),
                    "momento": round(m, 1), "noticias": round(nt, 1)}
        r["score"] = round(v + a + q + m + nt)
        r["why"] = why


def update_stocks() -> None:
    f = DATA / "stocks.json"
    if f.exists():
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
            age = datetime.now(timezone.utc) - datetime.fromisoformat(old["updated"])
            if not old.get("sample") and age.total_seconds() < STOCKS_MAX_AGE_H * 3600:
                print(f"  acciones al día (hace {age.total_seconds() / 3600:.1f} h), se omite")
                return
        except Exception:
            pass

    import yfinance as yf
    rows = []
    week_ago = datetime.now(timezone.utc).timestamp() - 7 * 86400
    for market, tickers in STOCKS.items():
        closes = price_histories(tickers)
        for sym in tickers:
            try:
                tk = yf.Ticker(sym)
                try:
                    info = tk.info or {}
                except Exception:                      # sin ratios: se muestra solo el precio
                    info = {}
                c = closes.get(sym)
                if c is None or len(c) < 60:
                    continue
                price = float(c.iloc[-1])
                news = _news_items(tk)
                recent = [n for n in news if datetime.fromisoformat(n["published"]).timestamp() >= week_ago]
                earn = None
                try:
                    cal = tk.calendar or {}
                    ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
                    if ed:
                        ed = ed[0] if isinstance(ed, (list, tuple)) else ed
                        earn = str(ed)[:10]
                except Exception:
                    pass
                target = _num(info.get("targetMeanPrice"))
                rows.append({
                    "symbol": sym, "market": market,
                    "name": info.get("shortName") or info.get("longName") or sym,
                    "sector": info.get("sector") or "",
                    "currency": info.get("currency") or "",
                    "price": round(price, 2),
                    "chg1m": pct(price, float(c.iloc[-22])),
                    "chg3m": pct(price, float(c.iloc[-64])) if len(c) > 64 else None,
                    "from_high": pct(price, float(c.max())),
                    "rsi": round(rsi(c), 1),
                    "above_sma200": price > float(c.tail(200).mean()),
                    "pe": _num(info.get("trailingPE")),
                    "fpe": _num(info.get("forwardPE")),
                    "peg": _num(info.get("trailingPegRatio") or info.get("pegRatio")),
                    "pb": _num(info.get("priceToBook")),
                    "div": _num(info.get("dividendYield")),
                    "roe": _num(info.get("returnOnEquity")),
                    "eps_growth": _num(info.get("earningsGrowth")),
                    "rev_growth": _num(info.get("revenueGrowth")),
                    "target": target,
                    "upside": pct(target, price) if target else None,
                    "rec": _num(info.get("recommendationMean")),
                    "analysts": info.get("numberOfAnalystOpinions") or 0,
                    "earnings": earn,
                    "news7d": len(recent),
                    "news_tone": sum(n["score"] for n in recent),
                    "news": news[:6],
                })
            except Exception as e:
                print(f"  aviso {sym}: {e}")

    if not rows:
        raise RuntimeError("sin datos de acciones")

    score_rows(rows)
    rows.sort(key=lambda r: r["score"], reverse=True)
    save("stocks.json", {"source": "Yahoo Finance (cotización, ratios, analistas y noticias)", "stocks": rows})


# --------------------------------------------------------------------------
# 6. Rupturas de máximos históricos (EE. UU. y Europa)
#    Usa precios de cierre ajustados por splits (no por dividendos).
# --------------------------------------------------------------------------
# Universo:
#  - EE. UU.: todas las acciones de NYSE, NASDAQ y NYSE American según el directorio
#    público de Nasdaq Trader, sin ETF, warrants, unidades, derechos ni preferentes,
#    y con un filtro mínimo de precio y liquidez para dejar fuera los chicharros.
#  - Europa: la lista EUROPE de abajo (grandes valores; puedes añadir los que quieras).
# Para no descargar décadas de historial en cada pasada se guarda en
# data/ath_cache.json el máximo histórico anterior a una fecha de corte. Ese
# historial completo se recalcula una vez por semana (--refresh) y en las pasadas
# normales solo se descarga desde la fecha de corte.
EUROPE = {
    "ASML.AS": "ASML", "SAP.DE": "SAP", "MC.PA": "LVMH", "OR.PA": "L'Oréal", "RMS.PA": "Hermès",
    "TTE.PA": "TotalEnergies", "SAN.PA": "Sanofi", "AIR.PA": "Airbus", "SU.PA": "Schneider Electric",
    "AI.PA": "Air Liquide", "BNP.PA": "BNP Paribas", "EL.PA": "EssilorLuxottica", "SAF.PA": "Safran",
    "CS.PA": "AXA", "DG.PA": "Vinci", "KER.PA": "Kering", "RI.PA": "Pernod Ricard", "BN.PA": "Danone",
    "GLE.PA": "Société Générale", "HO.PA": "Thales", "SIE.DE": "Siemens", "ALV.DE": "Allianz",
    "DTE.DE": "Deutsche Telekom", "MUV2.DE": "Munich Re", "BAS.DE": "BASF", "BAYN.DE": "Bayer",
    "BMW.DE": "BMW", "MBG.DE": "Mercedes-Benz", "VOW3.DE": "Volkswagen", "ADS.DE": "Adidas",
    "IFX.DE": "Infineon", "DBK.DE": "Deutsche Bank", "RHM.DE": "Rheinmetall", "DB1.DE": "Deutsche Börse",
    "ENR.DE": "Siemens Energy", "CBK.DE": "Commerzbank", "HNR1.DE": "Hannover Re", "MTX.DE": "MTU Aero",
    "ISP.MI": "Intesa Sanpaolo", "UCG.MI": "UniCredit", "ENEL.MI": "Enel", "ENI.MI": "Eni",
    "RACE.MI": "Ferrari", "LDO.MI": "Leonardo", "INGA.AS": "ING", "PRX.AS": "Prosus",
    "AD.AS": "Ahold Delhaize", "PHIA.AS": "Philips", "ADYEN.AS": "Adyen", "ABI.BR": "AB InBev",
    "NOKIA.HE": "Nokia", "NDA-FI.HE": "Nordea", "ITX.MC": "Inditex", "IBE.MC": "Iberdrola",
    "SAN.MC": "Banco Santander", "BBVA.MC": "BBVA", "CABK.MC": "CaixaBank", "SAB.MC": "Banco Sabadell",
    "IDR.MC": "Indra", "AENA.MC": "Aena", "FER.MC": "Ferrovial", "ACS.MC": "ACS",
    "NESN.SW": "Nestlé", "NOVN.SW": "Novartis", "ROG.SW": "Roche", "UBSG.SW": "UBS",
    "ZURN.SW": "Zurich Insurance", "ABBN.SW": "ABB", "AZN.L": "AstraZeneca", "SHEL.L": "Shell",
    "HSBA.L": "HSBC", "ULVR.L": "Unilever", "BP.L": "BP", "RIO.L": "Rio Tinto", "GSK.L": "GSK",
    "BATS.L": "British American Tobacco", "REL.L": "RELX", "LSEG.L": "London Stock Exchange",
    "RR.L": "Rolls-Royce", "BA.L": "BAE Systems", "BARC.L": "Barclays", "NOVO-B.CO": "Novo Nordisk",
}

US_MIN_PRICE = 5.0           # dólares
US_MIN_DOLLAR_VOL = 2e6      # negociado medio diario mínimo en dólares (50 sesiones)
CHUNK = 150
SUFFIX = {".AS": ("Países Bajos", "EUR"), ".DE": ("Alemania", "EUR"), ".PA": ("Francia", "EUR"),
          ".MI": ("Italia", "EUR"), ".BR": ("Bélgica", "EUR"), ".HE": ("Finlandia", "EUR"),
          ".MC": ("España", "EUR"), ".SW": ("Suiza", "CHF"), ".L": ("Reino Unido", "GBp"),
          ".CO": ("Dinamarca", "DKK")}

NASDAQ_DIR = ["https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
              "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"]
EXCH = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe", "V": "IEX", "Q": "NASDAQ"}
JUNK = re.compile(r"\b(warrants?|units?|rights?|preferred|depositary shares|notes due|debentures|"
                  r"subordinated|trust preferred|fixed[- ]to[- ]floating|%)\b", re.I)


def us_universe() -> dict[str, dict]:
    out = {}
    for url in NASDAQ_DIR:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        lines = [l.split("|") for l in r.text.strip().splitlines()]
        head, rows = lines[0], lines[1:]
        ix = {h: i for i, h in enumerate(head)}
        sym_col = "Symbol" if "Symbol" in ix else "ACT Symbol"
        for row in rows:
            if len(row) < len(head) or row[0].startswith("File Creation"):
                continue
            sym, name = row[ix[sym_col]], row[ix["Security Name"]]
            if row[ix["ETF"]] == "Y" or row[ix["Test Issue"]] == "Y":
                continue
            if "$" in sym or JUNK.search(name):
                continue
            if "." in sym and not re.fullmatch(r"[A-Z]+\.[A-B]", sym):   # solo clases A/B
                continue
            exch = "NASDAQ" if "nasdaqlisted" in url else EXCH.get(row[ix["Exchange"]], row[ix["Exchange"]])
            clean = re.sub(r"\s+-\s+.*$", "", name)                        # quita "- Common Stock"
            clean = re.sub(r"\s+(Common Stock|Class [A-C] Common Stock|Ordinary Shares).*$", "", clean)
            out[sym.replace(".", "-")] = {"name": clean.strip() or sym, "exchange": exch}
    if len(out) < 1000:
        raise RuntimeError(f"directorio de Nasdaq incompleto ({len(out)} valores)")
    return out


def universe() -> dict[str, dict]:
    u = {}
    try:
        for sym, meta in us_universe().items():
            u[sym] = {**meta, "region": "EE. UU."}
    except Exception as e:
        print(f"  aviso directorio EE. UU.: {e}")
    for sym, name in EUROPE.items():
        u[sym] = {"name": name, "exchange": "", "region": "Europa"}
    return u


def _download(tickers: list[str], **kw):
    """Descarga por lotes y devuelve {ticker: DataFrame}."""
    import time
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        try:
            df = yf.download(chunk, interval="1d", group_by="ticker", auto_adjust=False,
                             actions=True, progress=False, threads=True, **kw)
        except Exception as e:
            print(f"  aviso lote {i}: {e}")
            continue
        for t in chunk:
            try:
                d = df[t] if len(chunk) > 1 else df
                d = d.dropna(subset=["Close"])
                if len(d):
                    out[t] = d
            except Exception:
                pass
        print(f"  {min(i + CHUNK, len(tickers))}/{len(tickers)}")
        time.sleep(1.5)
    return out


def refresh_ath_cache(u: dict) -> dict:
    """Máximo de toda la historia anterior a la fecha de corte (hoy - 365 días)."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).date()
    data = _download(list(u), period="max")
    SOURCES_USED.add("Yahoo Finance")
    ath = {}
    for t, d in data.items():
        old = d[d.index.date < cutoff]
        if len(old):
            hi = old["High"].fillna(old["Close"])
            ath[t] = [round(float(hi.max()), 4), hi.idxmax().strftime("%Y-%m-%d")]
    cache = {"cutoff": cutoff.isoformat(), "built": NOW, "ath": ath}
    (DATA / "ath_cache.json").write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
    print(f"  caché de máximos: {len(ath)} valores, corte {cutoff}")
    return cache


def ath_scan(sym: str, meta: dict, d, pre=None) -> dict | None:
    """d: datos diarios desde la fecha de corte. pre: [máximo previo, fecha] de la caché."""
    import pandas as pd
    d = d.dropna(subset=["Close"])
    pre_ath, pre_date = (pre or [0.0, None])
    if "Stock Splits" in d:                       # split posterior al corte: ajusta el máximo cacheado
        f = d["Stock Splits"].replace(0, 1).fillna(1).prod()
        if f and f != 1:
            pre_ath = pre_ath / f
    if len(d) < 60 or (not pre_ath and len(d) < 120):
        return None
    hi = d["High"].fillna(d["Close"])
    close = d["Close"]
    vol = d["Volume"].fillna(0)
    c0 = float(close.iloc[-1])
    adv = float((close * vol).tail(50).mean())
    region = meta["region"]
    if region == "EE. UU." and (c0 < US_MIN_PRICE or adv < US_MIN_DOLLAR_VOL):
        return None                                # fuera del filtro de liquidez: no cuenta
    prevmax = hi.cummax().shift(1).fillna(0).clip(lower=pre_ath)
    newhigh = close > prevmax
    win_max = float(hi.max())
    ath = max(pre_ath, win_max)
    dist = (c0 / ath - 1) * 100
    last10 = newhigh.iloc[-10:]
    if newhigh.iloc[-1]:
        status = "hoy"
    elif last10.any() and dist >= -5:
        status = "reciente"
    elif dist >= -3:
        status = "cerca"
    else:
        return {"_far": True, "region": region, "dist": dist}

    def prev_high(upto: int):
        h = hi.iloc[:upto]
        wm = float(h.max()) if len(h) else 0.0
        if pre_ath >= wm and pre_date:
            return pre_ath, pd.Timestamp(pre_date)
        return wm, h.idxmax()

    if status in ("hoy", "reciente"):
        i = len(d) - 10 + int(max(j for j, v in enumerate(last10) if v))
        prev_ath, prev_idx = prev_high(i)
        base_days = (d.index[i] - prev_idx).days
    else:
        prev_ath, prev_idx = prev_high(len(d))
        prev_ath = ath
        base_days = (d.index[-1] - prev_idx).days

    avg50 = float(vol.iloc[-51:-1].mean()) if len(vol) > 51 else 0
    vol_ratio = round(float(vol.iloc[-1]) / avg50, 2) if avg50 > 0 else None
    ext50 = (c0 / float(close.tail(50).mean()) - 1) * 100
    nh20 = int(newhigh.iloc[-20:].sum())
    country, cur = ("EE. UU.", "USD")
    for suf, val in SUFFIX.items():
        if sym.endswith(suf):
            country, cur = val
    year = close.tail(252)
    step = max(1, len(year) // 50)
    sig = []
    if vol_ratio and vol_ratio >= 1.5:
        sig.append(f"Volumen x{vol_ratio:.1f}".replace(".", ","))
    if base_days >= 365:
        sig.append(f"Base de {base_days / 365:.1f} años".replace(".", ","))
    elif base_days >= 90:
        sig.append(f"Base de {base_days // 30} meses")
    if nh20 >= 5:
        sig.append(f"{nh20} máximos en 20 sesiones")
    if ext50 >= 15:
        sig.append(f"Extendida +{ext50:.0f} %")
    if status != "cerca" and vol_ratio is not None and vol_ratio < 0.8:
        sig.append("Ruptura con poco volumen")
    return {
        "symbol": sym, "name": meta["name"], "exchange": meta.get("exchange", ""), "region": region,
        "country": country, "currency": cur, "status": status,
        "price": round(c0, 2), "ath": round(ath, 2), "prev_ath": round(prev_ath, 2),
        "prev_ath_date": pd.Timestamp(prev_idx).strftime("%Y-%m-%d"),
        "dist": round(dist, 2), "over_prev": round((c0 / prev_ath - 1) * 100, 2) if prev_ath else 0,
        "base_days": int(base_days),
        "chg1d": pct(c0, float(close.iloc[-2])), "chg1m": pct(c0, float(close.iloc[-22])) if len(close) > 22 else None,
        "vol_ratio": vol_ratio, "adv": round(adv), "ext50": round(ext50, 1), "rsi": round(rsi(close), 1),
        "nh20": nh20, "spark": [round(float(v), 2) for v in year.iloc[::step]] + [round(c0, 2)],
        "signals": sig,
    }


STORE = ROOT / "data_store" / "us_bars.pkl.gz"   # no se sube a git: va en la caché de Actions
MASSIVE_HOSTS = ["https://api.massive.com", "https://api.polygon.io"]
MASSIVE_MAX_CALLS = 25                               # 5 por minuto en el plan gratuito


def massive_get(path: str, params: dict | None = None) -> dict:
    last = None
    for host in MASSIVE_HOSTS:
        try:
            r = requests.get(host + path, params=params or {}, timeout=60,
                             headers={**UA, "Authorization": f"Bearer {MASSIVE_KEY}"})
            if r.status_code == 429:                 # límite por minuto: espera y reintenta una vez
                time.sleep(65)
                r = requests.get(host + path, params=params or {}, timeout=60,
                                 headers={**UA, "Authorization": f"Bearer {MASSIVE_KEY}"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise RuntimeError(f"Massive: {last}")


def store_from_frames(frames: dict, cutoff: str) -> dict:
    import pandas as pd
    close = pd.DataFrame({t: d["Close"] for t, d in frames.items()})
    high = pd.DataFrame({t: d["High"].fillna(d["Close"]) for t, d in frames.items()})
    vol = pd.DataFrame({t: d["Volume"] for t, d in frames.items()})
    splits = []
    for t, d in frames.items():
        if "Stock Splits" in d:
            for dt, v in d["Stock Splits"].items():
                if v and v != 1:
                    splits.append([t, dt.strftime("%Y-%m-%d"), float(v)])
    return {"cutoff": cutoff, "close": close, "high": high, "vol": vol, "splits": splits}


def store_build_yahoo(tickers: list[str], cutoff: str) -> dict:
    print(f"  construyendo almacén de EE. UU. con Yahoo desde {cutoff}...")
    frames = _download(tickers, start=cutoff)
    SOURCES_USED.add("Yahoo Finance")
    return store_from_frames(frames, cutoff)


def store_update_massive(store: dict, universe_us: set[str]) -> int:
    """Añade los días que faltan con el endpoint de barras agrupadas. Devuelve días añadidos."""
    import pandas as pd
    last = store["close"].index.max()
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    days = [d for d in pd.bdate_range(last + pd.Timedelta(days=1), today)]
    if not days:
        return 0
    if len(days) > MASSIVE_MAX_CALLS:
        raise RuntimeError(f"faltan {len(days)} días, se reconstruye con Yahoo")
    added = 0
    for i, day in enumerate(days):
        j = massive_get(f"/v2/aggs/grouped/locale/us/market/stocks/{day:%Y-%m-%d}", {"adjusted": "true"})
        res = j.get("results") or []
        if res:
            rows = {x["T"].replace(".", "-"): x for x in res if x["T"].replace(".", "-") in universe_us}
            for key, fld in (("close", "c"), ("high", "h"), ("vol", "v")):
                row = pd.Series({t: x.get(fld) for t, x in rows.items()}, name=day, dtype="float64")
                store[key] = pd.concat([store[key], row.to_frame().T])
            added += 1
            print(f"  Massive {day:%Y-%m-%d}: {len(rows)} valores")
        else:
            print(f"  Massive {day:%Y-%m-%d}: sin datos (festivo o aún no publicado)")
        if i < len(days) - 1:
            time.sleep(13)
    # splits ejecutados después del último día que había: se ajusta todo lo anterior a la fecha
    try:
        time.sleep(13)
        sp = massive_get("/v3/reference/splits", {"execution_date.gt": last.strftime("%Y-%m-%d"), "limit": 1000})
        done = {(t, d) for t, d, _ in store["splits"]}
        for x in sp.get("results", []):
            t = x["ticker"].replace(".", "-")
            ex = pd.Timestamp(x["execution_date"])
            ratio = float(x["split_to"]) / float(x["split_from"])
            if t not in store["close"] or ratio <= 0 or ratio == 1 or (t, x["execution_date"]) in done or ex <= last:
                continue
            mask = store["close"].index < ex
            store["close"].loc[mask, t] /= ratio
            store["high"].loc[mask, t] /= ratio
            store["vol"].loc[mask, t] *= ratio
            store["splits"].append([t, x["execution_date"], ratio])
            print(f"  split {t} {x['split_from']}:{x['split_to']} el {x['execution_date']}")
    except Exception as e:
        print(f"  aviso splits Massive: {e}")
    if added:
        SOURCES_USED.add("Massive")
    return added


def store_load() -> dict | None:
    import pandas as pd
    if not STORE.exists():
        return None
    try:
        return pd.read_pickle(STORE, compression="gzip")
    except Exception as e:
        print(f"  aviso almacén: {e}")
        return None


def store_save(store: dict) -> None:
    import pandas as pd
    STORE.parent.mkdir(exist_ok=True)
    pd.to_pickle(store, STORE, compression="gzip")


def store_frames(store: dict, tickers: list[str]) -> dict:
    import pandas as pd
    sp = {}
    for t, dt, ratio in store["splits"]:
        sp.setdefault(t, []).append((pd.Timestamp(dt), ratio))
    out = {}
    for t in tickers:
        if t not in store["close"]:
            continue
        d = pd.DataFrame({"Close": store["close"][t], "High": store["high"][t],
                          "Volume": store["vol"][t]}).dropna(subset=["Close"])
        if not len(d):
            continue
        d["Stock Splits"] = 0.0
        for dt, ratio in sp.get(t, []):
            pos = d.index.searchsorted(dt)
            if pos < len(d):
                d.iloc[pos, d.columns.get_loc("Stock Splits")] = ratio
        out[t] = d
    return out


def us_frames(us: list[str], cutoff: str) -> dict:
    """Barras de EE. UU. desde la fecha de corte: almacén + Massive, o Yahoo si no hay otra."""
    import pandas as pd
    store = store_load()
    if store is not None and store.get("cutoff") != cutoff:
        # nueva fecha de corte tras la reconstrucción semanal: recorta lo anterior
        if pd.Timestamp(store["cutoff"]) <= pd.Timestamp(cutoff):
            for k in ("close", "high", "vol"):
                store[k] = store[k][store[k].index >= pd.Timestamp(cutoff)]
            store["splits"] = [x for x in store["splits"] if x[1] >= cutoff]
            store["cutoff"] = cutoff
        else:
            store = None
    if MASSIVE_KEY:
        try:
            if store is None:
                store = store_build_yahoo(us, cutoff)
            else:
                SOURCES_USED.add("almacén propio")
            store_update_massive(store, set(us))
            store_save(store)
            return store_frames(store, us)
        except Exception as e:
            print(f"  aviso Massive: {e}. Se usa Yahoo para EE. UU.")
    else:
        print("  sin MASSIVE_API_KEY: EE. UU. se descarga de Yahoo")
    store = store_build_yahoo(us, cutoff)
    store_save(store)
    return store_frames(store, us)


def update_ath(refresh: bool = False) -> None:
    u = universe()
    cpath = DATA / "ath_cache.json"
    cache = None
    if cpath.exists() and not refresh:
        try:
            cache = json.loads(cpath.read_text(encoding="utf-8"))
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(cache["built"])).days
            if age > 8:
                cache = None
        except Exception:
            cache = None
    if cache is None:
        print("  recalculando historial completo (puede tardar)...")
        cache = refresh_ath_cache(u)
    us = [t for t, m in u.items() if m["region"] == "EE. UU."]
    eu = [t for t, m in u.items() if m["region"] != "EE. UU."]
    data = us_frames(us, cache["cutoff"])
    eu_data = _download(eu, start=cache["cutoff"])
    for t in eu:                                      # respaldo europeo con Stooq si Yahoo falla
        if t not in eu_data:
            h = stooq_history(t)
            if h is not None:
                eu_data[t] = h
    if eu_data:
        SOURCES_USED.add("Yahoo Finance")
    data.update(eu_data)
    rows, breadth = [], {}
    for sym, d in data.items():
        meta = u[sym]
        try:
            r = ath_scan(sym, meta, d, cache["ath"].get(sym))
        except Exception as e:
            print(f"  aviso {sym}: {e}")
            continue
        if r is None:
            continue
        b = breadth.setdefault(meta["region"], {"total": 0, "near5": 0})
        b["total"] += 1
        if r.get("_far"):
            b["near5"] += r["dist"] >= -5
            continue
        b["near5"] += 1
        rows.append(r)
    if not breadth:
        raise RuntimeError("sin datos de máximos")
    order = {"hoy": 0, "reciente": 1, "cerca": 2}
    rows.sort(key=lambda r: (order[r["status"]], -(r["vol_ratio"] or 0)))
    src = ", ".join(sorted(SOURCES_USED - {"almacén propio"}))
    save("ath.json", {"source": f"Nasdaq Trader (lista de valores) y {src} (cotizaciones)",
                      "filters": {"us_min_price": US_MIN_PRICE, "us_min_dollar_vol": US_MIN_DOLLAR_VOL},
                      "breadth": breadth, "stocks": rows})

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--ath" in args:                      # escáner de máximos, workflow propio
        print("Actualizando máximos históricos...")
        update_ath(refresh="--refresh" in args)
        sys.exit(0)
    failed = 0
    jobs = [("calendario", update_calendar), ("sentimiento", update_sentiment),
            ("noticias", update_news), ("indicadores", update_indicators), ("acciones", update_stocks)]
    for name, fn in jobs:
        print(f"Actualizando {name}...")
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {e} (se conserva el último fichero)")
    sys.exit(1 if failed == len(jobs) else 0)
