# -*- coding: utf-8 -*-
"""设备身份锚点：只认一个 android_id，pretask 与壳启动前的 binder 共用。

目标 android_id 的来源优先级：
  1. 环境变量 MFA_TARGET_ANDROID_ID（用户显式指定，最高优先）
  2. <程序目录>/config/device_identity.json（首次运行自动绑定写入，可删可重置）
  3. 都没有 → 不按指纹过滤（单台设备照常工作；多台装了游戏的设备按歧义规则停止）

真机等其它装了游戏的设备一律视为非目标，不参与歧义判定——
身份只由 android_id 认定，端口/序列号只是连接地址。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

# 与 ensure_mumu.py / binder 的 config 目录保持一致（<程序目录>/config）。
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "device_identity.json"
ENV_KEY = "MFA_TARGET_ANDROID_ID"


def _log(message):
    """本模块被 pretask / binder 共用，不能反向依赖它们的 logger，写 stderr 即可。"""
    print(f"[身份] {message}", file=sys.stderr, flush=True)


def load_saved_target() -> str:
    """读自动绑定的 android_id；文件缺失/损坏一律当没绑定，不能让 pretask 卡死。"""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("target_android_id") or "").strip()


def save_target_android_id(android_id: str) -> bool:
    """把首次绑定的 android_id 写入 config/device_identity.json；失败只记日志。"""
    value = str(android_id or "").strip()
    if not value:
        return False
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "target_android_id": value,
                    "note": "首次运行自动绑定；删除本文件或运行 ensure_mumu.py --rebind 可重新绑定",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, CONFIG_FILE)
        return True
    except OSError as exc:
        _log(f"写入身份绑定文件失败：{CONFIG_FILE}（{exc}）")
        return False


def clear_saved_target() -> bool:
    """删除自动绑定文件；文件本来就不存在也算成功。"""
    try:
        CONFIG_FILE.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        _log(f"删除身份绑定文件失败：{CONFIG_FILE}（{exc}）")
        return False


def target_android_id() -> str:
    """环境变量 > 自动绑定文件 > 空串（空串表示不按指纹过滤）。"""
    from_env = os.environ.get(ENV_KEY, "").strip()
    if from_env:
        return from_env
    return load_saved_target()


def filter_by_target(devices, target=None):
    """devices: [(serial, android_id)] → (目标设备, 被滤掉的台数)。

    target 省略时按 target_android_id() 取；为空串表示不过滤。
    取不到 android_id 的条目（空串）在配置了目标时同样算非目标，避免一台读不到
    指纹的设备把整轮判定拖成「取不到身份」——但调用方需要能区分「读不到」与
    「读到了但不匹配」，因此 ensure_mumu 会单独记录这些条目。
    """
    if target is None:
        target = target_android_id()
    if not target:
        return list(devices), 0
    matched = [item for item in devices if item[1] == target]
    return matched, len(devices) - len(matched)


def pick_entry(serials):
    """同一 android_id 的多个入口视为同一台，优先 127.0.0.1:<port>，再按字典序。"""
    return min(serials, key=lambda s: (0 if s.startswith("127.0.0.1:") else 1, s))
