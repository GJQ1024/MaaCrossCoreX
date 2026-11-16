from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import ensure_mumu
import shutdown_mumu

TARGET = "0123456789abcdef"
NO_TARGET = ensure_mumu.NoTarget("目标 android_id 未匹配到设备")


def _found(manager):
    return (Path("MuMuPlayer-12.0"), manager, "adb.exe")


def test_target_is_resolved_by_android_id():
    manager = Path("MuMuManager.exe")
    with (
        patch.object(ensure_mumu, "find_mumu", return_value=_found(manager)),
        patch.object(
            ensure_mumu, "resolve_adb_target", return_value=("emulator-5556", TARGET)
        ),
        patch.object(ensure_mumu, "index_for_serial", return_value=1),
    ):
        assert shutdown_mumu.resolve_target() == (manager, 1)


def test_instance_index_falls_back_to_port_convention():
    manager = Path("MuMuManager.exe")
    with (
        patch.object(ensure_mumu, "find_mumu", return_value=_found(manager)),
        patch.object(
            ensure_mumu, "resolve_adb_target", return_value=("127.0.0.1:16416", TARGET)
        ),
        patch.object(ensure_mumu, "index_for_serial", return_value=None),
    ):
        assert shutdown_mumu.resolve_target() == (manager, 1)


def test_serial_without_port_convention_is_rejected():
    manager = Path("MuMuManager.exe")
    with (
        patch.object(ensure_mumu, "find_mumu", return_value=_found(manager)),
        patch.object(
            ensure_mumu, "resolve_adb_target", return_value=("emulator-5556", TARGET)
        ),
        patch.object(ensure_mumu, "index_for_serial", return_value=None),
    ):
        assert shutdown_mumu.resolve_target() is None


def test_missing_device_with_stopped_instance_is_already_closed():
    manager = Path("MuMuManager.exe")
    with (
        patch.object(ensure_mumu, "find_mumu", return_value=_found(manager)),
        patch.object(ensure_mumu, "resolve_adb_target", side_effect=NO_TARGET),
        patch.object(shutdown_mumu, "_cached_index", return_value=1),
        patch.object(
            ensure_mumu,
            "manager_info",
            return_value={"index": 1, "is_process_started": False, "is_android_started": False},
        ),
    ):
        assert shutdown_mumu.resolve_target() == shutdown_mumu.ALREADY_CLOSED


def test_missing_device_with_running_instance_is_not_closed():
    manager = Path("MuMuManager.exe")
    with (
        patch.object(ensure_mumu, "find_mumu", return_value=_found(manager)),
        patch.object(ensure_mumu, "resolve_adb_target", side_effect=NO_TARGET),
        patch.object(shutdown_mumu, "_cached_index", return_value=1),
        patch.object(
            ensure_mumu,
            "manager_info",
            return_value={"index": 1, "is_process_started": True},
        ),
    ):
        assert shutdown_mumu.resolve_target() is None


def test_missing_device_without_cached_index_and_nothing_running_is_closed():
    manager = Path("MuMuManager.exe")
    with (
        patch.object(ensure_mumu, "find_mumu", return_value=_found(manager)),
        patch.object(ensure_mumu, "resolve_adb_target", side_effect=NO_TARGET),
        patch.object(shutdown_mumu, "_cached_index", return_value=None),
        patch.object(ensure_mumu, "manager_info_all", return_value={}),
    ):
        assert shutdown_mumu.resolve_target() == shutdown_mumu.ALREADY_CLOSED


def test_missing_device_without_cached_index_but_other_instance_running_fails():
    manager = Path("MuMuManager.exe")
    with (
        patch.object(ensure_mumu, "find_mumu", return_value=_found(manager)),
        patch.object(ensure_mumu, "resolve_adb_target", side_effect=NO_TARGET),
        patch.object(shutdown_mumu, "_cached_index", return_value=None),
        patch.object(
            ensure_mumu,
            "manager_info_all",
            return_value={0: {"index": 0, "is_android_started": True}},
        ),
    ):
        assert shutdown_mumu.resolve_target() is None


def test_already_closed_reports_success_without_running_shutdown():
    with (
        patch.object(
            shutdown_mumu, "resolve_target", return_value=shutdown_mumu.ALREADY_CLOSED
        ),
        patch.object(ensure_mumu, "run", return_value=(0, "", "")) as mocked_run,
    ):
        assert shutdown_mumu.ShutdownMumuAction().run(None, None)
    mocked_run.assert_not_called()


def test_uses_manager_shutdown_command():
    target = (Path("MuMuManager.exe"), 3)
    with (
        patch.object(shutdown_mumu, "resolve_target", return_value=target),
        patch.object(ensure_mumu, "run", return_value=(0, "", "")) as mocked_run,
    ):
        assert shutdown_mumu.ShutdownMumuAction().run(None, None)
    mocked_run.assert_called_once_with(
        [target[0], "control", "--vmindex", 3, "shutdown"], timeout=30
    )


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"SHUTDOWN_MUMU_OK ({len(tests)} tests)")
