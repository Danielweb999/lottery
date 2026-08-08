#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
開獎速報推播
============
從 lottery.db 找出「還沒通知過的新開獎」，畫成一張圖，推送到 Discord。

沒有新開獎就什麼都不做，所以大樂透週三沒開就不會傳給你。
判斷依據是「期別有沒有變」，不是星期幾，遇到加開或順延都不會出錯。

用法：
  python notify.py                      # 正式執行
  python notify.py --test               # 不管有沒有新開獎，用最新一期各畫一張測試
  python notify.py --dry                # 只產生圖片存成 preview.png，不推送
  python notify.py --webhook <網址>     # 指定 webhook（預設讀環境變數或 discord_webhook.txt）
"""

import argparse
import datetime as dt
import io
import json
import os
import sys
import urllib.request
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "lottery.db")
STATE = os.path.join(HERE, "notified.json")
CONF = os.path.join(HERE, "設定")
os.makedirs(CONF, exist_ok=True)
_new = os.path.join(CONF, "discord_webhook.txt")
_old = os.path.join(HERE, "discord_webhook.txt")
HOOK_FILE = _new if (os.path.exists(_new) or not os.path.exists(_old)) else _old

RED, BLUE, GREEN = (200, 53, 43), (31, 79, 216), (20, 135, 90)
INK, MUTE, LINE_C = (28, 28, 30), (110, 110, 120), (226, 226, 232)
BG, CARD, HEAD = (255, 255, 255), (252, 252, 250), (24, 24, 26)

WEEK = "一二三四五六日"

FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKtc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK.ttc",
    "C:/Windows/Fonts/msjh.ttc",
    "C:/Windows/Fonts/msjhl.ttc",
    "C:/Windows/Fonts/mingliu.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]


def find_font():
    for p in FONTS:
        if os.path.exists(p):
            return p
    return None


_FP = None


def F(size, bold=False):
    from PIL import ImageFont
    global _FP
    if _FP is None:
        _FP = find_font()
        if not _FP:
            raise RuntimeError(
                "找不到中文字型。\n"
                "  Windows：應該要有 C:/Windows/Fonts/msjh.ttc\n"
                "  Linux：請安裝 fonts-noto-cjk")
    try:
        return ImageFont.truetype(_FP, size, index=1 if bold and _FP.endswith(".ttc") else 0)
    except Exception:
        return ImageFont.truetype(_FP, size)


# ────────────────────── 讀資料 ──────────────────────

def load_games():
    import sqlite3
    sys.path.insert(0, HERE)
    import lottery_db as L
    con = sqlite3.connect(DB)
    out = []
    for gid, g in L.GAMES.items():
        r = con.execute(
            "SELECT draw_id,draw_date,numbers,special,sum_main,sum_all "
            "FROM draws WHERE game=? ORDER BY draw_date DESC, draw_id DESC LIMIT 40",
            (gid,)).fetchall()
        if not r:
            continue
        hist = list(reversed(r))          # 舊 → 新
        latest = r[0]

        # 未開累計：每個號碼距離上次開出過了幾期。
        # 為了跟網頁上的數字一致，這裡用全部歷史算，不是只用上面那 40 期。
        allrows = con.execute(
            "SELECT numbers FROM draws WHERE game=? ORDER BY draw_date, draw_id",
            (gid,)).fetchall()
        pool = (list(range(10)) if g.get("kind") == "digit"
                else list(range(1, g["pool"] + 1)))
        seen_at, total = {}, len(allrows)
        for i, (nums,) in enumerate(allrows):
            for v in json.loads(nums):
                seen_at[v] = i
        gapl = sorted(((v, total if v not in seen_at else total - 1 - seen_at[v])
                       for v in pool), key=lambda x: -x[1])[:8]
        out.append(dict(
            gid=gid, name=g["name"], cfg=g,
            draw_id=str(latest[0]), date=latest[1],
            nums=json.loads(latest[2]), special=latest[3],
            sum_main=latest[4], sum_all=latest[5],
            hist=[(json.loads(x[2]), x[3], x[4], x[5]) for x in hist],
            th={sc: dict(zip(("lo", "tie", "hi"), L.thresholds(g, sc)))
                for sc in {c[1] for c in g["charts"]}},
            charts=g["charts"], gaps=gapl,
        ))
    con.close()
    return out


def classify(v, t, mode):
    """回傳 (標籤, 顏色)。mode: bs 大小 / oe 單雙"""
    if mode == "oe":
        return ("單", RED) if v % 2 else ("雙", BLUE)
    if t["tie"] is not None and v == t["tie"]:
        return "和", GREEN
    return ("小", BLUE) if v <= t["lo"] else ("大", RED)


def streak(hist, scope, t, mode):
    """目前連莊：和局不中斷，與路子圖一致"""
    last, n = None, 0
    for nums, sp, s6, s7 in reversed(hist):
        v = s7 if scope == "all" else s6
        lab, _ = classify(v, t, mode)
        if lab == "和":
            continue
        if last is None:
            last, n = lab, 1
        elif lab == last:
            n += 1
        else:
            break
    return last, n


# ────────────────────── 畫圖 ──────────────────────

_BALL = {}


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def ball_img(size, ring):
    """畫一顆跟網頁一樣的彩球：黃色徑向漸層球身＋彩種色外圈＋左上反光。

    PIL 沒有現成的徑向漸層，這裡用 Image.radial_gradient 當混色比例，
    在「米黃 → 金黃 → 深金」三段之間內插，再套圓形遮罩。
    """
    from PIL import Image, ImageDraw
    key = (size, ring)
    if key in _BALL:
        return _BALL[key]
    S = size * 4                                   # 先畫 4 倍再縮，邊緣才平滑
    g = Image.radial_gradient("L").resize((S, S))  # 中心 0、邊緣 255
    lo, mid, hi = _hex("#fff6d0"), _hex("#ffd75e"), _hex("#e8a81f")
    px = g.load()
    ball = Image.new("RGB", (S, S), hi)
    bp = ball.load()
    for y in range(S):
        for x in range(S):
            t = px[x, y] / 255
            if t < 0.5:
                k = t / 0.5
                bp[x, y] = tuple(round(lo[i] + (mid[i] - lo[i]) * k) for i in range(3))
            else:
                k = (t - 0.5) / 0.5
                bp[x, y] = tuple(round(mid[i] + (hi[i] - mid[i]) * k) for i in range(3))
    ball = ball.convert("RGBA")
    # 圓形遮罩
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, S - 1, S - 1], fill=255)
    ball.putalpha(mask)
    d = ImageDraw.Draw(ball)
    w = max(4, S // 16)
    d.ellipse([w // 2, w // 2, S - 1 - w // 2, S - 1 - w // 2], outline=ring, width=w)
    # 原本這裡有一顆左上反光的白點，縮到 40px 之後看起來像多一個小圈圈，
    # 反而干擾數字判讀，所以拿掉；立體感由徑向漸層本身呈現就夠了。
    ball = ball.resize((size, size), Image.LANCZOS)
    _BALL[key] = ball
    return ball

W = 900
PAD = 26


def card_height(g):
    return 102 + 42 * len(g["charts"]) + (34 if g.get("gaps") else 0)


def render(games, when):
    from PIL import Image, ImageDraw
    heights = [card_height(g) for g in games]
    H = 104 + sum(h + 12 for h in heights) + 46
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 標題列
    d.rectangle([0, 0, W, 84], fill=HEAD)
    d.text((PAD, 20), "樂透開獎速報", font=F(30, True), fill=(255, 255, 255))
    d.text((PAD, 56), when.strftime("%Y-%m-%d (") + WEEK[when.weekday()] + when.strftime(") %H:%M"),
           font=F(16), fill=(178, 178, 186))
    d.text((W - PAD, 30), f"{len(games)} 個彩種開獎", font=F(17), fill=(178, 178, 186), anchor="ra")

    y = 104
    for g in games:
        h = card_height(g)
        d.rounded_rectangle([PAD, y, W - PAD, y + h], 10, fill=CARD, outline=LINE_C, width=1)
        x = PAD + 20
        # 彩種名 + 期別
        d.text((x, y + 14), g["name"], font=F(21, True), fill=INK)
        # pilio 來源沒有正式期別（暫用日期當識別），這時就不顯示期別
        if g["draw_id"] != g["date"]:
            d.text((W - PAD - 20, y + 18), f"第 {g['draw_id']} 期",
                   font=F(14), fill=MUTE, anchor="ra")
        wd = dt.date.fromisoformat(g["date"])
        d.text((W - PAD - 20, y + 38), g["date"] + " (" + WEEK[wd.weekday()] + ")",
               font=F(14), fill=MUTE, anchor="ra")

        # 球號
        by = y + 48
        digit = g["cfg"]["kind"] == "digit"
        bx = x
        tint = _hex(g["cfg"].get("tint", "#2f6fed"))
        D = 40
        for nv in g["nums"]:
            img.paste(ball_img(D, tint), (bx, by), ball_img(D, tint))
            d.text((bx + D // 2, by + D // 2 + 1), str(nv) if digit else f"{nv:02d}",
                   font=F(18, True), fill=(26, 18, 6), anchor="mm")
            bx += D + 9
        if g["special"] is not None:
            d.text((bx + 2, by + D // 2), "＋", font=F(19), fill=MUTE, anchor="lm")
            bx += 27
            sp = _hex("#c8352b")
            img.paste(ball_img(D, sp), (bx, by), ball_img(D, sp))
            d.text((bx + D // 2, by + D // 2 + 1), f"{g['special']:02d}",
                   font=F(18, True), fill=(26, 18, 6), anchor="mm")

        # 每一種路子（大小 / 單雙，含 6 球 7 球）
        ry = y + 96
        for title, scope, mode in g["charts"]:
            t = g["th"][scope]
            v = g["sum_all"] if scope == "all" else g["sum_main"]
            lab, col = classify(v, t, mode)
            st_lab, st_n = streak(g["hist"], scope, t, mode)

            d.text((x, ry + 13), title, font=F(15), fill=MUTE, anchor="lm")
            d.text((x + 108, ry + 13), f"總和 {v}", font=F(15), fill=INK, anchor="lm")
            # 結果色塊
            bxx = x + 190
            d.rounded_rectangle([bxx, ry, bxx + 46, ry + 27], 6, fill=col)
            d.text((bxx + 23, ry + 13), lab, font=F(17, True), fill=(255, 255, 255), anchor="mm")
            # 連莊
            if st_lab:
                d.text((bxx + 60, ry + 13), f"連 {st_n} 個{st_lab}", font=F(14),
                       fill=(RED if st_lab in ("大", "單") else BLUE), anchor="lm")
            # 近 12 期迷你路子條，最新在最左邊（與網頁上的橫條方向一致）
            recent = []
            for nums, sp, s6, s7 in g["hist"][-12:]:
                vv = s7 if scope == "all" else s6
                recent.append(classify(vv, t, mode))
            step, box = 21, 18
            sx = W - PAD - 20 - step * len(recent) + (step - box)
            if ry == y + 96:        # 只在第一列標一次方向，放在球號那一列的右側空白處
                d.text((sx, y + 76), "近 12 期　右＝最新", font=F(11), fill=MUTE)
            for i, (lab2, col2) in enumerate(recent):
                x0 = sx + i * step
                d.rounded_rectangle([x0, ry + 4, x0 + box, ry + 22], 4, fill=col2)
                if i == len(recent) - 1:    # 最新一期加白框標示
                    d.rounded_rectangle([x0 - 2, ry + 2, x0 + box + 2, ry + 24], 5,
                                        outline=INK, width=2)
            ry += 42

        # 最久沒開的號碼（未開累計）。純統計，放在最後一列。
        if g.get("gaps"):
            d.text((x, ry + 12), "最久沒開", font=F(15), fill=MUTE, anchor="lm")
            gx = x + 108
            for nv, gp in g["gaps"]:
                if gx > W - PAD - 70:
                    break
                r = 13
                d.ellipse([gx, ry - 1, gx + r * 2, ry + r * 2 - 1],
                          fill=(233, 237, 246), outline=(180, 192, 216))
                d.text((gx + r, ry + r - 1), str(nv) if digit else f"{nv:02d}",
                       font=F(13, True), fill=(58, 74, 110), anchor="mm")
                d.text((gx + r * 2 + 3, ry + 12), f"{gp}", font=F(12),
                       fill=MUTE, anchor="lm")
                gx += r * 2 + 10 + (16 if gp < 10 else 22)
            ry += 34

        y += h + 12

    d.text((PAD, H - 32), "資料來源：台灣彩券 / 樂透王 / 加州官方 / pilio",
           font=F(13), fill=MUTE)
    return img


# ────────────────────── 推送 ──────────────────────

def post_discord(url, png_bytes, text, filename="lottery.png"):
    boundary = uuid.uuid4().hex
    payload = json.dumps({"content": text}).encode("utf-8")
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="payload_json"\r\n')
    parts.append(b"Content-Type: application/json\r\n\r\n")
    parts.append(payload + b"\r\n")
    if png_bytes:                      # 純文字訊息（例如來源異常提醒）不帶圖
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
            .encode())
        parts.append(b"Content-Type: image/png\r\n\r\n")
        parts.append(png_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "lottery-notifier",
    })
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return r.status


def normalize_hook(u):
    """discordapp.com 是 Discord 的舊網域，至今仍有效，統一轉成新網域。"""
    u = u.strip().strip('"').strip("'")
    return u.replace("://discordapp.com/", "://discord.com/") \
            .replace("://www.discord.com/", "://discord.com/")


def is_hook(u):
    return "discord.com/api/webhooks/" in normalize_hook(u)


def read_hook(arg):
    if arg:
        return normalize_hook(arg)
    v = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if v:
        return normalize_hook(v)
    if os.path.exists(HOOK_FILE):
        for line in open(HOOK_FILE, encoding="utf-8-sig"):
            s = line.strip()
            if s and not s.startswith("#"):
                return normalize_hook(s)
    return None


def set_hook():
    """讓使用者在視窗裡貼上網址，畫面不會顯示，避免又被複製出去。"""
    print("\n  ── 設定 Discord 推播網址 ──")
    print("  取得方式：頻道齒輪 → 整合 → Webhook → 新增 → 展開 → 複製 Webhook 網址")
    print("\n  ※ 為了安全，貼上時畫面完全不會顯示任何字，這是正常的。")
    print("    直接 Ctrl+V 貼上，再按 Enter。")
    try:
        import getpass
        u = getpass.getpass("\n  Webhook 網址（不會顯示）> ")
    except Exception:
        u = input("\n  Webhook 網址 > ")
    u = normalize_hook(u)
    if not u:
        print("\n  沒有輸入，已取消。")
        return 1
    if not is_hook(u):
        print("\n  這不是 Discord Webhook 網址。")
        print("  正確格式：https://discord.com/api/webhooks/數字/一長串英數字")
        print("  （discordapp.com 開頭的舊網址也可以）")
        return 1
    with open(HOOK_FILE, "w", encoding="utf-8") as f:
        f.write("# Discord Webhook — 這個檔案不要傳給任何人\n")
        f.write("# 要更換時，把下面那行整行換掉，或再執行一次「設定推播網址.bat」\n")
        f.write(u + "\n")
    tail = u.rsplit("/", 1)[-1]
    print(f"\n  已儲存：…/{tail[:6]}{'…' * bool(tail[6:])}")
    print(f"  位置：{HOOK_FILE}")
    print("\n  接著可以執行「測試推播.bat」實際發一張圖試試。")
    return 0


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(s):
    json.dump(s, open(STATE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, sort_keys=True)


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


def warn_bad_sources(state, webhook=None):
    """來源抓不到時，主動在 Discord 講一聲。

    存在的理由：某個彩種的來源掛掉時，系統的表現是「安靜地什麼都不做」，
    跟「今天本來就沒開獎」長得一模一樣，結果都是隔了好幾天才被發現。
    這裡讀 status.json 的抓取回報，有來源失敗就直接說。
    同一個彩種一天只講一次，不會每 15 分鐘吵你。
    """
    p = os.path.join(HERE, "status.json")
    if not os.path.exists(p):
        return
    try:
        st = json.load(open(p, encoding="utf-8"))
    except Exception:
        return

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    # 只有「資料真的落後」才叫。來源暫時 403、或半夜跑但本來就沒有新開獎，
    # 這種抓不到其實不影響任何事，以前照樣發通知，久了就變成雜訊被忽略。
    daily = {"ca_f5", "tw539", "tw3d", "tw4d"}     # 每天開
    def behind(gid, v):
        d = v.get("latest")
        if not d:
            return True
        try:
            days = (dt.date.today() - dt.date.fromisoformat(d)).days
        except Exception:
            return True
        return days >= (2 if gid in daily else 5)

    bad = [(gid, v) for gid, v in (st.get("fetch") or {}).items()
           if not v.get("ok") and behind(gid, v)]
    if not bad:
        return

    say = [(gid, v) for gid, v in bad
           if state.get("warned_" + gid) != today]
    for gid, v in bad:
        nm = (st.get("games", {}).get(gid) or {}).get("name", gid)
        print(f"  ⚠ {nm} 抓取失敗：{v.get('error')}")
    if not say:
        print("  （今天已經通知過來源異常，不重複打擾）")
        return

    hook = read_hook(webhook)
    if not hook:
        return
    lines = ["⚠ **有彩種抓不到資料**", f"（{st.get('built_by', '?')}執行，"
             f"{st.get('built_at_taipei', '?')}）", ""]
    for gid, v in say:
        nm = (st.get("games", {}).get(gid) or {}).get("name", gid)
        lines.append(f"**{nm}**　停在 {v.get('latest') or '無資料'}")
        lines.append(f"　　{v.get('error')}")
    lines.append("")
    lines.append("網站上其他彩種不受影響。同一款一天只提醒一次。")
    try:
        post_discord(hook, None, "\n".join(lines))
        for gid, _ in say:
            state["warned_" + gid] = today
        save_state(state)
        print(f"  已通知來源異常（{len(say)} 個彩種）")
    except Exception as e:
        print(f"  來源異常通知送出失敗：{e}")


def main():
    _start_log("推播 notify.py")
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default=None)
    ap.add_argument("--test", action="store_true", help="忽略已通知紀錄，強制產生")
    ap.add_argument("--dry", action="store_true", help="只存成 preview.png，不推送")
    ap.add_argument("--set-webhook", action="store_true", help="設定 Discord 推播網址")
    a = ap.parse_args()

    print("=" * 60)
    print("  開獎速報推播")
    print("=" * 60)

    if a.set_webhook:
        return set_hook()

    if not os.path.exists(DB):
        print("  找不到 lottery.db，請先建立資料庫。")
        return 0

    games = load_games()
    state = load_state()

    warn_bad_sources(state, a.webhook)

    new = []
    for g in games:
        if a.test or state.get(g["gid"]) != g["date"]:
            new.append(g)
            print(f"  ● {g['name']}　第 {g['draw_id']} 期（{g['date']}）")
        else:
            print(f"  ─ {g['name']}　無新開獎")

    if not new:
        print("\n  沒有新開獎，不推送。")
        return 0

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    print(f"\n  產生圖片（{len(new)} 個彩種）…")
    img = render(new, now)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    png = buf.getvalue()
    print(f"  圖片大小 {len(png) / 1024:.0f} KB　尺寸 {img.width}x{img.height}")

    if a.dry:
        p = os.path.join(HERE, "preview.png")
        open(p, "wb").write(png)
        print(f"  已存成 {p}（未推送）")
        return 0

    hook = read_hook(a.webhook)
    if not hook:
        print("\n  找不到 Discord Webhook 網址。")
        print(f"  請設定環境變數 DISCORD_WEBHOOK，或寫進 {HOOK_FILE}")
        return 1
    if not is_hook(hook):
        shown = hook if len(hook) <= 60 else hook[:55] + "…"
        print("\n  這個網址不是 Discord Webhook。")
        print(f"  目前讀到的是：{shown}")
        if "/channels/" in hook:
            print("\n  你複製到的是「頻道連結」，不是 Webhook 網址。")
            print("  Webhook 要這樣拿：")
            print("    頻道名稱旁的齒輪（編輯頻道）→ 整合 → Webhook")
            print("    → 新增 Webhook → 展開它 → 按「複製 Webhook 網址」")
        elif hook.startswith("#") or "貼在" in hook or "webhook" in hook.lower() and "/" not in hook:
            print("\n  看起來還沒把網址貼進去，讀到的是說明文字。")
        else:
            print("\n  正確格式長這樣：")
            print("    https://discord.com/api/webhooks/1234567890/AbCdEf...")
        print(f"\n  請確認 {HOOK_FILE} 裡的網址。")
        return 1

    names = "、".join(g["name"] for g in new)
    text = f"**{now.strftime('%m/%d')} 開獎速報**　{names}"
    try:
        st = post_discord(hook, png, text)
        print(f"  已推送（HTTP {st}）")
    except Exception as e:
        print(f"  推送失敗：{e}")
        return 1

    if not a.test:
        for g in new:
            state[g["gid"]] = g["date"]
        save_state(state)
        print(f"  已更新通知紀錄 {STATE}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  已中斷。")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n  發生錯誤：{e}")
        sys.exit(1)
