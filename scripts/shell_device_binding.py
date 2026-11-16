#!/usr/bin/env python3
"""壳启动前绑定设备：按 android_id 认设备，把结果写成壳能匹配的纯 ADB 设备。

背景（P-078）：
  MFAAvalonia 会在退出时把「当前设备」持久化成 MuMu 实例类型（`MuMu（实例 N）|...`），
  下次启动按该指纹匹配时依赖 MuMu 实例枚举；而 MuMuManager 会把运行中的实例
  误报成 `is_android_started=false`，枚举结果为空 → 匹配 0 台 → 回退到「按 adb 顺序
  自动挑」，于是挑到实例 0（碧蓝航线 127.0.0.1:16384），任务跑错号。

本脚本在壳启动之前运行：
  1. 先清掉 `adb devices` 里 offline 的残留条目；
  2. 枚举在线设备，按「装有交错战线包」筛出候选，并按 android_id 认身份；
  3. 枚举不到目标时（列表为空 / 读不到目标 android_id）才向 MuMuManager 取各实例的
     adb 端口逐个 `adb connect`，然后重新枚举；已能直接命中就绝不 connect；
  4. 命中 0 台或 ≥2 台 → 打印候选并退出码 3，调用方必须中止启动（不自动挑）；
     同一台设备有多个入口（如 emulator-5556 与 127.0.0.1:16416）按 android_id 去重，
     优先选 127.0.0.1:<port> 形式；
  5. 命中唯一一台 → 把它写成壳的纯 ADB 设备（序列号只作连接地址，身份由 android_id 认定）。

用法：
  python scripts/shell_device_binding.py [--dry-run]
退出码：0 已绑定/无需变更；3 设备歧义或取不到指纹；4 壳正在运行；5 配置不可用。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / "logs" / "shell_device_binding.log"
GAME_PACKAGE = os.environ.get("MFA_GAME_PACKAGE") or "com.megagame.crosscore.bilibili"

# 身份锚点与 pretask（agent/ensure_mumu.py）共用，避免两套口径漂移：
# 默认只认当前模拟器的 android_id，换机/重装后用 MFA_TARGET_ANDROID_ID 覆盖。
sys.path.insert(0, str(PROJECT_ROOT / "agent"))
from device_identity import filter_by_target, pick_entry, target_android_id  # noqa: E402

ADB_CANDIDATES = (
    Path(r"D:/Program Files/Netease/MuMu/nx_device/12.0/shell/adb.exe"),
    Path(r"D:/Program Files/Netease/MuMu/nx_main/adb.exe"),
)

MUMU_MANAGER_CANDIDATES = (
    Path(r"D:/Program Files/Netease/MuMu/nx_main/MuMuManager.exe"),
    Path(r"C:/Program Files/Netease/MuMu/nx_main/MuMuManager.exe"),
)

MAX_CONNECT_TRIES = 8

EXIT_OK = 0
EXIT_AMBIGUOUS = 3
EXIT_SHELL_RUNNING = 4
EXIT_CONFIG = 5


def log(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def adb_path() -> str:
    override = os.environ.get("ADB_PATH")
    if override and Path(override).is_file():
        return override.replace("\\", "/")
    for candidate in ADB_CANDIDATES:
        if candidate.is_file():
            return str(candidate).replace("\\", "/")
    return "adb"


def run_adb(adb: str, *args: str, timeout: int = 15) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [adb, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def device_entries(adb: str) -> list[tuple[str, str]]:
    """`adb devices` 的 (serial, 状态) 列表。"""
    code, output = run_adb(adb, "devices")
    if code != 0:
        return []
    entries = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            entries.append((parts[0], parts[1]))
    return entries


def online_serials(adb: str) -> list[str]:
    return [serial for serial, state in device_entries(adb) if state == "device"]


def drop_offline(adb: str) -> None:
    """清掉 offline 残留：它们会占着序列号，干扰枚举与后续 connect。"""
    offlines = [serial for serial, state in device_entries(adb) if state == "offline"]
    for serial in offlines:
        code, output = run_adb(adb, "disconnect", serial)
        result = output.strip() or ("成功" if code == 0 else f"退出码 {code}")
        log(f"[清理] 断开 offline 残留 {serial} → {result}")


def android_id(adb: str, serial: str) -> str:
    code, output = run_adb(adb, "-s", serial, "shell", "settings", "get", "secure", "android_id")
    return output.strip() if code == 0 else ""


def has_game(adb: str, serial: str) -> bool:
    code, output = run_adb(adb, "-s", serial, "shell", "pm", "list", "packages", GAME_PACKAGE)
    return code == 0 and GAME_PACKAGE in output


def mumu_ports() -> list[str]:
    """从 MuMuManager 取所有实例的 adb 地址。

    不看 is_android_started：MuMu 会把运行中的实例误报成 false（P-078）。
    """
    manager = next((p for p in MUMU_MANAGER_CANDIDATES if p.is_file()), None)
    if manager is None:
        log("[重连] 找不到 MuMuManager.exe，无法取实例端口")
        return []
    try:
        proc = subprocess.run(
            [str(manager), "info", "-v", "all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"[重连] 调用 MuMuManager 失败：{exc}")
        return []
    raw = proc.stdout or ""
    ports: list[str] = []
    try:
        instances = json.loads(raw)
    except ValueError:
        instances = None
    if isinstance(instances, dict):
        for inst in instances.values():
            if not isinstance(inst, dict):
                continue
            host = str(inst.get("adb_host_ip") or "127.0.0.1").strip()
            port = str(inst.get("adb_port") or "").strip()
            if port.isdigit() and f"{host}:{port}" not in ports:
                ports.append(f"{host}:{port}")
    if not ports and raw:
        # JSON 结构变化时的兜底：按 host/port 邻近顺序抓取
        for host, port in re.findall(
            r'"adb_host_ip"\s*:\s*"([^"]+)"[^}]*?"adb_port"\s*:\s*"?(\d+)"?', raw
        ):
            if f"{host}:{port}" not in ports:
                ports.append(f"{host}:{port}")
    if not ports and isinstance(instances, dict):
        # 实例运行中时 MuMuManager 也常常不报 adb 字段（本机实测就是全缺）；
        # MuMu 12 的 ADB 端口约定为 16384 + 32 × 实例号，按实例号推算兜底。
        for inst in instances.values():
            index = str(inst.get("index", "")).strip() if isinstance(inst, dict) else ""
            if index.isdigit():
                port = f"127.0.0.1:{16384 + 32 * int(index)}"
                if port not in ports:
                    ports.append(port)
        if ports:
            log(f"[重连] MuMuManager 未上报 adb 端口，按实例号推算：{ports}")
    return ports


def connect_ports(adb: str, ports: list[str]) -> list[tuple[str, str]]:
    """逐个 connect，返回 [(端口, 结果)]；全部尝试结果都回传给调用方打印。"""
    attempted: list[tuple[str, str]] = []
    for port in ports[:MAX_CONNECT_TRIES]:
        code, output = run_adb(adb, "connect", port, timeout=20)
        text = output.strip().splitlines()[-1] if output.strip() else ""
        ok = code == 0 and "connected to" in text.lower()
        result = text or f"退出码 {code}"
        attempted.append((port, result))
        log(f"[重连] adb connect {port} → {'成功' if ok else '失败'}（{result}）")
    if len(ports) > MAX_CONNECT_TRIES:
        log(f"[重连] 端口数超过上限 {MAX_CONNECT_TRIES}，剩余 {len(ports) - MAX_CONNECT_TRIES} 个未尝试")
    return attempted


def enumerate_devices(adb: str) -> list[tuple[str, str]]:
    """返回 [(serial, android_id)]：只保留装了游戏且读得到指纹的条目。"""
    serials = online_serials(adb)
    log(f"在线设备：{serials or '无'}")
    found: list[tuple[str, str]] = []
    for serial in serials:
        if not has_game(adb, serial):
            log(f"候选 {serial} → 无 {GAME_PACKAGE}，跳过")
            continue
        aid = android_id(adb, serial)
        log(f"候选 {serial} → 命中游戏包，android_id={aid or '（取不到）'}")
        if not aid:
            log(f"[身份] 指纹取不到：{serial} 的 android_id 为空，跳过该条目")
            continue
        found.append((serial, aid))
    return found


def select_target(devices: list[tuple[str, str]]) -> tuple[tuple[str, str] | None, str]:
    """按 android_id 认身份：同 ID 的多个入口视为同一台，优先 127.0.0.1:<port>。

    返回 ((serial, android_id), "") 或 (None, 失败原因)。
    """
    matched, dropped = filter_by_target(devices)
    if dropped:
        log(f"[身份] 目标 android_id={target_android_id()}：过滤掉 {dropped} 台非目标设备")
    groups: dict[str, list[str]] = {}
    for serial, aid in matched:
        groups.setdefault(aid, []).append(serial)
    if not groups:
        return None, f"目标 android_id={target_android_id()} 未匹配到设备"
    if len(groups) > 1:
        lines = "；".join(f"android_id={aid} → {serials}" for aid, serials in groups.items())
        return None, f"匹配到 {len(groups)} 台不同 android_id 的设备，身份歧义：{lines}"
    aid, serials = next(iter(groups.items()))
    serial = pick_entry(serials)
    if len(serials) > 1:
        log(f"[身份] android_id={aid} 有 {len(serials)} 个入口 {serials}，视为同一台，选 {serial}")
    return (serial, aid), ""


def resolve_target(adb: str) -> tuple[str, str] | None:
    # 1) 先按现有列表判：能直接命中就不折腾
    drop_offline(adb)
    devices = enumerate_devices(adb)
    resolved, reason = select_target(devices)
    if resolved is not None:
        log(f"[身份] 直接命中：{resolved[0]}（android_id={resolved[1]}），未执行任何 connect")
        return resolved
    log(f"[身份] 直接枚举未命中（{reason}），开始重连")

    # 2) 只有枚举不到目标才 connect；端口从 MuMuManager 取
    ports = mumu_ports()
    if not ports:
        log("[重连] MuMuManager 没有给出任何 adb 端口，停止（不自动挑）")
        return None
    log(f"[重连] 待尝试端口（上限 {MAX_CONNECT_TRIES}）：{ports}")
    attempted = connect_ports(adb, ports)

    # 3) connect 后重新枚举；新冒出来的 offline 也清掉再枚举
    drop_offline(adb)
    devices = enumerate_devices(adb)
    resolved, reason = select_target(devices)
    if resolved is not None:
        log(f"[身份] 重连后命中：{resolved[0]}（android_id={resolved[1]}）")
        return resolved

    log(f"[身份] 重连后仍未命中（{reason}），停止（不自动挑）")
    log("[重连] 已尝试的端口与结果：" + "；".join(f"{p} → {r}" for p, r in attempted))
    return None



def instance_config_path() -> Path | None:
    override = os.environ.get("MFA_INSTANCE_CONFIG")
    candidates = [
        Path(override) if override else None,
        Path.cwd() / "config" / "instances" / "default.json",
        PROJECT_ROOT / "dist" / "B" / "config" / "instances" / "default.json",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    return None


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    if any("--help" in arg for arg in sys.argv):
        print(__doc__)
        return EXIT_OK

    running = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq MFAAvalonia.exe", "/NH"],
        capture_output=True, text=True, errors="replace",
    )
    if "MFAAvalonia.exe" in (running.stdout or ""):
        log("壳正在运行，跳过绑定（应在壳启动前调用）")
        return EXIT_SHELL_RUNNING

    adb = adb_path()
    log(f"adb={adb}")
    resolved = resolve_target(adb)
    if resolved is None:
        return EXIT_AMBIGUOUS
    serial, aid = resolved

    config_path = instance_config_path()
    if config_path is None:
        log("找不到实例配置 config/instances/default.json，停止")
        return EXIT_CONFIG

    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log(f"实例配置无法解析：{config_path}（{exc}）")
        return EXIT_CONFIG

    # 指纹形状必须与 MaaToolkit.AdbDevice.Find() 的枚举结果一致：
    #   Name = "<serial>-<adb 完整路径>"，AdbPath = "adb"，Config = "{}"（Index=-1）。
    # 这样 MatchesFingerprint 可稳定得到 Name+Address+Port = 3/3，
    # 不依赖 MuMuManager 的实例号（其 is_android_started 有假阴性）。
    name = f"{serial}-{adb}"
    device = data.get("AdbDevice") or {}
    unchanged = (
        device.get("AdbSerial") == serial
        and device.get("Name") == name
        and device.get("AdbPath") == "adb"
        and device.get("Config") in ("{}", "")
        and "InfoHandle" not in device
        and data.get("RememberAdb") is True
        and data.get("UseFingerprintMatching") is True
    )
    if unchanged:
        log(
            f"已绑定目标设备 {serial}（android_id={aid}），"
            "指纹形状正确（Name=serial-adbpath, AdbPath=adb, Config={}），无需变更"
        )
        return EXIT_OK

    device.pop("InfoHandle", None)
    device.update({"Name": name, "AdbPath": "adb", "AdbSerial": serial, "Config": "{}"})
    data["AdbDevice"] = device
    data["RememberAdb"] = True
    data["UseFingerprintMatching"] = True
    if dry_run:
        log(
            f"[dry-run] 将把 AdbDevice 绑定到 {serial}（android_id={aid}）："
            f"Name={name}, AdbPath=adb, Config={{}}, RememberAdb=true, UseFingerprintMatching=true"
        )
        return EXIT_OK
    try:
        config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        log(f"写实例配置失败：{config_path}（{exc}）")
        return EXIT_CONFIG
    log(
        f"已绑定目标设备 {serial}（android_id={aid}）→ {config_path}；"
        "已写入指纹匹配形状：Name=serial-adbpath, AdbPath=adb, Config={}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
