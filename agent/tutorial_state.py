"""tutorial_once 状态键：实例 + 账号（UID）维度。

纯标准库，无 maa 依赖，供两处共用：
  * ``agent/my_action.py``：运行时一次性闸；
  * ``scripts/migrate_tutorial_state.py``：旧键迁移与 dry-run。

账号标识只用稳定 UID（``account_switch.py`` 在大厅 OCR 出的 UID），不用昵称等可变显示名。
当前账号缓存写在 ``config/account_current.json``，由切号成功（或脚本显式声明）时刷新。
缺账号标识时**不做任何默认假设**：调用方按 fail-safe「未知、跳过并记录」处理。
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "config" / "tutorial_state.json"
ACCOUNT_CACHE_PATH = PROJECT_ROOT / "config" / "account_current.json"
EVENT_PATH = PROJECT_ROOT / "config" / "tutorial_state_events.jsonl"

DEFAULT_INSTANCE = "default"


def _maa_root() -> Path:
    """工作区根（MAA）：优先找名为 MAA 的祖先目录，找不到退回 parents[3]。"""
    for parent in PROJECT_ROOT.parents:
        if parent.name == "MAA":
            return parent
    return PROJECT_ROOT.parents[3]


ACCOUNT_STATE_PATH = _maa_root() / "sandbox" / "account_switch_state.json"


def instance_key() -> str:
    raw = os.environ.get("MFA_INSTANCE_ID") or os.environ.get("MAA_INSTANCE_ID") or ""
    return raw.strip() or DEFAULT_INSTANCE


def composite_key(instance: str, uid: str) -> str:
    return f"{instance}:{uid}"


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def record_event(event: str, key: str = "", detail: str = "",
                 path: Path | None = None) -> None:
    """跳过/未知类事件单独记流水，便于复盘区分「真完成」与「未知跳过」。"""
    entry = {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        "key": key,
        "instance": instance_key(),
        "detail": detail,
    }
    try:
        target = path or EVENT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_account_cache(path: Path | None = None) -> dict:
    return load_json(path or ACCOUNT_CACHE_PATH)


def write_account_cache(uid: str | None, index: int | None = None,
                        source: str = "account_switch",
                        path: Path | None = None) -> None:
    """刷新当前账号缓存；uid 为空表示「当前账号未知」，闸会按 fail-safe 跳过。"""
    save_json(path or ACCOUNT_CACHE_PATH, {
        "instance": instance_key(),
        "index": index,
        "uid": str(uid).strip() if uid else None,
        "source": source,
        "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    })


def account_candidates(state_path: Path | None = None) -> list[str]:
    """本机历史上出现过的稳定 UID（``account_switch_state.json`` 里去重）。

    只有恰好 1 个候选时，旧实例键的归属才无歧义；0 或多于 1 个都视为无法判断。
    """
    data = load_json(state_path or ACCOUNT_STATE_PATH)
    uids: list[str] = []
    for record in data.values():
        if isinstance(record, dict):
            uid = str(record.get("uid") or "").strip()
            if uid and uid not in uids:
                uids.append(uid)
    return uids


def current_uid(cache_path: Path | None = None,
                state_path: Path | None = None) -> tuple[str | None, str]:
    """返回 (uid, 来源)。取不到唯一账号时 uid=None。

    来源：``cache``（切号后写入，实例需匹配）/ ``single-record``（本机只记录过一个 UID）
    / ``none``。
    """
    cache = read_account_cache(cache_path)
    uid = str(cache.get("uid") or "").strip()
    cached_instance = str(cache.get("instance") or DEFAULT_INSTANCE)
    if uid and cached_instance == instance_key():
        return uid, "cache"
    candidates = account_candidates(state_path)
    if len(candidates) == 1:
        return candidates[0], "single-record"
    return None, "none"


def resolve_count(counts: dict, instance: str, uid: str | None,
                  candidates: list[str] | None = None) -> tuple[int | None, str]:
    """解析计数并给出状态。

    返回 (count, status)：
      * (n, "ok")       命中新键 ``instance:uid``；
      * (0, "new")      有账号、无任何记录 -> 新号，可进教程；
      * (None, "legacy") 只有旧实例键且无法确认归属 -> fail-safe 跳过；
      * (None, "unknown") 拿不到账号标识 -> fail-safe 跳过；
      * (None, "corrupt") 计数不是整数 -> 按无法确认处理。
    """
    if not uid:
        # 有旧键但拿不到账号：这是「旧键归属不明」，与「全新环境、无任何记录」区分开。
        return (None, "legacy") if counts.get(instance) is not None else (None, "unknown")
    value = counts.get(composite_key(instance, uid))
    if value is not None:
        try:
            return int(value), "ok"
        except (TypeError, ValueError):
            return None, "corrupt"
    legacy = counts.get(instance)
    if legacy is None:
        return 0, "new"
    # 旧键存在但未迁移：仅当本机只有这一个候选账号时才能确认归属。
    if candidates == [uid]:
        try:
            return int(legacy), "legacy-ok"
        except (TypeError, ValueError):
            return None, "corrupt"
    return None, "legacy"
