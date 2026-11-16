# -*- coding: utf-8 -*-
"""导出资源统计 JSONL 为 CSV。

用法：
    python tools/export_resource_csv.py [account] [--date YYYY-MM-DD] [--out path.csv]
    - account 缺省为 default（或 MFA_INSTANCE_ID）
    - --date 缺省为今天，传 all 则导出全部
    - --out 缺省为 config/resource_stats/<account>_<date>.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

# 让 agent 目录可导入
_AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from resource_stats import jsonl_path, RESOURCES  # noqa: E402

CSV_HEADERS = [
    "ts", "account", "task", "run_id", "phase",
    "ap_before", "ap_after", "ap_delta",
    "coin_before", "coin_after", "coin_delta",
    "gem_before", "gem_after", "gem_delta",
]


def _resource_value(record: dict, key: str) -> int | None:
    res = record.get("resources", {}).get(key, {})
    val = res.get("value")
    if key == "ap":
        return val.get("current") if isinstance(val, dict) else None
    return val


def export(account: str, date: str, out: Path | None) -> Path:
    path = jsonl_path(account)
    if not path.exists():
        print(f"[export] 数据文件不存在: {path}")
        sys.exit(1)

    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if date != "all":
                if not rec.get("ts", "").startswith(date):
                    continue
            rows.append(rec)

    # before 记录按 run_id 索引，after 记录补齐差值
    before_map: dict[str, dict] = {}
    after_rows: list[dict] = []
    for rec in rows:
        if rec.get("phase") == "before":
            before_map[rec["run_id"]] = rec
        elif rec.get("phase") == "after":
            after_rows.append(rec)

    out_path = out or (path.parent / f"{account}_{date}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for rec in after_rows:
            before = before_map.get(rec.get("run_id", ""), {})
            delta = rec.get("delta", {})
            row = {
                "ts": rec.get("ts", ""),
                "account": rec.get("account", ""),
                "task": rec.get("task", ""),
                "run_id": rec.get("run_id", ""),
                "phase": "after",
            }
            for key in ("ap", "coin", "gem"):
                row[f"{key}_before"] = _resource_value(before, key)
                row[f"{key}_after"] = _resource_value(rec, key)
                row[f"{key}_delta"] = delta.get(key)
            writer.writerow(row)

    print(f"[export] 已导出 {len(after_rows)} 条任务记录 → {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="导出资源统计 CSV")
    parser.add_argument("account", nargs="?", default=None, help="账号名（缺省 default）")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="日期 YYYY-MM-DD 或 all（缺省今天）")
    parser.add_argument("--out", default=None, help="输出 CSV 路径")
    args = parser.parse_args()

    account = args.account or _default_account()
    out = Path(args.out) if args.out else None
    export(account, args.date, out)


def _default_account() -> str:
    import os
    configured = os.environ.get("MAA_INSTANCE_CONFIG")
    if configured:
        return Path(configured).stem
    return os.environ.get("MFA_INSTANCE_ID", "").strip() or "default"


if __name__ == "__main__":
    main()
