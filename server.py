# -*- coding: utf-8 -*-
"""
网页版缠论分析 - 本地数据服务

数据源（均为公开行情接口，无需 token）：
  主：腾讯财经 ifzq.gtimg.cn   —— 日/周/月（支持前/后复权）、1/5/15/30/60 分钟
  备：新浪财经 quotes.sina.cn   —— 日/周/月、分钟线、长历史
  搜索：新浪 suggest3  + 腾讯 smartbox

启动： python server.py   →   http://127.0.0.1:8770
"""
import os
import re
import json
import time
import shutil
import subprocess
from datetime import date, timedelta

import requests
from urllib.parse import urlencode, quote
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# ---- 内存缓存：同一 code+period+fqt 5 分钟内不重复请求外部 API ----
_cache: dict[str, dict] = {}
_CACHE_TTL = 300  # 秒

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

app = FastAPI(title="ChanLun Web")

# ---------------------------------------------------------------- 周期
# tx = 腾讯接口类型；sina = 新浪 scale
PERIODS = {
    "1m":     {"tx": "m1",   "sina": 1,    "label": "1分钟"},
    "5m":     {"tx": "m5",   "sina": 5,    "label": "5分钟"},
    "15m":    {"tx": "m15",  "sina": 15,   "label": "15分钟"},
    "30m":    {"tx": "m30",  "sina": 30,   "label": "30分钟"},
    "60m":    {"tx": "m60",  "sina": 60,   "label": "60分钟"},
    "day":    {"tx": "day",  "sina": 240,  "label": "日线"},
    "week":   {"tx": "week", "sina": 1200, "label": "周线"},
    "month":  {"tx": "month","sina": 7200, "label": "月线"},
    "quarter":{"tx": None,   "sina": None, "label": "季线"},
    "year":   {"tx": None,   "sina": None, "label": "年线"},
}
MINUTE_PERIODS = {"1m", "5m", "15m", "30m", "60m"}
TX_MAX = 640          # 腾讯单次返回上限

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Referer": "https://gu.qq.com/"})
_has_curl = shutil.which("curl") is not None


# ---------------------------------------------------------------- HTTP
def decode_auto(b: bytes, prefer: str = "utf-8") -> str:
    """
    解码响应体。注意：GBK 的中文双字节序列常常「恰好」是合法 UTF-8，
    靠异常无法区分（会解出 ũ 这类乱码字符），因此对已知站点显式指定编码。
    """
    if prefer == "gbk":
        try:
            return b.decode("gbk")
        except UnicodeDecodeError:
            return b.decode("utf-8", "ignore")
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("gbk", "ignore")
    if "\ufffd" in s:
        try:
            return b.decode("gbk", "ignore")
        except UnicodeDecodeError:
            pass
    return s


def _curl_bytes(url: str, timeout: int = 15, referer: str = "https://gu.qq.com/"):
    """行情站会拦截 Python urllib 的 TLS 指纹，优先用 curl 取数据。"""
    cmd = [
        "curl", "-s", "--compressed", "-m", str(timeout),
        "-A", UA,
        "-H", f"Referer: {referer}",
        "-H", "Accept: */*",
        "-H", "Accept-Language: zh-CN,zh;q=0.9",
        url,
    ]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout + 8)
    if p.returncode != 0:
        raise RuntimeError(f"curl exit {p.returncode}: {p.stderr.decode('utf-8', 'ignore')[:160]}")
    if not p.stdout.strip():
        raise RuntimeError("空响应")
    return p.stdout


def _get_json(url: str, timeout=18, referer="https://gu.qq.com/", encoding="utf-8"):
    last = None
    for i in range(2):
        if _has_curl:
            try:
                return json.loads(decode_auto(_curl_bytes(url, timeout, referer), encoding).strip())
            except Exception as e:                       # noqa: BLE001
                last = e
        try:
            r = _session.get(url, timeout=15, headers={"Referer": referer})
            r.raise_for_status()
            r.encoding = encoding
            return r.json()
        except Exception as e:                           # noqa: BLE001
            last = e
        time.sleep(0.4)
    raise RuntimeError(str(last) if last else "请求失败")


def _get_text(url: str, timeout=18, referer="https://finance.sina.com.cn/", encoding="utf-8"):
    last = None
    for i in range(2):
        if _has_curl:
            try:
                return decode_auto(_curl_bytes(url, timeout, referer), encoding)
            except Exception as e:                       # noqa: BLE001
                last = e
        try:
            r = _session.get(url, timeout=15, headers={"Referer": referer})
            r.raise_for_status()
            r.encoding = encoding
            return r.text
        except Exception as e:                           # noqa: BLE001
            last = e
        time.sleep(0.4)
    raise RuntimeError(str(last) if last else "请求失败")


# ---------------------------------------------------------------- 代码工具
def tx_symbol(code: str):
    """代码 → 腾讯格式：sh600519 / sz000001 / bj430047 / hk00700"""
    s = code.strip().lower()
    if s.startswith("hk"):
        digits = re.sub(r"\D", "", s)[-5:]
        return "hk" + digits.zfill(5)
    digits = re.sub(r"\D", "", s)[-6:]
    if digits.startswith(("60", "68", "51", "58", "56", "50", "11", "000001", "000688")):
        return "sh" + digits
    if digits.startswith(("8", "4", "9")):
        return "bj" + digits
    return "sz" + digits


def sina_symbol(code: str):
    return tx_symbol(code)          # 新浪与腾讯前缀一致


# ---------------------------------------------------------------- 取K线
def pick_klines(obj, sym, prefer):
    d = (obj.get("data") or {}).get(sym) or {}
    for k in prefer:
        v = d.get(k)
        if isinstance(v, list) and v and isinstance(v[0], list):
            return v
    for k, v in d.items():
        if isinstance(v, list) and v and isinstance(v[0], list):
            return v
    return []


def fmt_time(t):
    """腾讯分钟线返回 202609031342 / 20260903，统一成可读格式"""
    t = str(t).strip()
    if re.fullmatch(r"\d{12}", t):
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}"
    if re.fullmatch(r"\d{8}", t):
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]}"
    return t


def norm_tx(rows):
    """腾讯: [date, open, close, high, low, volume, amount, ...]"""
    out = []
    for r in rows:
        try:
            amt = 0.0
            if len(r) > 6 and isinstance(r[6], (int, float)):
                amt = float(r[6])
            out.append({
                "t": fmt_time(r[0]),
                "o": float(r[1]), "c": float(r[2]),
                "h": float(r[3]), "l": float(r[4]),
                "v": float(r[5]) if len(r) > 5 else 0.0,
                "amt": amt, "pct": 0.0,
            })
        except (ValueError, TypeError, IndexError):
            continue
    return out


def norm_sina(rows):
    """新浪: {day, open, high, low, close, volume, amount}"""
    out = []
    for r in rows:
        try:
            out.append({
                "t": r["day"],
                "o": float(r["open"]), "h": float(r["high"]),
                "l": float(r["low"]), "c": float(r["close"]),
                "v": float(r.get("volume") or 0),
                "amt": float(r.get("amount") or 0),
                "pct": 0.0,
            })
        except (ValueError, TypeError, KeyError):
            continue
    return out


def fetch_tx(sym, period, fqt, limit):
    p = PERIODS[period]
    if period in MINUTE_PERIODS:
        url = (f"https://ifzq.gtimg.cn/appstock/app/kline/mkline"
               f"?param={sym},{p['tx']},,{min(limit, 320)}")
        obj = _get_json(url)
        rows = pick_klines(obj, sym, [p["tx"]])
    else:
        fq = {1: "qfq", 2: "hfq", 0: ""}.get(fqt, "qfq")
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={sym},{p['tx']},,,{min(limit, TX_MAX)},{fq}")
        obj = _get_json(url)
        rows = pick_klines(obj, sym, [fq + p["tx"], p["tx"]])
    return norm_tx(rows)


def fetch_sina(sym, period, limit):
    scale = PERIODS[period].get("sina")
    if not scale:
        return []
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={sym}&scale={scale}&ma=no&datalen={limit}")
    txt = _get_text(url)
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return []
    try:
        rows = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return norm_sina(rows)


# ---------------------------------------------------------------- 周期聚合
def period_key(d: str, mode: str) -> str:
    """日线 → 周/月/季/年的分组键（周以周一为界）"""
    y, mth, day = int(d[:4]), int(d[5:7]), int(d[8:10])
    if mode == "week":
        mon = date(y, mth, day) - timedelta(days=date(y, mth, day).weekday())
        return mon.isoformat()
    if mode == "month":
        return f"{y:04d}-{mth:02d}"
    if mode == "quarter":
        return f"{y:04d}-Q{(mth - 1) // 3 + 1}"
    return f"{y:04d}"


def aggregate(bars, mode):
    """把日线聚合为周/月/季/年线"""
    out = []
    for b in bars:
        d = b["t"][:10]
        key = period_key(d, mode)
        if out and out[-1]["_k"] == key:
            c = out[-1]
            c["h"] = max(c["h"], b["h"]); c["l"] = min(c["l"], b["l"])
            c["c"] = b["c"]; c["v"] += b["v"]; c["amt"] += b["amt"]
            c["t"] = d
        else:
            out.append({"_k": key, "t": d, "o": b["o"], "c": b["c"],
                        "h": b["h"], "l": b["l"], "v": b["v"], "amt": b["amt"], "pct": 0.0})
    for c in out:
        c.pop("_k", None)
    return out


# ---------------------------------------------------------------- 接口
@app.get("/api/search")
def search(kw: str = Query(..., min_length=1), limit: int = 8):
    out, seen = [], set()
    MKT_NAME = {"hk": "港股", "sh": "沪", "sz": "深", "bj": "京"}

    def add(code, name, secid):
        if not code or code in seen or not name:
            return False
        seen.add(code)
        mkt = secid[:2].lower()
        out.append({
            "code": code, "name": name, "secid": secid,
            "type": ("指数" if mkt == "sh" and code.startswith("000")
                     else MKT_NAME.get(mkt, "")),
        })
        return True

    # 1) 腾讯 smartbox 优先（含港股、代码精确命中率高，GBK 编码）
    try:
        url = f"https://smartbox.gtimg.cn/s3/?q={quote(kw)}&t=all"
        txt = _get_text(url, referer="https://gu.qq.com/", encoding="gbk")
        m = re.search(r'v_hint="(.*?)"', txt, re.S)
        if m and m.group(1):
            for seg in m.group(1).split(";"):
                p = [x for x in seg.split("~") if x]
                # p[0]=市场代码(sh/sz/bj/hk) p[1]=代码 p[2]=名称
                if len(p) < 3 or not re.fullmatch(r"\d{5,6}", p[1]):
                    continue
                if add(p[1], p[2], p[0] + p[1]) and len(out) >= limit:
                    break
    except Exception:                                   # noqa: BLE001
        pass

    # 2) 新浪补充（A股中文名/拼音模糊搜索更全，GBK 编码）
    if len(out) < limit:
        try:
            url = ("https://suggest3.sinajs.cn/suggest/type=11,12,15,16,21,22,31,41"
                   f"&key={quote(kw)}")
            txt = _get_text(url, encoding="gbk")
            m = re.search(r'"(.*)"', txt, re.S)
            if m:
                for seg in m.group(1).split(";"):
                    parts = [x.strip() for x in seg.split(",")]
                    if len(parts) < 5:
                        continue
                    code = parts[2]
                    name = parts[4] or parts[0]
                    if not re.fullmatch(r"\d{6}", code):
                        continue
                    mkt = parts[3] if parts[3].startswith(("sh", "sz", "bj")) else tx_symbol(code)
                    if add(code, name, mkt) and len(out) >= limit:
                        break
        except Exception:                               # noqa: BLE001
            pass

    return JSONResponse({"list": out[:limit]})


@app.get("/api/kline")
def kline(
    code: str = Query(..., description="6位代码，或带市场前缀如 hk00700"),
    period: str = Query("day"),
    fqt: int = Query(1, description="0不复权 1前复权 2后复权"),
    limit: int = Query(600, ge=60, le=3000),
):
    if period not in PERIODS:
        raise HTTPException(status_code=400, detail=f"不支持的周期: {period}")

    # 支持带市场前缀：hk00700 / sh600519 / sz000001 / bj430047
    raw = (code or "").strip()
    m = re.match(r"^(hk|sh|sz|bj)\d{4,6}$", raw, re.I)
    if m:
        sym = raw.lower()
        code_disp = re.sub(r"\D", "", sym)            # 回显用：hk00700 或 600519
    else:
        code_disp = re.sub(r"\D", "", raw)[-6:]
        if not re.fullmatch(r"\d{6}", code_disp):
            raise HTTPException(status_code=400, detail="代码格式不正确，请输入6位数字或 hk+5位港股代码")
        sym = tx_symbol(code_disp)

    # ---- 缓存命中 ----
    cache_key = f"{sym}:{period}:{fqt}:{limit}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["t"] < _CACHE_TTL:
        return JSONResponse(cached["data"])
    bars, src = [], ""
    note = ""

    def healthy(bs):
        """复权数据异常时会出现非正价格，需要及时发现并回退"""
        return bool(bs) and min(b["l"] for b in bs) > 0

    def get_day(fq, want_long=False):
        """日线。不复权优先新浪（最长 1500 根 ≈ 6 年），复权走腾讯（640 根）"""
        if fq == 0 and want_long:
            b = fetch_sina(sym, "day", 1500)
            if healthy(b) and len(b) >= 100:
                return b, "sina-day"
        b = fetch_tx(sym, "day", fq, TX_MAX)
        if healthy(b):
            return b, "tx-day"
        b = fetch_sina(sym, "day", 1500)
        return b, "sina-day-fallback"

    try:
        if period in MINUTE_PERIODS:
            bars = fetch_tx(sym, period, fqt, limit)
            src = "tx"
            if len(bars) < 30:
                bars = fetch_sina(sym, period, limit)
                src = "sina"
            if fqt != 0:
                note = "分钟线不支持复权，已按不复权展示"

        elif period == "day":
            bars, src = get_day(fqt)

        elif period in ("week", "month"):
            if fqt == 0:
                # 不复权可直接取长历史
                bars = fetch_tx(sym, period, fqt, limit)
                src = "tx"
                if not healthy(bars) or len(bars) < 30:
                    bars = fetch_sina(sym, period, limit)
                    src = "sina"
            else:
                # 腾讯的周/月「前复权」序列存在错误（会出现负价格），
                # 因此复权场景统一由前复权日线自行聚合，保证价格自洽。
                base, s0 = get_day(fqt)
                bars = aggregate(base, period)
                src = s0 + "+agg"

        else:                                            # quarter / year
            base, s0 = get_day(fqt, want_long=(fqt == 0))
            bars = aggregate(base, period)
            src = s0 + "+agg"

    except Exception as e:                              # noqa: BLE001
        try:
            bars = fetch_sina(sym, period, limit)
            src = "sina-fallback"
        except Exception as e2:                         # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"行情获取失败：{e2}")

    if bars and not healthy(bars):
        # 最后一道防线：价格异常则退回不复权
        try:
            alt = fetch_sina(sym, period, limit) if period not in ("quarter", "year") else []
            if healthy(alt):
                bars, src, note = alt, "sina", "复权数据异常，已自动切换为不复权"
        except Exception:                               # noqa: BLE001
            pass

    if not bars:
        raise HTTPException(status_code=404, detail="未取到K线数据，请检查代码或周期")

    # 涨跌幅
    for i, b in enumerate(bars):
        prev = bars[i - 1]["c"] if i else b["o"]
        b["pct"] = round((b["c"] - prev) / prev * 100, 3) if prev else 0.0

    name = code_disp
    try:
        txt = _get_text(f"https://qt.gtimg.cn/q={sym}", timeout=8,
                        referer="https://gu.qq.com/", encoding="gbk")
        parts = txt.split("~")
        if len(parts) > 2 and parts[1]:
            name = parts[1].strip()
    except Exception:                                   # noqa: BLE001
        pass

    payload = {
        "code": code_disp, "name": name, "symbol": sym,
        "period": period,
        "periodLabel": PERIODS[period]["label"],
        "fqt": fqt, "source": src, "count": len(bars),
        "note": note,
        "bars": bars,
    }
    _cache[cache_key] = {"data": payload, "t": time.time()}
    return JSONResponse(payload)


@app.get("/api/periods")
def periods():
    return JSONResponse({"periods": [{"key": k, "label": v["label"]} for k, v in PERIODS.items()]})


# ---------------------------------------------------------------- 静态
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("=" * 54)
    print("  缠论分析 Web 服务已启动")
    print("  访问 http://127.0.0.1:8770")
    print("=" * 54)
    uvicorn.run(app, host="127.0.0.1", port=8770, log_level="warning")
