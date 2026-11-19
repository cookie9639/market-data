"""코스피 장마감 시황용 숫자 수집 스크립트.

GitHub Actions에서 한국시간 평일 장 마감 뒤 실행되어
data/kr_close_latest.json 과 data/history/kr_close_flows.csv 를 저장한다.
Claude 예약 작업 "코스피 장마감 시황"이 이 JSON을 읽어 해석만 한다.

- 지수·종목 가격, 이동평균: 야후 파이낸스(yfinance)
- 투자자별 매매(외국인·기관·개인·기타법인), 거래대금 상위: KRX(pykrx), 실패해도 나머지는 저장
"""

import csv
import json
import os
import traceback
from datetime import datetime, timezone, timedelta

import yfinance as yf

KST = timezone(timedelta(hours=9))
EOK = 100_000_000  # 1억 원

# (야후 티커, 이름, 그룹)
TICKERS = [
    ("^KS11", "코스피", "지수"),
    ("^KQ11", "코스닥", "지수"),
    ("^KS200", "코스피200", "지수"),
    ("KRW=X", "원/달러(야후, 실행 시각)", "환율"),
    # 고정 관찰
    ("005930.KS", "삼성전자", "고정관찰"),
    ("000660.KS", "SK하이닉스", "고정관찰"),
    ("006400.KS", "삼성SDI", "고정관찰"),
    # 반도체
    ("042700.KS", "한미반도체", "반도체"),
    ("009150.KS", "삼성전기", "반도체"),
    ("011070.KS", "LG이노텍", "반도체"),
    ("402340.KS", "SK스퀘어", "반도체"),
    # 송배전
    ("298040.KS", "효성중공업", "송배전"),
    ("001440.KS", "대한전선", "송배전"),
    # 로봇
    ("005380.KS", "현대차", "로봇"),
    ("307950.KS", "현대오토에버", "로봇"),
    ("012330.KS", "현대모비스", "로봇"),
    ("086280.KS", "현대글로비스", "로봇"),
    ("319400.KQ", "현대무벡스", "로봇"),
    ("277810.KQ", "레인보우로보틱스", "로봇"),
    ("454910.KS", "두산로보틱스", "로봇"),
    ("066570.KS", "LG전자", "로봇"),
]

ROUND_THE_CLOCK = {"KRW=X"}
MA_DAYS = (5, 10, 20, 60, 120)

# KRX 투자자별 매매를 볼 종목(6자리 코드)
FLOW_STOCKS = {"005930": "삼성전자", "000660": "SK하이닉스", "006400": "삼성SDI"}


def r2(x, n=2):
    return None if x is None else round(float(x), n)


def yahoo_item(ticker, today):
    try:
        hist = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=False)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        return {"ok": False, "error": "데이터 부족"}
    dates = [d.strftime("%Y-%m-%d") for d in hist.index]
    last, prev = hist.iloc[-1], hist.iloc[-2]
    close = float(last["Close"])
    out = {
        "ok": True,
        "date": dates[-1],
        "prev_date": dates[-2],
        "open": r2(last["Open"], 4),
        "high": r2(last["High"], 4),
        "low": r2(last["Low"], 4),
        "close": r2(close, 4),
        "prev_close": r2(prev["Close"], 4),
        "change_pct": r2((close / float(prev["Close"]) - 1) * 100),
        "volume": int(last["Volume"]) if last["Volume"] == last["Volume"] else None,
    }
    if len(hist) >= 6:
        out["change_5d_pct"] = r2((close / float(hist.iloc[-6]["Close"]) - 1) * 100)
    # 거래량 배수: 오늘 거래량 / 직전 20일 평균
    if len(hist) >= 21 and out["volume"]:
        avg20 = float(hist["Volume"].iloc[-21:-1].mean())
        if avg20 > 0:
            out["volume_vs_20d"] = r2(out["volume"] / avg20)
    # 이동평균(오늘 종가 포함)과 종가 위치
    mas = {}
    for n in MA_DAYS:
        if len(hist) >= n:
            ma = float(hist["Close"].iloc[-n:].mean())
            mas[f"ma{n}"] = r2(ma, 2)
            mas[f"vs_ma{n}_pct"] = r2((close / ma - 1) * 100)
    out["ma"] = mas
    # 내일 이평선 추정(내일 종가가 오늘과 같다고 가정)
    nxt = {}
    for n in MA_DAYS:
        if len(hist) >= n:
            window = list(hist["Close"].iloc[-(n - 1):]) + [close]
            nxt[f"ma{n}"] = r2(sum(window) / n, 2)
    out["ma_next_day_if_flat"] = nxt
    out["is_today"] = dates[-1] == today
    return out


def krx_flows(today_ymd):
    """pykrx로 투자자별 매매·거래대금 상위. 실패하면 error만 남긴다."""
    res = {"ok": False}
    try:
        from pykrx import stock
    except Exception as e:
        res["error"] = f"pykrx import 실패: {e}"
        return res

    errors = []

    # 1) 시장별 투자자 순매수(억 원)
    markets = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        try:
            df = stock.get_market_trading_value_by_date(today_ymd, today_ymd, mkt)
            if len(df):
                row = df.iloc[-1]
                markets[mkt] = {k: r2(row[k] / EOK, 0) for k in df.columns}
        except Exception as e:
            errors.append(f"{mkt} 투자자: {str(e)[:150]}")
    res["market_net_buy_eok"] = markets

    # 2) 종목별 투자자 순매수(억 원, 세부 구분 포함 → 기타법인)
    stocks = {}
    for code, name in FLOW_STOCKS.items():
        try:
            df = stock.get_market_trading_value_by_date(today_ymd, today_ymd, code, detail=True)
            if len(df):
                row = df.iloc[-1]
                stocks[name] = {k: r2(row[k] / EOK, 0) for k in df.columns}
        except Exception as e:
            errors.append(f"{name} 투자자: {str(e)[:150]}")
    res["stock_net_buy_eok"] = stocks

    # 3) 거래대금 상위 15(코스피·코스닥)
    tops = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        try:
            df = stock.get_market_ohlcv(today_ymd, market=mkt)
            if len(df) and df["거래대금"].sum() > 0:
                df = df.sort_values("거래대금", ascending=False).head(15)
                tops[mkt] = [
                    {
                        "code": code,
                        "name": stock.get_market_ticker_name(code),
                        "close": int(r["종가"]),
                        "change_pct": r2(r["등락률"]),
                        "trading_value_eok": r2(r["거래대금"] / EOK, 0),
                    }
                    for code, r in df.iterrows()
                ]
                # 상승·하락 종목 수
                all_df = stock.get_market_ohlcv(today_ymd, market=mkt)
                tops[mkt + "_breadth"] = {
                    "up": int((all_df["등락률"] > 0).sum()),
                    "down": int((all_df["등락률"] < 0).sum()),
                    "flat": int((all_df["등락률"] == 0).sum()),
                }
        except Exception as e:
            errors.append(f"{mkt} 거래대금 상위: {str(e)[:150]}")
    res["top_trading_value"] = tops

    res["ok"] = bool(markets) or bool(stocks) or bool(tops)
    if errors:
        res["errors"] = errors
    return res


def main():
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    today_ymd = now.strftime("%Y%m%d")

    items = {}
    for ticker, name, group in TICKERS:
        r = yahoo_item(ticker, today)
        r.update({"name": name, "group": group})
        items[ticker] = r

    kospi = items["^KS11"]
    session_date = kospi.get("date") if kospi.get("ok") else None
    market_open_today = session_date == today

    # 오늘 날짜 봉이 아닌 종목(장중 미반영·휴장 등)
    stale = [t for t, r in items.items()
             if r.get("ok") and t not in ROUND_THE_CLOCK and r.get("date") != session_date]

    # 환산 배율(코스피 ÷ 코스피200 지수). 선물 종가는 수집하지 않으므로 지수 기준
    ratio = None
    k200 = items["^KS200"]
    if kospi.get("ok") and k200.get("ok") and k200.get("date") == session_date:
        ratio = r2(kospi["close"] / k200["close"], 4)

    try:
        flows = krx_flows(today_ymd) if market_open_today else {"ok": False, "error": "오늘 장 데이터 없음(휴장 또는 미반영)"}
    except Exception:
        flows = {"ok": False, "error": traceback.format_exc()[-300:]}

    result = {
        "generated_at_kst": now.strftime("%Y-%m-%d %H:%M"),
        "krx_session_date": session_date,
        "market_open_today": market_open_today,
        "kospi_to_k200_index_ratio": ratio,
        "stale_tickers": stale,
        "failed_tickers": [t for t, r in items.items() if not r.get("ok")],
        "items": items,
        "krx": flows,
        "notes": [
            "가격·이평선은 야후 일봉. ma는 오늘 종가 포함, ma_next_day_if_flat은 내일 종가가 오늘과 같다고 가정한 값",
            "kospi_to_k200_index_ratio는 코스피 ÷ 코스피200 '지수'(선물 아님)",
            "krx 순매수 단위는 억 원(매수−매도). 기타법인 = 회사 자사주 매입이 잡히는 구분",
            "원/달러는 24시간 시세라 실행 시각 값(서울 종가 아님)",
            "K200 선물·외국인 선물·프로그램 매매는 수집하지 않음(검색으로 확인)",
        ],
    }

    os.makedirs("data/history", exist_ok=True)
    with open("data/kr_close_latest.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    # 일별 기록 누적(같은 날짜면 덮어쓰기)
    if market_open_today:
        hy = items["000660.KS"]
        mk = flows.get("market_net_buy_eok", {}).get("KOSPI", {}) if flows.get("ok") else {}
        hyf = flows.get("stock_net_buy_eok", {}).get("SK하이닉스", {}) if flows.get("ok") else {}
        row = {
            "date": today,
            "kospi": kospi.get("close"),
            "kospi_chg_pct": kospi.get("change_pct"),
            "kosdaq": items["^KQ11"].get("close"),
            "k200": k200.get("close"),
            "kospi_foreign_eok": mk.get("외국인합계"),
            "kospi_inst_eok": mk.get("기관합계"),
            "kospi_indiv_eok": mk.get("개인"),
            "hynix_close": hy.get("close"),
            "hynix_foreign_eok": hyf.get("외국인"),
            "hynix_other_corp_eok": hyf.get("기타법인"),
        }
        path = "data/history/kr_close_flows.csv"
        rows = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                rows = [r for r in csv.DictReader(f) if r["date"] != today]
        rows.append(row)
        rows.sort(key=lambda r: r["date"])
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            w.writeheader()
            w.writerows(rows)

    print(json.dumps({k: result[k] for k in
                      ("generated_at_kst", "krx_session_date", "market_open_today",
                       "stale_tickers", "failed_tickers")}, ensure_ascii=False))
    print("krx ok:", flows.get("ok"), flows.get("errors") or flows.get("error"))


if __name__ == "__main__":
    main()
