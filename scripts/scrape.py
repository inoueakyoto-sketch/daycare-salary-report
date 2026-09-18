#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大津市・草津市・守山市の放課後等デイサービス事業所における
児童発達支援管理責任者・児童指導員・専門職員の求人給与を
ジョブメドレー(job-medley.com)から取得し、data/summary.json / data/jobs_raw.csv を再生成する。

- job-medley.com の robots.txt は /apl/, /nm/, /ot/, /pt/, /st/, /cp/, /facility/ を
  一般UAに対して制限していないため、当該パスのみを対象にする。
- 取得前に robots.txt を都度確認し、Disallow に一致する場合はそのセクションをスキップする。
- 解析はページ構造の変化に強くするため、HTMLをテキスト化した上で
  施設名らしき行 + 給与パターン + 雇用形態パターンの近接マッチで抽出する
  (厳密なCSSセレクタには依存しない)。
- 取得や解析に失敗したセクションがあっても、既存のsummary.jsonを保持し
  サイト全体を壊さないようにする(フェイルソフト)。
"""
import json
import re
import sys
import time
import urllib.request
import urllib.robotparser
from pathlib import Path
from datetime import date

BASE = "https://job-medley.com"
UA = "Mozilla/5.0 (compatible; DaycareSalaryReportBot/1.0; +https://github.com/)"

CITY_CODES = {
    "大津市": "25201",
    "草津市": "25206",
    "守山市": "25207",
}

# job-medley 上の職種パス。専門職員はOT/PT/ST/公認心理師の複数パスをまとめて1カテゴリにする
ROLE_PATHS = {
    "児発管": ["nm"],
    "児童指導員": ["apl"],
    "専門職員": ["ot", "pt", "st", "cp"],
}

EXCLUDE_KEYWORDS = ["児童発達支援事業所", "コペルプラス", "レモネードキッズ", "あおい湖"]
# ↑ 児童発達支援(0-6歳)専用で放課後等デイサービスを行っていないと判明している施設名の一部。
#   将来的に事業所一覧(WAM NET等)との突合に置き換えるのが望ましい。

SALARY_RE = re.compile(r"(月給|時給)\s*([\d,]{3,7})\s*円?\s*(?:[~〜～\-]\s*([\d,]{3,7})\s*円)?")
EMPLOY_RE = re.compile(r"(正職員|正社員|パート|アルバイト|契約社員|非常勤)")

OUT_DIR = Path(__file__).resolve().parent.parent / "data"
CSV_PATH = OUT_DIR / "jobs_raw.csv"
JSON_PATH = OUT_DIR / "summary.json"


def robots_allowed(path: str) -> bool:
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{BASE}/robots.txt")
    try:
        rp.read()
    except Exception as e:
        print(f"[warn] robots.txt fetch failed ({e}); skipping {path} to be safe", file=sys.stderr)
        return False
    return rp.can_fetch(UA, f"{BASE}{path}")


def fetch(path: str) -> str | None:
    if not robots_allowed(path):
        print(f"[skip] robots.txt disallows {path}", file=sys.stderr)
        return None
    req = urllib.request.Request(f"{BASE}{path}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[warn] fetch failed for {path}: {e}", file=sys.stderr)
        return None


def html_to_lines(html: str) -> list[str]:
    # script/styleを除去
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    # ブロック要素の終わりを改行に変換
    html = re.sub(r"</(h1|h2|h3|h4|li|p|div|tr|td|dt|dd|span)>", "\n", html, flags=re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    lines = [l.strip() for l in text.splitlines()]
    return [l for l in lines if l]


def looks_like_facility_name(line: str) -> bool:
    if len(line) < 3 or len(line) > 60:
        return False
    if SALARY_RE.search(line) or EMPLOY_RE.search(line):
        return False
    if any(k in line for k in ["求人", "採用", "検索", "ログイン", "会員", "新着", "ジョブメドレー", "件"]):
        return False
    return True


def parse_jobs(html: str, city: str, role: str, source: str) -> list[dict]:
    lines = html_to_lines(html)
    jobs = []
    last_facility = None
    for line in lines:
        if looks_like_facility_name(line):
            last_facility = line
            continue
        sal = SALARY_RE.search(line)
        if sal and last_facility:
            pay_type = sal.group(1)
            pay_min = int(sal.group(2).replace(",", ""))
            pay_max = int(sal.group(3).replace(",", "")) if sal.group(3) else pay_min
            emp = EMPLOY_RE.search(line)
            employment = "正職員" if (emp and emp.group(1) in ("正職員", "正社員")) else (
                "パート" if emp else "不明"
            )
            if any(k in last_facility for k in EXCLUDE_KEYWORDS):
                continue
            jobs.append({
                "city": city, "facility": last_facility, "role": role,
                "employment": employment,
                "pay_type": "月給" if pay_type == "月給" else "時給",
                "pay_min": pay_min, "pay_max": pay_max,
                "source": source,
            })
    return jobs


def dedupe(jobs: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for j in jobs:
        key = (j["city"], j["facility"], j["role"], j["employment"], j["pay_type"], j["pay_min"], j["pay_max"])
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out


def aggregate(rows: list[dict]) -> dict:
    def agg(items):
        vals = [(r["pay_min"] + r["pay_max"]) / 2 for r in items]
        return {
            "n": len(items),
            "avg": sum(vals) / len(vals),
            "min": min(r["pay_min"] for r in items),
            "max": max(r["pay_max"] for r in items),
        }

    monthly, hourly = [], []
    for role in ["児発管", "児童指導員", "専門職員"]:
        m = [r for r in rows if r["role"] == role and r["pay_type"] == "月給"]
        h = [r for r in rows if r["role"] == role and r["pay_type"] == "時給"]
        if m:
            d = agg(m); d["role"] = role; monthly.append(d)
        if h:
            d = agg(h); d["role"] = role; hourly.append(d)

    by_city = []
    for city in CITY_CODES:
        items = [r for r in rows if r["city"] == city and r["role"] == "児童指導員" and r["pay_type"] == "月給"]
        if items:
            d = agg(items); d["city"] = city; by_city.append(d)

    return {"monthly": monthly, "hourly": hourly, "by_city_shido": by_city}


def main():
    all_jobs = []
    for city, code in CITY_CODES.items():
        for role, paths in ROLE_PATHS.items():
            for p in paths:
                path = f"/{p}/city{code}/"
                html = fetch(path)
                time.sleep(1)  # 負荷をかけないための間隔
                if not html:
                    continue
                jobs = parse_jobs(html, city, role, f"job-medley {p}/city{code}")
                all_jobs.extend(jobs)

    all_jobs = dedupe(all_jobs)

    if len(all_jobs) < 20:
        # 解析に失敗している可能性が高い場合は既存データを壊さない
        print(f"[warn] only {len(all_jobs)} rows parsed; keeping previous data untouched", file=sys.stderr)
        if JSON_PATH.exists():
            return
        # 初回実行で0件なら空でも書き出す(後続で気づけるように)

    summary = {
        "updated_at": date.today().strftime("%Y年%m月%d日"),
        "rows": all_jobs,
        "summary": aggregate(all_jobs),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    import csv
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["city", "facility", "role", "employment", "pay_type", "pay_min", "pay_max", "source"])
        w.writeheader()
        for r in all_jobs:
            w.writerow(r)

    print(f"done: {len(all_jobs)} rows written")


if __name__ == "__main__":
    main()
