# -*- coding: utf-8 -*-
"""JCZX 实例显示模式管理（仓库版；本地入口 apps/crosscore-auto/tools/device_mode.py 只做桥接）。

MuMu 实例 JCZX(index 1) 本身配置为 tablet.3 = 1280x720 横屏 + window_auto_rotate，
但安卓侧 wm size 若与窗口方向不一致会导致 MuMu 窗口信箱化/压扁。
本工具统一管理横屏（自动化用）与可选竖屏还原，并带画面有效性预检。

判据（方向/渲染面/黑屏）统一在 scripts/device_judge.py，本文件不含判据逻辑。

serial/adb 来源：--serial / --adb-path 参数 > 环境变量 ADB_SERIAL / ADB_PATH；
都空时明确报错，不写死端口（端口由模拟器分配、会漂移）。

用法：
  python tools/device_mode.py --landscape   # 设 1280x720 并预检
  python tools/device_mode.py --check       # 仅预检当前画面（只读）
  python tools/device_mode.py --restore     # 可选：还原 720x1280 竖屏（默认不还原）
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import device_judge  # noqa: E402

# 尺寸/密度常量唯一来源是 device_judge（此处重导出，供 run_task 等库消费方 import）；
# 可选竖屏参数（2026-11-02 起默认不还原；仅 --restore 显式调用时使用）
ORIGINAL_SIZE = device_judge.PORTRAIT_SIZE
ORIGINAL_DENSITY = device_judge.DENSITY
# 自动化用横屏：与 MuMu 窗口方向一致，避免信箱化
LANDSCAPE_SIZE = device_judge.LANDSCAPE_SIZE
LANDSCAPE_DENSITY = device_judge.DENSITY

# 宿主窗口宽高比三态（判 MuMu 窗口形状而非 adb 帧，只警告不阻断）：
#   < WINDOW_PORTRAIT_RATIO        竖窗（游戏画面被压成中间一条横带）
#   [x, MIN) 或 (MAX, ...]         非 16:9（拉伸/信箱黑边）
#   [MIN, MAX]                     正常满屏横屏
# 16:9=1.778；main_wnd 外框含标题栏（实测 1.664）、render_wnd 为纯画面（1.778），
# 下限 1.45 容纳标题栏/侧边工具条，竖窗实测 0.5625 稳落在 <0.95 区间。
WINDOW_PORTRAIT_RATIO = 0.95
WINDOW_RATIO_MIN = 1.45
WINDOW_RATIO_MAX = 2.20

MUMU_ROOTS = (
    r"D:\Program Files\Netease\MuMu",
    r"C:\Program Files\Netease\MuMu",
)


def adb_path() -> str:
    return os.environ.get("ADB_PATH") or "adb"


def current_serial() -> str:
    return os.environ.get("ADB_SERIAL") or ""


def require_serial() -> str:
    value = current_serial()
    if not value:
        raise SystemExit(
            "ADB_SERIAL 为空：请先用自动发现拿到实例 serial（或用 --serial 指定）；本工具不写死端口"
        )
    return value


def run_adb(*args, check=False):
    cmd = [adb_path(), "-s", require_serial(), *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"adb failed: {r.stderr}")
    return r.stdout.strip()


def set_mode(size: str, density: str):
    run_adb("shell", "wm", "size", size)
    run_adb("shell", "wm", "density", density)


def screencap_bytes() -> bytes:
    r = subprocess.run(
        [adb_path(), "-s", require_serial(), "exec-out", "screencap", "-p"],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"screencap failed: {r.stderr}")
    return r.stdout


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


def check_host_window(vm_index: str | None = None) -> dict:
    """窗口级检查：MuMu 宿主窗口必须是 16:9 横向（只警告不阻断）。

    教训：window_auto_rotate=true 时，安卓桌面 / B服 SDK 登录页（竖屏内容）会把
    MuMu 渲染面转成竖条（实测 nemudisplay 324x576，比例 0.5625），游戏画面被压成
    中间一条横带——而 adb 帧始终是 1280x720 横图，帧级 preflight 完全看不见。
    窗口形状用 Win32 GetWindowRect 直接量（MuMuManager info 报告 render_wnd /
    main_wnd 句柄，只读 API，不抢焦点、不动用户窗口布局）。
    """
    if sys.platform != "win32":
        return {"ok": True, "window_skipped": "非 Windows 平台，跳过窗口检查"}
    root = mumu_root()
    if root is None:
        return {"ok": True, "window_skipped": "未找到 MuMu 安装目录，跳过窗口检查"}

    idx = vm_index or os.environ.get("ADB_VM_INDEX", "1")
    manager = root / "nx_main" / "MuMuManager.exe"
    try:
        r = subprocess.run(
            [str(manager), "info", "-v", idx],
            capture_output=True, text=True, errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = r.stdout[r.stdout.find("{"):]
        info = json.loads(payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "window_skipped": f"MuMuManager 查询失败，跳过窗口检查: {exc}"}

    if not info.get("is_process_started"):
        return {"ok": False, "reason": f"MuMu 实例 {idx} 未在运行（先启动实例再跑任务）"}

    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    user32 = ctypes.windll.user32
    # render_wnd 是游戏渲染面（形状最准）；句柄失效时退回 main_wnd 外框
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
        detail = {"ok": True, "window": key, "window_size": f"{w}x{h}",
                  "window_ratio": round(ratio, 3)}
        if ratio < WINDOW_PORTRAIT_RATIO:
            detail.update(ok=False, reason=(
                f"检测到模拟器窗口为竖屏（{w}x{h}，宽高比 {ratio:.2f}），画面会有黑边，"
                f"不影响任务执行；建议在 MuMu 设置中把实例分辨率改为 1280×720 横屏后重启实例"))
        elif ratio < WINDOW_RATIO_MIN or ratio > WINDOW_RATIO_MAX:
            detail.update(ok=False, reason=(
                f"检测到模拟器窗口比例非 16:9（{w}x{h}，宽高比 {ratio:.2f}），画面可能被拉伸或"
                f"留黑边，不影响任务执行；建议在 MuMu 设置中把实例分辨率改为 1280×720 横屏后重启实例"))
        return detail
    return {"ok": False, "reason": "MuMuManager 报告的窗口句柄均无效（窗口刚重启？），不影响任务执行"}


def frame_info(data: bytes) -> dict:
    """PNG 解码成 device_judge.judge_frame 需要的 {w,h,fill,bbox_*}。"""
    im = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    h, w = im.shape[:2]
    mask = (im > device_judge.BLACK_THRESHOLD).any(axis=2)
    fill = float(mask.sum()) / (h * w)
    info = {"w": int(w), "h": int(h), "fill": fill}
    if mask.any():
        ys, xs = np.where(mask)
        info["bbox_w"] = int(xs.max() - xs.min() + 1)
        info["bbox_h"] = int(ys.max() - ys.min() + 1)
    return info


def preflight() -> dict:
    """方向读 mCurrentOrientation + 帧级渲染判定 + 宿主窗口形状检查（只警告）。

    方向与帧判据都在 device_judge；这里只保留流程。
    内置重试：切向瞬间 mCurrentOrientation 与帧缓冲都可能有 1-3 秒过渡
    （实机复现：切向瞬间尺寸/填充率短暂异常），间隔 1.2s 重试 4 次；
    持续失败才判定环境不正确。弹窗/黑边只会产生 note 附注，不算失败。
    """
    last = {"ok": False, "reason": "screencap 返回空"}
    for attempt in range(4):
        orientation = device_judge.get_orientation(current_serial(), adb_path())
        if orientation is not None and device_judge.is_portrait(orientation):
            last = {"ok": False,
                    "reason": f"方向为竖屏（mCurrentOrientation={orientation}），自动化固定横屏",
                    "orientation": orientation}
        else:
            if attempt == 0:
                if device_judge.is_landscape(orientation):
                    print(f"[preflight] 方向: 横屏（mCurrentOrientation={orientation}）")
                else:
                    print("[preflight] 方向: 未能读取 mCurrentOrientation，按渲染面尺寸兜底")
            try:
                data = screencap_bytes()
            except RuntimeError as exc:
                data = b""
                last = {"ok": False, "reason": str(exc)}
            if data:
                last = device_judge.judge_frame(frame_info(data))
                if last["ok"]:
                    break
        if attempt < 3:
            time.sleep(1.2)
    # 窗口形状只作为警告：帧级失败仍是硬中止，窗口不合规不改变 ok
    win = check_host_window()
    if win.get("window_skipped"):
        last["window_skipped"] = win["window_skipped"]
    elif not win.get("ok"):
        last["window_warning"] = win.get("reason")
    else:
        last["window"] = {k: v for k, v in win.items() if k != "ok"}
    return last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--landscape", action="store_true", help="设为横屏 1280x720 并预检")
    ap.add_argument("--restore", action="store_true", help="还原为用户原竖屏 720x1280")
    ap.add_argument("--check", action="store_true", help="仅预检当前画面（只读）")
    ap.add_argument("--serial", default="", help="ADB serial；默认读 ADB_SERIAL（不写死端口）")
    ap.add_argument("--adb-path", default="", help="adb 可执行文件路径；默认读 ADB_PATH")
    args = ap.parse_args()

    if args.serial:
        os.environ["ADB_SERIAL"] = args.serial
    if args.adb_path:
        os.environ["ADB_PATH"] = args.adb_path

    if args.landscape or args.check:
        if args.landscape:
            # 不再 wm size Override（会把已横屏设备翻成竖向渲染面，实测
            # mCurrentOrientation 翻成 0、窗口压成 0.56 竖条），
            # 改走 device_judge 的"竖→横→reset"共用切换
            device_judge.ensure_landscape_orientation(require_serial(), adb_path(), log=print)
        info = preflight()
        if info["ok"]:
            note = f"；{info['note']}" if info.get("note") else ""
            print(f"[preflight] OK: 渲染面 {info['w']}x{info['h']}，渲染正常{note}"
                  f"（fill={info['fill']} bbox={info.get('bbox_w')}x{info.get('bbox_h')} 仅诊断）")
            if "window" in info:
                print(f"[preflight] 窗口: {info['window']}")
            elif "window_skipped" in info:
                print(f"[preflight] 窗口检查跳过: {info.get('window_skipped')}")
            if "window_warning" in info:
                print(f"[preflight] 警告（不阻断）: {info['window_warning']}")
            return 0
        print(f"[preflight] FAIL: {info['reason']}")
        print(f"[preflight] 详情: {info}")
        print("[preflight] 中止：环境不正确（弹窗/黑边不算失败，只有竖屏、渲染面尺寸不对或整帧黑屏才拦）")
        return 2

    if args.restore:
        require_serial()
        set_mode(ORIGINAL_SIZE, ORIGINAL_DENSITY)
        print(f"[mode] 已还原竖屏 {ORIGINAL_SIZE} @ {ORIGINAL_DENSITY}（用户原始设置）")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
