# -*- coding: utf-8 -*-
"""身份锚点离线用例：ensure_mumu（pretask）与 shell_device_binding（binder）必须同一口径。

用例里用虚构的 android_id / 序列号模拟「一台真机 + 一台 MuMu 实例」：
  真机             android_id=<非目标>
  emulator-5556    android_id=<目标>
同一台的另一入口 127.0.0.1:16416 用于验证多入口去重。
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import device_identity  # noqa: E402
import ensure_mumu  # noqa: E402
import pretask_landscape  # noqa: E402
import shell_device_binding  # noqa: E402

TARGET = "0123456789abcdef"
REAL = "fedcba9876543210"
REAL_SERIAL = "R58M0000AAA"
EMULATOR = "emulator-5556"
LOOPBACK = "127.0.0.1:16416"


def _quiet(*_args, **_kwargs):
    pass


def resolve_offline(devices, ids, preferred=""):
    """离线跑 pretask 的 resolve_adb_target（不碰 adb、不写日志文件）。"""
    with (
        patch.dict(os.environ, {"MFA_TARGET_ANDROID_ID": TARGET}),
        patch.object(ensure_mumu, "log", _quiet),
        patch.object(ensure_mumu, "adb_devices", return_value=devices),
        patch.object(ensure_mumu, "adb_package_hit", return_value=True),
        patch.object(ensure_mumu, "adb_android_id", side_effect=lambda _adb, s: ids[s]),
    ):
        return ensure_mumu.resolve_adb_target("adb.exe", preferred)


def select_binder(devices):
    with patch.object(shell_device_binding, "log", _quiet):
        return shell_device_binding.select_target(devices)


def landscape_serial(devices, ids, lines=None):
    """离线跑 pretask_landscape 的包名+锚点发现（不碰 adb、不写日志文件）。"""

    def collect(message, *_args, **_kwargs):
        if lines is not None:
            lines.append(str(message))

    with (
        patch.dict(os.environ, {"MFA_TARGET_ANDROID_ID": TARGET}),
        patch.object(pretask_landscape, "online_serials", return_value=list(devices)),
        patch.object(pretask_landscape, "has_game_package", return_value=True),
        patch.object(pretask_landscape, "android_id", side_effect=lambda s: ids[s]),
        patch.object(pretask_landscape, "mumu_labels", return_value={}),
        patch.object(pretask_landscape, "device_model", return_value="model"),
        patch.object(pretask_landscape, "log", collect),
    ):
        return pretask_landscape.package_verified_serial()


def _real_and_emulator():
    devices = {REAL_SERIAL: "device", EMULATOR: "device"}
    ids = {REAL_SERIAL: REAL, EMULATOR: TARGET}
    return devices, ids


def test_default_anchor_matches_binder_default():
    with patch.dict(os.environ, {}, clear=True):
        assert device_identity.target_android_id() == ""
    assert device_identity.DEFAULT_TARGET_ANDROID_ID == ""


def test_real_device_is_filtered_and_emulator_is_unique_hit():
    devices, ids = _real_and_emulator()
    assert resolve_offline(devices, ids) == (EMULATOR, TARGET)


def test_same_android_id_prefers_loopback_entry():
    devices = {REAL_SERIAL: "device", EMULATOR: "device", LOOPBACK: "device"}
    ids = {REAL_SERIAL: REAL, EMULATOR: TARGET, LOOPBACK: TARGET}
    assert resolve_offline(devices, ids) == (LOOPBACK, TARGET)


def test_only_real_device_is_no_target_not_ambiguous():
    try:
        resolve_offline({REAL_SERIAL: "device"}, {REAL_SERIAL: REAL})
    except ensure_mumu.NoTarget as exc:
        assert "未匹配到设备" in str(exc)
    else:
        raise AssertionError("应抛 NoTarget")


def test_unreadable_android_id_is_treated_as_non_target():
    # 读不到指纹的设备按非目标滤掉，不能把整轮拖成失败
    devices = {EMULATOR: "device", "junk": "device"}
    ids = {EMULATOR: TARGET, "junk": ""}
    assert resolve_offline(devices, ids) == (EMULATOR, TARGET)


def test_pretask_and_binder_pick_the_same_device():
    devices = {REAL_SERIAL: "device", EMULATOR: "device", LOOPBACK: "device"}
    ids = {REAL_SERIAL: REAL, EMULATOR: TARGET, LOOPBACK: TARGET}
    picked, reason = select_binder([(REAL_SERIAL, REAL), (EMULATOR, TARGET), (LOOPBACK, TARGET)])
    with patch.dict(os.environ, {"MFA_TARGET_ANDROID_ID": TARGET}):
        pretask = resolve_offline(devices, ids)
    assert reason == "" and picked == pretask == (LOOPBACK, TARGET)


def test_binder_filters_real_device_silently():
    devices = {REAL_SERIAL: "device", EMULATOR: "device"}
    ids = {REAL_SERIAL: REAL, EMULATOR: TARGET}
    picked, reason = select_binder([(REAL_SERIAL, REAL), (EMULATOR, TARGET)])
    assert reason == "" and picked == (EMULATOR, TARGET)
    with patch.dict(os.environ, {"MFA_TARGET_ANDROID_ID": TARGET}):
        assert resolve_offline(devices, ids) == (EMULATOR, TARGET)


def test_real_device_only_appears_in_filter_summary():
    devices, ids = _real_and_emulator()
    lines = []
    with (
        patch.dict(os.environ, {"MFA_TARGET_ANDROID_ID": TARGET}),
        patch.object(ensure_mumu, "log", lambda message, *_a, **_k: lines.append(str(message))),
        patch.object(ensure_mumu, "adb_devices", return_value=devices),
        patch.object(ensure_mumu, "adb_package_hit", return_value=True),
        patch.object(ensure_mumu, "adb_android_id", side_effect=lambda _adb, s: ids[s]),
    ):
        assert ensure_mumu.resolve_adb_target("adb.exe") == (EMULATOR, TARGET)
    touched = [line for line in lines if REAL_SERIAL in line]
    summaries = [line for line in lines if "过滤掉 1 台非目标设备" in line]
    assert touched == [] and len(summaries) == 1


def test_landscape_filters_real_device_and_unique_hit():
    devices, ids = _real_and_emulator()
    lines = []
    assert landscape_serial(devices, ids, lines) == EMULATOR
    assert not [line for line in lines if REAL_SERIAL in line]
    assert len([line for line in lines if "过滤掉 1 台非目标设备" in line]) == 1


def test_landscape_only_real_device_reports_no_target():
    lines = []
    assert landscape_serial({REAL_SERIAL: "device"}, {REAL_SERIAL: REAL}, lines) is None
    assert any("未匹配到设备" in line and TARGET in line for line in lines)


def test_landscape_same_android_id_prefers_loopback_entry():
    devices = {EMULATOR: "device", LOOPBACK: "device"}
    ids = {EMULATOR: TARGET, LOOPBACK: TARGET}
    assert landscape_serial(devices, ids) == LOOPBACK


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print("DEVICE_IDENTITY_OK (%d tests)" % len(tests))
