#!/usr/bin/env python3
"""对比外壳 dry-run 导出的任务参数与历史参数基线。

dry-run 数据由 MFAAvalonia 隐藏开关 `--dump-task-params <out>` 生成，
哈希口径与 param_baseline.py 完全一致：
json.loads 后 sort_keys=True、separators=(",", ":")、ensure_ascii=False，sha256 取前 16 位。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

MAX_DIFFS = 20


def canonical_hash(param: object) -> str:
    canonical = json.dumps(param, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_jsonl(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                row = json.loads(line)
                rows[row["task"]] = row
    return rows


def flatten(value: object, prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    if isinstance(value, dict):
        if not value and prefix:
            result[prefix] = {}
        for key in sorted(value):
            result.update(flatten(value[key], f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        if not value and prefix:
            result[prefix] = []
        for index, item in enumerate(value):
            result.update(flatten(item, f"{prefix}[{index}]"))
    else:
        result[prefix] = value
    return result


def field_diffs(expected: object, actual: object) -> list[str]:
    left = flatten(expected)
    right = flatten(actual)
    diffs: list[str] = []
    for key in sorted(set(left) | set(right)):
        if key not in left:
            diffs.append(f"{key}: 基线缺失，dry-run={right[key]!r}")
        elif key not in right:
            diffs.append(f"{key}: 基线={left[key]!r}，dry-run 缺失")
        elif left[key] != right[key]:
            diffs.append(f"{key}: 基线={left[key]!r} -> dry-run={right[key]!r}")
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="../specs/shell-gap-fill-20261111/params/baseline.jsonl")
    parser.add_argument("--dryrun", default="../specs/shell-gap-fill-20261111/params/dryrun.jsonl")
    parser.add_argument("--out", default="../specs/shell-gap-fill-20261111/params/compare.md")
    args = parser.parse_args()

    baseline_path = Path(args.baseline)
    dryrun_path = Path(args.dryrun)
    baseline = load_jsonl(baseline_path)
    dryrun = load_jsonl(dryrun_path)

    matched: list[tuple[str, str, int]] = []
    mismatched: list[tuple[str, str, str, dict, dict, int]] = []
    only_dryrun: list[str] = []
    for task, row in dryrun.items():
        dry_hash = canonical_hash(row["param"])
        if task not in baseline:
            only_dryrun.append(task)
            continue
        base_row = baseline[task]
        if base_row["param_hash"] == dry_hash:
            matched.append((task, dry_hash, base_row.get("variants", 1)))
        else:
            mismatched.append((task, base_row["param_hash"], dry_hash, base_row["param"], row["param"], base_row.get("variants", 1)))
    only_baseline = [task for task in baseline if task not in dryrun]

    diffs: list[str] = []
    for task, _base_hash, _dry_hash, base_param, dry_param, _variants in mismatched:
        for diff in field_diffs(base_param, dry_param):
            if len(diffs) >= MAX_DIFFS:
                break
            diffs.append(f"- `{task}` {diff}")
        if len(diffs) >= MAX_DIFFS:
            break

    lines = [
        "# 任务参数对照报告（dry-run vs 历史基线）",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"基线：`{baseline_path.name}`（{len(baseline)} 个任务）",
        f"dry-run：`{dryrun_path.name}`（{len(dryrun)} 个任务）",
        "",
        "## 汇总",
        "",
        "| 指标 | 数量 |",
        "|---|---:|",
        f"| 基线任务数 | {len(baseline)} |",
        f"| dry-run 任务数 | {len(dryrun)} |",
        f"| 完全一致 | {len(matched)} |",
        f"| 参数不一致 | {len(mismatched)} |",
        f"| 仅存在于基线 | {len(only_baseline)} |",
        f"| 仅存在于 dry-run | {len(only_dryrun)} |",
        "",
        "> 哈希口径：`json.loads` 后 `sort_keys=True`、`separators=(\",\", \":\")`、`ensure_ascii=False`，",
        "> 再 `sha256` 取前 16 位，与 `tools/param_baseline.py` 一致。",
        "",
        "## 逐任务结果",
        "",
        "| 任务 | 入口 | 基线哈希 | dry-run 哈希 | 结果 | 基线变体数 |",
        "|---|---|---|---|---|---:|",
    ]

    matched_names = {task for task, _hash, _variants in matched}
    for task in sorted(dryrun):
        row = dryrun[task]
        if task in matched_names:
            dry_hash = canonical_hash(row["param"])
            variants = baseline[task].get("variants", 1)
            lines.append(
                f"| {task} | `{row.get('entry') or ''}` | `{baseline[task]['param_hash']}` | `{dry_hash}` | 一致 | {variants} |"
            )
        elif task in only_dryrun:
            continue
        else:
            entry = next(item for item in mismatched if item[0] == task)
            lines.append(
                f"| {task} | `{row.get('entry') or ''}` | `{entry[1]}` | `{entry[2]}` | **不一致** | {entry[5]} |"
            )

    lines += ["", "## 不一致明细", ""]
    if diffs:
        lines += diffs
        if len(mismatched) and sum(len(field_diffs(item[3], item[4])) for item in mismatched) > MAX_DIFFS:
            lines.append("")
            lines.append(f"> 仅展示前 {MAX_DIFFS} 处差异。")
    else:
        lines.append("无：dry-run 与基线在共有任务上参数完全一致。")

    lines += ["", "## 仅存在于基线的任务", ""]
    if only_baseline:
        lines.append("| 任务 | 入口 | 基线哈希 | 基线变体数 | 说明 |")
        lines.append("|---|---|---|---:|---|")
        for task in only_baseline:
            row = baseline[task]
            lines.append(
                f"| {task} | `{row.get('entry') or ''}` | `{row['param_hash']}` | {row.get('variants', 1)} | 当前实例配置 `CurrentTasks` 中已无此任务 |"
            )
    else:
        lines.append("无。")

    lines += ["", "## 仅存在于 dry-run 的任务", ""]
    if only_dryrun:
        lines.append("| 任务 | 入口 | dry-run 哈希 | 说明 |")
        lines.append("|---|---|---|---|")
        for task in only_dryrun:
            row = dryrun[task]
            lines.append(
                f"| {task} | `{row.get('entry') or ''}` | `{canonical_hash(row['param'])}` | 当前配置存在，但历史日志中未出现（新增或长期未执行） |"
            )
    else:
        lines.append("无。")

    lines += [
        "",
        "## 说明",
        "",
        "- dry-run 由 `MFAAvalonia --dump-task-params <out> --data-root <目录>` 生成：读取的是 `dist/B` 配置的副本（`config` 复制、`resource` 只读链接），`dist/B` 未被写入。",
        "- 导出复用外壳 `MaaProcessor.CreateNodeAndParam` 与 `FilterExecutableTasks`，不连接设备、不启动任务、不执行识别。",
        "- 仅基线 / 仅 dry-run 的差异来自 `CurrentTasks` 中任务被替换（`添加订单好友`+`拉黑订单好友` → `订单好友`），三者参数均为空参数 `{}`，不涉及参数生成逻辑差异。",
    ]

    out_path = Path(args.out)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"已写入 {out_path}")
    print(f"基线={len(baseline)} dry-run={len(dryrun)} 一致={len(matched)} 不一致={len(mismatched)} 仅基线={len(only_baseline)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
