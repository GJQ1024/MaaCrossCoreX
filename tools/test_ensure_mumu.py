from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import ensure_mumu


def test_selected_instance_prefers_explicit_mfa_config():
    with tempfile.TemporaryDirectory() as directory:
        temp_path = Path(directory)
        first = temp_path / "first.json"
        selected = temp_path / "selected.json"
        first.write_text('{"InstanceName":"first"}', encoding="utf-8")
        selected.write_text('{"InstanceName":"selected"}', encoding="utf-8")

        with patch.dict(
            os.environ, {"MFA_INSTANCE_CONFIG_PATH": str(selected)}, clear=True
        ), patch.object(ensure_mumu, "instance_files", return_value=[first, selected]):
            path, data = ensure_mumu.selected_instance()

        assert path == selected
        assert data["InstanceName"] == "selected"


def test_offline_connection_recovers_without_server_restart():
    runner = patch.object(ensure_mumu, "run", return_value=(0, "", ""))
    with (
        runner as mocked_run,
        patch.object(ensure_mumu, "connection_usable", side_effect=[False, True]),
    ):
        assert ensure_mumu.recover_existing_adb(
            Path("adb.exe"), "127.0.0.1:<port>", {}
        )
    commands = [call.args[0][1] for call in mocked_run.call_args_list]
    assert commands == ["disconnect", "connect"]


def test_other_online_device_blocks_global_adb_restart():
    with (
        patch.object(ensure_mumu, "run", return_value=(0, "", "")) as mocked_run,
        patch.object(ensure_mumu, "connection_usable", side_effect=[False, False]),
        patch.object(ensure_mumu, "adb_devices", return_value={"emulator-5554": "device"}),
    ):
        assert not ensure_mumu.recover_existing_adb(
            Path("adb.exe"), "127.0.0.1:<port>", {}
        )
    assert all(call.args[0][1] != "kill-server" for call in mocked_run.call_args_list)


def test_runtime_settings_read_task_options():
    instance = {
        "TaskItems": [{
            "entry": "进入首页",
            "option": [
                {"name": "MuMu实例", "index": 3},
                {"name": "模拟器自动启动", "index": 1},
                {"name": "每次重新检测连接", "index": 1},
            ],
        }]
    }
    definitions = {
        "option": {
            "MuMu实例": {"cases": [{"name": "自动"}, {"name": "0"}, {"name": "1"}, {"name": "2"}]},
            "模拟器自动启动": {"cases": [{"name": "开启"}, {"name": "关闭"}]},
            "每次重新检测连接": {"cases": [{"name": "关闭"}, {"name": "开启"}]},
        }
    }
    with patch.dict(os.environ, {}, clear=True), patch.object(
        ensure_mumu, "interface_data", return_value=definitions
    ):
        settings = ensure_mumu.runtime_settings(instance)
    assert settings == {
        "vm_index": 2,
        "auto_start": False,
        "redetect": True,
        "minimize_after_launch": False,
    }


def test_runtime_settings_read_minimize_emulator_switch():
    settings = ensure_mumu.runtime_settings({"MinimizeEmulatorAfterLaunch": True})
    assert settings["minimize_after_launch"] is True


def test_minimize_mumu_uses_detected_main_window():
    user32 = SimpleNamespace(IsWindow=Mock(return_value=1), ShowWindowAsync=Mock(return_value=1))
    fake_ctypes = SimpleNamespace(windll=SimpleNamespace(user32=user32))
    with (
        patch.object(ensure_mumu.os, "name", "nt"),
        patch.dict(sys.modules, {"ctypes": fake_ctypes}),
        patch.object(ensure_mumu, "manager_info", return_value={"main_wnd": "00130ADE"}),
    ):
        assert ensure_mumu.minimize_mumu(Path("MuMuManager.exe"), 1)
    user32.IsWindow.assert_called_once_with(int("00130ADE", 16))
    user32.ShowWindowAsync.assert_called_once_with(int("00130ADE", 16), 6)


def test_update_instance_syncs_detected_mumu_launcher():
    with tempfile.TemporaryDirectory() as directory:
        temp_path = Path(directory)
        root = temp_path / "MuMuPlayer-12.0"
        manager = root / "nx_main" / "MuMuManager.exe"
        manager.parent.mkdir(parents=True)
        manager.touch()
        config_path = temp_path / "config" / "instances" / "default.json"

        ensure_mumu.update_instance(
            config_path,
            {},
            root / "shell" / "adb.exe",
            "127.0.0.1:<port>",
            root,
            1,
        )

        saved = ensure_mumu.load_json(config_path)
        assert saved["SoftwarePath"] == str(manager.resolve())
        assert saved["EmulatorConfig"] == "control --vmindex 1 launch"


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"MUMU_DISCOVERY_OK ({len(tests)} tests)")
