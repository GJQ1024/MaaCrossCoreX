# -*- coding: utf-8 -*-
"""pretask：把 JCZX 切到横屏 1280x720 并做画面有效性预检，帧级不过则非零退出中止任务。

判据分层（2026-11-02）：方向/渲染面/黑屏（见下）决定识别可靠性，是硬闸门；
宿主窗口竖条或非 16:9 只影响观感，仅记录警告、不中止（见 host_window_warning）。

为什么要这一步：MuMu 实例物理面板 720×1280（竖），游戏 Activity 锁竖屏。
直接 `wm size 1280x720`（Override）会让 guest 上报 rotate_display 0 0，host 跟随
变竖屏渲染面（9:16 竖条，上下黑边）。正解是"竖→横→reset"切换：
`wm size 720x1280` → `wm size 1280x720` → `wm size reset`，清掉 Override 后
Android 以面板旋转(orientation=1)呈现 1280×720 横屏，游戏不再锁竖屏。
详见 docs/device.md「2026-11-02 18:13 根治结论」。

判定（2026-11-02 修订：方向读系统状态，不用截图推断；任一不满足即退出码 2）：
  1. 方向：dumpsys display 的 mCurrentOrientation（0/2=竖，1/3=横），
     与 ensure_landscape_orientation() 同源。竖屏才拦。
     物理帧 `wm size` 恒为 720x1280（竖面板、host 旋转 canvas 输出横屏），
     与方向无关，不作判据。
  2. 渲染面尺寸：screencap 帧恰为 1280x720（MAA 实际拿到的画面）。
  3. 渲染有效性：整帧暗像素占比 > 95% 才判渲染黑屏。
     弹窗/黑边只会让非黑占比（fill）下降，一律只作附注；不再用非黑区
     包围盒宽高比/覆盖率推断方向——游戏黑背景弹窗实测让 bbox 变成
     1232x539（宽高比 2.286）而被误判"压扁"，设备实际横屏正常。

用法（2026-11-04 起默认只读：打开界面/连接设备不再写 wm）：
  python scripts/pretask_landscape.py                  # 只读预检（默认，不写设备）
  python scripts/pretask_landscape.py --check          # 同上（兼容旧调用）
  python scripts/pretask_landscape.py --apply          # 任务真正开始前才允许写设备：
                                                       # 先探测原值，竖屏才切换，失败按原值回滚

本项目不提供「还原竖屏」：跑完保持 1280x720 横屏（2026-11-02 用户裁定，逻辑已移除）。

序列号来源（认包不认端口：端口由模拟器分配、会漂移，`is_main` 也会抢走自动选择）：
  1. 环境变量 ADB_SERIAL（仍要过包名校验）；
  2. 自动发现：`adb devices` 里所有 state=device 的条目（不限定 127.0.0.1），
     逐个查 pm list packages，只有装了交错战线（含渠道变体 crosscore）的才是候选；
     再按目标 android_id（与 binder/ensure_mumu 同一个 device_identity 锚点）过滤，
     真机等非目标设备静默滤掉、只留一行过滤摘要。
过滤后 0 台或 ≥2 个不同 android_id 都报错并打印候选，不随手挑一台继续跑；
同一 id 的多个入口（`emulator-xxxx` 与 `127.0.0.1:port`）按一台处理，优先 `127.0.0.1:port`。
身份闸门（2026-11-09，P-078）：壳当前连接的设备（config/instances/default.json 的
`AdbDevice.AdbSerial`）必须与按 android_id 认出的目标是同一台；指纹取不到或不是同一台
→ 退出码 3 中止，绝不自动改连其他实例。端口只作连上后的取值，不作设备身份。
选中后打印实例名与端口（MuMu 能拿到名字就显示 `JCZX (index 1) 127.0.0.1:xxxx`，
否则退化为 `127.0.0.1:xxxx (型号)`），便于用户一眼确认连的是哪台。
adb 路径：环境变量 ADB_PATH，否则用 MUMU_ROOT / 常见安装目录下的 nx_main\adb.exe。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / "logs" / "pretask_landscape.log"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import device_judge  # noqa: E402  判据唯一来源：方向/渲染面/黑屏（勿在本文件复刻）

# 设备身份锚点与 binder（scripts/shell_device_binding.py）和 pretask
# （agent/ensure_mumu.py）共用同一份实现，避免三处口径漂移。
sys.path.insert(0, str(PROJECT_ROOT / "agent"))
from device_identity import filter_by_target, pick_entry, target_android_id  # noqa: E402

MUMU_ROOTS = (
    r"D:\Program Files\Netease\MuMu",
    r"C:\Program Files\Netease\MuMu",
)

# 尺寸/密度/判据常量与切换实现（LANDSCAPE_SIZE / PORTRAIT_SIZE / DENSITY /
# EXPECTED_* / BLACK_* / ensure_landscape_orientation）统一在
# scripts/device_judge.py（与 device_mode / capture 共用）；
# 本文件只保留流程，改判据只改 device_judge。

EXIT_OK = 0
EXIT_ADB = 1
EXIT_PREFLIGHT = 2
EXIT_DEVICE_IDENTITY = 3

# 壳（MFAAvalonia）会把当前连接的设备实时写回实例配置；预检据此核对
# 「壳连的那台」和「按 android_id 认出来的那台」是不是同一台（P-078）。
SHELL_INSTANCE_CONFIGS = (
    Path("config/instances/default.json"),
    PROJECT_ROOT / "dist" / "B" / "config" / "instances" / "default.json",
    PROJECT_ROOT / "config" / "instances" / "default.json",
)

# 窗口级判据（2026-11-02 起只警告不阻断；帧级才是硬闸门）
WINDOW_PORTRAIT_RATIO = 0.95
WINDOW_RATIO_MIN = 1.45
WINDOW_RATIO_MAX = 2.20


def log(message: str) -> None:
    """stdout 给壳日志，文件给事后排查（pretask 卡住时只能看文件）。"""
    print(f"[pretask] {message}", flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def mumu_root() -> Path | None:
    """定位 MuMu 安装根目录，找不到时返回 None。"""
    roots = []
    env_root = os.environ.get("MUMU_ROOT")
    if env_root:
        roots.append(Path(env_root))
    roots += [Path(item) for item in MUMU_ROOTS]
    for root in roots:
        if (root / "nx_main" / "MuMuManager.exe").is_file():
            return root
    return None


def adb_path() -> str:
    env_adb = os.environ.get("ADB_PATH")
    if env_adb:
        return env_adb
    root = mumu_root()
    if root:
        candidate = root / "nx_main" / "adb.exe"
        if candidate.is_file():
            return str(candidate)
    return "adb"


_serial_cache: list[str] = []


def mumu_labels() -> dict[str, str]:
    """serial → `实例名 (index N)`；非 MuMu 或读不到时返回空表（只用于显示）。"""
    root = mumu_root()
    if root is None:
        return {}
    manager = str(root / "nx_main" / "MuMuManager.exe")
    try:
        result = subprocess.run(
            [manager, "info", "-v", "all"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return {}
        payload = json.loads(result.stdout[result.stdout.find("{") :])
    except Exception:
        return {}

    labels: dict[str, str] = {}
    for index, info in payload.items():
        if not isinstance(info, dict):
            continue
        port = info.get("adb_port")
        if not port:
            continue
        host = info.get("adb_host_ip") or "127.0.0.1"
        labels[f"{host}:{port}"] = f"{info.get('name') or 'MuMu 实例'} (index {index})"
    return labels


def device_model(serial: str) -> str:
    code, output, _ = adb_raw(["-s", serial, "shell", "getprop", "ro.product.model"], timeout=12)
    return output.strip() if code == 0 else ""


def describe_serial(serial: str, labels: dict[str, str]) -> str:
    """展示用：有实例名写实例名，没有就退化为 `serial (型号)`。"""
    label = labels.get(serial)
    if label:
        return f"{label} {serial}"
    model = device_model(serial)
    return f"{serial} ({model})" if model else serial


GAME_PACKAGE_KEYWORD = "crosscore"


def online_serials() -> list[str]:
    """列出 adb devices 里所有 state=device 的条目。

    不限定 127.0.0.1：不同模拟器端口规则不同，也可能出现 USB / emulator-xxxx。
    """
    try:
        result = subprocess.run(
            [adb_path(), "devices"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    serials = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def has_game_package(serial: str) -> bool:
    """查该实例是否装了交错战线（含 B 服等渠道变体）。

    用 adb_raw（显式 -s）而不是 run_adb：后者会再走 adb_serial()，
    在自动发现阶段会递归回 detect_serial。
    """
    if not serial:
        return False
    for _ in range(2):
        code, output, _ = adb_raw(
            ["-s", serial, "shell", "pm", "list", "packages"], timeout=20
        )
        if code == 0:
            return GAME_PACKAGE_KEYWORD in output.lower()
    return False


def android_id(serial: str) -> str:
    """设备物理身份。MuMu 会把同一台设备同时映射成 127.0.0.1:端口 与 emulator-xxxx，
    两个 serial 指向同一台机器，必须靠 android_id 去重（启动器已按同样方式去重）。"""
    code, output, _ = adb_raw(
        ["-s", serial, "shell", "settings", "get", "secure", "android_id"], timeout=12
    )
    return output.strip() if code == 0 else ""


def report(serial: str, labels: dict[str, str], hit: bool, chosen: bool = False) -> None:
    desc = describe_serial(serial, labels)
    if chosen:
        log(f"[连接] 已连接 {desc}")
        return
    log(f"[连接] 候选 {desc} → {'命中 crosscore，选用' if hit else '无 crosscore，跳过'}")


def package_verified_serial() -> str | None:
    """自动发现：包名筛选 + 目标 android_id 锚点，命中必须唯一。

    不用 adb devices 第一条：顺序不保证，`is_main` 也会把自动选择带到别的实例。
    真机等非目标设备在过滤阶段静默剔除（只留一行摘要），不参与「几台装了游戏」的判定。
    """
    labels = mumu_labels()
    candidates = online_serials()
    if not candidates:
        log("[连接] 没有在线的 adb 设备，无法按包名识别目标")
        return None

    with_game: list[tuple[str, str]] = []
    for serial in candidates:
        if not has_game_package(serial):
            report(serial, labels, False)
            continue
        with_game.append((serial, android_id(serial)))

    target = target_android_id()
    matched, dropped = filter_by_target(with_game)
    if dropped:
        log(f"[连接] 目标 android_id={target}：过滤掉 {dropped} 台非目标设备（不参与判定）")

    groups: dict[str, list[str]] = {}
    for serial, aid in matched:
        groups.setdefault(aid, []).append(serial)
    if not groups:
        log(
            f"[连接] 目标 android_id={target} 未匹配到设备（已检查："
            + "、".join(describe_serial(s, labels) for s in candidates) + "）"
        )
        return None
    if len(groups) > 1:
        detail = "；".join(f"android_id={aid} → {serials}" for aid, serials in groups.items())
        log(f"[连接] 匹配到 {len(groups)} 台不同 android_id 的设备，无法自动选定：{detail}")
        return None

    aid, serials = next(iter(groups.items()))
    serial = pick_entry(serials)
    if len(serials) > 1:
        log(f"[连接] android_id={aid} 有 {len(serials)} 个入口 {serials}，视为同一台，选 {serial}")
    report(serial, labels, True, chosen=True)
    return serial


def detect_serial() -> str | None:
    """按 环境变量 → 自动发现 的顺序解析，所有来源都要过包名校验。"""
    explicit = os.environ.get("ADB_SERIAL")
    if explicit:
        if has_game_package(explicit):
            report(explicit, mumu_labels(), True, chosen=True)
            return explicit
        log(f"[连接] ADB_SERIAL={explicit} 未检测到交错战线包，已拒绝使用，也不退回别的实例")
        return None

    return package_verified_serial()


def adb_serial() -> str:
    if _serial_cache:
        return _serial_cache[0]
    serial = detect_serial() or ""
    if serial:
        _serial_cache.append(serial)
    return serial


def shell_selected_serial() -> tuple[str, str]:
    """读壳当前连接的 ADB 序列号（只用于身份核对，不作为设备身份）。"""
    for candidate in SHELL_INSTANCE_CONFIGS:
        try:
            if not candidate.is_file():
                continue
            data = json.loads(candidate.read_text(encoding="utf-8"))
            serial = str((data.get("AdbDevice") or {}).get("AdbSerial") or "").strip()
            if serial:
                return serial, str(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return "", ""


def verify_device_identity(target_serial: str) -> bool:
    """P-078 硬闸门：壳连的设备必须与按 android_id 认出的目标为同一台。

    设备身份只认 android_id（P-047），端口/序列号只是连上后的取值。
    指纹取不到、或壳连的设备与目标不是同一台 → 报警中止，绝不自动改连别的实例。
    """
    shell_serial, source = shell_selected_serial()
    if not shell_serial:
        log("[身份] 读不到壳当前连接的设备（config/instances/default.json → AdbDevice.AdbSerial）")
        log("[身份] 请先在面板「当前控制器」里选好设备，再开始任务")
        return False

    target_aid = android_id(target_serial)
    shell_aid = android_id(shell_serial)
    if not shell_aid:
        log(f"[身份] 指纹取不到：{shell_serial} 的 android_id 为空（{source}），拒绝执行")
        return False
    if not target_aid:
        log(f"[身份] 指纹取不到：目标 {target_serial} 的 android_id 为空，拒绝执行")
        return False
    if shell_aid != target_aid:
        log(f"[身份] 控制器连错实例：壳当前 {shell_serial}（android_id={shell_aid}）"
            f"≠ 目标 {target_serial}（android_id={target_aid}）")
        log("[身份] 已中止任务：请在面板把「当前控制器」切到交错战线所在实例后重试"
            "（不会自动切换，也不会在别的实例上执行任何动作）")
        return False

    log(f"[身份] 指纹一致：壳当前 {shell_serial} 与目标 {target_serial} "
        f"同为 android_id={shell_aid}")
    return True


def adb_raw(args: list[str], timeout: int = 20) -> tuple[int, str, str]:
    """直接调 adb（不做 serial 解析），供自动发现 / 展示查询使用。"""
    try:
        result = subprocess.run(
            [adb_path(), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def run_adb(*args: str, timeout: int = 20) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            [adb_path(), "-s", adb_serial(), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as exc:  # adb 不存在 / 超时
        return 1, "", str(exc)


def screencap() -> bytes | None:
    try:
        result = subprocess.run(
            [adb_path(), "-s", adb_serial(), "exec-out", "screencap"],
            capture_output=True,
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not result.stdout:
            return None
        return result.stdout
    except Exception:
        return None


def parse_wm_size(raw: str) -> str:
    """wm size 会同时给 Physical 和 Override；有 Override 时以 Override 为准。"""
    override = re.search(r"Override size:\s*(\d+x\d+)", raw, re.IGNORECASE)
    if override:
        return override.group(1)
    physical = re.search(r"Physical size:\s*(\d+x\d+)", raw, re.IGNORECASE)
    return physical.group(1) if physical else ""


def set_mode(size: str, density: str) -> bool:
    for args in (("shell", "wm", "size", size), ("shell", "wm", "density", density)):
        code, _, error = run_adb(*args)
        if code != 0:
            log(f"设置失败 {' '.join(args)}：{error.strip()}")
            return False
    return True


def get_orientation() -> int | None:
    """读 mCurrentOrientation（解析与判据在 device_judge，preflight / 切换共用同一实现）。"""
    return device_judge.get_orientation(adb_serial(), adb_path())


def ensure_landscape_orientation() -> None:
    """竖→横→reset 切换（实现唯一在 device_judge.ensure_landscape_orientation）。

    背景：JCZX 物理面板 720×1280（竖），游戏 Activity 锁竖屏；
    直接 `wm size 1280x720`（Override）会把已横屏设备改坏（实测
    mCurrentOrientation 翻成 0、宿主窗口压成 0.56 竖条）。
    dance 细节见 device_judge 同名函数的 docstring，勿在此复刻。
    """
    if not device_judge.ensure_landscape_orientation(adb_serial(), adb_path(), log=log):
        log("警告：显示修正失败，已按原值回滚；后续预检会给出明确失败原因")


def _pixel_stats(pixels: memoryview, w: int, h: int, per_pixel: int) -> tuple[float, int, int]:
    """统计非黑像素占比与包围盒；只用 stdlib。

    发布包用的是嵌入式 Python，没有 numpy，所以这里逐像素扫。
    1280x720 约 0.9M 像素，单帧 1~2 秒，预检重试上限 4 次仍可接受。
    """
    threshold = device_judge.BLACK_THRESHOLD
    count = 0
    min_x, max_x = w, -1
    min_y, max_y = h, -1
    if per_pixel == 4:
        for y in range(h):
            base = y * w * 4
            for x in range(w):
                i = base + x * 4
                if pixels[i] > threshold or pixels[i + 1] > threshold or pixels[i + 2] > threshold:
                    count += 1
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y
    elif per_pixel == 3:
        for y in range(h):
            base = y * w * 3
            for x in range(w):
                i = base + x * 3
                if pixels[i] > threshold or pixels[i + 1] > threshold or pixels[i + 2] > threshold:
                    count += 1
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y
    else:
        # RGB565：各通道位宽 5/6/5，阈值按 12/255 等比缩放（约 1/2/1）
        for y in range(h):
            base = y * w * 2
            for x in range(w):
                i = base + x * 2
                value = pixels[i] | (pixels[i + 1] << 8)
                if (value >> 11) > 1 or ((value >> 5) & 0x3F) > 2 or (value & 0x1F) > 1:
                    count += 1
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y
    fill = count / float(w * h) if w and h else 0.0
    bw = max_x - min_x + 1 if max_x >= 0 else 0
    bh = max_y - min_y + 1 if max_y >= 0 else 0
    return fill, bw, bh


def analyze_frame(data: bytes):
    """解 adb exec-out screencap 的原始帧并直接给出统计量（无第三方依赖）。

    头部 12 字节（w, h, format）或 16 字节（多一个 colorspace），
    format: 1/2=RGBA/RGBX 4字节，3=RGB 3字节，4=RGB565 2字节。
    """
    for header in (12, 16):
        if len(data) <= header:
            continue
        w, h, fmt = (int.from_bytes(data[i : i + 4], "little") for i in (0, 4, 8))
        body = len(data) - header
        per_pixel = {1: 4, 2: 4, 3: 3, 4: 2}.get(fmt)
        if not per_pixel or w <= 0 or h <= 0:
            continue
        if body < w * h * per_pixel:
            continue
        pixels = memoryview(data)[header : header + w * h * per_pixel]
        fill, bw, bh = _pixel_stats(pixels, w, h, per_pixel)
        return {"w": w, "h": h, "fill": fill, "bbox_w": bw, "bbox_h": bh}
    return None


def preflight_once(data: bytes) -> dict:
    """解码原始帧后交给 device_judge.judge_frame 统一判定（尺寸 + 黑屏；弹窗只附注）。"""
    decoded = analyze_frame(data)
    if decoded is None:
        return {"ok": False, "reason": "帧解析失败（screencap 头部/格式不符）"}
    return device_judge.judge_frame(decoded)


def preflight(retries: int = 4, interval: float = 1.2) -> dict:
    """方向读系统状态（mCurrentOrientation），帧检查只管渲染有效性（判据在 device_judge）。

    切横竖屏瞬间会有 1-3 秒过渡帧，帧检查重试避免抢跑误报。
    """
    orientation = get_orientation()
    if orientation is not None and device_judge.is_portrait(orientation):
        return {
            "ok": False,
            "reason": f"方向为竖屏（mCurrentOrientation={orientation}），自动化固定横屏",
            "orientation": orientation,
        }
    if device_judge.is_landscape(orientation):
        log(f"方向: 横屏（mCurrentOrientation={orientation}）")
    else:
        log("方向: 未能读取 mCurrentOrientation（dumpsys 无该字段），改按渲染面尺寸兜底")

    last = {"ok": False, "reason": "未取到帧"}
    for attempt in range(retries):
        data = screencap()
        if data:
            last = preflight_once(data)
            if last["ok"]:
                return last
        else:
            last = {"ok": False, "reason": "screencap 返回空"}
        if attempt < retries - 1:
            time.sleep(interval)
    return last


def mumu_index_for(serial: str) -> str | None:
    """用当前选中的 serial 反查 MuMu 实例 index（只用于窗口诊断，不参与选择）。"""
    root = mumu_root()
    if root is None or not serial:
        return None
    manager = str(root / "nx_main" / "MuMuManager.exe")
    try:
        result = subprocess.run(
            [manager, "info", "-v", "all"],
            capture_output=True, text=True, errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(result.stdout[result.stdout.find("{"):])
    except Exception:
        return None
    for index, info in payload.items():
        if not isinstance(info, dict) or not info.get("adb_port"):
            continue
        host = info.get("adb_host_ip") or "127.0.0.1"
        if f"{host}:{info['adb_port']}" == serial:
            return str(index)
    return None


def host_window_warning() -> str:
    """宿主窗口形状检查，只返回警告文本，永不阻断任务（2026-11-02 判据分层）。

    帧级（方向/渲染面/黑屏，见 preflight）决定识别可靠性，是硬闸门；
    宿主窗口竖条/非 16:9 只影响观感与截图取证，adb 帧本身正常时任务照跑。
    这里只把提示写进 pretask 日志，便于事后追溯。
    """
    if os.name != "nt":
        return ""
    root = mumu_root()
    if root is None:
        return ""
    manager = root / "nx_main" / "MuMuManager.exe"
    index = mumu_index_for(adb_serial())
    if index is None:
        return ""
    try:
        result = subprocess.run(
            [str(manager), "info", "-v", index],
            capture_output=True, text=True, errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = result.stdout[result.stdout.find("{"):]
        info = json.loads(payload)
    except Exception as exc:  # noqa: BLE001
        return f"窗口形状检查跳过（MuMuManager 查询失败：{exc}）"
    if not info.get("is_process_started"):
        return ""
    try:
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long),
            ]

        user32 = ctypes.windll.user32
        for key in ("render_wnd", "main_wnd"):
            raw = info.get(key)
            if not (isinstance(raw, str) and raw):
                continue
            try:
                hwnd = int(raw, 16)
            except ValueError:
                continue
            rect = RECT()
            if not hwnd or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                continue
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w <= 50 or h <= 50:
                continue
            ratio = w / h
            if ratio < WINDOW_PORTRAIT_RATIO:
                return (
                    f"检测到模拟器窗口为竖屏（{w}x{h}，宽高比 {ratio:.2f}），画面会有黑边，"
                    f"不影响任务执行；建议在 MuMu 设置中把实例分辨率改为 1280×720 横屏后重启实例"
                )
            if ratio < WINDOW_RATIO_MIN or ratio > WINDOW_RATIO_MAX:
                return (
                    f"检测到模拟器窗口比例非 16:9（{w}x{h}，宽高比 {ratio:.2f}），画面可能被拉伸"
                    f"或留黑边，不影响任务执行；建议在 MuMu 设置中把实例分辨率改为 1280×720 横屏后重启实例"
                )
            return ""
        return "MuMuManager 报告的窗口句柄无效（窗口刚重启？），不影响任务执行"
    except Exception as exc:  # noqa: BLE001
        return f"窗口形状检查跳过（{exc}）"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="设备横屏预检：默认只读、不写设备；只有 --apply 才允许写")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只预检，不修改设备（默认行为）")
    mode.add_argument("--apply", action="store_true",
                      help="任务真正开始前使用：先探测原值，竖屏才切换，失败按原值回滚")
    args = parser.parse_args()

    if not adb_serial():
        log("未找到唯一的交错战线实例（实例里必须装有 com.megagame.crosscore.bilibili）")
        log("候选端口与查包结果见上；请确认目标模拟器实例已启动，或用 ADB_SERIAL 指定已装游戏的实例")
        if not os.environ.get("ADB_SERIAL"):
            log("提示：如果壳顶部显示「当前控制器：请选择模拟器」，请先在壳首页选择模拟器/ADB 控制器；"
                "本预检只提示，不会自动修改控制器选择")
        return EXIT_ADB

    # 先确认 adb 能连通，否则报明确错误而不是卡住
    code, out, error = run_adb("get-state", timeout=12)
    if code != 0 or out.strip() != "device":
        log(f"ADB 不可用（{adb_serial()}）：{error.strip() or out.strip() or '无输出'}")
        log("请确认目标模拟器实例已启动，或用 ADB_PATH / ADB_SERIAL 指定")
        return EXIT_ADB

    # P-078 身份闸门：壳连的设备必须与按 android_id 认出的目标一致
    if not verify_device_identity(adb_serial()):
        return EXIT_DEVICE_IDENTITY

    if args.apply:
        # 显示修正：竖→横切换（仅任务真正开始前执行，失败按原值回滚）
        ensure_landscape_orientation()
        # 确保 density 正确（不影响方向）：只在不一致时写，失败按原值回滚
        device_judge.ensure_density(adb_serial(), adb_path(), log=log)
        log("开始预检（--apply，允许写设备）")
    else:
        log("只读模式：只做检测、不写设备；确需修复请由任务入口传 --apply")

    info = preflight()
    if info["ok"]:
        note = f"；{info['note']}" if info.get("note") else ""
        log(
            f"预检通过：渲染面 {info['w']}x{info['h']}，渲染正常{note}"
            f"（fill={info['fill']} bbox={info['bbox_w']}x{info['bbox_h']} 仅诊断）"
        )
        warning = host_window_warning()
        if warning:
            log(f"警告（窗口级，不阻断）：{warning}")
        return EXIT_OK

    log(f"预检失败：{info['reason']}")
    log(f"详情：{info}")
    log("已中止本次自动化（预检失败，识别会失准）。请确认 MuMu 实例分辨率 1280x720 且已横屏"
        "（adb shell dumpsys display 查 mCurrentOrientation，0/2=竖 1/3=横）；"
        "弹窗/黑边不算失败，只有竖屏、渲染面尺寸不对或整帧黑屏才会拦")
    return EXIT_PREFLIGHT


if __name__ == "__main__":
    raise SystemExit(main())
