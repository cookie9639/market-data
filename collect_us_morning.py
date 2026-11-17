"""미장 아침 시황용 숫자 수집 스크립트.

GitHub Actions에서 매일(한국시간 평일 06:20쯤) 실행되어
data/us_morning_latest.json 과 data/history/ADR 프리미엄 기록을 저장한다.
Claude 예약 작업 "미장 아침 시황"이 이 JSON을 읽어 해석만 한다.
"""

import csv
import json
import math
import os
from datetime import datetime, timezone, timedelta

import yfinance as yf

KST = timezone(timedelta(hours=9))

# (티커, 이름, 그룹)
TICKERS = [
    # 미국 지수
    ("^DJI", "다우", "지수"),
    ("^GSPC", "S&P500", "지수"),
    ("^IXIC", "나스닥", "지수"),
    ("^RUT", "러셀2000", "지수"),
    ("^SOX", "필라델피아 반도체(SOX)", "지수"),
    ("^VIX", "VIX", "지수"),
    # 환율
    ("DX-Y.NYB", "달러인덱스(DXY)", "환율"),
    ("KRW=X", "원/달러(야후, 역외 시세)", "환율"),
    ("JPY=X", "엔/달러", "환율"),
    # 유가
    ("CL=F", "WTI", "유가"),
    ("BZ=F", "브렌트", "유가"),
    # 금리 (값 자체가 수익률 %)
    ("^TNX", "미 10년물 수익률", "금리"),
    ("2YY=F", "미 2년물 수익률(선물 기준)", "금리"),
    # 한국 연동
    ("EWY", "EWY(MSCI 한국 ETF)", "한국연동"),
    ("SKHY", "SK하이닉스 ADR", "한국연동"),
    ("^KS11", "코스피(전 거래일)", "한국연동"),
    ("^KS200", "코스피200 지수(전 거래일)", "한국연동"),
    ("000660.KS", "SK하이닉스 본주(전 거래일)", "한국연동"),
    ("005930.KS", "삼성전자(전 거래일)", "한국연동"),
    ("006400.KS", "삼성SDI(전 거래일)", "한국연동"),
    # 반도체
    ("MU", "마이크론", "반도체"),
    ("NVDA", "엔비디아", "반도체"),
    ("AMD", "AMD", "반도체"),
    ("AVGO", "브로드컴", "반도체"),
    ("AMAT", "어플라이드머티리얼즈", "반도체"),
    ("TSM", "TSMC ADR", "반도체"),
    ("SNDK", "샌디스크", "반도체"),
    # 전력·데이터센터·원전
    ("GEV", "GE버노바", "전력"),
    ("ETN", "이튼", "전력"),
    ("VRT", "버티브", "전력"),
    ("CEG", "컨스텔레이션에너지", "전력"),
    ("VST", "비스트라", "전력"),
    # 2차전지·리튬·ESS
    ("TSLA", "테슬라", "2차전지"),
    ("ALB", "앨버말", "2차전지"),
    ("SQM", "SQM", "2차전지"),
    ("LAC", "리튬아메리카스", "2차전지"),
    ("FLNC", "플루언스", "2차전지"),
    ("EOSE", "이오스에너지", "2차전지"),
    ("QS", "퀀텀스케이프", "2차전지"),
    ("SLDP", "솔리드파워", "2차전지"),
    ("ENPH", "엔페이즈", "2차전지"),
    # S&P500 섹터 ETF
    ("XLK", "기술", "섹터"),
    ("XLC", "커뮤니케이션", "섹터"),
    ("XLY", "임의소비재", "섹터"),
    ("XLP", "필수소비재", "섹터"),
    ("XLF", "금융", "섹터"),
    ("XLV", "헬스케어", "섹터"),
    ("XLI", "산업재", "섹터"),
    ("XLE", "에너지", "섹터"),
    ("XLB", "소재", "섹터"),
    ("XLU", "유틸리티", "섹터"),
    ("XLRE", "부동산", "섹터"),
]

RATE_TICKERS = {"^TNX", "2YY=F"}
KRX_TICKERS = {"^KS11", "^KS200", "000660.KS", "005930.KS", "006400.KS"}
ADR_RATIO = 10  # 보통주 1주 = ADR 10개


def fetch(ticker):
    """최근 일봉에서 마지막 종가와 그 전 종가를 가져온다."""
    try:
        hist = yf.Ticker(ticker).history(period="15d", interval="1d", auto_adjust=False)
    except Exception as e:  # 네트워크·티커 오류
        return {"ok": False, "error": str(e)[:200]}
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        return {"ok": False, "error": "데이터 부족"}
    last, prev = hist.iloc[-1], hist.iloc[-2]
    close, prev_close = float(last["Close"]), float(prev["Close"])
    out = {
        "ok": True,
        "date": hist.index[-1].strftime("%Y-%m-%d"),
        "prev_date": hist.index[-2].strftime("%Y-%m-%d"),
        "close": round(close, 4),
        "prev_close": round(prev_close, 4),
        "high": round(float(last["High"]), 4),
        "low": round(float(last["Low"]), 4),
    }
    if ticker in RATE_TICKERS:
        out["change_bp"] = round((close - prev_close) * 100, 1)
    else:
        out["change_pct"] = round((close / prev_close - 1) * 100, 2)
    if len(hist) >= 6:
        out["change_5d_pct"] = round((close / float(hist.iloc[-6]["Close"]) - 1) * 100, 2)
    return out


def main():
    now_kst = datetime.now(KST)
    items = {}
    for ticker, name, group in TICKERS:
        r = fetch(ticker)
        r.update({"name": name, "group": group})
        items[ticker] = r

    # 기준이 되는 미국 마감일 = S&P500 마지막 일봉 날짜
    us_date = items["^GSPC"].get("date")
    krx_date = items["000660.KS"].get("date")

    # 날짜가 기준과 다른 미국 종목 표시(오래된 값 걸러내기용)
    stale = []
    for ticker, r in items.items():
        if not r.get("ok"):
            continue
        if ticker in KRX_TICKERS or ticker in ("KRW=X", "JPY=X", "DX-Y.NYB", "CL=F", "BZ=F", "2YY=F"):
            continue
        if us_date and r["date"] != us_date:
            r["stale"] = True
            stale.append(ticker)

    # 섹터 순위
    sectors = sorted(
        [(t, r["name"], r["change_pct"]) for t, r in items.items()
         if r.get("group") == "섹터" and r.get("ok")],
        key=lambda x: x[2], reverse=True,
    )

    # 하이닉스 ADR 프리미엄
    premium = {"ok": False}
    adr, krx, fx = items["SKHY"], items["000660.KS"], items["KRW=X"]
    if adr.get("ok") and krx.get("ok") and fx.get("ok"):
        krw_value = adr["close"] * fx["close"] * ADR_RATIO
        prem = (krw_value / krx["close"] - 1) * 100
        premium = {
            "ok": True,
            "adr_date": adr["date"],
            "adr_close_usd": adr["close"],
            "adr_change_pct": adr["change_pct"],
            "usdkrw": round(fx["close"], 2),
            "usdkrw_source": "야후 KRW=X(실행 시각의 역외 시세, NDF 최종호가와 다를 수 있음)",
            "adr_krw_value": round(krw_value),
            "krx_date": krx["date"],
            "krx_close": krx["close"],
            "premium_pct": round(prem, 2),
            "mu_change_pct": items["MU"].get("change_pct"),
            "sox_change_pct": items["^SOX"].get("change_pct"),
        }

    result = {
        "generated_at_kst": now_kst.strftime("%Y-%m-%d %H:%M"),
        "us_session_date": us_date,
        "krx_compare_date": krx_date,
        "stale_tickers": stale,
        "failed_tickers": [t for t, r in items.items() if not r.get("ok")],
        "sector_rank": [{"ticker": t, "name": n, "change_pct": c} for t, n, c in sectors],
        "hynix_adr_premium": premium,
        "items": items,
        "notes": [
            "금리(^TNX, 2YY=F)의 close는 수익률(%)이고 change_bp는 bp 변화",
            "한국 종목(.KS, ^KS11, ^KS200)은 전 거래일 KRX 종가",
            "환율·유가·2년물은 24시간 시세라 실행 시각 값",
            "stale=true 종목은 날짜가 미국 마감일과 달라 쓰지 말 것",
        ],
    }

    os.makedirs("data/history", exist_ok=True)
    with open("data/us_morning_latest.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    # 프리미엄 기록 누적(같은 미국 마감일이면 덮어쓰기)
    if premium["ok"]:
        path = "data/history/hynix_adr_premium.csv"
        rows = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                rows = [r for r in csv.DictReader(f) if r["adr_date"] != premium["adr_date"]]
        rows.append({k: premium[k] for k in
                     ("adr_date", "adr_close_usd", "adr_change_pct", "usdkrw",
                      "adr_krw_value", "krx_date", "krx_close", "premium_pct")})
        rows.sort(key=lambda r: r["adr_date"])
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print(json.dumps({k: result[k] for k in
                      ("generated_at_kst", "us_session_date", "krx_compare_date",
                       "stale_tickers", "failed_tickers")}, ensure_ascii=False))
    print("premium:", premium.get("premium_pct"))


if __name__ == "__main__":
    main()
