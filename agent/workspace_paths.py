# -*- coding: utf-8 -*-
"""工作区根目录解析（全仓唯一实现）。

历史写法 ``Path(__file__).resolve().parents[3]`` 在浅目录（解压到
``D:\\Downloads\\xxx``）会 IndexError，在 ``C:\\a\\b\\c\\d`` 这类布局还可能
解析到盘符根，把 sandbox 写到 ``C:\\sandbox``。统一策略：

1. 环境变量 ``MFA_MAA_ROOT`` / ``MAA_ROOT``（显式覆盖）；
2. ``config/app_paths.json`` 的 ``maa_root``（显式覆盖，读坏按未配置）；
3. 名为 ``MAA`` 的祖先目录（开发工作区）；
4. ``parents[3]``（仅当存在、不是盘符根，且 ``parents[2]`` 名为 ``apps``）；
5. 调用方给的缺省目录（通常是 ``PROJECT_ROOT`` 或 ``PROJECT_ROOT/config``）。

任何情况下都不做越界索引；返回的路径由调用方按需创建。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


ENV_KEYS = ("MFA_MAA_ROOT", "MAA_ROOT")
CONFIG_REL = Path("config") / "app_paths.json"


def is_drive_root(path: Path) -> bool:
    """``C:\\`` / ``D:\\`` 这类盘符根（parent 指向自身）。"""
    try:
        return path.parent == path
    except OSError:
        return False


def configured_root(project_root: Path) -> Path | None:
    for key in ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return Path(value).expanduser()
    try:
        data = json.loads((project_root / CONFIG_REL).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    raw = data.get("maa_root") if isinstance(data, dict) else None
    value = raw.strip() if isinstance(raw, str) else ""
    return Path(value).expanduser() if value else None


def is_apps_layout(project_root: Path) -> bool:
    """仅 ``<根>/apps/<应用>/third_party/MCC_Framework`` 这一种布局认 ``parents[3]``。"""
    try:
        return len(project_root.parents) > 2 and project_root.parents[2].name.lower() == "apps"
    except OSError:
        return False


def maa_root(project_root: Path, fallback: Path | None = None) -> Path:
    """按统一策略解析工作区根；``fallback`` 缺省为 ``project_root``。

    ``parents[3]`` 只在 ``<根>/apps/<应用>/third_party/MCC_Framework`` 布局下采用：
    解压到任意深层目录时它可能指向 ``AppData`` 之类的位置，不能拿来当 sandbox 根。
    """
    explicit = configured_root(project_root)
    if explicit is not None:
        return explicit
    for parent in project_root.parents:
        if parent.name == "MAA":
            return parent
    if len(project_root.parents) > 3:
        candidate = project_root.parents[3]
        if not is_drive_root(candidate) and is_apps_layout(project_root):
            return candidate
    return fallback if fallback is not None else project_root
