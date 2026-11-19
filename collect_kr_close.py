"""코스피 장마감 시황용 숫자 수집 스크립트.

GitHub Actions에서 한국시간 평일 장 마감 뒤 실행되어
data/kr_close_latest.json 과 data/history/kr_close_flows.csv 를 저장한다.
Claude 예약 작업 "코스피 장마감 시황"이 이 JSON을 읽어 해석만 한다.

- 지수·종목 가격, 이동평균: 야후 파이낸스(yfinance)
- 투자자별 매매·프로그램·K200 지수·선물·거래대금 상위: 네이버 금융. 실패해도 나머지는 저장
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


NAVER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                 "Referer": "https://finance.naver.com/"}


def _num(x):
    """'1,234' '-56' '+3.41%' '상승 1,000' 같은 문자열 → float. 실패하면 None."""
    import re
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return None if x != x else float(x)
    t = str(x).replace(",", "").replace("%", "").strip()
    neg = t.startswith("-") or "하락" in t or "하한" in t
    m = re.search(r"\d+(\.\d+)?", t)
    if not m:
        return None
    v = float(m.group(0))
    return -v if neg else v


def _get(url, debug, key):
    import requests
    try:
        r = requests.get(url, headers=NAVER_HEADERS, timeout=20)
        debug[key] = r.status_code
        if r.status_code != 200:
            return None
        if "finance.naver.com" in url and "m.stock" not in url:
            r.encoding = "euc-kr"
        return r
    except Exception as e:
        debug[key] = str(e)[:150]
        return None


def _date_rows(html, yymmdd):
    """표에서 첫 칸이 오늘 날짜(26.11.19 또는 2026.11.19)인 행들을 숫자 리스트로."""
    import io
    import pandas as pd
    out = []
    for df in pd.read_html(io.StringIO(html)):
        for _, row in df.iterrows():
            first = str(row.iloc[0]).strip()
            if first in (yymmdd, "20" + yymmdd):
                out.append([str(v) for v in row.tolist()])
    return out


def naver_flows(today):
    """네이버 금융에서 투자자별 매매(시장), 프로그램, 종목별 외국인·기관, K200 선물, 거래대금 상위."""
    res = {"ok": False, "source": "네이버 금융"}
    debug = {}
    errors = []
    yymmdd = today[2:].replace("-", ".")
    ymd = today.replace("-", "")

    # 1)·2) 시장별 투자자 순매수·프로그램: 네이버 모바일 API(구 PC 페이지는 410으로 닫힘)
    import requests
    markets, prog, breadth = {}, {}, {}
    for mkt in ("KOSPI", "KOSDAQ"):
        url = f"https://m.stock.naver.com/api/index/{mkt}/integration"
        try:
            r = requests.get(url, headers=NAVER_HEADERS, timeout=20)
            debug[f"integration_{mkt}"] = r.status_code
            if r.status_code != 200:
                continue
            js = r.json()
            debug[f"integration_{mkt}_keys"] = list(js.keys()) if isinstance(js, dict) else str(type(js))
            # 투자자 동향(dealTrendInfo: 개인·외국인·기관, 억 원)
            deal = js.get("dealTrendInfo") or js.get("dealTrendInfos")
            if isinstance(deal, list):
                deal = deal[0] if deal else None
            if deal:
                debug[f"integration_{mkt}_deal_raw"] = deal
                biz = str(deal.get("bizdate", "")).replace("-", "")[:8]
                if biz == ymd:
                    markets[mkt] = {
                        "개인": _num(deal.get("personalValue")),
                        "외국인": _num(deal.get("foreignValue")),
                        "기관계": _num(deal.get("institutionalValue")),
                    }
                    # 그 밖에 숫자로 된 값(기타법인 등)이 있으면 원래 키 이름으로 같이 저장
                    for kk, vv in deal.items():
                        if kk not in ("bizdate", "personalValue", "foreignValue", "institutionalValue") and _num(vv) is not None:
                            markets[mkt][kk] = _num(vv)
                else:
                    errors.append(f"{mkt} 투자자: 날짜가 오늘이 아님({biz})")
            else:
                errors.append(f"{mkt} 투자자: dealTrendInfo 없음")
            # 프로그램(programTrendInfo: 차익·비차익·전체 순매수, 억 원)
            pt = js.get("programTrendInfo")
            if isinstance(pt, dict):
                biz = str(pt.get("bizdate", "")).replace("-", "")[:8]
                if biz == ymd:
                    prog[mkt] = {
                        "차익_순매수": _num(pt.get("indexDifferenceReal")),
                        "비차익_순매수": _num(pt.get("indexBiDifferenceReal")),
                        "전체_순매수": _num(pt.get("indexTotalReal")),
                    }
                else:
                    errors.append(f"{mkt} 프로그램: 날짜가 오늘이 아님({biz})")
            # 상승·하락 종목 수
            ud = js.get("upDownStockInfo")
            if isinstance(ud, dict):
                breadth[mkt] = {k: (_num(v) if _num(v) is not None else v) for k, v in ud.items()}
        except Exception as e:
            errors.append(f"{mkt} integration: {str(e)[:150]}")
    res["market_net_buy_eok"] = markets
    res["program_eok"] = prog
    res["breadth"] = breadth

    # 3) 종목별 외국인·기관·개인 순매매(주): 네이버 모바일 trend API → 종가로 억 원 환산(근사)
    stocks = {}
    for code, name in FLOW_STOCKS.items():
        try:
            r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize=5", headers=NAVER_HEADERS, timeout=20)
            debug[f"trend_{code}"] = r.status_code
            if r.status_code != 200:
                continue
            js = r.json()
            rows = js if isinstance(js, list) else (js.get("trends") or js.get("list") or [])
            if rows:
                debug[f"trend_{code}_sample"] = rows[0]
            row = next((x for x in rows if str(x.get("bizdate", "")).replace("-", "")[:8] == ymd), None)
            if not row:
                errors.append(f"{name} 외국인·기관: 오늘 날짜 없음({rows[0].get('bizdate') if rows else '빈 응답'})")
                continue
            close = _num(row.get("closePrice"))
            out = {}
            for label, key in (("외국인", "foreignerPureBuyQuant"), ("기관", "organPureBuyQuant"), ("개인", "individualPureBuyQuant")):
                q = _num(row.get(key))
                out[f"{label}_순매매_주"] = q
                out[f"{label}_억_근사"] = r2(q * close / EOK, 0) if q is not None and close else None
            out["외국인_보유율"] = _num(row.get("foreignerHoldRatio"))
            stocks[name] = out
        except Exception as e:
            errors.append(f"{name} 외국인·기관: {str(e)[:150]}")
    res["stock_net_buy"] = stocks

    # 4) 코스피200 지수와 K200 선물(최근월물): 모바일 API 우선, 안 되면 PC 페이지
    import re
    idx = {}
    for key, code in (("kospi200", "KPI200"), ("k200_futures", "FUT")):
        val = {"close": None}
        try:
            r = requests.get(f"https://m.stock.naver.com/api/index/{code}/basic", headers=NAVER_HEADERS, timeout=20)
            debug[f"mbasic_{code}"] = r.status_code
            if r.status_code == 200:
                js = r.json()
                debug[f"mbasic_{code}_sample"] = {k: js.get(k) for k in list(js)[:15]} if isinstance(js, dict) else None
                val = {"close": _num(js.get("closePrice")), "change": _num(js.get("compareToPreviousClosePrice")),
                       "change_pct": _num(js.get("fluctuationsRatio")), "time": js.get("localTradedAt")}
        except Exception as e:
            errors.append(f"{key} 모바일: {str(e)[:150]}")
        if val.get("close") is None:
            try:
                r = _get(f"https://finance.naver.com/sise/sise_index.naver?code={code}", debug, f"index_{code}")
                if r is not None:
                    m = re.search(r'id="now_value"[^>]*>\s*([\d,\.]+)', r.text)
                    val["close"] = _num(m.group(1)) if m else None
                    if not m:
                        i = r.text.find("now_value")
                        debug[f"index_{code}_snippet"] = r.text[max(0, i - 100): i + 300] if i >= 0 else r.text[:300]
            except Exception as e:
                errors.append(f"{key}: {str(e)[:150]}")
        idx[key] = val
    res["index"] = idx

    # 5) 거래대금 상위: 네이버 모바일 순위 API 여러 개(거래량 상위·상승·시총 상위)를 합쳐 거래대금 순으로 정렬
    tops = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        pool = {}
        for kind in ("quantTop", "up", "marketValue"):
            url = f"https://m.stock.naver.com/api/stocks/{kind}/{mkt}?page=1&pageSize=100"
            try:
                r = requests.get(url, headers=NAVER_HEADERS, timeout=20)
                debug[f"rank_{kind}_{mkt}"] = r.status_code
                if r.status_code != 200:
                    continue
                js = r.json()
                rows = js.get("stocks") if isinstance(js, dict) else js
                if not rows:
                    continue
                if f"rank_sample_{mkt}" not in debug:
                    debug[f"rank_sample_{mkt}"] = rows[0]
                for x in rows:
                    code = x.get("itemCode")
                    if code:
                        pool[code] = x
            except Exception as e:
                errors.append(f"{mkt} {kind}: {str(e)[:120]}")
        items = []
        for code, x in pool.items():
            tv = _num(x.get("accumulatedTradingValue"))
            items.append({
                "code": code,
                "name": x.get("stockName"),
                "close": _num(x.get("closePrice")),
                "change_pct": _num(x.get("fluctuationsRatio")) if (x.get("compareToPreviousPrice") or {}).get("name") != "FALLING" else -abs(_num(x.get("fluctuationsRatio")) or 0),
                "trading_value_raw": tv,
                "trading_value_text": x.get("accumulatedTradingValueKrwHangeul"),
            })
        items.sort(key=lambda z: z["trading_value_raw"] or 0, reverse=True)
        if items:
            tops[mkt] = items[:15]
        else:
            errors.append(f"{mkt} 거래대금 상위: 순위 API 응답 없음")
    res["top_trading_value"] = tops

    res["ok"] = bool(markets or prog or stocks or tops or any(v.get("close") for v in idx.values()))
    res["errors"] = errors
    res["debug_http"] = debug
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

    try:
        flows = naver_flows(today) if market_open_today else {"ok": False, "error": "오늘 장 데이터 없음(휴장 또는 미반영)"}
    except Exception:
        flows = {"ok": False, "error": traceback.format_exc()[-300:]}

    # 코스피200 지수는 야후에 없어 네이버 값으로 배율 계산
    k200_close = (flows.get("index", {}).get("kospi200") or {}).get("close") if flows.get("ok") is not None else None
    fut_close = (flows.get("index", {}).get("k200_futures") or {}).get("close")
    if kospi.get("ok") and k200_close:
        ratio = r2(kospi["close"] / k200_close, 4)
    ratio_fut = r2(kospi["close"] / fut_close, 4) if kospi.get("ok") and fut_close else None

    result = {
        "generated_at_kst": now.strftime("%Y-%m-%d %H:%M"),
        "krx_session_date": session_date,
        "market_open_today": market_open_today,
        "kospi_to_k200_index_ratio": ratio,
        "kospi_to_k200_futures_ratio": ratio_fut,
        "stale_tickers": stale,
        "failed_tickers": [t for t, r in items.items() if not r.get("ok")],
        "items": items,
        "krx": flows,
        "notes": [
            "가격·이평선은 야후 일봉. ma는 오늘 종가 포함, ma_next_day_if_flat은 내일 종가가 오늘과 같다고 가정한 값",
            "kospi_to_k200_index_ratio = 코스피 ÷ 코스피200 지수, kospi_to_k200_futures_ratio = 코스피 ÷ K200 선물(최근월물, 네이버)",
            "krx.market_net_buy_eok는 개인·외국인·기관계(억 원, 네이버 모바일). program_eok는 차익·비차익·전체 순매수(억 원). breadth는 상승·하락 종목 수",
            "krx(네이버) market_net_buy_eok·program_eok 단위는 억 원(순매수). 시장 기타법인 포함",
            "stock_net_buy는 네이버 모바일 종목별 외국인·기관·개인 순매매(주)와 종가로 환산한 억 원 근사. 종목별 기타법인(자사주)은 없음",
            "top_trading_value는 네이버 모바일 거래량 상위·상승·시총 상위를 합쳐 거래대금 순으로 정렬한 것. trading_value_raw 단위는 네이버 표기(백만 원으로 추정), trading_value_text가 있으면 그 한글 표기가 정확함",
            "원/달러는 24시간 시세라 실행 시각 값(서울 종가 아님)",
            "외국인 선물 수급은 수집하지 않음(검색으로 확인)",
        ],
    }

    os.makedirs("data/history", exist_ok=True)
    with open("data/kr_close_latest.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    # 일별 기록 누적(같은 날짜면 덮어쓰기)
    if market_open_today:
        hy = items["000660.KS"]
        mk = flows.get("market_net_buy_eok", {}).get("KOSPI", {}) if flows.get("ok") else {}
        hyf = flows.get("stock_net_buy", {}).get("SK하이닉스", {}) if flows.get("ok") else {}
        pg = flows.get("program_eok", {}).get("KOSPI", {}) if flows.get("ok") else {}
        row = {
            "date": today,
            "kospi": kospi.get("close"),
            "kospi_chg_pct": kospi.get("change_pct"),
            "kosdaq": items["^KQ11"].get("close"),
            "k200": k200_close,
            "k200_futures": fut_close,
            "kospi_foreign_eok": mk.get("외국인"),
            "kospi_inst_eok": mk.get("기관계"),
            "kospi_indiv_eok": mk.get("개인"),
            "kospi_program_nonarb_eok": pg.get("비차익_순매수"),
            "hynix_close": hy.get("close"),
            "hynix_foreign_eok_approx": hyf.get("외국인_억_근사"),
            "hynix_inst_eok_approx": hyf.get("기관_억_근사"),
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
