#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
樂透路子圖資料庫
================
抓取六種彩券近十年開獎紀錄，建立本機 SQLite 資料庫，並產生離線可看的路子圖網頁。

彩種與規則
----------
  台灣今彩539    1-39 選 5      總和 15-185   100 和 / 99↓小 / 101↑大
  加州 Fantasy 5 1-39 選 5      總和 15-185   同上
  台灣大樂透     1-49 選 6+1    6球 21-279    150 和 / 149↓小 / 151↑大
                                7球 28-322    175 和 / 174↓小 / 176↑大
  香港六合彩     1-49 選 6+1    同大樂透
  台灣三星彩     3 位 0-9       總和 0-27     0-13 小 / 14-27 大（無和）
  台灣四星彩     4 位 0-9       總和 0-36     0-17 小 / 18 和 / 19-36 大

  單雙一律看總和的奇偶。單=紅、雙=藍、大=紅、小=藍、和=綠。

用法
----
  python lottery_db.py                 # 抓近 10 年並產生網頁
  python lottery_db.py --years 5       # 只抓近 5 年
  python lottery_db.py --only tw539    # 只抓單一彩種
  python lottery_db.py --probe         # 只探測資料來源，不抓取
  python lottery_db.py --html          # 不抓取，只用現有資料庫重新產生網頁
  python lottery_db.py --stats         # 只看資料庫狀態
"""

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "lottery.db")
HTML = os.path.join(HERE, "樂透路子圖.html")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# ── 彩種設定 ──────────────────────────────────────────────
# kind: pick  = 從 1..pool 選 n 個（可能含特別號）
#       digit = d 位獨立 0-9 數字
GAMES = {
    "tw539": dict(
        name="台灣今彩539", short="539", src="taiwan", ep="Daily539Result",
        kind="pick", pool=39, n_main=5, has_special=False,
        charts=[("大小", "main", "bs"), ("單雙", "main", "oe")],
    ),
    "ca_f5": dict(
        name="加州 Fantasy 5", short="加州F5", src="calottery", ep=None,
        kind="pick", pool=39, n_main=5, has_special=False,
        charts=[("大小", "main", "bs"), ("單雙", "main", "oe")],
    ),
    "tw649": dict(
        name="台灣大樂透", short="大樂透", src="taiwan", ep="Lotto649Result",
        kind="pick", pool=49, n_main=6, has_special=True,
        charts=[("大小 6球", "main", "bs"), ("大小 7球", "all", "bs"),
                ("單雙 6球", "main", "oe"), ("單雙 7球", "all", "oe")],
    ),
    "hk6": dict(
        name="香港六合彩", short="六合彩", src="hkjc", ep=None,
        kind="pick", pool=49, n_main=6, has_special=True,
        charts=[("大小 6球", "main", "bs"), ("大小 7球", "all", "bs"),
                ("單雙 6球", "main", "oe"), ("單雙 7球", "all", "oe")],
    ),
    "tw3d": dict(
        name="台灣三星彩", short="三星彩", src="taiwan", ep="3DResult",
        kind="digit", digits=3, has_special=False,
        charts=[("大小", "main", "bs"), ("單雙", "main", "oe")],
    ),
    "tw4d": dict(
        name="台灣四星彩", short="四星彩", src="taiwan", ep="4DResult",
        kind="digit", digits=4, has_special=False,
        charts=[("大小", "main", "bs"), ("單雙", "main", "oe")],
    ),
}


def thresholds(g, scope):
    """回傳 (小上限, 和值 or None, 大下限)"""
    if g["kind"] == "digit":
        d = g["digits"]
        lo, hi = 0, 9 * d
        mid = (lo + hi) / 2
        if float(mid).is_integer():
            m = int(mid)
            return m - 1, m, m + 1
        return int(mid - 0.5), None, int(mid + 0.5)
    n = g["n_main"] + (1 if (scope == "all" and g["has_special"]) else 0)
    lo = sum(range(1, n + 1))
    hi = sum(range(g["pool"] - n + 1, g["pool"] + 1))
    mid = (lo + hi) / 2
    if float(mid).is_integer():
        m = int(mid)
        return m - 1, m, m + 1
    return int(mid - 0.5), None, int(mid + 0.5)


SCHEMA = """
CREATE TABLE IF NOT EXISTS draws (
    game       TEXT NOT NULL,
    draw_id    TEXT NOT NULL,
    draw_date  TEXT NOT NULL,
    numbers    TEXT NOT NULL,      -- JSON 陣列（主號，已排序或依開出順序）
    special    INTEGER,            -- 特別號，無則 NULL
    sum_main   INTEGER NOT NULL,
    sum_all    INTEGER,            -- 含特別號；無特別號則等同 sum_main
    PRIMARY KEY (game, draw_id)
);
CREATE INDEX IF NOT EXISTS ix_draws ON draws(game, draw_date);
-- 同一彩種同一天只會開一次獎。有了這個唯一索引，
-- 即使先後從 pilio 與官方 API 收到同一期（期別編號不同），也只會保留一筆。
CREATE UNIQUE INDEX IF NOT EXISTS ux_draws_day ON draws(game, draw_date);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, val TEXT);
"""


# ─────────────────────────── 網路 ───────────────────────────

INSECURE = False        # --insecure 時為 True
RECENT = False          # --recent：只抓最近的資料，供每日自動更新使用

# 每個彩種這一次抓取的結果，會一併寫進 status.json。
# 用意：雲端執行的畫面訊息在 GitHub Actions 的記錄裡，從外面看不到，
# 先前只能靠猜「到底是來源沒更新、還是雲端抓不到」。寫進狀態檔之後，
# 打開網址就能直接看到每個來源成功與否、抓到幾期、最新是哪一天。
FETCH_REPORT = {}
_CTX = None
_CTX_NOTE = ""


def ssl_context():
    """Windows 上 Python 常缺根憑證，這裡依序嘗試最可靠的來源。"""
    global _CTX, _CTX_NOTE
    if _CTX is not None:
        return _CTX
    import ssl
    if INSECURE:
        _CTX = ssl._create_unverified_context()
        _CTX_NOTE = "不驗證憑證（--insecure）"
        return _CTX
    # 1) certifi（最可靠，pip 一行就有）
    try:
        import certifi
        _CTX = ssl.create_default_context(cafile=certifi.where())
        _CTX_NOTE = f"certifi ({certifi.where()})"
        return _CTX
    except Exception:
        pass
    # 2) Windows 系統憑證庫
    try:
        ctx = ssl.create_default_context()
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
        _CTX = ctx
        _CTX_NOTE = "系統預設憑證庫"
        return _CTX
    except Exception:
        pass
    _CTX = ssl.create_default_context()
    _CTX_NOTE = "Python 預設"
    return _CTX


def http(url, retries=3, timeout=45):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json,text/plain,*/*"})
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl_context()) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                raise RuntimeError(
                    "SSL 憑證驗證失敗。請執行「修復憑證.bat」，"
                    "或用 --insecure 參數略過驗證。")
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{last}")


def hjson(url, **kw):
    return json.loads(http(url, **kw))


# ────────────────────── 各來源抓取器 ──────────────────────

def fetch_taiwan(gid, g, years):
    """台灣彩券官方 API，逐月抓取。"""
    today = dt.date.today()
    out, empty_streak, fail_streak = [], 0, 0
    months = 2 if RECENT else years * 12
    for k in range(months):
        y, m = today.year, today.month - k
        while m <= 0:
            m += 12
            y -= 1
        url = (f"https://api.taiwanlottery.com/TLCAPIWeB/Lottery/{g['ep']}"
               f"?month={y}-{m:02d}&pageNum=1&pageSize=50")
        try:
            j = hjson(url)
            fail_streak = 0
        except Exception as e:
            fail_streak += 1
            print(f"      {y}-{m:02d} 失敗 {e}")
            if fail_streak >= 6:
                print(f"      連續 {fail_streak} 次失敗，判定為被限流，中止此彩種。")
                print(f"      已取得 {len(out)} 期，稍後再執行即可補齊。")
                break
            time.sleep(3 * fail_streak)      # 退讓，給對方喘息
            continue
        c = j.get("content") or {}
        arr = None
        for key, v in c.items():
            if isinstance(v, list):
                arr = v
                break
        if not arr:
            empty_streak += 1
            if empty_streak >= 6:
                break          # 連續半年無資料，視為已到起點
            continue
        empty_streak = 0
        for it in arr:
            nums = it.get("drawNumberSize") or it.get("drawNumberAppear") or []
            if not nums:
                continue
            if g["kind"] == "digit":
                main, sp = list(nums), None
            elif g["has_special"]:
                main, sp = list(nums[:g["n_main"]]), nums[g["n_main"]] if len(nums) > g["n_main"] else None
            else:
                main, sp = list(nums[:g["n_main"]]), None
            out.append(mkrow(gid, str(it.get("period")),
                             str(it.get("lotteryDate", ""))[:10], main, sp))
        time.sleep(0.15)
    return out


# ── HTML 解析共用工具 ──────────────────────────────────
# 這幾個是所有網頁解析器都會用到的東西，缺一個就全部掛掉。
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t　]+")


def strip_tags(html):
    """把 HTML 變成純文字，並在列與欄的邊界留下換行／空白。"""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)</(tr|div|p|li|table)>", "\n", h)
    h = re.sub(r"(?i)</t[dh]>", " ", h)
    h = _TAG.sub(" ", h)
    h = (h.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">"))
    return "\n".join(_WS.sub(" ", ln).strip() for ln in h.split("\n"))


def _mk(gid, did, date, main, sp):
    return mkrow(gid, did, date, sorted(main), sp)


# 「(?<!\d)」用來擋掉 下期2026/07/28(二) 這種會被誤判成 26/07 28(二) 的字串
_PILIO_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})\s*(\d{2})\s*\([日一二三四五六]\)")


def parse_pilio(text, gid, n_main=6):
    """pilio 的號碼列表（539／大樂透／六合彩共用格式）。

    以「日期出現的位置」把整頁切成一段一段，每段代表一期。這樣不管欄位
    之間有沒有換行都能正確配對，而且特別號的搜尋範圍被下一個日期夾住，
    不會抓到下一期的號碼。
    """
    nums_re = re.compile(r"(?:\d{1,2}\s*,\s*){%d}\d{1,2}" % (n_main - 1))
    sp_re = re.compile(r"(?<![\d/])(\d{1,2})(?![\d/])")
    dates = [m for m in _PILIO_DATE.finditer(text)
             if 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31]
    out = []
    for i, dm in enumerate(dates):
        seg = text[dm.end(): dates[i + 1].start() if i + 1 < len(dates) else len(text)]
        nm = nums_re.search(seg)
        if not nm:
            continue
        try:
            main = [int(x) for x in re.sub(r"\s", "", nm.group(0)).split(",")]
        except Exception:
            continue
        if len(main) != n_main or not all(1 <= v <= 49 for v in main):
            continue
        sp = None
        sm = sp_re.search(seg[nm.end(): nm.end() + 40])
        if sm and 1 <= int(sm.group(1)) <= 49:
            sp = int(sm.group(1))
        mm, dd, yy = dm.groups()
        date = f"20{yy}-{int(mm):02d}-{int(dd):02d}"
        out.append(_mk(gid, date, date, main, sp))
    return out


# ── 台灣彩種的「快速來源」──────────────────────────────
# 官方 API 的回傳含中獎注數與獎金，要等銷售對帳才會出現，常比開球慢一兩個小時。
# pilio 只放號碼，公布快得多。所以先用 pilio 補上最新幾期，再讓官方 API 覆蓋
# （官方那筆有正式期別，會依「同一天只留一筆」的規則取代 pilio 的暫時紀錄）。
PILIO_TW = {
    "tw539": ("https://www.pilio.idv.tw/lto539/list.asp", "pick"),
    "tw649": ("https://www.pilio.idv.tw/ltobig/list.asp", "pick"),
    "tw3d":  ("https://www.pilio.idv.tw/lto/list3.asp", "digit"),
    "tw4d":  ("https://www.pilio.idv.tw/lto/list4.asp", "digit"),
}


def parse_pilio_digit(text, gid, ndigits):
    """三星彩／四星彩：號碼是空格分隔的單碼，後面還跟著「..(開N次)」，
    那個 N 不可以被當成獎號，所以每段先切到 '..' 為止。"""
    dates = [m for m in _PILIO_DATE.finditer(text)
             if 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31]
    out = []
    for i, dm in enumerate(dates):
        seg = text[dm.end(): dates[i + 1].start() if i + 1 < len(dates) else len(text)]
        seg = seg.split("..")[0][:60]
        digs = re.findall(r"(?<![\d])(\d)(?![\d])", seg)
        if len(digs) < ndigits:
            continue
        main = [int(x) for x in digs[:ndigits]]
        mm, dd, yy = dm.groups()
        date = f"20{yy}-{int(mm):02d}-{int(dd):02d}"
        out.append(mkrow(gid, date, date, main, None))
    return out


def fetch_pilio_tw(gid, g, pages=2):
    url, kind = PILIO_TW[gid]
    out = []
    for p in range(1, pages + 1):
        try:
            txt = strip_tags(http(f"{url}?indexpage={p}&orderby=new",
                                  retries=2, timeout=35))
        except Exception as e:
            print(f"      pilio 第 {p} 頁失敗 {e}")
            break
        rows = (parse_pilio_digit(txt, gid, g["digits"]) if kind == "digit"
                else parse_pilio(txt, gid, g["n_main"]))
        if not rows:
            break
        out += rows
        time.sleep(0.2)
    if out:
        newest = max(r["draw_date"] for r in out)
        print(f"      pilio 取得 {len(out)} 期（最新 {newest}）")
    return out


def fetch_tw(gid, g, years):
    """台灣四款彩種。

    每日更新（--recent）只用 pilio：它公布得比官方快一兩個小時，
    而且只要翻一頁，不會被限流。官方 API 的回傳要等銷售對帳才會出現，
    當日更新根本等不到，所以日常路徑完全不碰它。

    建立完整歷史（--years N）時才用官方 API，因為它有正式期別編號、
    而且一次能拿一整個月。
    """
    if RECENT:
        try:
            rows = (fetch_pilio_tw(gid, g, pages=2) if gid in PILIO_TW
                    else fetch_taiwan(gid, g, years))
        except Exception as e:
            print(f"      主來源失敗：{e}")
            rows = []
        rows = with_lw_backup(rows, gid, g, years)
        if not rows:
            raise RuntimeError("pilio 與樂透王都取不到資料")
        return rows

    rows = fetch_taiwan(gid, g, years)
    if gid in PILIO_TW:
        try:
            rows += fetch_pilio_tw(gid, g, pages=1)   # 補最新幾期
        except Exception as e:
            print(f"      pilio 補最新失敗（{e}），略過")
    return rows


def fetch_pilio_hk(gid, g, years):
    cutoff = ((dt.date.today() - dt.timedelta(days=45)).isoformat() if RECENT
              else (dt.date.today() - dt.timedelta(days=365 * years + 40)).isoformat())
    seen, out = set(), []
    for page in range(1, 4 if RECENT else 220):
        url = f"https://www.pilio.idv.tw/ltohk/list.asp?indexpage={page}&orderby=new"
        try:
            txt = strip_tags(http(url, retries=2, timeout=40))
        except Exception as e:
            print(f"      第 {page} 頁失敗 {e}")
            break
        rows = parse_pilio(txt, gid, g["n_main"])
        if not rows:
            break
        newest = min(r["draw_date"] for r in rows)
        for r in rows:
            if r["draw_id"] not in seen:
                seen.add(r["draw_id"])
                out.append(r)
        if page % 15 == 0:
            print(f"      第 {page} 頁 … 累計 {len(out):,} 期（最舊 {newest}）")
        if newest < cutoff:
            break
        time.sleep(0.25)
    out = [r for r in out if r["draw_date"] >= cutoff]
    if out:
        miss = sum(1 for r in out if r["special"] is None)
        cov = (1 - miss / len(out)) * 100
        print(f"      特別號覆蓋率 {cov:.1f}%" + (
            f"　⚠ 有 {miss:,} 期缺特別號，7 球的兩張路子圖會不完整" if miss else "　✔"))
    return out


# ── 樂透王 lotterywang.com：六個彩種全都有，當作共同備援 ──
#
# 為什麼要有備援：先前每個彩種都只靠單一網站，那個網站改版、擋 IP、
# 或當天沒更新，該彩種就整個停擺，只能等人手動處理。樂透王六款全收，
# 版型也一致，剛好可以當所有彩種的第二來源。
# 頁面版型（六款相同）：
#     2026.08.01 (六)          ← 日期
#     26｜083 期               ← 期別（六合彩會有 ｜ 分隔）
#     01 07 16 22 32 37 23     ← 號碼，有特別號的話排最後
# 而且每一期會重複輸出兩次（響應式版型的兩套 DOM），要去重。
LW_PATH = {"tw539": "lotto539", "tw649": "lotto649", "tw3d": "lotto3d",
           "tw4d": "lotto4d", "hk6": "lottoHK", "ca_f5": "lottoCA5"}

_LW_DATE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\s*\([日一二三四五六]\)")


def parse_lw(text, gid, g):
    """通用解析：先用日期把整頁切段，再在每段裡取「期」後面的號碼。

    用切段而不是一條大正規表示式，是因為日期與號碼之間的空白、
    期別的分隔符號各款不一樣，硬寫成單一規則很容易漏掉某一款。
    """
    if g.get("kind") == "digit":
        need, lo, hi = g["digits"], 0, 9
    else:
        need = g["n_main"] + (1 if g.get("has_special") else 0)
        lo, hi = 1, g["pool"]

    marks = list(_LW_DATE.finditer(text))
    out, seen = [], set()
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        seg = text[m.end():end]
        k = seg.find("期")
        if k < 0:
            continue
        pid = re.sub(r"\D", "", seg[:k])[-12:]      # 期別只留數字
        toks = re.findall(r"\d{1,2}", seg[k + 1:])  # 「期」後面緊接著就是號碼
        if len(toks) < need:
            continue
        v = [int(x) for x in toks[:need]]
        if not all(lo <= x <= hi for x in v):
            continue
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        day = f"{y}-{mo:02d}-{d:02d}"
        if day in seen:                              # 同一期的第二份 DOM
            continue
        seen.add(day)
        if g.get("kind") == "digit":
            # 三星彩／四星彩是「個十百千」，順序就是答案的一部分，
            # 千萬不能像其他彩種那樣排序（會把 4 9 1 變成 1 4 9）。
            out.append(mkrow(gid, pid, day, v, None))
        elif g.get("has_special"):
            out.append(_mk(gid, pid, day, v[:-1], v[-1]))
        else:
            out.append(_mk(gid, pid, day, v, None))
    return out


def parse_lotterywang(text, gid, n_main=5):
    """加州 Fantasy 5 的舊介面，內部走上面的通用解析器。

    保留這個名字是為了讓 selftest 測到的就是真正在跑的程式碼——
    先前有過「測試測的是假的解析器，真的那支壞了卻沒人發現」的教訓。
    """
    return parse_lw(text, gid, {"kind": "pick", "n_main": n_main,
                                "pool": 39, "has_special": False})


def fetch_lw(gid, g, years):
    """從樂透王抓。每日更新只翻首頁（最近十期，一次連線就夠）。"""
    path = LW_PATH.get(gid)
    if not path:
        return []
    if RECENT:
        # 年份頁是行之有年、確定可用的路徑，優先用它。
        # 首頁只列最近十期、比較輕，但版型跟年份頁不完全一樣，
        # 曾經因此整個解析不到東西，所以只拿來當年份頁失敗時的備胎。
        y = dt.date.today().year
        for url in (f"https://www.lotterywang.com/{path}/year/{y}",
                    f"https://www.lotterywang.com/{path}"):
            try:
                rows = parse_lw(strip_tags(http(url, retries=2, timeout=45)), gid, g)
            except Exception as e:
                print(f"      樂透王 {url.rsplit('/', 1)[-1]} 失敗：{e}")
                continue
            if rows:
                return rows
            print(f"      樂透王 {url.rsplit('/', 1)[-1]} 解析不到資料，換下一個位址")
        return []

    out, miss, this_y = [], 0, dt.date.today().year
    for y in range(this_y, this_y - years - 1, -1):
        try:
            rows = parse_lw(strip_tags(
                http(f"https://www.lotterywang.com/{path}/year/{y}",
                     retries=2, timeout=50)), gid, g)
        except Exception as e:
            print(f"      樂透王 {y} 年失敗 {e}")
            rows = []
        if rows:
            out += rows
            miss = 0
        else:
            miss += 1
            if miss >= 2:
                break
        time.sleep(0.3)
    return out


def with_lw_backup(rows, gid, g, years):
    """把樂透王的結果併進來。

    回傳時備援排在前面、主來源排在後面：寫入是 INSERT OR REPLACE，
    後寫的會蓋前面的，這樣同一期就會以主來源的期別編號為準。
    """
    try:
        extra = fetch_lw(gid, g, years)
    except Exception as e:
        print(f"      樂透王備援失敗：{e}")
        return rows
    if not extra:
        return rows
    newest = max((r["draw_date"] for r in rows), default=None)
    more = [r for r in extra if newest is None or r["draw_date"] > newest]
    print(f"      樂透王備援 {len(extra):,} 期"
          + (f"，其中 {len(more)} 期比主來源新" if more else "（無更新的）"))
    return extra + rows


def fetch_ca_lotterywang(gid, g, years):
    """加州的舊名字，現在統一走通用的樂透王抓取。"""
    return fetch_lw(gid, g, years)


def fetch_hk(gid, g, years):
    """香港六合彩：只用 pilio。

    原本還留了 GitHub 鏡像當備援，但它從來沒成功過，反而害慘了自己——
    每日更新時 pilio 只會回傳約 18 期（45 天內），舊程式卻要求「超過 100 期
    才算成功」，於是把好好的資料丟掉、轉去試那個壞掉的鏡像。
    六合彩因此從來沒有被自動更新過。
    """
    try:
        rows = fetch_pilio_hk(gid, g, years)
    except Exception as e:
        print(f"      pilio 失敗：{e}")
        rows = []
    rows = with_lw_backup(rows, gid, g, years)
    if not rows:
        raise RuntimeError("pilio 與樂透王都取不到六合彩資料")
    return rows


def fetch_ca_official(gid, g, years):
    """加州官方 API（DrawGameId 10 = Fantasy 5）。

    期別編號跟樂透王完全一致（例如 11957 期兩邊都是同一期），
    但日期基準不同：官方記的是加州當地開獎日，樂透王記的是台灣公布日，
    剛好晚一天。這裡統一換算成台灣公布日，兩個來源才會合成同一筆，
    不會在路子圖上變成兩格。
    """
    # 每頁上限就是 20，要求更多它會直接回 null（不是錯誤碼，是空回應）。
    # 先前寫 60，於是雲端每次都拿到 null，加州因此永遠更新不到。
    PAGE = 20
    pages = 1 if RECENT else max(1, min(200, years * 370 // PAGE + 1))
    raw = []
    for pg in range(1, pages + 1):
        url = ("https://www.calottery.com/api/DrawGameApi/"
               f"DrawGamePastDrawResults/10/{pg}/{PAGE}?ts={int(time.time())}")
        d = json.loads(http(url, retries=2, timeout=45))
        if not isinstance(d, dict):
            if pg == 1:
                raise RuntimeError("官方 API 回傳空內容（每頁筆數超過上限時會這樣）")
            break
        got = d.get("PreviousDraws") or []
        if not got:
            break
        raw += got
        if len(got) < PAGE:
            break
        time.sleep(0.2)

    out = []
    for it in raw:
        wn = it.get("WinningNumbers") or {}
        try:
            main = [int(wn[k]["Number"]) for k in sorted(wn, key=lambda x: int(x))
                    if not wn[k].get("IsSpecial")]
        except Exception:
            continue
        day = (it.get("DrawDate") or "")[:10]
        if len(main) != g["n_main"] or len(day) != 10:
            continue
        try:
            tw = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
        except Exception:
            continue
        out.append(_mk(gid, str(it.get("DrawNumber") or ""), tw, main, None))
    return out


def fetch_ca(gid, g, years):
    """加州 Fantasy 5：樂透王為主，官方 API 為備援。

    這兩個來源剛好互補，所以兩個都留：
      樂透王    台灣連得到；但雲端主機（GitHub 美國機房）不一定連得到。
      官方 API  台灣 IP 會被 403；從美國機房反而正常。
    只要其中一邊活著就抓得到，不會再出現「本機更新得到、雲端永遠不更新」。
    """
    rows, why = [], []
    try:
        rows = fetch_lw(gid, g, years)
        if rows:
            print(f"      樂透王 {len(rows):,} 期"
                  f"（最新 {max(r['draw_date'] for r in rows)}）")
        else:
            why.append("樂透王：連得上但解析不到資料")
    except Exception as e:
        why.append(f"樂透王：{e}")
        print(f"      樂透王失敗：{e}")
    newest = max((r["draw_date"] for r in rows), default=None)

    try:
        off = fetch_ca_official(gid, g, years)
    except Exception as e:
        off = []
        why.append(f"官方：{e}")
        print(f"      官方 API 失敗：{e}")
    if off:
        extra = [r for r in off if newest is None or r["draw_date"] > newest]
        print(f"      官方 API {len(off):,} 期"
              + (f"，其中 {len(extra)} 期比樂透王還新" if extra else "（無更新的）"))
        rows += off

    if not rows:
        raise RuntimeError("兩個來源都失敗 — " + "；".join(why))
    return rows


FETCHERS = {"taiwan": fetch_tw, "calottery": fetch_ca, "hkjc": fetch_hk}


def mkrow(gid, draw_id, date, main, special):
    sm = sum(main)
    sa = sm + special if special is not None else sm
    return dict(game=gid, draw_id=draw_id, draw_date=date,
                numbers=json.dumps(main), special=special,
                sum_main=sm, sum_all=sa)


# ─────────────── 理論分布（用來檢驗開獎是否公正）───────────────

def dist_pick(pool, n):
    """從 1..pool 取 n 個相異數，總和的計數分布。"""
    dp = [defaultdict(int) for _ in range(n + 1)]
    dp[0][0] = 1
    for v in range(1, pool + 1):
        for c in range(min(n, v) - 1, -1, -1):
            for s, cnt in list(dp[c].items()):
                dp[c + 1][s + v] += cnt
    return dp[n]


def dist_digit(d):
    dp = {0: 1}
    for _ in range(d):
        nd = defaultdict(int)
        for s, c in dp.items():
            for v in range(10):
                nd[s + v] += c
        dp = nd
    return dp


def theory(g, scope):
    if g["kind"] == "digit":
        dd = dist_digit(g["digits"])
    else:
        n = g["n_main"] + (1 if (scope == "all" and g["has_special"]) else 0)
        dd = dist_pick(g["pool"], n)
    tot = sum(dd.values())
    lo_max, tie, hi_min = thresholds(g, scope)
    p_small = sum(c for s, c in dd.items() if s <= lo_max) / tot
    p_tie = (dd.get(tie, 0) / tot) if tie is not None else 0.0
    p_big = sum(c for s, c in dd.items() if s >= hi_min) / tot
    p_odd = sum(c for s, c in dd.items() if s % 2 == 1) / tot
    return dict(small=p_small, tie=p_tie, big=p_big, odd=p_odd, even=1 - p_odd)


# ─────────────────────────── 資料庫 ───────────────────────────

def connect():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def upsert(con, rows):
    """寫入開獎資料，並且防止不同來源的期別編號互相打架。

    這張表有兩個唯一性條件：主鍵是（彩種, 期別），另外還有（彩種, 日期）。
    不同來源對期別的編法不一樣（pilio 用日期當編號、官方與樂透王用正式期別），
    萬一某個來源給的期別編號在資料庫裡剛好屬於「另一天」，
    INSERT OR REPLACE 會同時撞到兩個索引，把那另外一天的資料一起刪掉。

    這不是假設：加了樂透王備援之後，539／三星／四星各無聲無息少了一筆。
    所以這裡先檢查，發現期別會撞到別天時，就沿用當天原本的編號。
    """
    if not rows:
        return 0

    games = {r["game"] for r in rows}
    qs = ",".join("?" * len(games))
    id_owner, day_id = {}, {}
    for g, did, day in con.execute(
            f"SELECT game, draw_id, draw_date FROM draws WHERE game IN ({qs})",
            tuple(games)):
        id_owner[(g, did)] = day
        day_id[(g, day)] = did

    seen, safe = set(), []
    for r in reversed(rows):                      # 同一批有重複時以最後一筆為準
        key = (r["game"], r["draw_date"])
        if key in seen:
            continue
        seen.add(key)
        owner = id_owner.get((r["game"], r["draw_id"]))
        if owner is not None and owner != r["draw_date"]:
            r = dict(r, draw_id=day_id.get(key, r["draw_date"]))
        safe.append(r)
    rows = list(reversed(safe))

    cols = ["game", "draw_id", "draw_date", "numbers", "special", "sum_main", "sum_all"]
    con.executemany(
        f"INSERT OR REPLACE INTO draws ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [[r[c] for c in cols] for r in rows])
    return len(rows)


def stats(con):
    print(f"\n  {'彩種':<16}{'期數':>8}   {'起':<12}{'迄':<12}")
    print("  " + "-" * 52)
    total = 0
    for gid, g in GAMES.items():
        r = con.execute("SELECT COUNT(*),MIN(draw_date),MAX(draw_date) FROM draws WHERE game=?",
                        (gid,)).fetchone()
        total += r[0]
        print(f"  {g['name']:<16}{r[0]:>8,}   {r[1] or '—':<12}{r[2] or '—':<12}")
    print("  " + "-" * 52)
    print(f"  {'合計':<16}{total:>8,}")
    return total


# ─────────────────────────── 產生網頁 ───────────────────────────

def build_html(con, force=False):
    # 安全鎖：如果這次能呈現的彩種數比上次少，代表抓取出了問題，
    # 寧可不動網頁，也不要把好好的網站蓋成殘缺版本。
    prev = con.execute("SELECT val FROM meta WHERE key='html_games'").fetchone()
    prev_n = int(prev[0]) if prev else 0

    payload = {}
    for gid, g in GAMES.items():
        rows = con.execute(
            "SELECT draw_id,draw_date,numbers,special,sum_main,sum_all "
            "FROM draws WHERE game=? ORDER BY draw_date, draw_id", (gid,)).fetchall()
        if not rows:
            continue
        th = {}
        scopes = {c[1] for c in g["charts"]}
        for sc in scopes:
            lo, tie, hi = thresholds(g, sc)
            th[sc] = dict(lo=lo, tie=tie, hi=hi, theory=theory(g, sc))
        payload[gid] = dict(
            name=g["name"], short=g["short"], kind=g["kind"],
            pool=g.get("pool"), digits=g.get("digits"),
            has_special=bool(g.get("has_special")),
            charts=[list(c) for c in g["charts"]], th=th,
            rows=[[r[0], r[1], json.loads(r[2]), r[3], r[4], r[5]] for r in rows],
        )
    if len(payload) < prev_n and not force:
        print("\n" + "!" * 62)
        print(f"  已中止：這次只有 {len(payload)} 個彩種有資料，上次是 {prev_n} 個。")
        print(f"  有資料的：{'、'.join(v['name'] for v in payload.values()) or '（無）'}")
        print("  這通常代表抓取被擋或網路異常，不是真的沒開獎。")
        print("  為避免把完整的網站蓋成殘缺版本，這次不重新產生網頁。")
        print("  確定要覆蓋請加參數 --force-html。")
        print("!" * 62)
        return False

    # 一律用台北時間。以前這裡用 datetime.now()（機器的本地時間），
    # 在自己電腦上剛好是台北時間看不出問題，但雲端主機是 UTC，
    # 網頁上就會顯示成 8 小時前，而且跟 status.json 的台北時間永遠對不起來。
    tp = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = HTML_TMPL.replace("/*__DATA__*/", data).replace(
        "__BUILT__", tp.strftime("%Y-%m-%d %H:%M"))
    with open(HTML, "w", encoding="utf-8") as f:
        f.write(html)
    con.execute("INSERT OR REPLACE INTO meta(key,val) VALUES('html_games',?)",
                (str(len(payload)),))
    con.commit()
    print(f"\n  已產生網頁：{HTML}")
    print(f"  收錄 {len(payload)} 個彩種：{'、'.join(v['name'] for v in payload.values())}")
    print(f"  檔案大小：{os.path.getsize(HTML) / 1048576:.2f} MB（資料已內嵌，離線可看）")

    # 狀態檔：純文字、極小，用來從外部確認「網頁裡實際裝了哪些資料」。
    # 網頁的資料藏在 <script> 裡，從外面看不到，出問題時很難判斷是資料沒進來
    # 還是瀏覽器快取。有了這個檔，一眼就知道。
    # 時間必須跟上面網頁裡的 __BUILT__ 完全同一個，否則前端會誤判成快取。
    # 是誰產生的：雲端排程還是使用者自己按？兩邊看到的網路環境不一樣
    # （例如樂透王從台灣連得到、從美國機房不一定），沒有這一欄就分不出
    # 「這份回報是哪一邊的」，先前為此判斷錯過好幾次。
    st = {"built_at_taipei": tp.strftime("%Y-%m-%d %H:%M:%S"),
          "built_by": "雲端" if os.environ.get("GITHUB_ACTIONS") else "本機",
          "games": {gid: {"name": v["name"],
                          "latest_date": v["rows"][-1][1],
                          "latest_numbers": v["rows"][-1][2],
                          "special": v["rows"][-1][3],
                          "count": len(v["rows"])}
                    for gid, v in payload.items()}}
    if FETCH_REPORT:
        st["fetch"] = FETCH_REPORT
    sp = os.path.join(os.path.dirname(HTML) or ".", "status.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    print(f"  狀態檔：{os.path.basename(sp)}")
    for gid, v in st["games"].items():
        print(f"    {v['name']:<16}{v['latest_date']}   {v['latest_numbers']}")
    return True


HTML_TMPL = r"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, must-revalidate">
<title>樂透路子圖</title>
<style>
:root{--blue:#1f4fd8;--red:#c8352b;--green:#14875a;--ink:#1c1c1e;--mute:#6f6f77;
 --line:#e3e3e8;--bg:#f7f6f4;--cell:28px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font-family:"Noto Sans TC","Microsoft JhengHei",-apple-system,"Segoe UI",sans-serif;
 font-size:15px;line-height:1.6}
.wrap{max-width:1240px;margin:0 auto;padding:24px 24px 90px;background:#fff;min-height:100vh;
 box-shadow:0 0 0 1px var(--line)}
/* 速報／未開累計的浮層 */
.mask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50;
 padding:18px;overflow:auto}
.mask.on{display:block}
.panel{max-width:680px;margin:0 auto;background:#fff;border-radius:12px;
 box-shadow:0 12px 40px rgba(0,0,0,.3)}
.ptop{display:flex;align-items:center;gap:8px;padding:12px 16px;
 border-bottom:1px solid var(--line);position:sticky;top:0;background:#fff;
 border-radius:12px 12px 0 0;flex-wrap:wrap}
#pbody{padding:16px}
.flashline{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
 padding:9px 0;border-bottom:1px solid var(--line)}
.flashline .k{color:var(--mute);font-size:12.5px;min-width:74px}
.pball{display:inline-flex;align-items:center;justify-content:center;
 width:34px;height:34px;border-radius:50%;background:var(--ink);color:#fff;
 font-weight:700;font-size:14px}
.pball.sp{background:var(--green)}
.pball.cold{background:#5a6f9e}
.pill{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12.5px;
 font-weight:700;color:#fff}
.pill.b{background:var(--red)}.pill.s{background:var(--blue)}
.pill.t{background:var(--green)}
.gaprow{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12.5px}
.gaprow .no{width:30px;height:30px;border-radius:50%;flex:0 0 auto;
 display:flex;align-items:center;justify-content:center;font-weight:700;
 background:#ececef;font-size:12.5px}
/* bw / bf 一定要是 block，之前寫成 inline 的 span，寬度完全不會生效，
   結果整排長條都是空的。*/
.gaprow .bw{display:block;flex:1;background:#eef0f4;border-radius:4px;height:16px}
.gaprow .bf{display:block;height:100%;border-radius:4px;background:#5a6f9e;min-width:2px}
.gaprow.hot .no{background:var(--red);color:#fff}
.gaprow.hot .bf{background:var(--red)}
.gaprow .nn{width:34px;text-align:right;color:var(--mute);font-weight:700}
table.gt{border-collapse:collapse;width:100%;font-size:13px}
table.gt th,table.gt td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left}
table.gt th{color:var(--mute);font-weight:600;cursor:pointer;white-space:nowrap}
table.gt tr.hot td{background:#fdf0ef;font-weight:700}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;
 padding-bottom:13px;border-bottom:3px solid var(--ink)}
h1{font-size:21px;margin:0;font-weight:700}
.tag{font-size:12px;padding:3px 9px;border-radius:99px;font-weight:600;
 background:#e4f2ea;color:#136b47;border:1px solid #a9d6c1}
.tabs{display:flex;gap:5px;border-bottom:1px solid var(--line);margin-top:16px;flex-wrap:wrap}
.tab{font:inherit;font-size:14px;padding:9px 16px;border:1px solid transparent;border-bottom:0;
 background:none;cursor:pointer;color:var(--mute);border-radius:7px 7px 0 0}
.tab.on{background:#fff;border-color:var(--line);color:var(--ink);font-weight:700;
 margin-bottom:-1px;box-shadow:0 -2px 0 var(--ink) inset}
/* 控制列：原本每個欄位都配一個文字標籤又留大間距，佔掉太多版面。
   改成緊湊排列，標籤直接寫進選項文字裡（例如「近 120 期」本身就看得懂）。*/
.bar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:8px 0 9px;
 border-bottom:1px solid var(--line);margin-bottom:14px}
select,.btn{font:inherit;font-size:12.5px;padding:4px 9px;border:1px solid #ccccd4;
 border-radius:6px;background:#fff;color:var(--ink);cursor:pointer;line-height:1.5}
.btn.sm{font-size:11.5px;padding:3px 8px}
.bar .sep{flex:1}
select:hover,.btn:hover{border-color:#9a9aa8}
label{font-size:12px;color:var(--mute)}
/* 未開累計與開獎明細並列；窄畫面自動疊成上下 */
.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:22px;
 align-items:start}
/* 大小與單雙並排；四張圖（大樂透、六合彩）就排成 2x2 */
.roads{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px}
.roads .road{margin-bottom:0}
.roads .side{flex:0 0 132px;padding:11px 12px}
.two h2{display:flex;align-items:center;gap:8px;margin:26px 0 8px}
.two .sub{font-size:11.5px;font-weight:400;color:var(--mute)}
@media(max-width:1000px){.two,.roads{grid-template-columns:minmax(0,1fr)}}
.road{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;
 margin-bottom:16px;background:#fff}
.road .side{flex:0 0 176px;background:#f4f3f0;border-right:1px solid var(--line);
 padding:13px 15px;display:flex;flex-direction:column;justify-content:center}
.side .ttl{font-size:15px;font-weight:700;margin-bottom:2px}
.side .sub{font-size:10.5px;color:var(--mute);margin-bottom:9px;line-height:1.45}
.side .cnt{font-size:16px;font-weight:700;display:flex;justify-content:space-between;
 align-items:baseline;gap:8px;line-height:1.55}
.side .cnt span:first-child{font-size:13.5px}
.side .pct{font-size:10.5px;color:var(--mute);font-weight:400;margin-top:8px;line-height:1.5}
.grid-scroll{flex:1;overflow-x:auto;padding:10px 8px}
.grid{display:grid;grid-template-rows:repeat(6,var(--cell));grid-auto-flow:column;
 grid-auto-columns:var(--cell)}
.gc{position:relative;border-right:1px solid #eff0f2;border-bottom:1px solid #eff0f2}
.mk{position:absolute;left:2px;top:2px;right:2px;bottom:2px;display:flex;align-items:center;
 justify-content:center;font-size:13.5px;font-weight:700;color:#fff;border-radius:5px;z-index:2;
 transition:transform .1s;cursor:default}
.mk:hover{transform:scale(1.22);z-index:9;box-shadow:0 3px 10px rgba(0,0,0,.25)}
.mk.b{background:var(--blue)}.mk.r{background:var(--red)}.mk.g{background:var(--green)}
body.hollow .mk{background:transparent;border:2.5px solid;border-radius:50%}
body.hollow .mk.b{color:var(--blue);border-color:var(--blue)}
body.hollow .mk.r{color:var(--red);border-color:var(--red)}
body.hollow .mk.g{color:var(--green);border-color:var(--green)}
.lnk{position:absolute;z-index:1;border-radius:2px}
.lnk.v{left:calc(50% - 2px);top:-4px;width:4px;height:10px}
.lnk.h{top:calc(50% - 2px);left:-4px;height:4px;width:10px}
.lnk.b{background:var(--blue)}.lnk.r{background:var(--red)}.lnk.g{background:var(--green)}
.turn{position:absolute;right:-1px;bottom:-1px;font-size:9px;color:#fff;
 background:rgba(0,0,0,.45);border-radius:3px;padding:0 2px;line-height:1.3;z-index:3}
.c-b{color:var(--blue)}.c-r{color:var(--red)}.c-g{color:var(--green)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--mute);
 margin:12px 0 0;padding:11px 14px;background:#f7f7f5;border-radius:6px}
.sw{display:inline-block;width:14px;height:14px;border-radius:4px;vertical-align:-2px;margin-right:4px}

/* ── 最新一期強調 ── */
.mk.newest{outline:3px solid #111;outline-offset:2px;z-index:8}
.mk.newest::after{content:"新";position:absolute;top:-15px;left:50%;transform:translateX(-50%);
 font-size:9px;font-weight:700;color:#fff;background:#111;border-radius:3px;padding:0 3px;
 line-height:1.5}

/* ── 頂部「現在的狀況」摘要 ── */
.now{border:2px solid var(--ink);border-radius:10px;padding:15px 18px;margin-bottom:18px;
 background:#fffdf7}
.now .hd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:12px;
 padding-bottom:10px;border-bottom:1px solid #e8e2cf}
.now .hd b{font-size:16px}
.now .hd .dt{font-size:13px;color:var(--mute)}
.now .balls .ball{width:32px;height:32px;font-size:14px;margin-right:6px}
.nrow{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:7px 0;font-size:13.5px}
.nrow+.nrow{border-top:1px dashed #e8e2cf}
.nrow .lb{flex:0 0 96px;font-weight:700;font-size:13px}
.nrow .st{flex:0 0 128px;font-size:12.5px;color:var(--mute)}
.nrow .st b{font-size:15px}
.strip{display:flex;gap:3px;flex-wrap:wrap}
.strip i{width:22px;height:22px;border-radius:4px;color:#fff;font-size:11.5px;font-weight:700;
 display:flex;align-items:center;justify-content:center;font-style:normal}
.strip i.b{background:var(--blue)}.strip i.r{background:var(--red)}.strip i.g{background:var(--green)}
.strip i:first-child{outline:2px solid #111;outline-offset:1px}
.arrowhint{font-size:11px;color:var(--mute);margin-left:2px}
h2{font-size:17px;margin:32px 0 10px;padding-left:11px;border-left:5px solid var(--ink)}
table{border-collapse:collapse;width:100%;font-size:13px}
th{background:#f2f1ee;text-align:left;padding:8px 11px;border-bottom:1px solid var(--line);
 font-weight:700;white-space:nowrap;font-size:12.5px}
td{padding:7px 11px;border-bottom:1px solid #f0f0ef}
tbody tr:hover{background:#f8fafe}
.num{text-align:right;font-variant-numeric:tabular-nums}
/* 開獎明細裡的號碼：原本是淺灰底＋淺灰字，在白底上幾乎看不見。
   改成深色實心圓，跟頁首那排球一致。*/
.ball{display:inline-flex;align-items:center;justify-content:center;
 width:27px;height:27px;margin-right:5px;border-radius:50%;
 background:#2b3550;color:#fff;font-weight:700;
 font-size:12.5px;font-variant-numeric:tabular-nums}
.ball.sp{background:#b8860b;color:#fff}
.fbox{border:1px solid var(--line);border-radius:9px;overflow:hidden;margin-top:10px}
.warn{background:#fff8e6;border:1px solid #ecd9a4;border-radius:9px;padding:14px 18px;
 font-size:13.5px;line-height:1.8;margin-bottom:18px}
.warn b{color:#8a6212}
.sig{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;white-space:nowrap}
.sig.ok{background:#e4f2ea;color:#136b47}
.sig.no{background:#fbe9e6;color:#a8392c}
.fbox{overflow-x:auto}

/* ══ 手機版面 ══ */
@media (max-width:760px){
  body{font-size:14px}
  .wrap{padding:14px 12px 60px;box-shadow:none}
  h1{font-size:18px}
  h2{font-size:15px;margin:24px 0 8px}
  header{padding-bottom:10px}
  .tab{padding:8px 12px;font-size:13px}
  .bar{gap:7px;padding:11px 0 12px}
  select,.btn{font-size:12.5px;padding:5px 9px}

  /* 資訊欄改成橫置在圖的上方，把整個寬度讓給路子圖 */
  .road{flex-direction:column}
  .road .side{flex:1 1 auto;border-right:0;border-bottom:1px solid var(--line);
    flex-direction:row;flex-wrap:wrap;align-items:baseline;gap:6px 14px;padding:10px 12px}
  .side .ttl{margin:0;flex:0 0 100%}
  .side .sub{margin:0;flex:0 0 100%;order:9}
  .side .cnt{display:inline-flex;justify-content:flex-start;gap:5px;font-size:15px}
  .side .pct{margin:0;flex:0 0 100%;order:10}
  .grid-scroll{padding:8px 4px}

  .now{padding:12px 13px}
  .now .hd{gap:6px}
  .now .hd b{font-size:15px}
  .nrow{gap:7px}
  .nrow .lb{flex:0 0 100%}
  .nrow .st{flex:0 0 100%}
  .strip i{width:20px;height:20px;font-size:11px}
  .arrowhint{flex:0 0 100%}

  .legend{gap:8px 14px;font-size:11.5px;padding:9px 11px}
  table{font-size:11.5px}
  th,td{padding:6px 7px}
  .ball{min-width:20px;padding:1px 4px;font-size:11px}
  .warn{padding:12px 13px;font-size:12.5px}
}
</style></head><body><div class="wrap">

<header><h1>樂透路子圖</h1>
<span class="tag" id="hdr"></span>
<span style="font-size:12px;color:var(--mute)">建立於 __BUILT__ · 資料已內嵌，離線可看</span></header>

<div class="tabs" id="tabs"></div>

<div class="bar">
  <select id="n"><option value="120">近 120 期</option><option value="200">近 200 期</option>
  <option value="400">近 400 期</option><option value="99999">全部期數</option></select>
  <select id="yr"><option value="">全部年份</option></select>
  <select id="dens">
    <option value="28">格子大</option><option value="22" selected>格子中</option>
    <option value="16">格子小</option><option value="12">格子最小</option></select>
  <button class="btn" id="style">實心方格</button>
  <button class="btn" id="lang">中文</button>
  <button class="btn" id="toend">跳到最新 →</button>
  <span class="sep"></span>
  <button class="btn" id="flash">速報</button>
  <button class="btn" id="gapbtn">未開累計</button>
</div>

<div id="mask" class="mask"><div class="panel">
  <div class="ptop"><b id="ptitle"></b>
    <span style="flex:1"></span>
    <button class="btn" id="pcopy" style="display:none">複製圖片</button>
    <button class="btn" id="pdl" style="display:none">下載圖片</button>
    <button class="btn" id="psort" style="display:none">排序：最久沒開</button>
    <button class="btn" id="pswitch" style="display:none">看法：橫條圖</button>
    <button class="btn" id="pclose">關閉</button></div>
  <div id="pbody"></div>
</div></div>

<div id="out"></div>

</div>
<script>
const DATA=/*__DATA__*/;
const $=i=>document.getElementById(i);
let CUR=Object.keys(DATA)[0], LANG="zh";
const L={zh:{B:"大",S:"小",T:"和",O:"單",E:"雙"},en:{B:"B",S:"S",T:"T",O:"O",E:"E"}};

/* 大路排法：同結果往下長，換結果就開新的一欄。
   一旦這條連莊撞到底（或下一格被前面的龍尾佔住）而轉向右邊之後，
   就一路往右走到底，不再回頭往下——中途忽下忽右很容易被誤讀成兩段。*/
function build(seq,maxRows=6){
  const occ={},placed=[];
  let col=0,row=0,startCol=0,prev=null,maxCol=-1,first=true,tailing=false;
  seq.forEach(s=>{
    let tail=false,nc=false;
    if(first){col=0;row=0;startCol=0;nc=true;first=false;tailing=false;}
    else if(s.k!==prev){let c=startCol+1;while(occ[c+",0"])c++;col=c;row=0;startCol=c;nc=true;tailing=false;}
    else if(!tailing&&row+1<maxRows&&!occ[col+","+(row+1)]){row++;}
    else{let c=col+1;while(occ[c+","+row])c++;col=c;tail=true;tailing=true;}
    occ[col+","+row]=1;maxCol=Math.max(maxCol,col);
    placed.push({c:col,r:row,s,tail,nc});prev=s.k;
  });
  return{placed,cols:maxCol+1};
}
function draw(el,seq){
  el.innerHTML="";let last=null;
  seq.forEach(s=>{if(s.tie){s.k=last===null?s.raw:last;}else{s.k=s.raw;last=s.raw;}});
  const{placed,cols}=build(seq);const total=Math.max(cols,10);
  el.style.gridTemplateColumns=`repeat(${total},var(--cell))`;
  const box={},frag=document.createDocumentFragment();
  for(let c=0;c<total;c++)for(let r=0;r<6;r++){
    const d=document.createElement("div");d.className="gc";
    d.style.gridColumn=c+1;d.style.gridRow=r+1;box[c+","+r]=d;frag.appendChild(d);}
  el.appendChild(frag);
  placed.forEach((p,idx)=>{
    const cell=box[p.c+","+p.r];if(!cell)return;
    const cls=p.s.tie?"g":(p.s.blue?"b":"r");
    if(!p.nc){const ln=document.createElement("div");ln.className="lnk "+(p.tail?"h ":"v ")+cls;cell.appendChild(ln);}
    const m=document.createElement("div");
    m.className="mk "+cls+(idx===placed.length-1?" newest":"");
    m.textContent=L[LANG][p.s.raw];
    m.title=p.s.tip;
    if(p.tail){const t=document.createElement("div");t.className="turn";t.textContent="↳";m.appendChild(t);}
    cell.appendChild(m);});
}
function pct(x){return (x*100).toFixed(2)+"%";}

/* 由開獎資料算出某一張路子圖的結果序列（舊 → 新） */
function seqOf(G,rows,scope,mode){
  const t=G.th[scope];
  return rows.map(r=>{
    const v=scope==="all"?r[5]:r[4];
    let raw,blue,tie=false;
    if(mode==="bs"){
      if(t.tie!==null&&v===t.tie){raw="T";blue=false;tie=true;}
      else if(v<=t.lo){raw="S";blue=true;}
      else{raw="B";blue=false;}
    }else{ raw=(v%2===1)?"O":"E"; blue=(raw==="E"); }
    const nums=r[2].join(" ")+(r[3]!==null?" + "+r[3]:"");
    return {raw,blue,tie,v,
      tip:`${r[1]}${r[0]===r[1]?"":"　第 "+r[0]+" 期"}\n號碼：${nums}\n${scope==="all"?"7球":"6球"}總和：${v}`};
  });
}
const clsOf=s=>s.tie?"g":(s.blue?"b":"r");

/* 球號顯示：1-39 或 1-49 這種補成兩位（07）；
   三星彩、四星彩的每一位就是單一數字，絕對不能補零 */
const ballTxt=(G,x)=> G.kind==="digit" ? String(x) : String(x).padStart(2,"0");

/* 目前連莊長度：和局不中斷，與路子圖畫法一致 */
function streakOf(s){
  let last=null,n=0;
  for(let i=s.length-1;i>=0;i--){
    if(s[i].tie) continue;
    if(last===null){ last=s[i].raw; n=1; }
    else if(s[i].raw===last) n++;
    else break;
  }
  return {raw:last,n};
}

function render(){
  const G=DATA[CUR];
  let rows=G.rows.slice();
  const yr=$("yr").value;
  if(yr) rows=rows.filter(r=>r[1].slice(0,4)===yr);
  const n=+$("n").value; if(rows.length>n) rows=rows.slice(-n);
  document.documentElement.style.setProperty("--cell",$("dens").value+"px");

  /* ── 頂部：現在的狀況（不用捲動就看得到最新） ── */
  const last=rows[rows.length-1];
  let H="";
  if(last){
    H+=`<div class="now"><div class="hd">
      <b>最新一期</b><span class="dt">${last[1]}${last[0]===last[1]?"":"　第 "+last[0]+" 期"}</span>
      <span class="balls">${last[2].map(x=>`<span class="ball">${ballTxt(G,x)}</span>`).join("")}
      ${last[3]!==null?`<span class="ball sp">${ballTxt(G,last[3])}</span>`:""}</span>
      <span class="dt">總和 ${last[4]}${last[5]!==last[4]?` / ${last[5]}`:""}</span></div>`;
    G.charts.forEach(([title,scope,mode])=>{
      const s=seqOf(G,rows,scope,mode);
      const st=streakOf(s);
      const lab=L[LANG][st.raw]||"—";
      const recent=s.slice(-14).reverse();
      H+=`<div class="nrow">
        <span class="lb">${title}</span>
        <span class="st">目前 <b class="c-${clsOf(s[s.length-1])==="b"?"b":clsOf(s[s.length-1])==="r"?"r":"g"}">${L[LANG][s[s.length-1].raw]}</b>
          ｜連 <b>${st.n}</b> 個${lab}</span>
        <span class="strip">${recent.map(x=>`<i class="${clsOf(x)}" title="${x.tip}">${L[LANG][x.raw]}</i>`).join("")}</span></div>`;
    });
    H+=`</div>`;
  }

  H+=`<div class="roads">`;
  G.charts.forEach(([title,scope,mode],ci)=>{
    const t=G.th[scope], s=seqOf(G,rows,scope,mode);
    const cnt=k=>s.filter(x=>x.raw===k).length;
    const N=s.length;
    let side;
    if(mode==="bs"){
      const B=cnt("B"),S=cnt("S"),T=cnt("T");
      side=`<div class="cnt"><span class="c-r">大</span><span class="c-r">${B}</span></div>
        <div class="cnt"><span class="c-b">小</span><span class="c-b">${S}</span></div>`
        +(t.tie!==null?`<div class="cnt"><span class="c-g">和</span><span class="c-g">${T}</span></div>`:"")
        +`<div class="pct">大 ${pct(B/N)}　小 ${pct(S/N)}${t.tie!==null?"<br>和 "+pct(T/N):""}</div>`;
    }else{
      const O=cnt("O"),E=cnt("E");
      side=`<div class="cnt"><span class="c-r">單</span><span class="c-r">${O}</span></div>
        <div class="cnt"><span class="c-b">雙</span><span class="c-b">${E}</span></div>
        <div class="pct">單 ${pct(O/N)}　雙 ${pct(E/N)}</div>`;
    }
    const sub = mode==="bs"
      ? `小 ≤${t.lo}${t.tie!==null?"　和 "+t.tie:""}　大 ≥${t.hi}`
      : "總和奇偶";
    H+=`<div class="road"><div class="side"><div class="ttl">${title}</div>
      <div class="sub">${sub}</div>${side}</div>
      <div class="grid-scroll"><div class="grid" id="g${ci}"></div></div></div>`;
  });
  H+=`</div>`;

  H+=`<div class="legend">
    <span><i class="sw" style="background:var(--red)"></i><b>紅</b> = 大 / 單</span>
    <span><i class="sw" style="background:var(--blue)"></i><b>藍</b> = 小 / 雙</span>
    <span><i class="sw" style="background:var(--green)"></i><b>綠</b> = 和（不中斷連莊）</span>
    <span>同結果往下，換結果換欄，滿 6 格往右拖尾（<b>↳</b>）</span>
    <span>共 ${rows.length.toLocaleString()} 期</span></div>`;

  // 號碼查詢（只有三星彩、四星彩需要：它們是「數字組合」而不是選號）
  if(G.kind==="digit"){
    const ex=G.rows[G.rows.length-1][2].join("");
    H+=`<h2>號碼查詢　<span class="sub">正彩＝位置也要一樣；組彩＝號碼一樣、順序不拘</span></h2>
      <div class="fbox" style="padding:12px 14px">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input id="qn" maxlength="${G.digits}" inputmode="numeric"
            placeholder="輸入 ${G.digits} 位數字，例如 ${ex}"
            style="font:inherit;font-size:16px;letter-spacing:4px;padding:5px 11px;
              border:1px solid #ccccd4;border-radius:6px;width:170px">
          <button class="btn" id="qgo">查詢</button>
          <span id="qmsg" style="font-size:12.5px;color:var(--mute)"></span>
        </div>
        <div id="qout" style="margin-top:12px"></div>
      </div>`;
  }

  // 未開累計與開獎明細並列（橫條拉太寬很難讀，各佔一半剛好）
  H+=`<div class="two">`;
  H+=`<div><h2>未開累計　<span class="sub">`+
     (G.kind==="digit"?"0-9，不分位數":(G.has_special?"只計正選":"距上次開出"))+
     `</span><span style="flex:1"></span>`+
     `<button class="btn sm" id="mgsort">最久沒開</button>`+
     `<button class="btn sm" id="mgview">橫條圖</button></h2>
     <div class="fbox" style="padding:10px 12px;max-height:560px;overflow:auto" id="mgbox"></div></div>`;

  H+=`<div><h2>開獎明細　<span class="sub">最近 40 期</span></h2>
    <div class="fbox" style="max-height:560px;overflow:auto"><table><thead><tr>
    <th>日期</th><th>期別</th><th>開出號碼</th><th class="num">6球和</th>`
    +(G.charts.some(c=>c[1]==="all")?`<th class="num">7球和</th>`:``)
    +`<th>大小</th><th>單雙</th></tr></thead><tbody>`;
  const t6=G.th["main"], t7=G.th["all"];
  rows.slice().reverse().slice(0,40).forEach(r=>{
    const balls=r[2].map(x=>`<span class="ball">${ballTxt(G,x)}</span>`).join("")
      +(r[3]!==null?`<span class="ball sp">${ballTxt(G,r[3])}</span>`:"");
    const bs=v=>{const t=t6;return (t.tie!==null&&v===t.tie)?'<b class="c-g">和</b>'
      :(v<=t.lo?'<b class="c-b">小</b>':'<b class="c-r">大</b>');};
    const oe=v=>v%2===1?'<b class="c-r">單</b>':'<b class="c-b">雙</b>';
    H+=`<tr><td>${r[1]}</td><td style="color:var(--mute);font-size:11.5px">${r[0]===r[1]?"—":r[0]}</td>
      <td>${balls}</td><td class="num">${r[4]}</td>`
      +(t7?`<td class="num">${r[5]}</td>`:``)
      +`<td>${bs(r[4])}</td><td>${oe(r[4])}</td></tr>`;
  });
  H+=`</tbody></table></div></div></div>`;
  $("out").innerHTML=H;
  paintMainGap();
  bindQuery();
  G.charts.forEach(([title,scope,mode],ci)=>{
    draw(document.getElementById("g"+ci), seqOf(G,rows,scope,mode));
  });
  scrollToEnd();
  $("hdr").textContent=`${DATA[CUR].name} · ${DATA[CUR].rows.length.toLocaleString()} 期`;
}

/* 每次重繪都自動捲到最右邊（最新一期），不必自己拉滑鼠。
   手機瀏覽器有時在 innerHTML 之後還沒算好寬度，所以多跑兩次確保到位。*/
function scrollToEnd(){
  const go=()=>document.querySelectorAll(".grid-scroll")
    .forEach(el=>{el.scrollLeft=el.scrollWidth;});
  go();
  requestAnimationFrame(go);
  setTimeout(go,120);
}

function initTabs(){
  $("tabs").innerHTML=Object.keys(DATA).map((k,i)=>
    `<button class="tab${i===0?" on":""}" data-k="${k}">${DATA[k].short}</button>`).join("");
  document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
    document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on"));
    t.classList.add("on");CUR=t.dataset.k;initYears();render();});
}
function initYears(){
  const ys=[...new Set(DATA[CUR].rows.map(r=>r[1].slice(0,4)))].sort().reverse();
  $("yr").innerHTML=`<option value="">全部</option>`+ys.map(y=>`<option>${y}</option>`).join("");
}
["n","yr","dens"].forEach(i=>$(i).onchange=render);
$("toend").onclick=scrollToEnd;
$("style").onclick=e=>{document.body.classList.toggle("hollow");
  e.target.textContent=document.body.classList.contains("hollow")?"空心圓圈":"實心方格";};
$("lang").onclick=e=>{LANG=LANG==="zh"?"en":"zh";
  e.target.textContent=LANG==="zh"?"中文":"英文";render();};
// ══ 速報 / 未開累計 ══════════════════════════════════════
// 未開累計＝這個號碼距離上次開出，已經過了幾期（截圖上那個「未開期數」）。
// 選號型彩種算 1..pool 每個號碼；三星彩／四星彩改算 0-9 每個數字
// （不分位數，任一位出現就算開過）。有特別號的彩種只看正選六顆。
function gaps(G){
  const digit = G.kind==="digit";
  const list = digit ? [...Array(10).keys()]
                     : [...Array(G.pool).keys()].map(i=>i+1);
  const last = {};                       // 號碼 → 最近一次開出的列索引
  G.rows.forEach((r,i)=>{ r[2].forEach(v=>{ last[v]=i; }); });
  const n = G.rows.length;
  return list.map(v=>({
    v,
    gap: last[v]===undefined ? n : n-1-last[v],
    date: last[v]===undefined ? null : G.rows[last[v]][1],
    never: last[v]===undefined
  }));
}

function cls(G,scope,mode,row){
  const t=G.th[scope], s=(scope==="all"?row[5]:row[4]);
  if(mode==="oe") return s%2 ? ["單","b"] : ["雙","s"];
  if(t.tie!==null && s===t.tie) return ["和","t"];
  return s<=t.lo ? ["小","s"] : ["大","b"];
}

function flashHTML(G){
  const r=G.rows[G.rows.length-1];
  const pad=x=>G.kind==="digit"?String(x):String(x).padStart(2,"0");
  // 期別若跟日期一樣，代表來源沒給正式期別，就不要印出「第 2026-08-04 期」
  const pid = r[0]===r[1] ? "" :
    `<span style="color:var(--mute);font-size:12.5px">第 ${r[0]} 期</span>`;
  let h=`<div class="flashline"><span class="k">日期</span><b>${r[1]}</b>${pid}</div>`;
  h+=`<div class="flashline"><span class="k">${G.kind==="digit"?"獎號":"開出號碼"}</span>`+
     r[2].map(v=>`<span class="pball">${pad(v)}</span>`).join("")+
     (r[3]!==null&&r[3]!==undefined?`<span class="pball sp">${pad(r[3])}</span>`
        +`<span style="color:var(--mute);font-size:12px">特別號</span>`:"")+`</div>`;
  h+=`<div class="flashline"><span class="k">總和</span><b>${r[4]}</b>`+
     (r[5]!==r[4]?`<span style="color:var(--mute);font-size:12.5px">（含特別號 ${r[5]}）</span>`:"")+`</div>`;
  // 每一種路子：結果 + 目前連幾個 + 近 12 期迷你路子條（跟 Discord 卡片一致）
  G.charts.forEach(c=>{
    const [txt,k]=cls(G,c[1],c[2],r);
    const s=seqOf(G,G.rows,c[1],c[2]);
    const st=streakOf(s);
    const lab=L[LANG][st.raw]||"";
    const mini=s.slice(-12).reverse().map((x,i)=>{
      const cc=clsOf(x);
      const col=cc==="b"?"#1f4fd8":cc==="r"?"#c8352b":"#14875a";
      return `<i style="display:inline-block;width:19px;height:19px;border-radius:4px;`+
             `margin-right:3px;background:${col}`+
             (i===0?";outline:2px solid #1c1c1e;outline-offset:1px":"")+`"></i>`;
    }).join("");
    h+=`<div class="flashline"><span class="k">${c[0]}</span>`+
       `<span class="pill ${k}">${txt}</span>`+
       `<span style="color:var(--mute);font-size:12.5px">連 ${st.n} 個${lab}</span>`+
       `<span style="flex:1"></span><span style="line-height:1">${mini}</span></div>`;
  });
  const cold=gaps(G).sort((a,b)=>b.gap-a.gap).slice(0,8);
  h+=`<div class="flashline" style="border:none;align-items:flex-start">`+
     `<span class="k">最久沒開</span><span>`+
     cold.map(x=>`<span class="pball cold" style="margin:2px 3px 2px 0">`+
       `${pad(x.v)}</span><span style="color:var(--mute);font-size:12px;margin-right:8px">`+
       `${x.gap}期</span>`).join("")+`</span></div>`;
  return h;
}

function gapHTML(G,view,sortBy,head){
  const pad=x=>G.kind==="digit"?String(x):String(x).padStart(2,"0");
  let g=gaps(G);
  const mx=Math.max(1,...g.map(x=>x.gap));
  const hot=new Set(g.slice().sort((a,b)=>b.gap-a.gap).slice(0,5).map(x=>x.v));
  g = sortBy==="no" ? g.slice().sort((a,b)=>a.v-b.v)
                    : g.slice().sort((a,b)=>b.gap-a.gap);
  head = head===undefined ? "" : head;
  if(view==="bar"){
    return head+g.map(x=>
      `<div class="gaprow${hot.has(x.v)?" hot":""}"><span class="no">${pad(x.v)}</span>`+
      `<span class="bw"><span class="bf" style="width:${Math.max(2,Math.round(x.gap/mx*100))}%"></span></span>`+
      `<span class="nn">${x.gap}</span></div>`).join("");
  }
  return head+`<table class="gt"><tr><th data-s="no">號碼 ⇅</th>`+
    `<th data-s="gap">未開期數 ⇅</th><th>上次開出</th></tr>`+
    g.map(x=>`<tr class="${hot.has(x.v)?"hot":""}"><td>${pad(x.v)}</td>`+
      `<td>${x.gap}</td><td>${x.date||"這段期間都沒開過"}</td></tr>`).join("")+`</table>`;
}

/* 號碼查詢：三星彩／四星彩專用
   正彩 = 數字與位置都相同；組彩 = 同一組數字、順序不拘（正彩是組彩的一種）。*/
function bindQuery(){
  const go=$("qgo"); if(!go) return;
  go.onclick=runQuery;
  $("qn").onkeydown=e=>{ if(e.key==="Enter") runQuery(); };
}
function runQuery(){
  const G=DATA[CUR], box=$("qout"), msg=$("qmsg");
  const v=($("qn").value||"").replace(/\D/g,"");
  if(v.length!==G.digits){
    msg.textContent=`請輸入 ${G.digits} 位數字`; box.innerHTML=""; return;
  }
  msg.textContent="";
  const want=v.split("").map(Number);
  const sig=a=>a.slice().sort().join("");
  const ws=want.join(""), wk=sig(want);
  const N=G.rows.length;
  const hit=[];
  G.rows.forEach((r,i)=>{
    const s=r[2].join("");
    if(sig(r[2])!==wk) return;
    hit.push({date:r[1], nums:s, exact:s===ws, ago:N-1-i});
  });
  const ex=hit.filter(h=>h.exact);
  const stat=(lab,arr,note)=>{
    const last=arr.length?arr[arr.length-1]:null;
    return `<div style="flex:1;min-width:190px;background:#f7f6f4;border-radius:8px;padding:10px 12px">
      <div style="font-size:12.5px;color:var(--mute)">${lab}${note}</div>
      <div style="font-size:22px;font-weight:700;margin:2px 0 1px">${arr.length}<span
        style="font-size:13px;font-weight:400;color:var(--mute)"> 次</span></div>
      <div style="font-size:12px;color:var(--mute)">${
        last?`最近 ${last.date}（${last.ago===0?"就是最新一期":last.ago+" 期前"}）`:"從來沒有開過"}</div></div>`;
  };
  let h=`<div style="display:flex;gap:10px;flex-wrap:wrap">`+
    stat(`正彩 ${ws}`,ex,"　位置相同")+
    stat(`組彩 ${ws}`,hit,"　順序不拘")+`</div>`;
  if(hit.length){
    h+=`<div style="max-height:230px;overflow:auto;margin-top:10px">
      <table class="gt"><tr><th>日期</th><th>開出</th><th>種類</th><th>距今</th></tr>`+
      hit.slice().reverse().map(x=>`<tr><td>${x.date}</td>`+
        `<td style="letter-spacing:3px;font-weight:700">${x.nums}</td>`+
        `<td>${x.exact?'<b class="c-r">正彩</b>':'<span style="color:var(--mute)">組彩</span>'}</td>`+
        `<td>${x.ago===0?"最新一期":x.ago+" 期前"}</td></tr>`).join("")+`</table></div>`;
  }
  h+=`<div style="font-size:12px;color:var(--mute);margin-top:8px">`+
     `查詢範圍：全部 ${N.toLocaleString()} 期（${G.rows[0][1]} 起）。組彩次數已含正彩。</div>`;
  box.innerHTML=h;
}

// 主畫面上的未開累計（跟浮層各自記住自己的看法與排序）
let MV="bar", MS="gap";
function paintMainGap(){
  const box=$("mgbox");
  if(!box) return;
  box.innerHTML=gapHTML(DATA[CUR],MV,MS);
  $("mgview").textContent=MV==="bar"?"橫條圖":"表格";
  $("mgsort").textContent=MS==="gap"?"最久沒開":"號碼順序";
  $("mgview").onclick=()=>{MV=MV==="bar"?"table":"bar";paintMainGap();};
  $("mgsort").onclick=()=>{MS=MS==="gap"?"no":"gap";paintMainGap();};
  if(MV==="table"){
    box.querySelectorAll("table.gt th[data-s]").forEach(t=>{
      t.onclick=()=>{MS=t.dataset.s;paintMainGap();};
    });
  }
}

let PV="bar", PS="gap", PMODE="";
function openPanel(kind){
  const G=DATA[CUR];
  PMODE=kind;
  $("ptitle").textContent=G.name+(kind==="flash"?"　開獎速報":"　未開累計");
  $("pswitch").style.display=kind==="gap"?"":"none";
  $("psort").style.display=kind==="gap"?"":"none";
  $("pcopy").style.display=$("pdl").style.display=kind==="flash"?"":"none";
  paintPanel();
  $("mask").classList.add("on");
}

/* 把速報卡轉成圖片。
   做法是把那段 HTML 包進 SVG 的 foreignObject 再畫到 canvas，
   全部離線完成，不需要任何外部程式庫，樣式也要一起塞進去才不會走版。*/
function cardCSS(){
  return `*{box-sizing:border-box;margin:0}
  .w{width:660px;padding:20px 24px;background:#fff;
   font-family:"Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;
   font-size:15px;color:#1c1c1e;line-height:1.6}
  .t{font-size:19px;font-weight:700;padding-bottom:10px;
   border-bottom:3px solid #1c1c1e;margin-bottom:6px}
  .flashline{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
   padding:9px 0;border-bottom:1px solid #e3e3e8}
  .flashline .k{color:#6f6f77;font-size:12.5px;min-width:74px}
  .pball{display:inline-flex;align-items:center;justify-content:center;
   width:34px;height:34px;border-radius:50%;background:#1c1c1e;color:#fff;
   font-weight:700;font-size:14px}
  .pball.sp{background:#14875a}.pball.cold{background:#5a6f9e}
  .pill{display:inline-block;padding:2px 9px;border-radius:11px;
   font-size:12.5px;font-weight:700;color:#fff}
  .pill.b{background:#c8352b}.pill.s{background:#1f4fd8}.pill.t{background:#14875a}`;
}
function cardBlob(cb){
  const G=DATA[CUR];
  const html=`<div xmlns="http://www.w3.org/1999/xhtml" class="w">`+
    `<div class="t">${G.name}　開獎速報</div>${flashHTML(G)}</div>`;
  const probe=document.createElement("div");
  probe.style.cssText="position:absolute;left:-9999px;top:0";
  probe.innerHTML=`<style>${cardCSS()}</style>`+html;
  document.body.appendChild(probe);
  const h=Math.ceil(probe.querySelector(".w").getBoundingClientRect().height)+4;
  probe.remove();
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="660" height="${h}">`+
    `<foreignObject width="100%" height="100%">`+
    `<style>${cardCSS()}</style>${html}</foreignObject></svg>`;
  const img=new Image();
  img.onload=()=>{
    const s=2, c=document.createElement("canvas");
    c.width=660*s; c.height=h*s;
    const x=c.getContext("2d");
    x.fillStyle="#fff"; x.fillRect(0,0,c.width,c.height);
    x.scale(s,s); x.drawImage(img,0,0);
    c.toBlob(b=>cb(b,G),"image/png");
  };
  img.onerror=()=>cb(null,G);
  img.src="data:image/svg+xml;charset=utf-8,"+encodeURIComponent(svg);
}
function flashName(G){
  return G.name+"_"+G.rows[G.rows.length-1][1]+"_速報.png";
}
function tellBtn(el,txt){
  const old=el.textContent; el.textContent=txt;
  setTimeout(()=>{el.textContent=old;},1800);
}
$("pdl").onclick=()=>cardBlob((b,G)=>{
  if(!b){tellBtn($("pdl"),"產生失敗");return;}
  const a=document.createElement("a");
  a.href=URL.createObjectURL(b); a.download=flashName(G);
  a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),4000);
  tellBtn($("pdl"),"已下載");
});
$("pcopy").onclick=()=>cardBlob(async (b,G)=>{
  if(!b){tellBtn($("pcopy"),"產生失敗");return;}
  try{
    await navigator.clipboard.write([new ClipboardItem({"image/png":b})]);
    tellBtn($("pcopy"),"已複製");
  }catch(e){
    tellBtn($("pcopy"),"改用下載");   // Safari／非 https 不允許寫剪貼簿
    $("pdl").click();
  }
});
function paintPanel(){
  const G=DATA[CUR];
  $("pbody").innerHTML = PMODE==="flash" ? flashHTML(G) : gapHTML(G,PV,PS,gapHead(G));
  $("pswitch").textContent="看法："+(PV==="bar"?"橫條圖":"表格");
  $("psort").textContent="排序："+(PS==="gap"?"最久沒開":"號碼順序");
  if(PMODE==="gap"&&PV==="table"){
    document.querySelectorAll("table.gt th[data-s]").forEach(t=>{
      t.onclick=()=>{PS=t.dataset.s;paintPanel();};
    });
  }
}
function gapHead(G){
  return `<div style="color:var(--mute);font-size:12.5px;margin-bottom:10px">`+
    `共 ${G.rows.length.toLocaleString()} 期資料，最後一期 ${G.rows[G.rows.length-1][1]}。`+
    `數字＝距離上次開出已經過幾期，0 代表最新一期就有開。`+
    (G.kind==="digit"?"（不分位數，任一位出現就算開過）"
                     :(G.has_special?"（只計正選，不含特別號）":""))+`</div>`;
}
$("flash").onclick=()=>openPanel("flash");
$("gapbtn").onclick=()=>openPanel("gap");
$("pswitch").onclick=()=>{PV=PV==="bar"?"table":"bar";paintPanel();};
$("psort").onclick=()=>{PS=PS==="gap"?"no":"gap";paintPanel();};
$("pclose").onclick=()=>$("mask").classList.remove("on");
$("mask").onclick=e=>{ if(e.target===$("mask")) $("mask").classList.remove("on"); };
document.addEventListener("keydown",e=>{
  if(e.key==="Escape") $("mask").classList.remove("on");
});

initTabs();initYears();render();

// ── 自動取得最新版 ─────────────────────────────────────────
// GitHub 的 CDN 會把整頁存起來重複使用約十分鐘，這段期間就算按 F5
// 甚至 Ctrl+F5 都沒有用——你的瀏覽器問的還是同一個網址，CDN 回你同一份。
// 唯一能繞過的方法是換一個網址，所以這裡在網址後面加一個版本參數，
// 自動重載一次就會拿到最新的頁面，不必你動手。
// 用版本參數本身當防呆：重載後參數已等於線上版本，就不會再重載第二次。
(function(){
  try{
    fetch("status.json?t="+Date.now(),{cache:"no-store"})
      .then(function(r){return r.json();})
      .then(function(s){
        var mine="__BUILT__";
        var live=(s.built_at_taipei||"").slice(0,16);
        if(!live||live===mine)return;
        var tried=new URLSearchParams(location.search).get("v");
        if(tried!==live){
          location.replace(location.pathname+"?v="+encodeURIComponent(live));
          return;
        }
        // 已經換過網址還是舊的（極少見，通常是離線或 CDN 還沒同步）。
        // 只在右下角放一個小提示，不要擋住畫面。
        var b=document.createElement("div");
        b.style.cssText="position:fixed;right:14px;bottom:14px;z-index:99;"+
          "background:#fff;color:var(--mute);border:1px solid var(--line);"+
          "border-radius:8px;padding:8px 12px;font-size:12px;line-height:1.5;"+
          "box-shadow:0 2px 10px rgba(0,0,0,.08);max-width:280px";
        b.textContent="線上已有 "+live+" 的版本，稍候幾分鐘再開就會是最新的。";
        b.title="點一下關閉";
        b.onclick=function(){b.remove();};
        document.body.appendChild(b);
      })
      .catch(function(){});
  }catch(e){}
})();
</script></body></html>"""


# ─────────────────────────── 主流程 ───────────────────────────

# ── 執行記錄 ──────────────────────────────────────────────
# 設定環境變數 LOTTERY_LOG 後，畫面上的所有訊息會同時寫入該檔案，
# 這樣出問題時不必請使用者複製貼上，直接看檔案就知道發生什麼事。
class _Tee:
    def __init__(self, stream, path):
        self.stream = stream
        self.fh = open(path, "a", encoding="utf-8")

    def write(self, s):
        self.stream.write(s)
        self.fh.write(s)
        self.fh.flush()

    def flush(self):
        self.stream.flush()
        self.fh.flush()


def _start_log(tag):
    p = os.environ.get("LOTTERY_LOG")
    if not p:
        return
    try:
        sys.stdout = _Tee(sys.stdout, p)
        sys.stderr = sys.stdout
        import datetime as _d
        tp = _d.datetime.now(_d.timezone(_d.timedelta(hours=8)))
        print(f"\n{'#' * 66}\n#  {tag}　{tp:%Y-%m-%d %H:%M:%S} 台北\n{'#' * 66}")
    except Exception:
        pass


def main():
    _start_log("抓取資料 lottery_db.py")
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--only", default=None)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--insecure", action="store_true",
                    help="略過 SSL 憑證驗證（憑證修不好時的最後手段）")
    ap.add_argument("--recent", action="store_true",
                    help="只抓最近兩個月，供每日自動更新使用（快很多）")
    ap.add_argument("--out", default=None,
                    help="指定產生的網頁檔名，例如 --out index.html")
    ap.add_argument("--force-html", action="store_true",
                    help="即使彩種數量減少也強制重新產生網頁")
    a = ap.parse_args()

    global INSECURE, RECENT, HTML
    INSECURE = a.insecure
    RECENT = a.recent
    if a.out:
        HTML = a.out if os.path.isabs(a.out) else os.path.join(HERE, a.out)
    if RECENT:
        print("  模式：只更新最近資料（每日自動更新用）\n")
    if INSECURE:
        print("  ⚠ 已停用 SSL 憑證驗證。這些都是公開唯讀端點，風險有限，")
        print("    但建議之後還是執行「修復憑證.bat」把憑證裝好。\n")

    print("=" * 64)
    print("  樂透路子圖資料庫")
    print("=" * 64)

    con = connect()

    if a.stats:
        stats(con); con.close(); return

    if a.html:
        stats(con); build_html(con, a.force_html); con.close(); return

    if a.probe:
        import ssl as _ssl
        print(f"\n  Python {sys.version.split()[0]}　OpenSSL {_ssl.OPENSSL_VERSION}")
        ssl_context()
        print(f"  憑證來源：{_CTX_NOTE}")
        try:
            import certifi; print(f"  certifi：已安裝 {certifi.__version__}")
        except ImportError:
            print("  certifi：未安裝  ← 若下方出現憑證錯誤，請先執行「修復憑證.bat」")

        print("\n  逐一測試各站台連線")
        print("  " + "-" * 62)
        hosts = [
            ("台灣彩券 539", "https://api.taiwanlottery.com/TLCAPIWeB/Lottery/"
                             "Daily539Result?month=2026-07&pageNum=1&pageSize=1"),
            ("台灣彩券 大樂透", "https://api.taiwanlottery.com/TLCAPIWeB/Lottery/"
                                "Lotto649Result?month=2026-06&pageNum=1&pageSize=1"),
            ("樂透王 加州F5", "https://www.lotterywang.com/lottoCA5/year/2026"),
            ("pilio 六合彩", "https://www.pilio.idv.tw/ltohk/list.asp"
                             "?indexpage=1&orderby=new"),
        ]
        okc = 0
        for nm, u in hosts:
            try:
                t = http(u, retries=1, timeout=25)
                print(f"  ✔ {nm:<16} 正常（{len(t):,} 位元組）")
                okc += 1
            except Exception as e:
                print(f"  ✘ {nm:<16} {e}")
        print("  " + "-" * 62)
        print(f"  {okc}/{len(hosts)} 個站台可連線")

        if okc == 0:
            print("\n  全部失敗 → 憑證或網路問題，先執行「修復憑證.bat」再試一次。")
            con.close(); return

        def show(rows, label):
            if not rows:
                print(f"  → {label}：沒有解析到任何資料")
                return
            r = sorted(rows, key=lambda x: x["draw_date"])[-1]
            nums = json.loads(r["numbers"])
            print(f"  → {label}：解析到 {len(rows):,} 期")
            print(f"     最新一期 {r['draw_date']}　號碼 {nums}"
                  + (f" 特{r['special']}" if r["special"] is not None else "")
                  + f"　總和 {r['sum_main']}"
                  + (f" / {r['sum_all']}" if r["sum_all"] != r["sum_main"] else ""))

        print("\n  試抓 加州 Fantasy 5（樂透王，僅取今年驗證解析）：")
        try:
            show(fetch_ca_lotterywang("ca_f5", GAMES["ca_f5"], 0), "加州 F5")
        except Exception as e:
            print(f"  → 失敗：{e}")

        print("\n  試抓 香港六合彩（pilio，僅取前 2 頁驗證解析）：")
        try:
            t = strip_tags(http("https://www.pilio.idv.tw/ltohk/list.asp"
                                "?indexpage=1&orderby=new", retries=2, timeout=40))
            show(parse_pilio(t, "hk6", 6), "六合彩")
        except Exception as e:
            print(f"  → 失敗：{e}")
        con.close(); return

    todo = [a.only] if a.only else list(GAMES)
    print(f"  抓取近 {a.years} 年，共 {len(todo)} 個彩種\n")

    report = []
    for gid in todo:
        g = GAMES.get(gid)
        if not g:
            print(f"  未知彩種 {gid}"); continue
        before = con.execute(
            "SELECT MAX(draw_date), COUNT(*) FROM draws WHERE game=?", (gid,)).fetchone()
        print(f"  ● {g['name']}　（目前最新 {before[0] or '無'}）")
        t0 = time.time()
        try:
            rows = FETCHERS[g["src"]](gid, g, a.years)
        except Exception as e:
            print(f"      ✘ 失敗：{e}\n")
            report.append((g["name"], before[0], before[0], 0, f"失敗：{e}"))
            FETCH_REPORT[gid] = {"ok": False, "error": str(e)[:200],
                                 "fetched": 0, "added": 0, "latest": before[0]}
            continue
        rows = [r for r in rows if r["draw_date"]]
        upsert(con, rows); con.commit()
        after = con.execute(
            "SELECT MAX(draw_date), COUNT(*) FROM draws WHERE game=?", (gid,)).fetchone()
        added = after[1] - before[1]
        FETCH_REPORT[gid] = {"ok": True, "error": None, "fetched": len(rows),
                             "added": added, "latest": after[0],
                             "source_latest": max((r["draw_date"] for r in rows),
                                                  default=None)}
        note = "有新資料" if after[0] != before[0] else "已是最新"
        print(f"      抓回 {len(rows):,} 期，新增 {added} 期，"
              f"最新 {before[0] or '無'} → {after[0]}　{note}"
              f"　({time.time() - t0:.1f}s)\n")
        report.append((g["name"], before[0], after[0], added, note))

    print("  " + "─" * 62)
    print(f"  {'彩種':<16}{'更新前':<13}{'更新後':<13}{'新增':>5}  狀態")
    print("  " + "─" * 62)
    for nm, b, af, ad, note in report:
        print(f"  {nm:<16}{str(b or '無'):<13}{str(af or '無'):<13}{ad:>5}  {note}")
    print("  " + "─" * 62)

    total = stats(con)
    ok = build_html(con, a.force_html) if total else False
    con.close()
    print("\n" + "=" * 64)
    if ok:
        print("  完成。開啟「樂透路子圖.html」即可查看。")
    else:
        print("  網頁未更新（見上方說明）。原本的網頁維持不變。")
    print("=" * 64)
    if not ok:
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  已中斷。")
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"\n  發生錯誤：{e}")
        sys.exit(1)
