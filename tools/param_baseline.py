#!/usr/bin/env python3
"""从 MFA 外壳日志中提取「任务管线合并」参数，生成参数基线。

用途：补壳阶段 0 的参数对照。外壳在每次构建任务参数时都会打印
`[任务管线合并] 任务#N 名称=[...] 入口=[...] 参数=[...]`（参数为多行 JSON），
这里把这些真实生效参数抽出来，作为后续调度器 dry-run 的比对基准。

不依赖设备，不修改任何配置，只读取 logs 目录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HEAD_RE = re.compile(
    r"\[任务管线合并\] 任务#(?P<index>\d+) 名称=\[(?P<name>.*?)\] 入口=\[(?P<entry>.*?)\] 参数=(?P<rest>.*)$"
)
LOG_START_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
FILE_DATE_RE = re.compile(r"log-(\d{8})\.log")


def normalize_json_text(raw: str) -> str:
    """日志里偶尔出现全角冒号，解析前统一成半角。"""
    return raw.replace("：", ":")


def parse_file(path: Path) -> list[dict]:
    records = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    while i < len(lines):
        m = HEAD_RE.search(lines[i])
        if not m:
            i += 1
            continue

        block = [m.group("rest").strip()]
        j = i + 1
        while j < len(lines) and not LOG_START_RE.match(lines[j]):
            block.append(lines[j])
            j += 1

        raw = normalize_json_text("\n".join(block).strip())
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            records.append(
                {
                    "name": m.group("name"),
                    "entry": m.group("entry"),
                    "param_raw": raw,
                    "param": None,
                    "param_hash": "PARSE_ERROR",
                    "source": path.name,
                }
            )
            i = j
            continue

        canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        records.append(
            {
                "name": m.group("name"),
                "entry": m.group("entry"),
                "param": canonical,
                "param_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
                "source": path.name,
            }
        )
        i = j
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", default="dist/B/logs", help="外壳日志目录（默认 dist/B/logs）")
    parser.add_argument("--out", default="specs/shell-gap-fill-20261111/params", help="基线输出目录")
    args = parser.parse_args()

    log_dir = Path(args.logs)
    files = sorted(
        (p for p in log_dir.glob("log-*.log") if FILE_DATE_RE.search(p.name)),
        key=lambda p: p.name,
    )
    if not files:
        print(f"未找到日志：{log_dir}/log-YYYYMMDD.log")
        return 1

    by_task: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        for rec in parse_file(f):
            by_task[rec["name"]].append(rec)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    baseline_rows = []
    summary_rows = []
    parse_errors = []
    for name in sorted(by_task):
        records = by_task[name]
        if any(r["param"] is None for r in records):
            parse_errors.append(name)
        valid = [r for r in records if r["param"] is not None]
        if not valid:
            continue
        variants = {r["param_hash"] for r in valid}
        latest = valid[-1]
        baseline_rows.append(
            {
                "task": name,
                "entry": latest["entry"],
                "param_hash": latest["param_hash"],
                "param": json.loads(latest["param"]),
                "occurrences": len(valid),
                "variants": len(variants),
                "first_seen": valid[0]["source"],
                "last_seen": latest["source"],
            }
        )
        summary_rows.append(
            {
                "task": name,
                "entry": latest["entry"],
                "occurrences": len(valid),
                "variants": len(variants),
                "hash": latest["param_hash"],
                "last_seen": latest["source"],
            }
        )

    with (out_dir / "baseline.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for row in baseline_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 参数基线（自动生成）",
        "",
        f"生成时间：{generated_at}",
        f"日志范围：{files[0].name} ~ {files[-1].name}",
        f"任务数：{len(baseline_rows)}",
        "",
        "| 任务 | 入口 | 出现次数 | 参数变体数 | 参数哈希 | 最近出现 |",
        "|---|---|---:|---:|---|---|",
    ]
    for row in summary_rows:
        lines.append(
            f"| {row['task']} | `{row['entry']}` | {row['occurrences']} | {row['variants']} | `{row['hash']}` | {row['last_seen']} |"
        )
    lines += [
        "",
        "> 参数变体数 > 1 说明同名任务在不同日期生成过不同参数（例如日期、账号、选项被改过），",
        "> 比对时必须连同出现日期一起看，不能只比一组。",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")

    print(f"已写入 {out_dir / 'baseline.jsonl'}")
    print(f"已写入 {out_dir / 'summary.md'}")
    print(f"任务数={len(baseline_rows)} 日志数={len(files)} 解析失败任务={len(parse_errors)}")
    if parse_errors:
        print("解析失败：" + "、".join(parse_errors))
    multi = [r["task"] for r in summary_rows if r["variants"] > 1]
    print(f"存在多组参数的任务数={len(multi)}")
    if multi:
        print("多组参数任务：" + "、".join(multi))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
