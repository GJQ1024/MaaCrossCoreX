# -*- coding: utf-8 -*-
"""通用 UI 常量与纯函数（不依赖 maa，可被单测直接 import）。

所有遮罩/返回/滑动/确认/资源判据只在本文件存一份；`ui_mask.py` 只负责
Maa 适配（取帧、点击、复核、日志），任务侧一律引用 preset，不再各抄阈值。
坐标基准 1280x720。

来源见 spec `specs/overlay-doomsday-20261109/spec.md` §1A.1。
"""

from __future__ import annotations

import re

import numpy as np

from viewport import scale_roi

# 遮罩集中参数：kind=ocr 用 roi+expected 识别，kind=template 用模板匹配。
# target 为绝对点；target_offset 相对识别框左上角（网络不稳定用 OCR 框基准）。
MASK_SPECS = {
    "item_popup": {
        "kind": "ocr", "roi": [447, 117, 405, 117],
        "expected": ["获得物品", "合成成功"],
        "target": [640, 537],
        "verify_absent": ["获得物品", "合成成功"], "retries": 2, "delay": 1.0,
    },
    "battle_result": {
        "kind": "ocr", "roi": [0, 20, 760, 240],
        # 2026-11-11 实机：扫荡结算页艺术字标题 OCR 恒为「扫荡成坊」(0.8427)，
        # 同页「伤害统计」稳定 0.9917；只写「扫荡成功」永不命中，会漏关整页。
        "expected": ["战斗结束", "战斗胜利", "战斗失败", "扫荡成功", "扫荡成坊", "伤害统计"],
        "target": [640, 400],
        "verify_absent": ["战斗结束", "战斗胜利", "战斗失败", "扫荡成功", "扫荡成坊", "伤害统计"],
        "retries": 2, "delay": 1.2,
    },
    "chip_warehouse_full": {
        "kind": "ocr", "roi": [250, 160, 780, 400],
        "expected": ["芯片仓库已达上限"],
        "confirm": {"roi": [420, 390, 340, 120], "expected": ["确定"]},
        "verify_absent": ["芯片仓库已达上限"], "retries": 2, "delay": 1.0,
    },
    "announcement": {
        "kind": "template", "roi": [900, 55, 362, 200],
        "template": "StartUp/CloseAnnouncement.png", "retries": 1, "delay": 1.0,
    },
    "anniversary": {
        "kind": "template", "roi": [900, 55, 362, 200],
        "template": "StartUp/ZNGG.png", "retries": 1, "delay": 1.0,
    },
    "promo": {
        "kind": "template", "roi": [900, 55, 362, 200],
        "template": "StartUp/ClosePromo.png", "retries": 1, "delay": 1.0,
    },
    "network_unstable": {
        "kind": "ocr", "roi": [480, 311, 312, 39],
        "expected": ["网络不稳定"],
        "target_offset": [166, 118],
        "verify_absent": ["网络不稳定"], "retries": 1, "delay": 1.5,
    },
    "login_lost": {
        "kind": "ocr", "roi": [300, 180, 680, 360],
        "expected": ["连接服务器失败", "即将返回登录界面"],
        "action": "wait_login",
    },
}

# 通用遮罩分流器（preset="any"）的识别范围与优先级。
# 断线/登录类不在此列：由 通用-断线恢复 单独处理，避免两条链路抢同一个遮罩。
MASK_ANY_ORDER = [
    "item_popup", "chip_warehouse_full", "battle_result", "announcement", "anniversary", "promo",
]

# 滑动集中参数：begin/end 为 1280x720 坐标，duration 毫秒。
SWIPE_PRESETS = {
    "slow_left": {"begin": [1100, 430], "end": [200, 435], "duration": 900, "repeat": 1},
    "scroll_top": {
        "begin": [1100, 560], "end": [1100, 160], "duration": 700,
        "repeat": 8, "stop_on_stable": True,
    },
}

# 主界面守卫（ensure_main）：任务入口「进入前一次性确认 + 有界自救」的唯一参数来源。
# 判据：右下「补给站」OCR 与顶栏任一资源图标同帧命中（两者取与，避免子页残留顶栏被误判为主界面），
# 且同帧没有顶层遮罩（anchor/图标都在底栏与顶栏，模态弹窗只盖中央，单看它们无法排除
# 「主界面 + 付费/公告弹窗」；blocker_presets 复用通用遮罩判据，命中即判 not_main，
# 由恢复阶梯关闭后重判。2026-11-17 G4 帧 s10_promo_popup.png 即该假通过现场）。
# 恢复阶梯：待机点时钟 → 关遮罩 → 取消二次确认 → 点首页按钮 → 点左上返回箭头；
# 绝不盲按 BACK：主界面按 BACK 会退到 B 站游戏中心（见 BACK_TABLE 注释与 11-16 冷启动事故）。
HOME_GUARD = {
    "anchor": {"roi": [934, 582, 176, 127], "expected": ["补给站"], "ocr_threshold": 0.6},
    "resource_roi": [0, 0, 1280, 120],
    "resource_templates": ["HomeResourceAp.png", "HomeResourceCoin.png", "HomeResourceGem.png"],
    "resource_threshold": 0.6,
    "blocker_presets": list(MASK_ANY_ORDER),
    "standby": {
        "roi": [0, 540, 320, 140],
        "pattern": r"^[0-2]?\d\s*[:：.]\s*[0-5]\d",
        "threshold": 0.85,
    },
    "home_templates": ["StartUp/HomeButton.png", "StartUp/HomeButton1.png"],
    "home_roi": [180, 10, 100, 70],
    "back_template": "StartUp/BackButton.png",
    "back_roi": [0, 0, 200, 90],
    "template_threshold": 0.8,
    "max_rounds": 4,
    "step_wait": 1.5,
}

# 返回口径查表：anchor 是页面锚点，action 决定返回动作。
# personal_panel 只点空白，禁止 post_press_key（会退出游戏）。
# profile_page 是点头像打开的档案页：左上「<」可安全返回，BACK 键会退到 B 站游戏中心。
BACK_TABLE = [
    {"id": "personal_panel", "anchor": ["成就", "档案"], "action": "click_blank", "point": [333, 430]},
    {"id": "profile_page", "anchor": ["支援助战", "徽章展示"], "action": "click_back_template"},
    {"id": "dorm_exit", "anchor": ["是否退出宿舍"], "action": "click_text", "text": "确定"},
    {"id": "activity_page", "anchor": ["活动探索", "暴虐", "演算单元"], "action": "click_back_template"},
    {"id": "sub_panel", "anchor": [], "action": "keyevent_back"},
]

# 二次确认白名单：命中任一词时 generic 不代点取消（由各自专用流程处理）。
CONFIRM_WHITELIST = [
    "网络不稳定", "连接服务器失败", "即将返回登录界面",
    "是否退出宿舍", "是否撤退", "是否消耗1次战斗次数",
    "是否购买", "是否兑换", "是否使用", "战术重置", "宿舍赠送",
]

# 资源 OCR 区域：体力 / 活动券 / 末日战线次数 / 阀限禁区次数。
RESOURCE_ROIS = {
    "stamina": [1000, 20, 105, 55],
    "activity_tickets": [1180, 20, 70, 55],
    "doomsday_attempts": [1035, 585, 110, 40],
    "threshold_attempts": [960, 632, 140, 45],
}


def parse_count(text) -> tuple[int, int] | None:
    """OCR 文本取 cur/max：正则 (\\d+)\\s*[/／]\\s*(\\d+)，只取第一个匹配。

    text 可以是单个字符串，也可以是 OCR 文本列表（按空格拼接）。
    来源：spec §1A.1 / 末日战线次数解析。
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = " ".join(str(item) for item in text if item is not None)
    match = re.search(r"(\d+)\s*[/／]\s*(\d+)", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_first_int(text) -> int | None:
    """OCR 文本取第一个连续数字；读不到返回 None。"""
    if text is None:
        return None
    if not isinstance(text, str):
        text = " ".join(str(item) for item in text if item is not None)
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


def ocr_texts(result) -> list[str]:
    """Maa 识别结果的全部文本：best_result + all_results，过滤 None。

    来源：my_action.VerifiedClickAction._ocr_texts / cost_guard 的同口径写法。
    """
    texts = []
    if not result:
        return texts
    candidates = [getattr(result, "best_result", None)]
    candidates.extend(getattr(result, "all_results", None) or [])
    for item in candidates:
        value = getattr(item, "text", None)
        if value is None:
            continue
        texts.append(str(value))
    return texts


def mean_brightness(image, roi=None) -> float:
    """平均亮度：roi 为空取整帧（对应 mainstory _mean_brightness），
    给了 roi 则取该区域三通道灰度均值（对应 _bright / _tab_state）。
    """
    gray = np.asarray(image, dtype=np.float32).mean(axis=2)
    if not roi:
        return float(gray.mean())
    x, y, w, h = [int(v) for v in roi]
    crop = gray[y:y + h, x:x + w]
    if crop.size == 0:
        return 0.0
    return float(crop.mean())


def frame_flatness(image) -> float:
    """16x9 采样网格的灰度标准差（原样搬迁 mainstory _frame_flat）。

    转场淡入淡出时整帧接近纯色，std 很小；扁平帧不能参与锚定比较。
    """
    gray = np.asarray(image, dtype=np.float32).mean(axis=2)
    height, width = gray.shape[:2]
    rows = np.linspace(0, height - 1, 9).astype(int)
    cols = np.linspace(0, width - 1, 16).astype(int)
    return float(gray[np.ix_(rows, cols)].std())


def frame_diff(a, b) -> float:
    """两帧 16x9 采样点的平均灰度差（原样搬迁 mainstory _frame_diff）。

    尺寸不一致视为无法比较，返回 999.0（与原实现一致）。
    """
    ga = np.asarray(a, dtype=np.float32).mean(axis=2)
    gb = np.asarray(b, dtype=np.float32).mean(axis=2)
    if ga.shape[:2] != gb.shape[:2]:
        return 999.0
    height, width = ga.shape[:2]
    rows = np.linspace(0, height - 1, 9).astype(int)
    cols = np.linspace(0, width - 1, 16).astype(int)
    return float(np.abs(ga[np.ix_(rows, cols)] - gb[np.ix_(rows, cols)]).mean())


def bright_mask(image, roi, threshold=160) -> float:
    """ROI 内亮像素比例：gray=三通道均值，亮像素 = gray >= threshold。

    与 chip_filter_flow._read_detail_lock_visual 的 gray >= 160 同口径
    （默认阈值 160，调用方不得下调）。
    """
    gray = np.asarray(image)[..., :3].astype(np.float32).mean(axis=2)
    x, y, w, h = [int(v) for v in roi]
    crop = gray[y:y + h, x:x + w]
    if crop.size == 0:
        return 0.0
    return float((crop >= float(threshold)).mean())


def _option_box(point) -> list[int]:
    """point=[x,y] 时按 chip_build_select._selected_at 的 70x26 采样框展开；
    已是 4 元组时按原 box 直接使用（兼容 _color_state 的原始调用口径）。
    """
    values = [int(v) for v in point]
    if len(values) >= 4:
        return values[:4]
    return [values[0] - 35, values[1] - 13, 70, 26]


def read_option_state(image, point) -> str | None:
    """读取选项文字颜色状态：gold（选中）/ gray（未选中）/ flash（按压白闪）。

    原样搬迁 chip_build_select._color_state，判据与阈值一字不改：
    Maa 截图是 BGR，选中≈[64,175,233]、未选中≈[141,138,138]、
    点按瞬间整块刷白≈[224,227,232]（必须等白闪消退再判定）。
    point 支持 [x,y]（默认 70x26 采样框）或 [x,y,w,h]。
    """
    try:
        arr = np.asarray(image)
    except Exception:
        return None
    if arr.ndim < 3:
        return None
    x, y, w, h = _option_box(point)
    crop = arr[max(0, y - 2):y + h + 2, max(0, x - 2):x + w + 2, :3].astype(np.int16)
    if crop.size == 0:
        return None
    lum = crop.mean(axis=2)
    mask = lum > 120
    if int(mask.sum()) < 8:
        return None
    mean = crop[mask].mean(axis=0)
    b, g, r = float(mean[0]), float(mean[1]), float(mean[2])
    if b > 200 and g > 200 and r > 200:
        return "flash"
    if r - b > 60 and r > 180:
        return "gold"
    if max(mean) - min(mean) < 25:
        return "gray"
    return None


def yellow_frame_selected(image, point) -> bool | None:
    """读取黄色选中框；原样搬迁 chip_filter_flow.quality_option_is_selected。

    逐像素判据与阈值一字不改：绿>100、边缘通道最大值>160、最小值<130，
    黄色像素占比 >= 0.04 即视为选中；与 RGB/BGR 通道顺序无关。
    """
    x, y, width, height = scale_roi(
        image, [point[0] - 64, point[1] - 58, 128, 118]
    )
    crop = np.asarray(image)[y:y + height, x:x + width]
    if crop.size == 0 or crop.ndim < 3 or crop.shape[2] < 3:
        return None
    channels = crop[..., :3].astype(np.int16)
    edge_a = channels[..., 0]
    green = channels[..., 1]
    edge_b = channels[..., 2]
    yellow = (
        (green > 100)
        & (np.maximum(edge_a, edge_b) > 160)
        & (np.minimum(edge_a, edge_b) < 130)
    )
    return float(yellow.mean()) >= 0.04
