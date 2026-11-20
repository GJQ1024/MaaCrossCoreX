# -*- coding: utf-8 -*-
"""adb / MuMu 定位回归用例（离线，不碰真实注册表、进程、盘符扫描）。

背景（2026-11-18）：v1.0.4 在非默认安装目录（``D:\\YXCrossCore-12.0`` 这类定制版）
上定位不到 adb，pretask 直接失败。这里锁住三类入口与三条布局，防止再退回
「只认 nx_main\\MuMuManager.exe / 只认目录名 MuMu*」：

  A. adb 在 shell 的并列目录（nx_main），安装根没有 MuMuManager.exe；
  B. adb 由外部 platform-tools 提供（模拟器本体完全没带 adb）；
  C. 目录里只有模拟器主体 exe、完全没有 adb —— 必须报错并给出可操作指引。

另加：SoftwarePath 指向 ``shell\\MuMuPlayer.exe`` 时必须归一到安装根；
无任何锚点时不瞎扫盘；pretask --apply / ensure_mumu 在 adb 缺失时软跳过（退出码 0）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import ExitStack
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import ensure_mumu  # noqa: E402
import mumu_locator  # noqa: E402
import pretask_landscape  # noqa: E402


def _touch(root: Path, *relatives: str) -> Path:
    for relative in relatives:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ")
    return root


def _instance_config(project: Path, **values) -> Path:
    config = project / "config" / "instances" / "default.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return config


def _isolate(project: Path, extra_env=None, external=None) -> ExitStack:
    """屏蔽真实环境：注册表、进程、盘符扫描、PATH、SDK 目录全部替换为空。"""
    mumu_locator.reset_cache()
    stack = ExitStack()
    stack.enter_context(patch.object(mumu_locator, "_common_roots", return_value=[]))
    stack.enter_context(patch.object(mumu_locator, "registry_roots", return_value=[]))
    stack.enter_context(patch.object(mumu_locator, "_process_roots", return_value=[]))
    stack.enter_context(patch.object(mumu_locator.shutil, "which", return_value=None))
    if external is not None:
        stack.enter_context(
            patch.object(mumu_locator, "external_platform_tools", return_value=external)
        )
    env = {
        "MFA_INSTANCE_CONFIG_PATH": str(project / "config" / "instances" / "default.json")
    }
    env.update(extra_env or {})
    stack.enter_context(patch.dict(os.environ, env, clear=True))
    return stack


def test_shell_player_exe_maps_to_install_root():
    with tempfile.TemporaryDirectory() as directory:
        root = _touch(Path(directory) / "YXCrossCore-12.0", "shell/MuMuPlayer.exe")
        assert mumu_locator.root_from(root / "shell" / "MuMuPlayer.exe") == root


def test_nx_device_shell_maps_to_outer_install_root():
    with tempfile.TemporaryDirectory() as directory:
        root = _touch(
            Path(directory) / "MuMuPlayer-12.0",
            "nx_device/12.0/shell/adb.exe",
            "nx_device/12.0/shell/MuMuNxDevice.exe",
        )
        exe = root / "nx_device" / "12.0" / "shell" / "MuMuNxDevice.exe"
        assert mumu_locator.root_from(exe) == root


def test_adb_in_sibling_nx_main_without_manager():
    """场景 A：adb 在 nx_main（与 shell 并列），安装根没有 MuMuManager.exe。"""
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        root = _touch(
            Path(directory) / "YXCrossCore-12.0",
            "shell/MuMuPlayer.exe",
            "nx_main/adb.exe",
        )
        _instance_config(project, SoftwarePath=str(root / "shell" / "MuMuPlayer.exe"))

        with _isolate(project):
            found_root, manager, adb = mumu_locator.find_mumu(project)
            assert adb == root / "nx_main" / "adb.exe"
            assert manager is None, "没有管理器也不能拖累 adb 定位"
            assert found_root == root
            assert Path(mumu_locator.resolve_adb(project)) == root / "nx_main" / "adb.exe"


def test_manager_only_is_still_reported():
    """adb 缺失时仍要返回管理器，别把「没有 adb」误报成「没装 MuMu」。"""
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        root = _touch(Path(directory) / "MuMuPlayer-12.0", "nx_main/MuMuManager.exe")
        _instance_config(project, SoftwarePath=str(root / "nx_main" / "MuMuManager.exe"))

        with _isolate(project):
            found_root, manager, adb = mumu_locator.find_mumu(project)
            assert adb is None
            assert manager == root / "nx_main" / "MuMuManager.exe"
            assert found_root == root


def test_external_platform_tools_used_when_emulator_has_no_adb():
    """场景 B：模拟器目录里没有 adb，只有外部 platform-tools。"""
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        root = _touch(Path(directory) / "YXCrossCore-12.0", "shell/MuMuPlayer.exe")
        sdk = _touch(Path(directory) / "sdk", "platform-tools/adb.exe")
        adb = sdk / "platform-tools" / "adb.exe"
        _instance_config(project, SoftwarePath=str(root / "shell" / "MuMuPlayer.exe"))

        with _isolate(project, {"ANDROID_HOME": str(sdk)}):
            assert Path(mumu_locator.resolve_adb(project)) == adb


def test_explicit_mumu_adb_env_wins_without_anchor():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        base = _touch(Path(directory) / "custom", "bin/adb.exe")
        adb = base / "bin" / "adb.exe"
        with _isolate(project, {"MUMU_ADB": str(adb)}):
            assert Path(mumu_locator.resolve_adb(project)) == adb


def test_mumu_home_env_is_enough_for_end_users():
    """只想填安装目录的普通用户：MUMU_HOME 一层就该能找出来。"""
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        root = _touch(
            Path(directory) / "YXCrossCore-12.0",
            "app/MuMuPlayer.exe",
            "bin/adb.exe",
        )
        with _isolate(project, {"MUMU_HOME": str(root)}):
            assert Path(mumu_locator.resolve_adb(project)) == root / "bin" / "adb.exe"


def test_missing_adb_raises_with_actionable_hint():
    """场景 C：只有模拟器主体 exe、完全没有 adb —— 明确报错并给可操作修法。"""
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        root = _touch(Path(directory) / "YXCrossCore-12.0", "shell/MuMuPlayer.exe")
        _instance_config(project, SoftwarePath=str(root / "shell" / "MuMuPlayer.exe"))

        with _isolate(project):
            try:
                mumu_locator.resolve_adb(project)
            except mumu_locator.MuMuNotFound as exc:
                message = str(exc)
            else:
                raise AssertionError("没有 adb 时必须报错，不能返回裸 adb")
        assert "不回落到裸 adb" in message
        assert "MUMU_ADB" in message, "日志要直接告诉用户填哪里"
        assert "Adb路径" in message
        assert "怎么修" in message
        assert str(root) in message, "要列出实际检查过的候选目录，便于排查"


def test_no_anchor_does_not_scan_and_fails_loudly():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        with _isolate(project):
            assert mumu_locator.candidate_roots(project) == []
            try:
                mumu_locator.resolve_adb(project)
            except mumu_locator.MuMuNotFound as exc:
                assert "怎么修" in str(exc)
            else:
                raise AssertionError("无锚点时应明确报错")


def test_nx_device_adb_preferred_over_root_layout():
    """与壳/指纹口径一致：优先 nx_device\\*\\shell\\adb.exe。"""
    with tempfile.TemporaryDirectory() as directory:
        root = _touch(
            Path(directory) / "MuMuPlayer-12.0",
            "shell/adb.exe",
            "nx_device/12.0/shell/adb.exe",
        )
        assert mumu_locator._adb_in_layouts(root) == root / "nx_device" / "12.0" / "shell" / "adb.exe"


def test_pretask_apply_soft_skips_when_adb_missing():
    lines: list[str] = []
    error = "未找到可用的 adb.exe（不回落到裸 adb）。已检查：\n  怎么修（任选其一）"
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(pretask_landscape, "log", lines.append),
        patch.object(pretask_landscape.sys, "argv", ["pretask_landscape.py", "--apply"]),
        patch.object(pretask_landscape, "_adb_cache", []),
        patch.object(pretask_landscape, "_adb_error", ""),
        patch.object(mumu_locator, "resolve_adb", side_effect=mumu_locator.MuMuNotFound(error)),
    ):
        assert pretask_landscape.main() == pretask_landscape.EXIT_OK
    assert any("任务链继续" in line for line in lines)
    assert any("怎么修" in line for line in lines)


def test_pretask_check_still_fails_loudly():
    lines: list[str] = []
    error = "未找到可用的 adb.exe（不回落到裸 adb）。已检查：\n  怎么修（任选其一）"
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(pretask_landscape, "log", lines.append),
        patch.object(pretask_landscape.sys, "argv", ["pretask_landscape.py", "--check"]),
        patch.object(pretask_landscape, "_adb_cache", []),
        patch.object(pretask_landscape, "_adb_error", ""),
        patch.object(mumu_locator, "resolve_adb", side_effect=mumu_locator.MuMuNotFound(error)),
    ):
        assert pretask_landscape.main() == pretask_landscape.EXIT_ADB
    assert any("怎么修" in line for line in lines)


def test_ensure_mumu_soft_skips_when_adb_missing():
    lines: list[str] = []
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(ensure_mumu, "log", lines.append),
        patch.object(ensure_mumu, "selected_instance", return_value=(None, {})),
        patch.object(ensure_mumu, "resolve_adb_path", return_value=""),
        patch.object(ensure_mumu, "find_mumu", return_value=(None, None, None)),
        patch.object(mumu_locator, "describe_search", return_value=["怎么修（任选其一）"]),
    ):
        assert ensure_mumu.main() == ensure_mumu.EXIT_OK
    assert any("任务链继续" in line for line in lines)
    assert any("怎么修" in line for line in lines)


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"MUMU_LOCATOR_OK ({len(tests)} tests)")
