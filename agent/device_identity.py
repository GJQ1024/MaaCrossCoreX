# -*- coding: utf-8 -*-
"""设备身份锚点：只认一个 android_id，pretask 与壳启动前的 binder 共用。

换机/重装后用环境变量 MFA_TARGET_ANDROID_ID 指定目标设备；未设置时
不做过滤（全部设备参与，不参与歧义判定）——身份只由 android_id 认定，
端口/序列号只是连接地址。
"""

from __future__ import annotations

import os

DEFAULT_TARGET_ANDROID_ID = ""


def target_android_id() -> str:
    return os.environ.get("MFA_TARGET_ANDROID_ID", DEFAULT_TARGET_ANDROID_ID).strip()


def filter_by_target(devices):
    """devices: [(serial, android_id)] → (目标设备, 被滤掉的台数)。

    取不到 android_id 的条目（空串）同样算非目标，避免一台读不到指纹的设备
    把整轮判定拖成「取不到身份」。
    """
    target = target_android_id()
    if not target:
        return list(devices), 0
    matched = [item for item in devices if item[1] == target]
    return matched, len(devices) - len(matched)


def pick_entry(serials):
    """同一 android_id 的多个入口视为同一台，优先 127.0.0.1:<port>，再按字典序。"""
    return min(serials, key=lambda s: (0 if s.startswith("127.0.0.1:") else 1, s))
