# -*- coding: utf-8 -*-
"""雲端採集：抓一次盤口，存成原始 JSON 檔。

這支跑在 GitHub Actions 上，**摸不到你電腦裡的 mlb.db**。所以它只做一件事：
把 API 的原始回應原封不動存成檔案，比對與寫入資料庫留在本機做
（見 import_snapshots.py）。

這樣切開有三個好處：

- 雲端不需要資料庫，也就不需要把資料庫塞進 repo 或想辦法同步。
- 檔案只新增不修改，git 處理起來乾淨，也不會有併發寫入的問題。
- 原始回應完整保留。之後改變解析方式或型態定義時不必重抓，
  而歷史盤口重抓是要花錢的。

用法（Actions 會這樣呼叫）：
    python collect.py --out snapshots
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"
REGIONS = "eu"
MARKETS = "h2h,spreads,totals"
COST_PER_CALL = 3


def fetch(key):
    params = {"apiKey": key, "regions": REGIONS, "markets": MARKETS,
              "oddsFormat": "american"}
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{API}/sports/{SPORT}/odds?{query}",
        headers={"User-Agent": "odds-collector"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        remaining = resp.headers.get("x-requests-remaining")
        return body, int(remaining) if remaining else None


def filename(stamp):
    """檔名用 UTC 時間，冒號換成減號——Windows 檔名不接受冒號。"""
    return stamp.replace(":", "-") + ".json"


def save(data, out_dir, stamp, remaining=None):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename(stamp))
    payload = {"captured_at": stamp, "sport": SPORT, "regions": REGIONS,
               "markets": MARKETS, "credits_remaining": remaining,
               "games": len(data), "data": data}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    return path


def main():
    ap = argparse.ArgumentParser(description="抓一次盤口存成 JSON")
    ap.add_argument("--out", default="snapshots", help="輸出目錄")
    args = ap.parse_args()

    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        raise SystemExit("環境變數 ODDS_API_KEY 沒有設定")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data, remaining = fetch(key)
    if not data:
        print(f"{stamp}：目前沒有賽事，不存檔（仍已扣 {COST_PER_CALL} 點）")
        return

    path = save(data, args.out, stamp, remaining)
    print(f"{stamp}：{len(data)} 場 → {path}")
    if remaining is not None:
        print(f"剩餘額度 {remaining}，可再跑 {remaining // COST_PER_CALL} 次")
        if remaining < COST_PER_CALL * 10:
            print("⚠ 額度快用完了")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
