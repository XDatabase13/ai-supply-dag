import json
import re
import time
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

MAX_RETRIES = 5
RETRY_INTERVAL = 10
STALE_DAYS = 5

CONFIG_FILE = Path("island_config.json")
OUTPUT_FILE = Path("data.json")
JST = timezone(timedelta(hours=9))


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_previous():
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def collect_all_companies(config):
    """Flatten all companies from all islands, attaching island id."""
    companies = []
    for island in config["islands"]:
        for c in island["companies"]:
            entry = dict(c)
            entry["island"] = island["id"]
            companies.append(entry)
    return companies


def batch_download(tickers):
    """Download OHLCV for all tickers at once with retry."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = yf.download(
                tickers,
                period="5d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if data.empty:
                raise ValueError("Empty response from yfinance")
            return data
        except Exception as e:
            print(f"[batch_download] attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_INTERVAL)
    return None


def extract_price(data, ticker, multi):
    """Return (price, change_pct, date_str) or (None, None, None) on failure."""
    try:
        if multi:
            col = ("Close", ticker)
            if col not in data.columns:
                return None, None, None
            series = data[col].dropna()
        else:
            series = data["Close"].dropna()

        if len(series) < 2:
            return None, None, None

        latest = float(series.iloc[-1])
        prev = float(series.iloc[-2])
        change_pct = round((latest - prev) / prev * 100, 2)
        date_str = series.index[-1].strftime("%Y-%m-%d")
        return round(latest, 4), change_pct, date_str
    except Exception:
        return None, None, None


def fetch_ticker_info(ticker):
    """Return (market_cap, currency) for one ticker with retry."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            fi = yf.Ticker(ticker).fast_info
            market_cap = getattr(fi, "market_cap", None)
            currency = getattr(fi, "currency", None)
            return market_cap, currency
        except Exception as e:
            print(f"  [info] {ticker} attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_INTERVAL)
    return None, None


def is_fresh(date_str):
    """True if date_str is within STALE_DAYS of today (JST)."""
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now(JST).date()
        return (today - d).days <= STALE_DAYS
    except Exception:
        return False


def main():
    config = load_config()
    previous = load_previous()

    all_companies = collect_all_companies(config)
    listed = [c for c in all_companies if c.get("listed") and c.get("ticker")]
    tickers = [c["ticker"] for c in listed]

    print(f"Fetching {len(tickers)} listed tickers...")
    raw = batch_download(tickers)
    multi = raw is not None and isinstance(raw.columns, pd.MultiIndex)

    companies_out = {}
    success_count = 0

    for company in all_companies:
        ticker = company.get("ticker")
        name = company["name"]
        island_id = company["island"]
        is_listed = company.get("listed", False)

        # Key: ticker for listed, name for unlisted
        key = ticker if (is_listed and ticker) else name

        entry = {
            "name": name,
            "island": island_id,
            "ticker": ticker,
            "listed": is_listed,
            "currency": None,
            "price": None,
            "change_pct": None,
            "market_cap": None,
            "date": None,
        }

        if not is_listed or not ticker:
            companies_out[key] = entry
            continue

        # Price from batch download
        price, change_pct, date_str = (
            extract_price(raw, ticker, multi) if raw is not None else (None, None, None)
        )

        # Per-ticker market_cap and currency
        market_cap, currency = (None, None)
        if raw is not None:
            market_cap, currency = fetch_ticker_info(ticker)
            if market_cap is not None:
                market_cap = int(market_cap)

        # Fallback to previous data
        if price is None:
            prev_entry = (previous or {}).get("companies", {}).get(key)
            if prev_entry and is_fresh(prev_entry.get("date")):
                price = prev_entry.get("price")
                change_pct = prev_entry.get("change_pct")
                date_str = prev_entry.get("date")
                currency = currency or prev_entry.get("currency")
                market_cap = market_cap or prev_entry.get("market_cap")
                print(f"  [STALE] {ticker}: using previous data ({date_str})")
            else:
                print(f"  [FAIL]  {ticker}: no data available")
        else:
            success_count += 1
            print(f"  [OK]    {ticker}: {price} ({change_pct:+.2f}%) {currency}")

        entry.update(
            {
                "currency": currency,
                "price": price,
                "change_pct": change_pct,
                "market_cap": market_cap,
                "date": date_str,
            }
        )
        companies_out[key] = entry

    # If all listed tickers failed, fall back to entire previous data.json
    if success_count == 0 and previous:
        print("All fetches failed — returning previous data.json unchanged.")
        return previous

    # Build summary (only listed companies with data)
    with_data = [
        v for v in companies_out.values() if v["listed"] and v["change_pct"] is not None
    ]
    up = [c for c in with_data if c["change_pct"] > 0]
    down = [c for c in with_data if c["change_pct"] < 0]
    unchanged = [c for c in with_data if c["change_pct"] == 0]
    top_gainer = max(with_data, key=lambda x: x["change_pct"]) if with_data else None
    top_loser = min(with_data, key=lambda x: x["change_pct"]) if with_data else None

    output = {
        "_meta": {
            "generated_at": datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
            "status": "complete",
            "site_name": config["site_name"],
        },
        "companies": companies_out,
        "islands": config["islands"],
        "edges": config["edges"],
        "crossover_companies": config["crossover_companies"],
        "summary": {
            "total_companies": len(all_companies),
            "listed": len(listed),
            "unlisted": len(all_companies) - len(listed),
            "up_count": len(up),
            "down_count": len(down),
            "unchanged_count": len(unchanged),
            "top_gainer": (
                {
                    "name": top_gainer["name"],
                    "ticker": top_gainer["ticker"],
                    "change_pct": top_gainer["change_pct"],
                }
                if top_gainer
                else None
            ),
            "top_loser": (
                {
                    "name": top_loser["name"],
                    "ticker": top_loser["ticker"],
                    "change_pct": top_loser["change_pct"],
                }
                if top_loser
                else None
            ),
        },
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    bake_index_html(output, Path("index.html"), config)

    print(
        f"\nDone. success={success_count}/{len(listed)} | "
        f"up={len(up)} down={len(down)} unchanged={len(unchanged)}"
    )
    if top_gainer:
        print(f"  Top gainer: {top_gainer['name']} ({top_gainer['ticker']}) {top_gainer['change_pct']:+.2f}%")
    if top_loser:
        print(f"  Top loser : {top_loser['name']} ({top_loser['ticker']}) {top_loser['change_pct']:+.2f}%")

    return output


_ISLAND_SUB = {
    "ai_model": "AI MODEL", "hyperscaler": "HYPERSCALER",
    "eda": "EDA / IP", "ai_chip": "AI CHIP",
    "manufacturing": "FOUNDRY", "memory": "MEMORY",
    "equipment": "SPE / MATERIALS", "dc_infra": "DC INFRA",
    "physical_ai": "PHYSICAL AI",
}


def _replace_between(text, start, end, inner):
    pattern = re.escape(start) + r".*?" + re.escape(end)
    if not re.search(pattern, text, flags=re.DOTALL):
        print(f"[bake] マーカーが見つかりません: {start}")
        return text
    return re.sub(pattern, lambda _: start + inner + end, text, count=1, flags=re.DOTALL)


def _fmt_price(price, currency):
    if price is None:
        return "—"
    if currency == "JPY":
        return f"¥{round(price):,}"
    if currency == "KRW":
        return f"₩{round(price):,}"
    return f"${price:.2f}"


def _chg_html(chg):
    if chg is None:
        return "—", ""
    if chg > 0.05:
        return f"▲{chg:.2f}%", "color:var(--up)"
    if chg < -0.05:
        return f"▼{abs(chg):.2f}%", "color:var(--down)"
    return f"±{abs(chg):.2f}%", "color:var(--flat)"


def bake_index_html(data, index_path, config):
    if not index_path.exists():
        print("[bake] index.html が見つかりません。スキップ。")
        return
    companies = data.get("companies", {})
    islands = config.get("islands", [])
    cx_map = {cx["name"]: cx for cx in config.get("crossover_companies", [])}

    parts = []
    parts.append(
        '<div class="lead"><p class="lead-h">地図の読み方</p><ul>'
        '<li><b>島（円）</b> … AIサプライチェーン上の9つのセクター。</li>'
        '<li><b>ノード（点）</b> … 個別銘柄。大きさは時価総額（対数スケール）、色は前日比（緑＝上昇、赤＝下落）。非上場はグレー固定。</li>'
        '<li><b>矢印</b> … セクター間の供給の流れ（商流ベース）。</li>'
        '<li><b>橙色の縁取り</b> … 複数の島にまたがる銘柄（Intel・Samsung Electronics）。</li>'
        '</ul></div>\n'
    )

    for isl in islands:
        isl_id = isl["id"]
        sub = _ISLAND_SUB.get(isl_id, "")
        members = [c for c in companies.values() if c.get("island") == isl_id]
        parts.append(
            f'<div class="sec"><div class="sec-h">'
            f'<h2>{isl["name"]}</h2>'
            f'<span class="en">{sub}</span>'
            f'<span class="cnt">{len(members)}社</span></div>\n'
        )
        for c in members:
            key = c.get("ticker") or c.get("name", "")
            name = c.get("name", "")
            cx = cx_map.get(name)
            cross_tag = ""
            if cx:
                also_names = []
                for also_id in cx.get("also", []):
                    for isl2 in islands:
                        if isl2["id"] == also_id:
                            also_names.append(isl2["name"])
                            break
                if also_names:
                    cross_tag = f' <span class="also-tag">※{"・".join(also_names)}にも関連</span>'
            desc = c.get("description", "")
            if not c.get("listed"):
                parts.append(
                    f'<div class="row">'
                    f'<span class="cd">—</span>'
                    f'<span class="nm">{name}<span class="unl">非上場</span></span>'
                    f'<span class="pr">—</span><span class="chg">—</span>'
                    f'<span class="ds">{desc}</span></div>\n'
                )
            else:
                price_s = _fmt_price(c.get("price"), c.get("currency"))
                chg_text, chg_style = _chg_html(c.get("change_pct"))
                style_attr = f' style="{chg_style}"' if chg_style else ""
                parts.append(
                    f'<div class="row">'
                    f'<span class="cd">{key}</span>'
                    f'<span class="nm">{name}{cross_tag}</span>'
                    f'<span class="pr" data-col="pr" data-key="{key}">{price_s}</span>'
                    f'<span class="chg" data-col="chg" data-key="{key}"{style_attr}>{chg_text}</span>'
                    f'<span class="ds">{desc}</span></div>\n'
                )
        parts.append('</div>\n')

    inner = "\n" + "".join(parts)
    content = index_path.read_text(encoding="utf-8")
    content = _replace_between(content, "<!--STOCK_LIST_START-->", "<!--STOCK_LIST_END-->", inner)
    index_path.write_text(content, encoding="utf-8")
    print("[bake] index.html 焼き込み完了")


if __name__ == "__main__":
    main()
