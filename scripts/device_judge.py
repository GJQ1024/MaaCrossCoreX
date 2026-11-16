# -*- coding: utf-8 -*-
"""设备方向与渲染有效性判定 —— 唯一判据源（2026-11-02）。

pretask_landscape.py / device_mode.py / capture.py 共用本模块。
历史上同一段判据曾有三份副本，弹窗误报修了一份漏两份，故抽到这里；
改判据只改本文件，调用方只保留流程（重试/窗口检查/日志）。

口径：
  1. 方向：dumpsys display 的 mCurrentOrientation（0/2=竖，1/3=横）。
     物理帧 `wm size` 恒为 720x1280（竖面板、host 旋转 canvas 输出横屏），
     与方向无关，不作判据。
  2. 渲染面尺寸：帧恰为 1280x720（MAA 实际拿到的画面）。
  3. 渲染有效性：整帧暗像素占比 > 95% 才算黑屏；弹窗/黑边只会让非黑占比
     （fill）下降，一律只作附注。禁止用非黑区包围盒宽高比/覆盖率推断方向——
     弹窗黑背景实测把 bbox 挤成 1232x539（宽高比 2.286）而被误判"压扁"，
     设备实际横屏正常。
"""

from __future__ import annotations

import re
import subprocess
import time

EXPECTED_W, EXPECTED_H = 1280, 720
BLACK_THRESHOLD = 12      # 单像素暗判定阈值（三通道均低于它算暗像素）
BLACK_FRAME_RATIO = 0.95  # 全帧暗像素占比超过它 = 渲染黑屏（弹窗黑边远达不到）
POPUP_FILL_HINT = 0.5     # 非黑占比低于它时附注"画面偏暗/有弹窗遮挡"，不影响判定

LANDSCAPE_SIZE = "1280x720"
PORTRAIT_SIZE = "720x1280"  # 与物理面板一致，用于"先竖再横"切换
DENSITY = "240"

ORIENTATION_PORTRAIT = (0, 2)
ORIENTATION_LANDSCAPE = (1, 3)

# 待机画面分两类（2026-11-06 用户澄清后拆分，此前混为一谈）：
#
#   1) 游戏屏保（game_screensaver）——游戏自己的待机/挂机画面。
#      前台仍是 com.megagame.crosscore.bilibili，isKeyguardShowing=false；
#      画面是游戏 CG（角色/场景全屏）+ 左下大号时钟/日期/电量 + 右下角圆形刷新图标，
#      没有系统状态栏、没有系统锁屏壁纸与「上滑解锁」提示。
#      证据帧（三帧外观一致，均有右上角无状态栏、右下圆形图标）：
#        sandbox/tutorial_run3/frames/s00_lobby_before.png  12:15 / 2026/11/03 / 84%
#        sandbox/tutorial_neg/33_wake/001.png               11:25 / 2026/11/03 / 90%
#        sandbox/mail_state2.png                            05:01 / 2026/11/06 / 71%
#      实测 2026-11-06 17:24 屏保帧：mCurrentFocus 为该游戏 Activity、
#      isKeyguardShowing=false，轻点中性点后回到补给站主界面。
#      `svc power stayon true` 对它无效——它由游戏客户端自己计时触发。
#
#   2) 系统锁屏（system_lock）——Android keyguard。
#      前台为 SystemUI/Keyguard（游戏不在前台），isKeyguardShowing 通常为 true；
#      画面是系统壁纸 + 居中时钟/日期，带状态栏/通知/上滑提示。
#
# 视觉三要素（时钟+日期+电量）只能说明「这是待机画面」，**不能**区分两类；
# 区分靠前台包名 + keyguard 状态。旧实现把三要素直接当系统锁屏，故把游戏屏保
# 误判成锁屏（误发 KEYCODE_WAKEUP + 上滑，且以为 stayon 能压住它）。
IDLE_VISUAL_REGION = (0, 480, 560, 240)  # x, y, w, h（1280x720 画面）
# 这三条正则是「容忍 OCR 粘连」的写法：实测 '05:01P.M'、'FRIDAY/2026/11/0671%口'
# 会把时钟/日期/电量连成一串，带 \b 的旧写法会整条匹配失败（mail_state2.png 实测）。
_IDLE_CLOCK_RE = re.compile(r"(?<!\d)\d{1,2}\s*[:：.]\s*[0-5]\d(?!\d)")
_IDLE_DATE_RE = re.compile(r"(?:MON|TUE|WED|THU|FRI|SAT|SUN)[A-Z]*|\d{4}/\d{1,2}/\d{1,2}")
_IDLE_BATTERY_RE = re.compile(r"\d{1,3}\s?%")

GAME_PACKAGE = "com.megagame.crosscore.bilibili"
SYSTEMUI_PACKAGES = ("com.android.systemui", "com.android.keyguard", "com.android.internal.systemui")

SCREEN_LABELS = {
    "game_screensaver": "游戏屏保（游戏内待机画面，前台仍是游戏，非系统锁屏）",
    "system_lock": "Android 系统锁屏（keyguard 显示中，前台为 SystemUI/Keyguard）",
    "idle_unknown": "待机画面（前台/keyguard 读不到，无法区分游戏屏保与系统锁屏）",
    "black": "渲染黑屏（整帧接近全黑）",
    "normal": "正常游戏界面",
}

# 游戏屏保唤醒中性点（1280x720）：右缘中段空地。
# 依据 2026-11-06 实机帧 sandbox/screen_after_tap.png 的主界面布局避让：
#   新途补给/星贸凭证/活动 y<180、满赠馈赠 x<=1010、出击 x<=1015 且 y>=470、
#   底部功能栏 x<=1105、左侧菜单 x<=270；屏保自带的圆形刷新图标约 (965,657)。
# (1200,400) 同时落在两类画面的空白画布上，点它不会触发任何入口/礼包/公告/购买。
NEUTRAL_TAP = (1200, 400)
BACK_KEYEVENT = 4

# MuMu 实测：KEYCODE_POWER(26) / KEYCODE_SLEEP(223) 被忽略，屏幕状态与
# mWakefulness 都不变；只有 KEYCODE_WAKEUP(224) 能点亮，随后需上滑解锁
# （input swipe 640 620 640 160 300）。仅用于真正的系统锁屏，不用于游戏屏保。
WAKE_KEYEVENT = 224
UNLOCK_SWIPE = ("640", "620", "640", "160", "300")


def _adb_text(adb_path: str, args: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(
            [adb_path, *args],
            capture_output=True, text=True, errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode, result.stdout
    except Exception:  # noqa: BLE001  adb 不存在 / 超时，按读取失败处理
        return 1, ""


def get_orientation(serial: str, adb_path: str = "adb") -> int | None:
    """读 dumpsys display 的 mCurrentOrientation（0/2=竖，1/3=横）；失败返回 None。"""
    if not serial:
        return None
    code, out = _adb_text(adb_path, ["-s", serial, "shell", "dumpsys", "display"])
    if code != 0:
        return None
    match = re.search(r"mCurrentOrientation\s*=\s*(\d)", out)
    return int(match.group(1)) if match else None


def _wm(adb_path: str, serial: str, *args: str, timeout: int = 20) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            [adb_path, "-s", serial, *args],
            capture_output=True, text=True, errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def read_wm_state(serial: str, adb_path: str = "adb") -> dict:
    """只读探测 wm size / density：physical 恒有，override 无则记 None。

    写设备前必须先调本函数存原值（2026-11-03 事故：dist 旧副本只写
    `wm size 1280x720` 不 reset，把设备永久留在 Override 状态）。
    """
    size_code, size_out = _adb_text(adb_path, ["-s", serial, "shell", "wm", "size"])
    den_code, den_out = _adb_text(adb_path, ["-s", serial, "shell", "wm", "density"])
    return {
        "size_physical": (re.search(r"Physical size:\s*(\d+x\d+)", size_out) or [None, None])[1]
        if size_code == 0 else None,
        "size_override": (re.search(r"Override size:\s*(\d+x\d+)", size_out) or [None, None])[1]
        if size_code == 0 else None,
        "density_physical": (re.search(r"Physical density:\s*(\d+)", den_out) or [None, None])[1]
        if den_code == 0 else None,
        "density_override": (re.search(r"Override density:\s*(\d+)", den_out) or [None, None])[1]
        if den_code == 0 else None,
    }


def _restore_wm(serial: str, adb_path: str, original: dict, log=print) -> None:
    """按原值回滚：原本无 Override 就 reset，原本有就写回原值。先 size 后 density。"""
    size_target = original.get("size_override") or "reset"
    code, out, err = _wm(adb_path, serial, "shell", "wm", "size", size_target)
    log(f"  回滚 wm size {size_target} → exit={code} {out.strip() or err.strip()}")
    density_target = original.get("density_override") or "reset"
    code, out, err = _wm(adb_path, serial, "shell", "wm", "density", density_target)
    log(f"  回滚 wm density {density_target} → exit={code} {out.strip() or err.strip()}")


def ensure_density(serial: str, adb_path: str = "adb", log=print) -> bool:
    """只在密度与目标不一致时写；写失败按原值回滚（原无 Override 则 reset）。"""
    state = read_wm_state(serial, adb_path)
    current = state.get("density_override") or state.get("density_physical")
    if current == DENSITY:
        log(f"密度已是 {DENSITY}，跳过写入")
        return True
    original = {"size_override": state.get("size_override"),
                "density_override": state.get("density_override")}
    code, out, err = _wm(adb_path, serial, "shell", "wm", "density", DENSITY)
    log(f"  wm density {DENSITY} → exit={code} {out.strip() or err.strip()}")
    if code != 0:
        _restore_wm(serial, adb_path, original, log)
        return False
    return True


def ensure_landscape_orientation(serial: str, adb_path: str, log=print) -> bool:
    """竖→横→reset 切换让 MuMu 渲染面稳定横屏（2026-11-02 根治方案，共用实现）。

    背景：物理面板 720×1280（竖），游戏 Activity 锁竖屏。直接
    `wm size 1280x720`（Override）会让 guest 上报 rotate_display，host 跟随
    变竖屏渲染面（实测 mCurrentOrientation 翻成 0、窗口压成 0.56 竖条）——
    capture 旧流程就是这么把横屏设备改坏的。
    正解：先 `wm size 720x1280` 切回物理竖屏 → `wm size 1280x720` 触发方向重算
    → `wm size reset` 清掉 Override，让面板旋转(orientation=1)承担横屏。

    2026-11-03 加固：**写前先只读探测原值**，任一写命令失败或切换后仍是竖屏
    都按原值回滚（原本无 Override → `wm size reset`；原本有 → 写回原值），
    不再把设备留在 Override 脏状态。返回 True=已是/已成功横屏，False=失败已回滚。
    注意：进程被强杀（SIGKILL/断电）时无法回滚，调用方仍需在启动时探测。
    """
    orientation = get_orientation(serial, adb_path)
    if not is_portrait(orientation):
        log(f"当前方向 orientation={orientation}（横屏），跳过切换（未写设备）")
        return True

    original = read_wm_state(serial, adb_path)
    log(f"检测到竖屏 orientation={orientation}，执行竖→横切换"
        f"（原值 size_override={original.get('size_override')} "
        f"density_override={original.get('density_override')}）")

    final = None
    ok = True
    try:
        for size, pause in ((PORTRAIT_SIZE, 3), (LANDSCAPE_SIZE, 5), ("reset", 2)):
            code, out, err = _wm(adb_path, serial, "shell", "wm", "size", size)
            log(f"  wm size {size} → exit={code} {out.strip() or err.strip()}")
            if code != 0:
                ok = False
                break
            time.sleep(pause)
        if ok:
            final = get_orientation(serial, adb_path)
            if is_portrait(final):
                log(f"警告：切换后 orientation 仍为 {final}（竖屏），显示修正未生效")
                ok = False
    except Exception as exc:  # noqa: BLE001  adb 中途异常也要回滚
        log(f"警告：切换过程异常 {exc}")
        ok = False

    if not ok:
        log("切换失败，按原值回滚（避免把设备留在 Override 脏状态）")
        _restore_wm(serial, adb_path, original, log)
        return False

    log(f"切换成功：orientation={final}（横屏）")
    return True


def is_portrait(orientation: int | None) -> bool:
    """0/2 为竖屏，1/3 为横屏；未知时保守当竖屏（触发切换）。"""
    if orientation is None:
        return True
    return orientation in ORIENTATION_PORTRAIT


def is_landscape(orientation: int | None) -> bool:
    return orientation is not None and orientation in ORIENTATION_LANDSCAPE


def probe_foreground(serial: str, adb_path: str = "adb") -> dict:
    """读 `mCurrentFocus` 与 `isKeyguardShowing`，作为屏保/锁屏的区分主判据。

    返回 {"package": str|None, "activity": str|None, "keyguard": bool|None}；
    读取失败时字段全为 None，调用方必须按「无法区分」降级，不能默认当锁屏。
    """
    empty = {"package": None, "activity": None, "keyguard": None}
    if not serial:
        return empty
    code, out = _adb_text(adb_path, ["-s", serial, "shell", "dumpsys", "window"])
    if code != 0:
        return empty
    matched = re.search(
        r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z][\w.]*)/([\w.$]+)", out)
    keyguard = re.search(r"isKeyguardShowing=(true|false)", out)
    return {
        "package": matched.group(1) if matched else None,
        "activity": matched.group(2) if matched else None,
        "keyguard": keyguard.group(1) == "true" if keyguard else None,
    }


def _text_box(item) -> tuple[str, object]:
    """兼容 (text, box) 元组与带 .text/.box 的识别结果对象。"""
    if isinstance(item, (tuple, list)):
        return str(item[0]), item[1]
    return str(getattr(item, "text", "")), getattr(item, "box", None)


def _box_center_in(box, region: tuple[int, int, int, int]) -> bool:
    rx, ry, rw, rh = region
    if box is None:
        return False
    if hasattr(box, "x"):
        bx, by, bw, bh = box.x, box.y, box.w, box.h
    else:
        bx, by, bw, bh = (int(v) for v in box)
    cx, cy = bx + bw / 2, by + bh / 2
    return rx <= cx <= rx + rw and ry <= cy <= ry + rh


def looks_like_idle_screen(items, region=IDLE_VISUAL_REGION) -> bool:
    """视觉待机判据：时钟 + 星期/日期 + 电量百分比同时出现在左下区域。

    **只说明「这是待机画面」，不区分游戏屏保与系统锁屏**——两类共用这套外观。
    限定左下区域是为了排除游戏内玩家面板右上角的时间/电量（s02_menu.png，y=15）。
    证据帧：s00_lobby_before.png、33_wake/001.png、mail_state2.png（均为游戏屏保）。
    """
    seen_clock = seen_date = seen_battery = False
    for item in items or []:
        text, box = _text_box(item)
        if not _box_center_in(box, region):
            continue
        seen_clock = seen_clock or bool(_IDLE_CLOCK_RE.search(text))
        seen_date = seen_date or bool(_IDLE_DATE_RE.search(text))
        seen_battery = seen_battery or bool(_IDLE_BATTERY_RE.search(text))
    return seen_clock and seen_date and seen_battery


def diagnose_screen(items=None, serial: str | None = None,
                    adb_path: str = "adb", frame_info: dict | None = None) -> dict:
    """区分「游戏屏保 / 系统锁屏 / 黑屏 / 正常」，给出置信度与日志文案。

    返回 {"kind", "confidence", "reason", "foreground", "keyguard"}。
    判据优先级：
      1. 整帧接近全黑 → black（high）；
      2. keyguard=true 或前台包名属 SystemUI → system_lock（high）；
      3. 前台是游戏包 + 左下时钟/日期/电量 → game_screensaver（high）；
         前台是游戏包但无待机外观 → normal（high）；
      4. 前台/keyguard 都读不到 + 待机外观 → idle_unknown（low，调用方需降级处理）。
    """
    fill = (frame_info or {}).get("fill")
    if fill is not None and fill <= 1.0 - BLACK_FRAME_RATIO:
        return {"kind": "black", "confidence": "high",
                "reason": f"暗像素占比 {1 - fill:.0%} ≥ {BLACK_FRAME_RATIO:.0%}",
                "foreground": None, "keyguard": None}

    fg = probe_foreground(serial or "", adb_path)
    package, keyguard = fg["package"], fg["keyguard"]
    idle = looks_like_idle_screen(items)

    if keyguard is True or (package or "") in SYSTEMUI_PACKAGES:
        return {"kind": "system_lock", "confidence": "high",
                "reason": f"keyguard={keyguard} 前台={package}",
                "foreground": package, "keyguard": keyguard}
    if package and "crosscore" in package:
        if idle:
            return {"kind": "game_screensaver", "confidence": "high",
                    "reason": f"前台={package}、keyguard=false、左下时钟+日期+电量",
                    "foreground": package, "keyguard": keyguard}
        return {"kind": "normal", "confidence": "high",
                "reason": f"前台={package}、无待机三要素",
                "foreground": package, "keyguard": keyguard}
    if package is None and keyguard is None:
        if idle:
            return {"kind": "idle_unknown", "confidence": "low",
                    "reason": "前台/keyguard 读取失败，视觉只能确认是待机画面",
                    "foreground": None, "keyguard": None}
        return {"kind": "normal", "confidence": "low",
                "reason": "前台/keyguard 读取失败且无待机外观",
                "foreground": None, "keyguard": None}
    return {"kind": "normal", "confidence": "medium",
            "reason": f"前台={package}、keyguard={keyguard}、无待机三要素",
            "foreground": package, "keyguard": keyguard}


def tap_neutral(serial: str, adb_path: str = "adb") -> tuple[int, int]:
    """轻点中性点唤醒游戏屏保；返回实际坐标。"""
    x, y = NEUTRAL_TAP
    _adb_text(adb_path, ["-s", serial, "shell", "input", "tap", str(x), str(y)])
    return x, y


def press_back(serial: str, adb_path: str = "adb") -> None:
    """按一次 BACK（屏保的第二手段；主界面按 BACK 只会触发退出确认，不会购买/领取）。"""
    _adb_text(adb_path, ["-s", serial, "shell", "input", "keyevent",
                         str(BACK_KEYEVENT)])


def unlock_system(serial: str, adb_path: str = "adb") -> None:
    """系统锁屏专用：KEYCODE_WAKEUP 点亮 + 上滑解锁。游戏屏保不得调用。"""
    _adb_text(adb_path, ["-s", serial, "shell", "input", "keyevent",
                         str(WAKE_KEYEVENT)], timeout=20)
    time.sleep(0.8)
    _adb_text(adb_path, ["-s", serial, "shell", "input", "swipe",
                         *UNLOCK_SWIPE], timeout=20)


def is_black_frame(frame_info: dict) -> bool:
    """整帧暗像素占比 > BLACK_FRAME_RATIO 才算渲染黑屏；缺 fill 按全黑处理。"""
    fill = frame_info.get("fill")
    if fill is None:
        return True
    return fill <= 1.0 - BLACK_FRAME_RATIO


def popup_note(frame_info: dict) -> str:
    """画面偏暗时的用户附注；不影响判定。"""
    fill = frame_info.get("fill")
    if fill is not None and fill < POPUP_FILL_HINT:
        return "画面偏暗或有弹窗遮挡（附注，不影响判定）"
    return ""


def judge_frame(frame_info: dict) -> dict:
    """渲染面判定（唯一出口）：只有"尺寸不对"或"整帧接近全黑"才失败。

    frame_info 需含 w / h / fill；bbox_* 可选，仅诊断透传。
    弹窗遮挡只产生 note 附注，ok 保持 True。
    """
    w, h = frame_info.get("w", 0), frame_info.get("h", 0)
    if w != EXPECTED_W or h != EXPECTED_H:
        return {
            "ok": False,
            "reason": f"渲染面尺寸 {w}x{h} != {EXPECTED_W}x{EXPECTED_H}（自动化固定横屏渲染面）",
            "w": w, "h": h,
        }
    if is_black_frame(frame_info):
        fill = frame_info.get("fill") or 0.0
        return {
            "ok": False,
            "reason": f"整帧接近全黑（暗像素占比 {1 - fill:.0%} >= {BLACK_FRAME_RATIO:.0%}），渲染异常",
            "w": w, "h": h, "fill": round(fill, 4),
        }
    result = {
        "ok": True, "w": w, "h": h,
        "fill": round(frame_info["fill"], 4),
    }
    if frame_info.get("bbox_w") is not None:
        result["bbox_w"] = frame_info["bbox_w"]
        result["bbox_h"] = frame_info["bbox_h"]
    note = popup_note(frame_info)
    if note:
        result["note"] = note
    return result
