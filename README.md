# daycare-salary-report

大津市・草津市・守山市の放課後等デイサービス事業所における、児童発達支援管理責任者・児童指導員・専門職員の給与相場レポート。

- サイト: GitHub Pages で公開 (Actions からデプロイ)
- データ更新: 3日間隔 (GitHub Actions cron)
- データ出典: [job-medley.com](https://job-medley.com/)（robots.txtでクロール許可されている `/apl/` `/nm/` `/ot/` `/pt/` `/st/` `/cp/` 配下のみ使用）

## 構成

- `index.html` — レポート本体
- `data/summary.json` — 集計データ(自動更新)
- `data/jobs_raw.csv` — 生データ(自動更新)
- `scripts/scrape.py` — データ取得・集計スクリプト
- `.github/workflows/update.yml` — 3日間隔の自動更新・再デプロイ
