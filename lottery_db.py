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
        if gid not in PILIO_TW:
            return fetch_taiwan(gid, g, years)
        return fetch_pilio_tw(gid, g, pages=2)
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


# ── 加州 Fantasy 5：lotterywang.com（依年份分頁）──
def parse_lotterywang(text, gid, n_main=5):
    pat = re.compile(
        r"(\d{4})\.(\d{1,2})\.(\d{1,2})\s*\([日一二三四五六]\)[^0-9]{0,40}?"
        r"(\d{3,7})\s*期((?:\s*\d{1,2}\b){%d})" % n_main)
    out, seen = [], set()
    for m in pat.finditer(text):
        y, mo, d, period, nums = m.groups()
        try:
            main = [int(x) for x in nums.split()]
        except Exception:
            continue
        if len(main) != n_main or not all(1 <= v <= 39 for v in main):
            continue
        if period in seen:          # 該站每筆會重複輸出兩次（RWD 版型）
            continue
        seen.add(period)
        out.append(_mk(gid, period, f"{y}-{int(mo):02d}-{int(d):02d}", main, None))
    return out


def fetch_ca_lotterywang(gid, g, years):
    this_y = dt.date.today().year
    out, miss = [], 0
    back = 0 if RECENT else years
    for y in range(this_y, this_y - back - 1, -1):
        url = f"https://www.lotterywang.com/lottoCA5/year/{y}"
        try:
            rows = parse_lotterywang(strip_tags(http(url, retries=2, timeout=45)),
                                     gid, g["n_main"])
        except Exception as e:
            print(f"      {y} 年失敗 {e}")
            rows = []
        if rows:
            print(f"      {y} 年 {len(rows):,} 期")
            out += rows
            miss = 0
        else:
            miss += 1
            if miss >= 2:
                print(f"      {y} 年起無資料，停止回溯")
                break
        time.sleep(0.3)
    return out


def fetch_hk(gid, g, years):
    """香港六合彩：只用 pilio。

    原本還留了 GitHub 鏡像當備援，但它從來沒成功過，反而害慘了自己——
    每日更新時 pilio 只會回傳約 18 期（45 天內），舊程式卻要求「超過 100 期
    才算成功」，於是把好好的資料丟掉、轉去試那個壞掉的鏡像。
    六合彩因此從來沒有被自動更新過。
    """
    rows = fetch_pilio_hk(gid, g, years)
    if not rows:
        raise RuntimeError("pilio 沒有回傳任何六合彩資料")
    return rows


def fetch_ca(gid, g, years):
    """加州 Fantasy 5：只用樂透王。

    官方 calottery API 對台灣的 IP 一律回 403，試幾次都一樣，
    每次還要浪費時間掃描遊戲代號，因此完全移除。
    """
    return fetch_ca_lotterywang(gid, g, years)


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
    if not rows:
        return 0
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

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = HTML_TMPL.replace("/*__DATA__*/", data).replace(
        "__BUILT__", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    with open(HTML, "w", encoding="utf-8") as f:
        f.write(html)
    con.execute("INSERT OR REPLACE INTO meta(key,val) VALUES('html_games',?)",
                (str(len(payload)),))
    con.commit()
    print(f"\n  已產生網頁：{HTML}")
    print(f"  收錄 {len(payload)} 個彩種：{'、'.join(v['name'] for v in payload.values())}")
    print(f"  檔案大小：{os.path.getsize(HTML) / 1048576:.2f} MB（資料已內嵌，離線可看）")
    return True


HTML_TMPL = r"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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
.bar{display:flex;gap:9px;flex-wrap:wrap;align-items:center;padding:14px 0 15px;
 border-bottom:1px solid var(--line);margin-bottom:18px}
select,.btn{font:inherit;font-size:13.5px;padding:6px 11px;border:1px solid #ccccd4;
 border-radius:6px;background:#fff;color:var(--ink);cursor:pointer}
select:hover,.btn:hover{border-color:#9a9aa8}
label{font-size:12.5px;color:var(--mute)}
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
.now .balls .ball{background:#e9edf5;font-weight:700;font-size:13px;min-width:27px;padding:2px 6px}
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
.ball{display:inline-block;min-width:23px;padding:1px 5px;margin-right:3px;text-align:center;
 background:#eef1f6;border-radius:4px;font-size:12px;font-variant-numeric:tabular-nums}
.ball.sp{background:#fdecc8;font-weight:700}
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
  <label>期數</label>
  <select id="n"><option value="120">近 120 期</option><option value="200">近 200 期</option>
  <option value="400">近 400 期</option><option value="99999">全部</option></select>
  <label>年份</label><select id="yr"><option value="">全部</option></select>
  <label>格子</label><select id="dens">
    <option value="28">大</option><option value="22" selected>中</option>
    <option value="16">小</option><option value="12">最小（整季一覽）</option></select>
  <button class="btn" id="style">樣式：實心方格</button>
  <button class="btn" id="lang">文字：中文</button>
  <button class="btn" id="toend">跳到最新 →</button>
</div>

<div id="out"></div>

<div class="warn" style="margin-top:30px">
<b>先講清楚這張圖能做什麼、不能做什麼。</b><br>
樂透每期都是<b>獨立事件</b>，開獎機器不記得上一期開什麼。路子圖能忠實呈現歷史型態，
但<b>「連開五個大」不會讓下一期更容易開小，也不會更容易開大</b>——這是機率論裡最經典的謬誤（賭徒謬誤）。<br>
下方「理論值對照」是有意義的部分：把實際開出比例跟數學上算出的理論機率對照，
可以檢驗這個彩種的開獎是否公正。如果實際與理論明顯不符，那才是真的值得追查的事。<br>
順帶一提，這些遊戲的<b>大小並非各 50%</b>——因為總和分布是鐘形的，中間值出現機率最高，
所以「和」的機率遠比直覺高，這在理論值表裡看得很清楚。
</div>

</div>
<script>
const DATA=/*__DATA__*/;
const $=i=>document.getElementById(i);
let CUR=Object.keys(DATA)[0], LANG="zh";
const L={zh:{B:"大",S:"小",T:"和",O:"單",E:"雙"},en:{B:"B",S:"S",T:"T",O:"O",E:"E"}};

function build(seq,maxRows=6){
  const occ={},placed=[];let col=0,row=0,startCol=0,prev=null,maxCol=-1,first=true;
  seq.forEach(s=>{
    let tail=false,nc=false;
    if(first){col=0;row=0;startCol=0;nc=true;first=false;}
    else if(s.k!==prev){let c=startCol+1;while(occ[c+",0"])c++;col=c;row=0;startCol=c;nc=true;}
    else if(row+1<maxRows&&!occ[col+","+(row+1)]){row++;}
    else{let c=col+1;while(occ[c+","+row])c++;col=c;tail=true;}
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
        <span class="strip">${recent.map(x=>`<i class="${clsOf(x)}" title="${x.tip}">${L[LANG][x.raw]}</i>`).join("")}</span>
        <span class="arrowhint">← 左邊是最新</span></div>`;
    });
    H+=`</div>`;
  }

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
        +`<div class="pct">大 ${pct(B/N)}　小 ${pct(S/N)}${t.tie!==null?"　和 "+pct(T/N):""}<br>
          理論 ${pct(t.theory.big)} / ${pct(t.theory.small)}${t.tie!==null?" / "+pct(t.theory.tie):""}</div>`;
    }else{
      const O=cnt("O"),E=cnt("E");
      side=`<div class="cnt"><span class="c-r">單</span><span class="c-r">${O}</span></div>
        <div class="cnt"><span class="c-b">雙</span><span class="c-b">${E}</span></div>
        <div class="pct">單 ${pct(O/N)}　雙 ${pct(E/N)}<br>理論 ${pct(t.theory.odd)} / ${pct(t.theory.even)}</div>`;
    }
    const sub = mode==="bs"
      ? `小 ≤${t.lo}${t.tie!==null?"　和 "+t.tie:""}　大 ≥${t.hi}`
      : "總和奇偶";
    H+=`<div class="road"><div class="side"><div class="ttl">${title}</div>
      <div class="sub">${sub}</div>${side}</div>
      <div class="grid-scroll"><div class="grid" id="g${ci}"></div></div></div>`;
  });

  H+=`<div class="legend">
    <span><i class="sw" style="background:var(--red)"></i><b>紅</b> = 大 / 單</span>
    <span><i class="sw" style="background:var(--blue)"></i><b>藍</b> = 小 / 雙</span>
    <span><i class="sw" style="background:var(--green)"></i><b>綠</b> = 和（不中斷連莊）</span>
    <span>同結果往下，換結果換欄，滿 6 格往右拖尾（<b>↳</b>）</span>
    <span>共 ${rows.length.toLocaleString()} 期</span></div>`;

  // 理論值對照
  H+=`<h2>理論值對照　<span style="font-size:12.5px;font-weight:400;color:var(--mute)">
      實際開出比例 vs 數學算出的理論機率（用全部資料，不受上方期數篩選影響）</span></h2>
    <div class="fbox"><table><thead><tr><th>項目</th><th class="num">實際次數</th>
    <th class="num">實際比例</th><th class="num">理論機率</th><th class="num">差距</th>
    <th class="num">z 值</th><th>判定</th></tr></thead><tbody>`;
  const all=G.rows;
  const seen=new Set();
  G.charts.forEach(([title,scope,mode])=>{
    const key=scope+mode; if(seen.has(key))return; seen.add(key);
    const t=G.th[scope];
    const vals=all.map(r=>scope==="all"?r[5]:r[4]);
    const N=vals.length;
    const items = mode==="bs"
      ? [["大",vals.filter(v=>v>=t.hi).length,t.theory.big],
         ["小",vals.filter(v=>v<=t.lo).length,t.theory.small]]
         .concat(t.tie!==null?[["和",vals.filter(v=>v===t.tie).length,t.theory.tie]]:[])
      : [["單",vals.filter(v=>v%2===1).length,t.theory.odd],
         ["雙",vals.filter(v=>v%2===0).length,t.theory.even]];
    items.forEach(([lab,k,p])=>{
      const obs=k/N, se=Math.sqrt(p*(1-p)/N), z=(obs-p)/se;
      const ok=Math.abs(z)<1.96;
      H+=`<tr><td><b>${title.replace(/大小|單雙/,"")||""} ${lab}</b>
        <span style="color:var(--mute);font-size:11px">${scope==="all"?"7球":"6球"}</span></td>
        <td class="num">${k.toLocaleString()} / ${N.toLocaleString()}</td>
        <td class="num">${pct(obs)}</td><td class="num">${pct(p)}</td>
        <td class="num">${(obs-p>=0?"+":"")+((obs-p)*100).toFixed(2)}pp</td>
        <td class="num">${z.toFixed(2)}</td>
        <td><span class="sig ${ok?"ok":"no"}">${ok?"符合理論":"偏離 >2σ"}</span></td></tr>`;
    });
  });
  H+=`</tbody></table></div>`;

  // 明細
  H+=`<h2>開獎明細　<span style="font-size:12.5px;font-weight:400;color:var(--mute)">最近 100 期</span></h2>
    <div class="fbox" style="max-height:520px;overflow:auto"><table><thead><tr>
    <th>日期</th><th>期別</th><th>開出號碼</th><th class="num">6球和</th>`
    +(G.charts.some(c=>c[1]==="all")?`<th class="num">7球和</th>`:``)
    +`<th>大小</th><th>單雙</th></tr></thead><tbody>`;
  const t6=G.th["main"], t7=G.th["all"];
  rows.slice().reverse().slice(0,100).forEach(r=>{
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
  H+=`</tbody></table></div>`;
  $("out").innerHTML=H;
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
  e.target.textContent="樣式："+(document.body.classList.contains("hollow")?"空心圓圈":"實心方格");};
$("lang").onclick=e=>{LANG=LANG==="zh"?"en":"zh";
  e.target.textContent="文字："+(LANG==="zh"?"中文":"英文");render();};
initTabs();initYears();render();
</script></body></html>"""


# ─────────────────────────── 主流程 ───────────────────────────

def main():
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

    for gid in todo:
        g = GAMES.get(gid)
        if not g:
            print(f"  未知彩種 {gid}"); continue
        print(f"  ● {g['name']}")
        t0 = time.time()
        try:
            rows = FETCHERS[g["src"]](gid, g, a.years)
        except Exception as e:
            print(f"      失敗：{e}\n")
            continue
        rows = [r for r in rows if r["draw_date"]]
        upsert(con, rows); con.commit()
        print(f"      取得 {len(rows):,} 期　({time.time() - t0:.1f}s)\n")

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
