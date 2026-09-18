#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大津市・草津市・守山市の放課後等デイサービス事業所における
児童発達支援管理責任者・児童指導員・専門職員の求人給与を
ジョブメドレー(job-medley.com)から取得し、data/summary.json / data/jobs_raw.csv を再生成する。

- job-medley.com の robots.txt は /apl/, /nm/, /ot/, /pt/, /st/, /cp/, /facility/ を
  一般UAに対して制限していないため、当該パスのみを対象にする。
- 取得前に robots.txt を都度確認し、Disallow に一致する場合はそのセクションをスキップする。
- 解析は「求人個別ページへのリンク(/apl/12345/ のような数値ID付きURL)を含む見出し」を
  求人カードの起点として検出し、そのカード内(次のカードが始まるまでの範囲)に限定して
  給与・雇用形態を探す方式にしている。これにより、広告コピー等の無関係なテキストを
  施設名や給与と誤って結び付けることを防ぐ。
- 各行には、集計値の根拠を後から確認できるよう、求人個別ページの実URL(source_url)を
  必ず保持する。URLが取得できなかった行は採用しない。
- 取得や解析に失敗した場合や、明らかに不自然な結果(件数が極端に少ない/多い、
  相場からかけ離れた金額)が出た場合は、既存のsummary.jsonを保持し
  サイト全体を壊さないようにする(フェイルソフト)。
"""
import csv
import json
import re
import sys
import time
import urllib.request
import urllib.robotparser
from pathlib import Path
from datetime import date

BASE = "https://job-medley.com"
UA = "Mozilla/5.0 (compatible; DaycareSalaryReportBot/1.0; +https://github.com/inoueakyoto-sketch/daycare-salary-report)"

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

# 放課後等デイサービスの求人であることを確認するためのキーワード
# (job-medleyのカテゴリページには近縁の児童発達支援など他サービスの求人も混在するため)
INCLUDE_HINT = ["放課後等デイサービス", "放課後デイサービス", "放デイ", "学童"]
EXCLUDE_HINT = ["児童発達支援事業所", "児童発達支援センター"]  # 放課後等デイを併記しない限り除外

# 求人個別ページのURLパターン (例: /apl/791850/ , /nm/637160/ )
# <h3>見出し内のリンクを想定しているが、マークアップの変化に備えて
# 「該当パス+数値IDへのリンク」自体を求人カードの起点として広めに検出する。
CARD_RE = re.compile(
    r'<a[^>]+href="(/(?:apl|nm|ot|pt|st|cp)/(\d+)/[^"]*)"[^>]*>(.*?)</a>',
    re.S,
)
SALARY_RE = re.compile(r"(月給|時給)\s*([\d,]{3,7})\s*円?\s*(?:[~〜～\-]\s*([\d,]{3,7})\s*円)?")
EMPLOY_RE = re.compile(r"(正職員|正社員|パート|アルバイト|契約社員|非常勤)")

# 明らかに相場から外れる値は取り込まない(パース事故のフェイルセーフ)
PLAUSIBLE = {
    "月給": (100_000, 600_000),
    "時給": (900, 3_500),
}

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


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", s).strip()


def parse_jobs(html: str, city: str, role: str) -> list[dict]:
    matches = list(CARD_RE.finditer(html))
    jobs = []
    for i, m in enumerate(matches):
        href, job_id, raw_name = m.group(1), m.group(2), m.group(3)
        facility = strip_tags(raw_name)
        NON_NAME = {"続きを見る", "求人を見る", "詳細を見る", "お気に入り", "キープする", "NEW", "No image", ""}
        if not facility or len(facility) < 2 or facility in NON_NAME:
            continue

        # このカードの範囲 = このマッチの終わりから次のカードの開始まで
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html), start + 4000)
        block = html[start:end]
        block_text = strip_tags(block)

        # 放課後等デイサービスの求人か確認(施設名・カード本文どちらかにヒントがあればOK)
        haystack = facility + " " + block_text
        if any(k in haystack for k in EXCLUDE_HINT) and not any(k in haystack for k in INCLUDE_HINT):
            continue

        sal = SALARY_RE.search(block_text)
        if not sal:
            continue
        pay_type = "月給" if sal.group(1) == "月給" else "時給"
        try:
            pay_min = int(sal.group(2).replace(",", ""))
            pay_max = int(sal.group(3).replace(",", "")) if sal.group(3) else pay_min
        except ValueError:
            continue

        lo, hi = PLAUSIBLE[pay_type]
        if not (lo <= pay_min <= hi and lo <= pay_max <= hi):
            print(f"[skip] implausible salary {pay_type} {pay_min}-{pay_max} for {facility}", file=sys.stderr)
            continue

        emp = EMPLOY_RE.search(block_text)
        employment = "正職員" if (emp and emp.group(1) in ("正職員", "正社員")) else (
            "パート" if emp else "不明"
        )

        jobs.append({
            "city": city,
            "facility": facility,
            "role": role,
            "employment": employment,
            "pay_type": pay_type,
            "pay_min": pay_min,
            "pay_max": pay_max,
            "source_url": f"{BASE}{href.split('?')[0]}",
        })
    return jobs


def dedupe(jobs: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for j in jobs:
        key = j["source_url"]
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
                jobs = parse_jobs(html, city, role)
                all_jobs.extend(jobs)
                print(f"[info] {path} -> {len(jobs)} rows")

    all_jobs = dedupe(all_jobs)
    print(f"[info] total after dedupe: {len(all_jobs)}")

    if len(all_jobs) < 20:
        print(f"[warn] only {len(all_jobs)} rows parsed; keeping previous data untouched", file=sys.stderr)
        if JSON_PATH.exists():
            sys.exit(1)  # ワークフロー側でコミットしないようにエラー終了させる

    summary = {
        "updated_at": date.today().strftime("%Y年%m月%d日"),
        "rows": all_jobs,
        "summary": aggregate(all_jobs),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["city", "facility", "role", "employment", "pay_type", "pay_min", "pay_max", "source_url"])
        w.writeheader()
        for r in all_jobs:
            w.writerow(r)

    print(f"done: {len(all_jobs)} rows written")


if __name__ == "__main__":
    main()
