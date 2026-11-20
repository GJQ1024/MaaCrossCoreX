# -*- coding: utf-8 -*-
"""把 tutorial_once 的旧实例键迁移成「实例:账号UID」键，支持 dry-run。

旧格式：{"tutorial_build10": {"<实例>": 1}}        # 只有实例，同一实例多账号会串用
新格式：{"tutorial_build10": {"<实例>:<uid>": 1}}

规则：
  * 只新增新键，**不删除**旧键（旧键保留可读，便于回滚与人工核对）；
  * 新键已存在则跳过（幂等，重复跑不会产生重复项、不覆盖已有计数）；
  * 旧键归属只能靠稳定 UID 判定：本机只记录过一个 UID 时才迁移；
    找不到 UID 或记录过多个 UID（无法判断旧计数属于谁）一律标 AMBIGUOUS 并退出码 2，
    **不猜、不写**；
  * 默认 dry-run；确认映射后加 --apply 才落盘。

用法：
  python scripts/migrate_tutorial_state.py                 # dry-run，打印映射
  python scripts/migrate_tutorial_state.py --apply         # 真正写入
  python scripts/migrate_tutorial_state.py --account-uid <uid>   # 显式声明当前账号
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "agent"))

import tutorial_state  # noqa: E402


def collect_legacy(data: dict) -> list[tuple[str, str, int]]:
    """返回 [(计数键, 旧实例键, 计数)]；已是复合键的跳过。"""
    rows = []
    for key, counts in data.items():
        if not isinstance(counts, dict):
            continue
        for instance, value in counts.items():
            if ":" in str(instance):
                continue
            rows.append((key, str(instance), value))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="tutorial_state 旧键迁移（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只打印计划）")
    ap.add_argument("--state", default=str(tutorial_state.STATE_PATH), help="状态文件路径")
    ap.add_argument("--cache", default=str(tutorial_state.ACCOUNT_CACHE_PATH),
                    help="当前账号缓存路径")
    ap.add_argument("--account-state", default=str(tutorial_state.ACCOUNT_STATE_PATH),
                    help="account_switch 状态文件路径（取本机历史 UID）")
    ap.add_argument("--account-uid", default=None,
                    help="显式声明当前账号 UID（写入账号缓存，用于单账号首次启用）")
    ap.add_argument("--index", type=int, default=None, help="配合 --account-uid 的账号序号")
    args = ap.parse_args()

    if args.account_uid:
        tutorial_state.write_account_cache(
            args.account_uid, args.index, source="manual", path=Path(args.cache))
        print(f"[cache] 当前账号 UID={args.account_uid} -> {args.cache}")

    state_path = Path(args.state)
    data = tutorial_state.load_json(state_path)
    legacy = collect_legacy(data)
    if not legacy:
        print(f"[plan] {state_path} 无旧键可迁移（文件不存在或已是复合键）")
        return 0

    uid, source = tutorial_state.current_uid(Path(args.cache), Path(args.account_state))
    candidates = tutorial_state.account_candidates(Path(args.account_state))
    ambiguous_reason = ""
    if not uid:
        ambiguous_reason = (
            "本机未记录到任何 UID" if not candidates
            else f"本机记录过 {len(candidates)} 个 UID（{', '.join(candidates)}），旧计数归属不明"
        )

    if ambiguous_reason:
        for key, instance, value in legacy:
            print(f"[AMBIGUOUS] {key}: {instance}({value}) -> 未知（{ambiguous_reason}）")
        print("[stop] 归属不明的旧键一律不写；请先用 --account-uid 明确当前账号后重跑")
        return 2

    migrated = 0
    skipped = 0
    for key, instance, value in legacy:
        target = tutorial_state.composite_key(instance, uid)
        counts = data[key]
        if target in counts:
            skipped += 1
            print(f"[skip] {key}: {instance} -> {target} 已存在（{counts[target]}），不覆盖")
            continue
        if args.apply:
            counts[target] = value
            migrated += 1
            print(f"[apply] {key}: {instance}({value}) -> {target}")
        else:
            migrated += 1
            print(f"[plan] {key}: {instance}({value}) -> {target}")

    if args.apply and migrated:
        tutorial_state.save_json(state_path, data)
        print(f"[done] 已写入 {state_path}（新增 {migrated} 条，跳过 {skipped} 条）")
    elif not args.apply:
        print(f"[dry-run] 将新增 {migrated} 条，跳过 {skipped} 条；确认后加 --apply")
    else:
        print(f"[done] 无新增（跳过 {skipped} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
