# -*- coding: utf-8 -*-
"""主线刷取：屏幕适配层（Custom 动作）。

坐标全部来自主线刷取队伍页的实机测量（1280 基准）：
    现在： 594,509      体力数字框 [710,498,150,40]
    扫荡后：596,554     数字框     [700,545,130,35]
    减号 (226,559)      加号 (545,559)      开始战斗 (832,560)
单次消耗 24（7-6 与 7-10 都是 24，关卡详情页与队伍页双向核对过）。

页面对照：`现在 − 扫荡后 = 次数 × 24`。游戏不直接显示次数，所以次数一律反推，
绝不依赖某个"次数"数字位 —— 这与每日探索踩过的坑一致。
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import re
import time

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from chip_filter_flow import instance_config_path
from stop_guard import ensure_running

import ui_common
import workspace_paths

log = logging.getLogger("laa.mainstory")

COST_PER_RUN = 24
COUNT_MIN = 1
COUNT_MAX = 10

ROI_STAMINA_NOW = [710, 498, 150, 40]
ROI_STAMINA_AFTER = [700, 545, 130, 35]
MINUS_POINT = (226, 559)
PLUS_POINT = (545, 559)
CLICK_DELAY = 0.45
VERIFY_ATTEMPTS = 4

POTION_RECOVERY = {"小药": 10, "大药": 120}
POTION_USE_LIMIT = 50
# 体力药数量弹窗是**独立的一页**（实测：标题「备用燃料 / 使用后恢复10燃料」、
# 持有：35），控件与每日探索一致：
#   药量数字 (782,382,22,34)   减号 (573,383)   加号 (897,383)   确认使用 (772,460)
ROI_POTION_COUNT = [750, 370, 84, 56]
POTION_MINUS_POINT = (590, 400)
POTION_PLUS_POINT = (910, 400)           # (545, 559)

# ---------------------------------------------------------------- 区域画布定位
# 主线「虚拟场域」的区域不是列表，是一整幅可平移的 2D 画布，而且**会记住上次平移位置**。
# 所以不能再假定「地区4 居中」，改成两步：
#   ① 先把画布推到确定的左上角：反复「手指往右下拖」（视口往左上走），
#      直到相邻两帧的感知哈希不再变化（= 已抵边界）；
#   ② 再从左上角按蛇形平移，逐视口 OCR 找「0/x」的区域卡片并点进去。
# 起点/终点都避开左侧「章节选择」浮层（x < 350）和右下「收容管理处」按钮。
# 实测：**斜向拖拽不会移动画布**，必须拆成「先横后纵」两次拖拽。
CANVAS_PULL_CORNER = (
    ((450, 400), (1150, 400)),                      # 视口 → 左
    ((780, 150), (780, 600)),                       # 视口 → 上
)
# 锚定「没变化」时换一条滑动线再确认（避开左侧浮层与右下按钮）
CANVAS_PULL_RETRY = (
    ((500, 520), (1180, 520)),                      # 视口 → 左
    ((1020, 150), (1020, 600)),                     # 视口 → 上
)
CANVAS_STEPS = {
    "right": ((1150, 400), (450, 400)),             # 视口 → 右
    "left": ((450, 400), (1150, 400)),              # 视口 → 左
    "down": ((780, 600), (780, 150)),               # 视口 → 下
}
# 平移失败时换一条滑动线再试（起点落在卡片/浮层上会被吞掉，不能直接判边界）
PAN_RETRY = {
    "right": ((1180, 560), (500, 560)),
    "left": ((500, 560), (1180, 560)),
    "down": ((1020, 600), (1020, 150)),
}
ANCHOR_MAX_SWIPES = 8
ANCHOR_STABLE_FRAMES = 3
ANCHOR_SWIPE_MS = 700
ANCHOR_SETTLE_S = 0.9
# 扫描上限：先横扫到边界，再下移一行反向扫；每行最多 4 个视口、最多 5 行
# （第 7 章画布比「3 屏 × 3 屏」高：地区5 在更下面一行，所以行数放到 5）
SCAN_MAX_COLS = 4
SCAN_MAX_ROWS = 5
PROGRESS_CARD_RE = re.compile(r"(\d+)\s*[/／]\s*(\d+)")
# 卡片上的进度是独立文字（如「3/9」/「★3/9」）；必须整行匹配，
# 否则「阶段4/6」「累计36/45」这类整句也会被当成区域进度（实机已踩）。
PROGRESS_CARD_STRICT_RE = re.compile(r"[★☆\s]*(\d+)\s*[/／]\s*(\d+)\s*")
PROGRESS_ZERO_RE = re.compile(r"0\s*[/／]\s*\d+")
# 右下角固定 UI 条（阶段/累计/奖励），不是区域卡片；候选命中它一律排除
FIXED_UI_BOTTOM_RIGHT = (980, 555, 300, 165)
REGION_TAB_ROI = [190, 590, 130, 60]
# 点卡片的兜底偏移（相对「★0/x」进度文字中心）。实机名字标签在进度文字右上方，
# 用 OCR 找得到标签就用标签中心；找不到才退回这些固定偏移。
CARD_CLICK_OFFSETS = ((80, -55, "名右"), (40, -55, "名中"), (0, -55, "名左"), (0, -110, "徽标"))

# ---------------------------------------------------------------- 模式页签
# 选中态判据：文字框区域平均亮度更高。实机两组反向样本（普通选中/困难未选中）
# 131 vs 80、（困难选中/普通未选中）78 vs 114，差值 ≥25 可靠；<25 判读不出，明确失败。
# 严禁盲点页签 —— 点错会把画布重置到别的位置。
TAB_BRIGHT_DIFF = 25
TARGET_MODE = "普通"          # 目标模式，可由 pipeline 参数覆盖
MODE_CLICK_DELAY_S = 0.6
MODE_MAX_CLICKS = 2

# ---------------------------------------------------------------- 关卡选择
# 关卡行只认主线「关卡7-x」（S7-x 是剧情关，点了会进播放页，禁止点）。
# 「新」是卡片右上角的小角标，必须独立识别；并进关卡名正则会读成「关卡7-61」。
LEVEL_ROW_RE = re.compile(r"关卡\s*(7[-\s]*\d+)")
# 剧情关（关卡S7-x）与主线关卡分开识别，不并入同一个正则：剧情关不消耗燃料，
# 且没有「开始战斗」，不能用「能弹挑战按钮」这一条判据。
STORY_LEVEL_RE = re.compile(r"关卡\s*S\s*(\d+)\s*[-－—]\s*(\d+)")
CHALLENGE_RE = re.compile(r"挑战|出发|开始战斗")
NEW_BADGE_RE = re.compile(r"^新$")
NEW_BADGE_DX = (40, 260)      # 角标中心相对关卡名中心：右偏
NEW_BADGE_DY = (-60, 60)      # 同一行

# ---------------------------------------------------------------- 首通分支
# 「首次通关」是首通详情页才有的标记；首通后消失，因此它本身就是「该走首通」的信号。
# 首通时「扫荡」置灰但 OCR 仍能读出，所以判断可扫荡只能看亮度（实测亮态 169）。
FIRST_CLEAR_RE = re.compile(r"首次通关")
PROGRESS_RE = re.compile(r"进度\s*[：:]?\s*(\d+)\s*%")
SWEEP_ENABLED_BRIGHT = 140
BATTLE_VICTORY_RE = re.compile(r"战斗胜利|行动结束")
BATTLE_DEFEAT_RE = re.compile(r"战斗失败")
BATTLE_TIMEOUT_S = 180   # 7-21 实测从 HUD 到结算约 90s，90s 看门狗会误判失败；放到 180s
FIRST_CLEAR_START = (1175, 656)      # 首通详情页「开始战斗」
FIRST_CLEAR_TEAM_START = (1095, 645)  # 编队页「开始行动」
AUTO_BATTLE_POINT = (1096, 44)       # 战斗 HUD 右上自动战斗开关
SETTLE_POINT = (640, 340)            # 结算页空白（旧实现用，已弃用：点了无反应）
# 结算页右下 ▶ 实测（P-036）：[1237,595]-[1249,620]，中心 (1243,607)，13×26 白色三角。
# 注意：这个 ▶ 是**装饰**，点击无反应；退出结算页靠 Android BACK。
SETTLE_ARROW_ROI = [1225, 585, 45, 55]
SETTLE_ARROW_POINT = (1243, 607)
SETTLE_ARROW_MIN_WHITE = 100

# ---------------------------------------------------------------- 燃料埋点（只写日志）
# 主界面与编队页的燃料位置不同（实机 1280×720 截图核对）：
#   主界面「162/240」box=[544,32,78,21]（离线 OCR 实测；ROI 左边界给足，
#   否则最左边的「1」会被切掉读成「62」）
#   编队页「244/240」≈ [1088,41,150,20]
FUEL_ROI_HOME = [510, 15, 160, 55]
FUEL_ROI_TEAM = [1055, 24, 225, 60]
FUEL_RE = re.compile(r"(\d{1,3})\s*[/／]\s*(\d{1,3})")
TEAM_COST_ROI = [900, 600, 380, 120]
TEAM_COST_RE = re.compile(r"燃料\s*[-－—]\s*(\d+)")

TASK_NAME = "主线刷取"
MODE_OPTION = "主线刷取_体力消耗方式"
MODE_CONSUME_ALL = "消耗完体力"
RUNS_OPTION = "主线刷取_自定次数"
RUNS_INPUT = "扫荡次数"
POTION_OPTION_CONSUME = "主线刷取_使用体力药-消耗完"
POTION_OPTION_CUSTOM = "主线刷取_使用体力药-自定"
POTION_TYPE_OPTION = "主线刷取_体力药类型"
POTION_NUM_OPTION = "主线刷取_体力药数量"

_SESSION: dict = {}


def _param(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _ocr_digits(context, node, image, roi):
    """读 ROI 内的第一个数字（与每日探索保持同一实现）。

    绝不能把多个片段拼起来：同一 ROI 内 OCR 偶尔返回两个含数字的片段，
    拼接会得到 "11" 这种假数值，进而按错误差值点加减键。
    """
    result = context.run_recognition(
        node, image,
        pipeline_override={node: {"recognition": "OCR", "roi": list(roi), "expected": []}},
    )
    texts = []
    if result:
        candidates = [getattr(result, "best_result", None)]
        candidates.extend(getattr(result, "all_results", None) or [])
        for item in candidates:
            value = getattr(item, "text", None)
            if value is not None:
                texts.append(str(value))
    numbers = re.findall(r"\d+", " ".join(texts))
    return int(numbers[0]) if numbers else None


def _click(context, x, y):
    ensure_running(context)
    context.tasker.controller.post_click(int(x), int(y)).wait()
    ensure_running(context)


def _frame(context):
    return context.tasker.controller.post_screencap().wait().get()


def _swipe(context, begin, end, duration=700):
    ensure_running(context)
    context.tasker.controller.post_swipe(
        int(begin[0]), int(begin[1]), int(end[0]), int(end[1]), int(duration),
    ).wait()
    ensure_running(context)


def _back(context):
    """按一次返回键（Android BACK），用于收掉误开的面板。"""
    ensure_running(context)
    context.tasker.controller.post_press_key(4).wait()
    ensure_running(context)


def _now():
    return time.strftime("%H:%M:%S")


def _ocr_texts(context, roi, image=None):
    """读指定 ROI 的全部 OCR 文本（埋点专用，不参与任何判据）。"""
    image = image if image is not None else _frame(context)
    try:
        result = context.run_recognition(
            "主线刷取_区域进度OCR", image,
            pipeline_override={"主线刷取_区域进度OCR": {
                "recognition": "OCR", "roi": list(roi), "expected": [], "threshold": 0.2}},
        )
    except Exception:  # noqa: BLE001
        log.warning("主线刷取：埋点 OCR 失败 roi=%s", roi, exc_info=True)
        return []
    texts = []
    if result:
        for item in list(getattr(result, "all_results", None) or []) + [getattr(result, "best_result", None)]:
            value = getattr(item, "text", None) if item is not None else None
            if value:
                texts.append(str(value))
    return texts


def _fuel(context, tag, roi=None, image=None, shot=None):
    """读右上角「当前/上限」燃料并落盘（数字必带图）。读不到只记「未读到」。"""
    image = image if image is not None else _frame(context)
    shot = shot or _snapshot(image, f"燃料_{tag}")
    texts = _ocr_texts(context, roi or FUEL_ROI_HOME, image)
    m = FUEL_RE.search(" ".join(texts))
    if not m:
        # OCR 偶发把「64/240」粘成「64240」（斜杠丢失）；按「当前 1-3 位 + 上限 3 位」兜底
        merged = re.fullmatch(r"(\d{1,3})(\d{3})", re.sub(r"\s", "", " ".join(texts)))
        if merged:
            log.warning("埋点 燃料 %s=%s/%s（斜杠丢失，按拼接兜底）@%s 图=%s",
                        tag, merged.group(1), merged.group(2), _now(), shot)
            value = int(merged.group(1)), int(merged.group(2))
        else:
            log.warning("埋点 燃料 %s 未读到 @%s 图=%s texts=%r", tag, _now(), shot, texts[:5])
            return None
    else:
        log.warning("埋点 燃料 %s=%s/%s @%s 图=%s", tag, m.group(1), m.group(2), _now(), shot)
        value = int(m.group(1)), int(m.group(2))
    _SESSION.setdefault("fuel", {})[tag] = value
    return value


_MCC_ROOT = Path(__file__).resolve().parent.parent
SHOT_DIR = workspace_paths.maa_root(_MCC_ROOT) / "sandbox" / "shots"


def _snapshot(image, tag):
    """证据帧落盘（只看不判；失败不影响主流程）。返回图片路径，失败返回 None。"""
    try:
        import numpy as np
        from PIL import Image

        out = SHOT_DIR / f"{time.strftime('%m%d')}_主线首通"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{time.strftime('%H%M%S')}_{tag}.png"
        Image.fromarray(np.asarray(image)[:, :, ::-1]).save(path)
        log.warning("埋点 截图 %s 图=%s", tag, path)
        return str(path)
    except Exception:  # noqa: BLE001
        log.warning("主线刷取：截图失败 %s", tag, exc_info=True)
        return None


def _fail_shot(context, stage):
    """失败/卡点：先存图再报，返回图片路径。"""
    path = _snapshot(_frame(context), f"失败_{stage}")
    log.error("主线刷取：失败帧 stage=%s 图=%s", stage, path)
    return path


def _settle_arrow_white(image):
    """结算页右下 ▶ 的白色像素数（实测 177；主要用于判断「是否已到可退出态」）。"""
    import numpy as np

    x, y, w, h = SETTLE_ARROW_ROI
    crop = np.asarray(image)[y:y + h, x:x + w]
    white = (crop[:, :, 0] > 200) & (crop[:, :, 1] > 200) & (crop[:, :, 2] > 200)
    return int(white.sum())


def _frame_hash(image):
    """画布帧的感知哈希（16×9 灰度均值哈希）。

    不能用整帧 md5：地图上的粒子/水流动一直在变，整帧永远不相等。
    用均值哈希只保留大结构，滑动到边界后相邻两帧必然相同。
    这里只用 numpy（venv 里没有 cv2），做法是等间隔采样后与全图均值比较。
    """
    import numpy as np

    gray = np.asarray(image, dtype=np.float32).mean(axis=2)
    height, width = gray.shape[:2]
    rows = np.linspace(0, height - 1, 9).astype(int)
    cols = np.linspace(0, width - 1, 16).astype(int)
    small = gray[np.ix_(rows, cols)]
    avg = float(small.mean())
    return "".join("1" if float(v) > avg else "0" for v in small.reshape(-1))


def _mean_brightness(image) -> float:
    """整帧平均亮度；用来识别转场黑屏（黑帧的 hash 恒等，不能参与锚定比较）。

    判据集中在 ui_common.mean_brightness，本函数只做委托。
    """
    return ui_common.mean_brightness(image)


def _frame_flat(image) -> float:
    """采样网格的灰度标准差；转场淡入淡出时整帧接近纯色，std 很小。

    纯色帧的均值哈希恒为全 0（所有采样点都不大于均值），会被误判成「已到边界」，
    所以锚定比较前必须排除扁平帧。判据集中在 ui_common.frame_flatness。
    """
    return ui_common.frame_flatness(image)


def _frame_diff(a, b) -> float:
    """两帧 16×9 采样点的平均灰度差。作为 hash 之外的「画面有没有动」信号：
    hash 相同但 diff 明显 >4 时仍可判为发生位移（小步位移可能不改变 16×9 的 0/1 格局）。
    判据集中在 ui_common.frame_diff。"""
    return ui_common.frame_diff(a, b)


def _ocr_items(context, image):
    """整个视口 OCR，返回全部文本项：[(text, box, score)]，按阅读顺序排序。"""
    try:
        detail = context.run_recognition(
            "主线刷取_区域进度OCR", image,
            pipeline_override={
                "主线刷取_区域进度OCR": {
                    "recognition": "OCR",
                    "roi": [0, 0, 1280, 720],
                    "expected": [],
                    "threshold": 0.2,
                }
            },
        )
    except Exception:  # noqa: BLE001
        log.warning("主线刷取：视口 OCR 失败", exc_info=True)
        return []
    if not detail:
        return []
    items = []
    seen = set()
    for item in list(getattr(detail, "all_results", None) or []) + [getattr(detail, "best_result", None)]:
        if item is None:
            continue
        text = str(getattr(item, "text", "") or "")
        box = getattr(item, "box", None)
        if not text or box is None:
            continue
        b = tuple(int(v) for v in box)
        if b in seen:
            continue
        seen.add(b)
        items.append((text, b, float(getattr(item, "score", 0.0) or 0.0)))
    items.sort(key=lambda it: (it[1][1], it[1][0]))
    return items


def _overlap(box, region):
    """两个 [x,y,w,h] 是否相交。"""
    ax, ay, aw, ah = box
    bx, by, bw, bh = region
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _progress_candidates(items):
    """区域候选：进度未满（cur < total）。

    半开区域（如 3/9）是首通后仍要继续刷的那一个，优先；0/9 多在未解锁区域，
    排在后面。已满（9/9）一律排除，避免回头扫已通关区域。
    """
    found = []
    for it in items:
        m = PROGRESS_CARD_STRICT_RE.fullmatch(it[0])
        if not m:
            continue
        # 右下角固定 UI（「阶段 4/6 · 累计 33/45 · 奖励」）里的「36/45」会被 OCR
        # 切成独立文字而整行匹配成功；点它的「奖励」标签会打开奖励面板、挡住整张画布。
        b = it[1]
        if _overlap(b, FIXED_UI_BOTTOM_RIGHT):
            continue
        cur, total = int(m.group(1)), int(m.group(2))
        if total > 0 and cur < total:
            found.append((it, 0 if cur > 0 else 1))
    found.sort(key=lambda pair: pair[1])
    return [pair[0] for pair in found]


CLIPPED_CARD_EDGE = 140


def _clipped_region_cards(items):
    """贴边且进度读不到的区域卡：返回 [(「地区N」文本, box, 名字标签或 None)]。

    实机 2026-11-09 08:38 失败帧：地区8「核心元库」卡贴在画布左边界外，进度只露出
    星标末位，OCR 读不到 x/y；其余可见区域（地区7 6/6、地区6 9/9、地区4 12/12）都已满，
    于是 12 个视口都找不到「进度未满」候选。
    这类卡片的进度不可读，但名字标签仍在屏幕内：用名字中心作候选点尝试进入，
    是否可刷仍由 _enter_region 的两道门判定，不放宽任何阈值。
    """
    progress_boxes = [it[1] for it in items if PROGRESS_CARD_RE.search(it[0])]
    found = []
    for text, box, _score in items:
        if not re.fullmatch(r"地区\s*\d*", text):
            continue
        x, y, w, h = box
        if (x > CLIPPED_CARD_EDGE and y > CLIPPED_CARD_EDGE
                and x + w < 1280 - CLIPPED_CARD_EDGE
                and y + h < 720 - CLIPPED_CARD_EDGE):
            continue
        card_area = (x - 180, y - 70, 400, 170)
        if any(_overlap(pb, card_area) for pb in progress_boxes):
            continue          # 这张卡的进度可读，交给 _progress_candidates
        label = None
        best_d = None
        for t2, b2, _s2 in items:
            if not t2.strip() or t2 == text:
                continue
            if PROGRESS_CARD_RE.search(t2) or re.fullmatch(r"地区\s*\d*", t2):
                continue
            if re.fullmatch(r"[\d\W_]+", t2):
                continue
            dx = b2[0] + b2[2] / 2 - (x + w / 2)
            dy = b2[1] + b2[3] / 2 - (y + h / 2)
            if not (-200 <= dx <= 200 and -140 <= dy <= 10):
                continue
            d = dx * dx + dy * dy
            if best_d is None or d < best_d:
                best_d = d
                label = (t2, b2)
        found.append((text, box, label))
    return found


def _card_label(items, progress):
    """给一条「★0/x」进度文字找它所属区域的名字标签。

    实机卡片布局：名字标签在进度文字的**右上方**（不是正上方），点名字标签能打开
    章节选择；点进度文字/徽标常常打不开。所以优先用同名卡片里最近的、位于右上方的
    非数字文本作为点击目标。
    """
    px = progress[1][0] + progress[1][2] / 2
    py = progress[1][1] + progress[1][3] / 2
    best = None
    best_d = None
    for text, box, _score in items:
        if PROGRESS_CARD_RE.search(text):
            continue
        if not text.strip() or not re.search(r"\w", text):
            continue
        if re.fullmatch(r"[\d\W_]+", text):      # 纯数字/符号（如 9/9）
            continue
        if re.fullmatch(r"地区\s*\d*\s*", text):  # 「地区N」行不可点，点它没反应
            continue
        cx = box[0] + box[2] / 2
        cy = box[1] + box[3] / 2
        dx = cx - px
        dy = cy - py
        if not (-60 <= dx <= 240 and -140 <= dy <= -10):
            continue
        dist = dx * dx + dy * dy
        if best_d is None or dist < best_d:
            best_d = dist
            best = (text, box)
    return best


def _tab_state(context, image):
    """找「普通/困难」页签并按文字框平均亮度判选中态。

    返回 (selected, brightness表, readable)。readable=False 表示页签缺失或亮度差
    不足 25 —— 调用方必须明确失败，绝不允许盲点页签。
    """
    items = [it for it in _ocr_items(context, image) if it[0].strip() in ("普通", "困难")]
    if len(items) < 2:
        log.error("主线刷取：全屏只读到 %d 个模式页签（需要普通+困难），判读不出", len(items))
        return None, {}, False
    table = {}
    for text, box, _score in items:
        x, y, w, h = box
        table[text.strip()] = ui_common.mean_brightness(image, [x, y, w, h])
    diff = abs(table.get("普通", 0.0) - table.get("困难", 0.0))
    readable = "普通" in table and "困难" in table and diff >= TAB_BRIGHT_DIFF
    selected = max(table, key=table.get) if table else None
    log.warning("主线刷取：页签亮度 普通=%.0f 困难=%.0f 差=%.0f 选中=%s 可读=%s",
                table.get("普通", -1), table.get("困难", -1), diff,
                selected if readable else "读不出", readable)
    return (selected if readable else None), table, readable


def _select_mode(context, param):
    """按配置选模式：先复核实际选中态，已是目标就不点；不是才点，点后复核。

    最多点 MODE_MAX_CLICKS 次，仍不成立即失败（不盲点、不带错误模式继续）。
    """
    target = str(param.get("mode") or TARGET_MODE)
    if target not in ("普通", "困难"):
        log.error("主线刷取：模式配置非法 %r", target)
        return False
    clicks = 0
    for _check in range(MODE_MAX_CLICKS + 1):
        selected, table, readable = _tab_state(context, _frame(context))
        if not readable:
            log.error("主线刷取：页签读不出（亮度差不足 %d 或页签缺失），不盲点，停止",
                      TAB_BRIGHT_DIFF)
            return False
        if selected == target:
            log.warning("主线刷取：当前已是「%s」模式（普通=%.0f/困难=%.0f），不点击",
                        target, table.get("普通", -1), table.get("困难", -1))
            _SESSION["mode"] = target
            return True
        if clicks >= MODE_MAX_CLICKS:
            break
        items = [it for it in _ocr_items(context, _frame(context))
                 if it[0].strip() == target]
        if not items:
            log.error("主线刷取：读不到「%s」页签坐标，停止", target)
            return False
        box = items[0][1]
        cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
        clicks += 1
        log.warning("主线刷取：当前选中=%s，点「%s」页签(%d,%d) 第%d次",
                    selected, target, cx, cy, clicks)
        _click(context, cx, cy)
        time.sleep(MODE_CLICK_DELAY_S)
    log.error("主线刷取：点「%s」页签 %d 次后复核仍不是目标模式，停止", target, clicks)
    return False


def _pan(context, step):
    """按 CANVAS_STEPS[step] 平移一步，返回 (帧, hash, 视口是否有变化)。

    「有变化」用帧感知哈希判断：到边界后画布不再移动，hash 相同 —— 这比
    「滑动次数」可靠，也避免在边界上无限扫。
    同一方向会换一条滑动线再试一次：起点落在区域卡片/浮层上时拖拽会被吞掉，
    hash 不变并不等于真的到边界。
    """
    before = _frame_hash(_frame(context))
    image = _frame(context)
    after = before
    for begin, end in (CANVAS_STEPS[step], PAN_RETRY[step]):
        _swipe(context, begin, end, duration=600)
        time.sleep(0.85)
        image = _frame(context)
        if _mean_brightness(image) < 20 or _frame_flat(image) < 8:
            time.sleep(0.8)
            image = _frame(context)
        after = _frame_hash(image)
        if after != before:
            return image, after, True
    return image, after, False


PAGE_KEYWORDS = ("章节选择", "章节选", "关卡7-", "进度")


def _close_panel(context):
    """收起关卡详情 + 章节面板，回到画布。只在确实还看得到面板时才点，
    避免盲点地图误开别的区域（实机点地图空白会弹未解锁提示）。"""
    items = _ocr_items(context, _frame(context))
    if any(s >= 0.4 and any(k in t for k in ("关卡目标", "扫荡", "开始战斗"))
           for t, _b, s in items):
        _click(context, 760, 540)
        time.sleep(0.9)
        items = _ocr_items(context, _frame(context))
    if any(s >= 0.4 and "章节选择" in t for t, _b, s in items):
        _click(context, 1010, 170)
        time.sleep(0.9)


def _row_locked(row, items):
    """卡面右侧同一行读到「待探索」即判锁定。锁定卡点进去没有挑战按钮，白跑一趟。"""
    box = row["box"]
    cy = box[1] + box[3] // 2
    for text, b, s in items:
        if s < 0.4 or "待探索" not in text:
            continue
        if abs((b[1] + b[3] // 2) - cy) <= 60 and b[0] >= box[0] + box[2] - 20:
            return True
    return False


def _pick_level(context):
    """章节选择页选关：优先带「新」角标的关卡，点卡面后必须弹出挑战按钮才算能刷。

    返回 True 时关卡详情页已打开，后续直接走「关卡详情已到」。
    目标不可进则按列表从下往上（序号最大）逐个回退验证，全失败返回 False。
    """
    items = _ocr_items(context, _frame(context))
    # 「新」角标渲染比列表晚一拍，读不到就再等两拍（实机：入口刚进页时读不到）
    for _ in range(2):
        if any(NEW_BADGE_RE.match(t.strip()) and s >= 0.3 for t, _b, s in items):
            break
        time.sleep(0.8)
        items = _ocr_items(context, _frame(context))
    rows = []
    for text, box, score in items:
        if score < 0.4:
            continue
        story = bool(STORY_LEVEL_RE.search(text))
        if not story and not LEVEL_ROW_RE.search(text):
            continue
        rows.append({"text": text, "box": box, "score": score, "new": False, "story": story})
    if not rows:
        log.error("主线刷取：章节选择页读不到任何主线关卡行（关卡7-x），无法选关")
        return False
    # 锁定卡（「待探索」）直接排除，除非整页都锁
    rows = [r for r in rows if not _row_locked(r, items)] or rows
    progress = next((PROGRESS_RE.search(t) for t, _b, s in items
                     if s >= 0.4 and PROGRESS_RE.search(t)), None)
    if progress:
        _SESSION["progress_before"] = int(progress.group(1))
        log.warning("主线刷取：章节页进度=%s%%（首通后核验增长用）", progress.group(1))
    rows.sort(key=lambda r: r["box"][1])
    badges = [b for t, b, s in items if NEW_BADGE_RE.match(t.strip()) and s >= 0.3]
    for row in rows:
        bx = row["box"][0] + row["box"][2] // 2
        by = row["box"][1] + row["box"][3] // 2
        for box in badges:
            cx = box[0] + box[2] // 2
            cy = box[1] + box[3] // 2
            if (NEW_BADGE_DX[0] <= cx - bx <= NEW_BADGE_DX[1]
                    and NEW_BADGE_DY[0] <= cy - by <= NEW_BADGE_DY[1]):
                row["new"] = True
    new_rows = [r for r in rows if r["new"]]
    if new_rows:
        target = new_rows[0]
        badge_box = badges[0] if badges else None
        log.warning("主线刷取：检测到「新」角标，目标关卡=%r box=%s 角标box=%s",
                    target["text"], target["box"], badge_box)
        candidates = [target] + [r for r in reversed(rows) if not r["new"]]
    else:
        if progress and int(progress.group(1)) >= 100:
            log.warning("主线刷取：本地区进度 100%% 且本屏无「新」角标 —— 已全部通关，放弃该区域")
            return False
        log.warning("主线刷取：本屏读不出「新」角标，退化为按列表最下方（序号最大）逐个验挑战按钮")
        candidates = list(reversed(rows))
    tried = []
    for row in candidates:
        box = row["box"]
        cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
        log.warning("主线刷取：点关卡卡面 %r 中心=(%d,%d)%s",
                    row["text"], cx, cy, "（带新）" if row["new"] else "")
        _click(context, cx, cy)
        time.sleep(1.8)
        page = _ocr_items(context, _frame(context))
        hit = next(((t, b, s) for t, b, s in page
                    if s >= 0.4 and CHALLENGE_RE.search(t)), None)
        if hit:
            first = any(s >= 0.4 and FIRST_CLEAR_RE.search(t) for t, _b, s in page)
            if not row["new"] and not first:
                log.warning("主线刷取：关卡 %r 详情页无「首次通关」——已通关，跳过不反重打（命中 %r）",
                            row["text"], hit[0])
                _back(context)
                time.sleep(1.2)
                tried.append(row["text"])
                continue
            if tried:
                log.warning("主线刷取：目标 %r 锁定，退到 %r（命中挑战按钮 %r box=%s）",
                            tried[0], row["text"], hit[0], hit[1])
            else:
                log.warning("主线刷取：关卡 %r 可进入，命中挑战按钮 %r box=%s",
                            row["text"], hit[0], hit[1])
            _SESSION["level"] = {"text": row["text"], "new": row["new"],
                                 "fallback_from": tried[0] if tried else None}
            return True
        fallback = tried[0] if tried else None
        tried.append(row["text"])
        # 只认剧情关自己的按钮/标签；不能用泛化的「剧情/跳过/继续」——
        # 章节列表里 S7-17 那行本身就带「剧情」二字，会把锁定关误判成剧情关（实机踩过）。
        if row["story"] or any(s >= 0.4 and ("开始播放" in t or "剧情关卡" in t)
                               for t, _b, s in page):
            shot = _snapshot(_frame(context), "剧情关_已点进")
            log.warning("主线刷取：关卡 %r 为剧情关（无挑战按钮），进入剧情流程 图=%s",
                        row["text"], shot)
            _SESSION["level"] = {"text": row["text"], "new": row["new"],
                                 "story": True, "fallback_from": fallback}
            return True
        log.warning("主线刷取：关卡 %r 点卡面后未见挑战/出发按钮，判定锁定或不可刷", row["text"])
    log.error("主线刷取：本地区所有关卡都点不出挑战按钮（试过 %s），判定无可刷关卡", tried)
    return False


def _bright(image, box) -> float:
    """ROI 平均亮度；判据集中在 ui_common.mean_brightness。"""
    return ui_common.mean_brightness(image, box)


def _norm_level(text) -> str:
    return re.sub(r"\s+", "", str(text))


def _skip_story(context, items=None) -> bool:
    """剧情页处理：弹窗「跳过剧情+确认」成对点，否则点「跳过」，再退到「点击继续」。"""
    items = items if items is not None else _ocr_items(context, _frame(context))
    for text, _b, score in items:
        if score >= 0.4 and "跳过剧情" in text:
            confirm = next(((b2, s2) for t2, b2, s2 in items
                            if s2 >= 0.4 and t2.strip() == "确认"), None)
            if confirm:
                _click(context, confirm[0][0] + confirm[0][2] // 2,
                       confirm[0][1] + confirm[0][3] // 2)
                log.warning("主线刷取：首通 剧情弹窗——已点「确认」")
                time.sleep(1.0)
                return True
    for text, box, score in items:
        if score >= 0.4 and text.strip() == "跳过":
            _click(context, box[0] + box[2] // 2, box[1] + box[3] // 2)
            log.warning("主线刷取：首通 剧情——已点「跳过」")
            time.sleep(1.0)
            return True
    for text, _b, score in items:
        if score >= 0.4 and ("点击继续" in text or "点击空白处继续" in text):
            _click(context, *SETTLE_POINT)
            log.warning("主线刷取：首通 剧情——已点「点击继续」")
            time.sleep(1.0)
            return True
    return False


def _wait_battle_hud(context, timeout):
    """等战斗 HUD（波次/操作），期间把剧情跳过；超时返回 None。"""
    end = time.time() + timeout
    while time.time() < end:
        items = _ocr_items(context, _frame(context))
        if any(s >= 0.4 and ("波次" in t or "操作" in t) for t, _b, s in items):
            return items
        _skip_story(context, items)
        time.sleep(1.0)
    return None


def _auto_battle_on(context):
    """自动战斗闸：沿用 combat_autobattle 的琥珀色指标；读不到返回 (None, {})。"""
    try:
        from combat_autobattle import CombatAutoStateRecognition

        metrics = CombatAutoStateRecognition()._amber_metrics(_frame(context))
    except Exception:  # noqa: BLE001
        log.exception("主线刷取：自动战斗状态判定异常")
        return None, {}
    if not isinstance(metrics, dict):
        return bool(metrics), {"raw": repr(metrics)}
    ok = metrics.get("ok")
    if ok is None:
        ok = metrics.get("on")
    if ok is None:
        ok = metrics.get("hit")
    return (bool(ok) if ok is not None else None), metrics


def _first_clear(context) -> bool:
    """首通：点开始战斗→编队页开始行动→跳过剧情→自动战斗闸→看门狗→结算→
    战后剧情→回章节选择，并核验「首次通关消失 / 扫荡变亮 / 地区进度增长」。

    任一环节失败都返回 False（由 pipeline 走 abort，绝不以成功收尾）。
    """
    level = _SESSION.get("level") or {}
    level_text = str(level.get("text") or "")
    before = _SESSION.get("progress_before")
    log.warning("主线刷取：首通分支开始 关卡=%r 战前进度=%s%%", level_text, before)

    _click(context, *FIRST_CLEAR_START)
    end = time.time() + 8
    on_team = False
    while time.time() < end:
        items = _ocr_items(context, _frame(context))
        if any(s >= 0.4 and "开始行动" in t for t, _b, s in items):
            on_team = True
            break
        time.sleep(1.0)
    if not on_team:
        log.error("主线刷取：首通失败 阶段=详情页点开始战斗 —— 8s 内未见编队页「开始行动」")
        _fail_shot(context, "详情页点开始战斗")
        _close_panel(context)
        return False
    log.warning("主线刷取：首通 已到编队页")
    # 埋点：编队页「燃料-XX」是唯一能直接测出单场成本的读数，必须落盘
    team_image = _frame(context)
    team_shot = _snapshot(team_image, "编队页_含燃料XX")
    team_texts = _ocr_texts(context, TEAM_COST_ROI, team_image)
    cost = next((TEAM_COST_RE.search(t) for t in team_texts if TEAM_COST_RE.search(t)), None)
    if cost:
        log.warning("埋点 编队页 %s 显示「燃料-%s」 @%s 图=%s", level_text, cost.group(1), _now(), team_shot)
    else:
        log.warning("埋点 编队页 %s 该页无「燃料-XX」读数 @%s 图=%s texts=%r",
                    level_text, _now(), team_shot, team_texts[:6])
    _fuel(context, "编队页", FUEL_ROI_TEAM, team_image, shot=team_shot)

    log.warning("埋点 开战点击 开始行动 @%s", _now())
    _click(context, *FIRST_CLEAR_TEAM_START)
    time.sleep(1.6)
    gate = _ocr_items(context, _frame(context))
    if any(s >= 0.4 and "芯片仓已满" in t for t, _b, s in gate):
        shot = _fail_shot(context, "芯片仓已满弹窗")
        log.error("主线刷取：首通失败 阶段=编队页开始行动 —— 被「芯片仓已满，是否进行清理？」拦截；"
                  "清芯片需要人工判断（不自动点「确定」删除芯片） 图=%s", shot)
        # 只点「取消」把弹窗收掉（非破坏性），把决定权留给用户
        cancel = next((b for t, b, s in gate if s >= 0.4 and t.strip() == "取消"), None)
        if cancel:
            _click(context, cancel[0] + cancel[2] // 2, cancel[1] + cancel[3] // 2)
            time.sleep(0.8)
        return False
    _snapshot(_frame(context), "开战瞬间")
    if _wait_battle_hud(context, 45) is None:
        log.error("主线刷取：首通失败 阶段=编队页点开始行动 —— 45s 内未出现战斗 HUD")
        _fail_shot(context, "开战未见HUD")
        _close_panel(context)
        return False
    log.warning("主线刷取：首通 已进战斗")
    log.warning("埋点 开战成功 HUD 出现 @%s", _now())

    ok, metrics = _auto_battle_on(context)
    log.warning("主线刷取：首通 自动战斗初查 ok=%s 指标=%s", ok, metrics)
    for attempt in range(2):
        if ok:
            break
        _click(context, *AUTO_BATTLE_POINT)
        time.sleep(0.9)
        ok, metrics = _auto_battle_on(context)
        log.warning("主线刷取：首通 自动战斗纠正第%d次 ok=%s 指标=%s", attempt + 1, ok, metrics)
    if not ok:
        log.error("主线刷取：首通失败 阶段=自动战斗闸 —— 两次纠正后仍未开启")
        _fail_shot(context, "自动战斗闸")
        return False
    log.warning("主线刷取：首通 自动战斗闸 PASS")

    end = time.time() + BATTLE_TIMEOUT_S
    result = None
    while time.time() < end:
        items = _ocr_items(context, _frame(context))
        text = " ".join(t for t, _b, s in items if s >= 0.4)
        if BATTLE_VICTORY_RE.search(text):
            result = "胜利"
            break
        if BATTLE_DEFEAT_RE.search(text):
            result = "失败"
            break
        if _skip_story(context, items):
            continue
        time.sleep(3)
    if result is None:
        log.error("主线刷取：首通失败 阶段=战斗看门狗 —— %ds 内未出结算", BATTLE_TIMEOUT_S)
        _fail_shot(context, "战斗超时未结算")
        return False
    log.warning("主线刷取：首通 战斗结束 result=%s", result)
    settle_shot = _snapshot(_frame(context), "结算页")
    log.warning("埋点 结算 result=%s @%s 图=%s", result, _now(), settle_shot)
    if result != "胜利":
        log.error("主线刷取：首通失败 阶段=战斗 —— 结果为失败")
        return False

    # 结算页是多段动画：星级展示 → 经验条滚动 → 奖励翻牌 → 右下 ▶ 出现代表可退出。
    # 实机实测（P-036）：▶ 在 [1237,595]-[1249,620]（13×26 白色三角），**点击无反应**
    # （是装饰），所以 ▶ **只当「可退态」信号，不作点击目标**；退出动作是 Android BACK，
    # 是否换页只看页面关键词（章节选择），不用固定等待。
    log.warning("埋点 结算页 ▶ 仅作可退态信号；点击无反应为实机事实（判定为装饰），退出用 BACK @%s",
                _now())
    settle_end = time.time() + 60
    arrow_seen = False
    back = False
    exit_shot = None
    while time.time() < settle_end:
        image = _frame(context)
        items = _ocr_items(context, image)
        if any(s >= 0.4 and "章节选择" in t for t, _b, s in items):
            back = True
            break
        if _skip_story(context, items):
            continue
        if not arrow_seen and _settle_arrow_white(image) >= SETTLE_ARROW_MIN_WHITE:
            arrow_seen = True
            exit_shot = _snapshot(image, "结算_▶可退态")
            log.warning("埋点 结算页 ▶ 已出现（可退态）@%s → 按 BACK 图=%s", _now(), exit_shot)
            _back(context)
            time.sleep(1.2)
            continue
        time.sleep(1.2)
    if not back:
        shot = _fail_shot(context, "未回章节选择")
        still = _ocr_items(context, _frame(context))
        on_settle = any(s >= 0.4 and "战斗胜利" in t for t, _b, s in still)
        if not arrow_seen:
            reason = "箭头未出现（60s 内没等到可退态）"
        elif on_settle:
            reason = "BACK 后页面未切换（仍在结算页）"
        else:
            reason = "BACK 后页面未切换到章节选择"
        log.error("主线刷取：首通失败 阶段=结算页退出 —— %s 图=%s", reason, shot)
        return False
    log.warning("埋点 回章节选择 @%s", _now())

    chapter_image = _frame(context)
    chapter_shot = _snapshot(chapter_image, "回章节_地区进度")
    items = _ocr_items(context, chapter_image)
    m = next((PROGRESS_RE.search(t) for t, _b, s in items
              if s >= 0.4 and PROGRESS_RE.search(t)), None)
    after = int(m.group(1)) if m else None
    progress_ok = before is not None and after is not None and after > int(before)
    log.warning("主线刷取：首通核验 进度 前=%s%% 后=%s%% 通过=%s 图=%s",
                before, after, progress_ok, chapter_shot)

    target = _norm_level(level_text).replace("关卡", "")
    row = next(((t, b, s) for t, b, s in items
                if s >= 0.4 and LEVEL_ROW_RE.search(t)
                and _norm_level(t).startswith("关卡" + target)), None)
    if row is None:
        # 本关若是当前地区最后一关，通关后游戏会自动切到下一地区，
        # 章节列表只剩下一地区的新关卡（实机 7-19 → 地区8 核心元库 7-20）。
        # 这时旧关卡详情已不可达，改用「下一关带『新』」作为进度证据，
        # 首次通关消失/扫荡亮度明确记「无证据」，不硬造假读数。
        next_text = _next_normal_level(level_text)
        nrow = next(((t, b, s) for t, b, s in items
                     if s >= 0.4 and LEVEL_ROW_RE.search(t) and next_text
                     and _norm_level(t).startswith(next_text)), None)
        if nrow is not None and _badge_near(items, nrow[1]):
            log.warning("主线刷取：首通核验 进度=地区推进（%r 已不在当前章节列表；下一关 %r 带「新」）"
                        "首次通关消失/扫荡亮度=无证据（跨区后旧关卡详情不可达） 图=%s",
                        level_text, next_text, chapter_shot)
            return True
        log.error("主线刷取：首通失败 阶段=核验 —— 回章节页后读不到 %r 的关卡行（现有=%s） 图=%s",
                  level_text, [t for t, _b, s in items if LEVEL_ROW_RE.search(t)][:6], chapter_shot)
        return False
    box = row[1]
    _click(context, box[0] + box[2] // 2, box[1] + box[3] // 2)
    time.sleep(1.8)
    detail_image = _frame(context)
    detail_items = _ocr_items(context, detail_image)
    detail_shot = _snapshot(detail_image, "战后详情_三项核验")
    still_first = any(s >= 0.4 and FIRST_CLEAR_RE.search(t) for t, _b, s in detail_items)
    sweep = next(((b, s) for t, b, s in detail_items
                  if s >= 0.4 and t.strip() == "扫荡"), None)
    sweep_bright = _bright(detail_image, sweep[0]) if sweep else None
    sweep_ok = sweep is not None and sweep_bright >= SWEEP_ENABLED_BRIGHT
    log.warning("主线刷取：首通核验 首次通关仍在=%s 扫荡box=%s 亮度=%s 阈值=%d 通过=%s 图=%s",
                still_first, sweep[0] if sweep else None,
                "-" if sweep_bright is None else f"{sweep_bright:.0f}",
                SWEEP_ENABLED_BRIGHT, sweep_ok, detail_shot)
    _close_panel(context)
    if still_first or not sweep_ok or not progress_ok:
        log.error("主线刷取：首通失败 阶段=战后三项核验 —— 标记消失=%s 扫荡亮=%s 进度增长=%s",
                  not still_first, sweep_ok, progress_ok)
        return False
    log.warning("主线刷取：首通成功 关卡=%r 三项核验全部通过", level_text)
    return True


def _next_level_text(level_text):
    """由剧情关文本推出下一关（关卡S7-17 → 关卡7-18）；解析不了返回 None。"""
    m = STORY_LEVEL_RE.search(level_text or "")
    if not m:
        return None
    return f"关卡{m.group(1)}-{int(m.group(2)) + 1}"


def _next_normal_level(level_text):
    """由普通关卡文本推出下一关（关卡7-19 → 关卡7-20）；解析不了返回 None。"""
    m = re.search(r"关卡\s*(\d+)\s*[-－—]\s*(\d+)", level_text or "")
    if not m:
        return None
    return f"关卡{m.group(1)}-{int(m.group(2)) + 1}"


def _badge_near(items, box):
    """「新」角标是否落在指定卡面的右上方（相对卡面中心，非绝对坐标）。"""
    cx = box[0] + box[2] // 2
    cy = box[1] + box[3] // 2
    return any(NEW_BADGE_RE.match(t.strip()) and s >= 0.3
               and NEW_BADGE_DX[0] <= (b[0] + b[2] // 2) - cx <= NEW_BADGE_DX[1]
               and NEW_BADGE_DY[0] <= (b[1] + b[3] // 2) - cy <= NEW_BADGE_DY[1]
               for t, b, s in items)


def _story_clear(context):
    """剧情关（关卡S7-x）：只播剧情、不消耗燃料；成功判据 = 下一关解锁。

    流程：点「开始播放」→ 剧情「跳过/点击继续」循环（含「确认」弹窗）→ 回章节选择。
    「回章节选择」必须等**真的播过**（started+advanced）才认：关卡详情页左侧也会显示
    「章节选择」字样，不加这道门会一开始就误判已回章节。
    解锁验证是只读二次确认：回章节页后点下一关卡面，连读两次看能否弹「开始战斗」，
    验完立刻 BACK 回章节页，不进入战斗。
    """
    level = dict(_SESSION.get("level") or {})
    level_text = level.get("text", "剧情关")
    target = _next_level_text(level_text)
    log.warning("主线刷取：剧情关开始 %r，下一关=%r；未扣燃料（剧情关）", level_text, target)

    end = time.time() + 120
    started = advanced = back = False
    play_shot = None
    while time.time() < end:
        image = _frame(context)
        items = _ocr_items(context, image)
        if _skip_story(context, items):
            advanced = True
            continue
        if not started:
            play = next((b for t, b, s in items
                         if s >= 0.4 and ("开始播放" in t or "剧情关卡" in t)), None)
            if play:
                _click(context, play[0] + play[2] // 2, play[1] + play[3] // 2)
                started = True
                play_shot = _snapshot(image, "剧情关_开始播放")
                log.warning("埋点 剧情关 已点「开始播放」@%s 图=%s", _now(), play_shot)
                time.sleep(1.5)
                continue
        if started and advanced and any(s >= 0.4 and "章节选择" in t for t, _b, s in items):
            back = True
            break
        # 兜底：剧情常见「点击空白处继续」，点屏幕中部推进
        _click(context, *SETTLE_POINT)
        time.sleep(1.2)
    if not back:
        shot = _fail_shot(context, "剧情关未回章节")
        log.error("主线刷取：剧情关失败 —— 120s 内未回到章节选择（开始播放=%s 剧情推进=%s） 图=%s",
                  started, advanced, shot)
        return False
    chapter_shot = _snapshot(_frame(context), "剧情关_回章节")
    log.warning("埋点 剧情关 已回章节选择 @%s 图=%s", _now(), chapter_shot)

    if not target:
        log.warning("主线刷取：剧情关 %r 推不出下一关号，解锁判据=无证据", level_text)
        return False
    items = _ocr_items(context, _frame(context))
    row = next(((t, b, s) for t, b, s in items
                if s >= 0.4 and LEVEL_ROW_RE.search(t)
                and _norm_level(t).startswith(target)), None)
    if row is None:
        shot = _fail_shot(context, "剧情关_读不到下一关")
        log.error("主线刷取：剧情关核验失败 —— 回章节后读不到 %r（现有=%s），解锁判据=无证据 图=%s",
                  target, [t for t, _b, s in items if LEVEL_ROW_RE.search(t)][:6], shot)
        return False
    # 「新」是否已位移到下一关（辅助证据，不参与判定）
    badges = [b for t, b, s in items if NEW_BADGE_RE.match(t.strip()) and s >= 0.3]
    box = row[1]
    rx, ry = box[0] + box[2] // 2, box[1] + box[3] // 2
    new_moved = any(NEW_BADGE_DX[0] <= (bx2 := b[0] + b[2] // 2) - rx <= NEW_BADGE_DX[1]
                    and NEW_BADGE_DY[0] <= (b[1] + b[3] // 2) - ry <= NEW_BADGE_DY[1]
                    for b in badges)
    log.warning("主线刷取：剧情关 下一关 %r 卡面 box=%s；「新」已位移=%s（本章「新」数量=%d）",
                target, box, new_moved, len(badges))
    _click(context, rx, ry)
    hits, shots = [], []
    for i in (1, 2):
        time.sleep(1.8)
        page_items = _ocr_items(context, _frame(context))
        hit = next((t for t, _b, s in page_items if s >= 0.4 and CHALLENGE_RE.search(t)), None)
        hits.append(hit)
        shots.append(_snapshot(_frame(context), f"剧情关_下一关解锁核验_{i}"))
        log.warning("主线刷取：剧情关 解锁核验第%d次 命中挑战=%r 图=%s", i, hit, shots[-1])
    _back(context)
    time.sleep(1.2)
    if hits[0] and hits[1]:
        log.warning("主线刷取：剧情关成功 —— %r 已解锁（两次均弹 %r）「新」位移=%s 图=%s",
                    target, hits[0], new_moved, shots[-1])
        return True
    log.error("主线刷取：剧情关核验失败 —— %r 两次挑战按钮=%s（「新」位移=%s） 图=%s",
              target, hits, new_moved, shots[-1])
    return False


def _ensure_home(context, seconds):
    """确认真的在主界面（基地/任务/编队），避免拿过场帧当读数依据。"""
    end = time.time() + seconds
    while time.time() < end:
        items = _ocr_items(context, _frame(context))
        if any(s >= 0.4 and any(k in t for k in ("基地", "任务", "编队"))
               for t, _b, s in items):
            return True
        time.sleep(0.8)
    return False


def _enter_region(context, x, y):
    """两级可进入校验：① 点候选能到「章节选择」页；② 再点关卡能弹「挑战/出发」。

    第一级只认页面文字（章节选择 / 关卡7-x / 进度），不猜固定 ROI；
    第二级由 _pick_level 负责（只认挑战/出发/开始战斗，排除剧情关的「开始播放」）。
    任一级不过就收起面板返回 False，由上层换下一个区域候选。
    """
    _click(context, x, y)
    time.sleep(1.7)
    items = _ocr_items(context, _frame(context))
    hit = next(((t, b, s) for t, b, s in items
                if any(k in t for k in PAGE_KEYWORDS)), None)
    if hit is None:
        log.warning("主线刷取：点(%d,%d)后全屏未见页面关键词(章节选择/关卡7-x/进度)", x, y)
        return False
    log.warning("主线刷取：第一道门通过 —— 进页判据命中 文本=%r box=%s 置信=%.3f",
                hit[0], hit[1], hit[2])
    if _pick_level(context):
        log.warning("主线刷取：第二道门通过 —— 关卡已可挑战，详情页已打开")
        return True
    log.warning("主线刷取：第二道门未通过（本地区没有能弹挑战按钮的关卡），收起面板换候选")
    _close_panel(context)
    return False


_CASES_CACHE: dict = {}


def option_cases(option_name):
    """取某个 select 选项的 case 名列表 —— MFA 存的是 index，要用它换算成名字。"""
    if option_name in _CASES_CACHE:
        return _CASES_CACHE[option_name]
    names = []
    root = Path(__file__).resolve().parent.parent
    for candidate in (root / "interface.json", root / "assets" / "interface.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        names = [c.get("name")
                 for c in data.get("option", {}).get(option_name, {}).get("cases", [])]
        if names:
            break
    if not names:
        log.error("主线刷取：拿不到选项 %s 的 case 列表，将按默认处理", option_name)
    _CASES_CACHE[option_name] = names
    return names


def option_case(item, option_name):
    """select 的值是 index；换算成 case 名。取不到就返回 None。"""
    cases = option_cases(option_name)
    idx = item.get("index")
    if isinstance(idx, int) and 0 <= idx < len(cases):
        return cases[idx]
    # 兼容：某些版本可能直接存名字
    for k in ("selected", "value", "case"):
        v = item.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            return str(v)
    return None


def _walk_options(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        yield from _walk_options(item.get("sub_options") or item.get("option") or [])


def _load_settings():
    """从实例配置读「体力消耗方式」与「自定次数」。

    MFA 把用户填的值放在 item["data"][输入名]，不是 item["input"]（见每日探索的坑）。
    """
    try:
        config = json.loads(instance_config_path().read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        log.error("主线刷取：读取实例配置失败 %s", exc)
        return None

    task = next(
        (item for item in config.get("TaskItems", []) if item.get("name") == TASK_NAME),
        None,
    )
    if task is None:
        log.warning("主线刷取：实例配置里没有该任务，按默认「消耗完体力」处理")
        return {"mode": MODE_CONSUME_ALL, "runs": 1}

    mode = MODE_CONSUME_ALL
    runs = 1
    potion_consume, potion_custom = "不使用", "不使用"
    potion_type, potion_num = "小药", 1
    for item in _walk_options(task.get("option", [])):
        name = item.get("name")
        if name in (POTION_OPTION_CONSUME, POTION_OPTION_CUSTOM):
            value = option_case(item, name)
            if value:
                if name == POTION_OPTION_CONSUME:
                    potion_consume = str(value)
                else:
                    potion_custom = str(value)
        elif name == POTION_TYPE_OPTION:
            value = option_case(item, name)
            if value:
                potion_type = str(value)
        elif name == POTION_NUM_OPTION:
            data = item.get("data") or {}
            raw = data.get("体力药数量")
            if raw is None and data:
                raw = next(iter(data.values()))
            try:
                potion_num = max(1, int(str(raw).strip()))
            except (TypeError, ValueError):
                potion_num = 1
        elif name == MODE_OPTION:
            value = option_case(item, name)
            if value:
                mode = str(value)
        elif name == RUNS_OPTION:
            data = item.get("data") or {}
            raw = data.get(RUNS_INPUT)
            if raw is None and data:
                raw = next(iter(data.values()))
            try:
                runs = max(COUNT_MIN, int(str(raw).strip()))
            except (TypeError, ValueError):
                runs = 1
    # 消耗完体力 → 读「-消耗完」那支；指定次数 → 读「-自定」那支
    potion = potion_consume if mode == MODE_CONSUME_ALL else potion_custom
    log.warning("主线刷取：读到设置 模式=%s 次数=%d 体力药=%s/%s×%d",
                mode, runs, potion, potion_type, potion_num)
    return {"mode": mode, "runs": runs, "potion": potion,
            "potion_type": potion_type, "potion_num": potion_num}


def plan_batch(mode: str, remaining: int, stamina: int) -> int:
    """本批该扫几次；返回 0 表示没有下一批、正常结束。

    消耗完体力：按当前体力算，一批最多 10 次；不够一次就结束（返回 0）。
    指定次数：  按剩余待扫次数算；体力不足时不裁剪 —— 让「开始战斗」跳到
                燃料选择页去补，这正是用户要的行为。
    """
    if mode == MODE_CONSUME_ALL:
        return min(stamina // COST_PER_RUN, COUNT_MAX)
    want = min(int(remaining), COUNT_MAX)
    return want if want > 0 else 0


class MainStoryAction(CustomAction):
    def run(self, context, argv) -> bool:
        param = _param(getattr(argv, "custom_action_param", None))
        operation = str(param.get("operation", ""))
        try:
            if operation == "init":
                _SESSION.clear()
                _SESSION["remaining"] = None
                _SESSION["stamina_ok"] = False
                _SESSION["potion_used"] = 0
                _SESSION["forced_target"] = None
                _SESSION["stamina"] = None
                log.warning("主线刷取：本轮状态已重置")
                return True
            if operation == "read_stamina":
                return self._read_stamina(context)
            if operation == "set_count":
                return self._set_count(context, param)
            if operation == "prepare":
                return self._prepare(context)
            if operation == "set_potion_count":
                return self._set_potion_count(context)
            if operation == "batch_done":
                return self._batch_done(context)
            if operation == "anchor_pick":
                return self._anchor_pick(context)
            if operation == "select_mode":
                return _select_mode(context, param)
            if operation == "first_clear":
                return _first_clear(context)
            if operation == "log_fuel":
                tag = str(param.get("tag", "未标注"))
                if not _ensure_home(context, 4.0):
                    log.warning("埋点 燃料 %s 无证据（4s 内未确认主界面，未截过场帧冒充）@%s",
                                tag, _now())
                    return True
                _fuel(context, tag)
                return True
            if operation == "story_clear":
                return _story_clear(context)
            if operation == "abort":
                log.error("主线刷取：主动中止 —— %s", param.get("reason", "未写明原因"))
                return False
            if operation == "finish":
                return self.finish(context)
            log.error("主线刷取：未知操作 %s", operation)
            return False
        except Exception:  # noqa: BLE001
            log.exception("主线刷取：操作失败 %s", operation)
            return False

    def _read_stamina(self, context) -> bool:
        """读当前体力。读不到、读到 0、或两次不一致都算失败并重试。"""
        last = None
        for attempt in range(3):
            image = context.tasker.controller.post_screencap().wait().get()
            first = _ocr_digits(context, "主线刷取_读取体力", image, ROI_STAMINA_NOW)
            time.sleep(0.4)
            image = context.tasker.controller.post_screencap().wait().get()
            second = _ocr_digits(context, "主线刷取_读取体力", image, ROI_STAMINA_NOW)
            last = (first, second)
            # 0 是合法体力（刚好用完），只排除读不到与超出上限的值
            ok = (first is not None and first == second and 0 <= int(first) <= 999)
            log.warning("主线刷取：读体力 第%d次 -> %s / %s %s",
                        attempt + 1, first, second, "OK" if ok else "丢弃")
            if ok:
                _SESSION["stamina"] = int(first)
                _SESSION["stamina_ok"] = True
                log.warning("主线刷取：当前体力稳定读取=%d（单次消耗 %d）", first, COST_PER_RUN)
                return True
            time.sleep(0.6)
        _SESSION["stamina_ok"] = False
        _SESSION["stamina"] = None
        log.error("主线刷取：连续 3 次读不到体力（最后 %s），转为不补药继续", last)
        # 仍返回 True：检查器不允许 next 与 on_error 指向同一节点。
        # stamina_ok 已是 False，后续 prepare 会跳过补药。
        return True

    def _set_count(self, context, param) -> bool:
        settings = _load_settings()
        if settings is None:
            return False
        if not _SESSION.get("stamina_ok") and settings["mode"] == MODE_CONSUME_ALL:
            log.error("主线刷取：消耗完体力模式但体力读取失败，无法算次数")
            return False
        stamina = int(_SESSION.get("stamina") or 0)        # 0 合法
        mode, requested = settings["mode"], settings["runs"]
        if param.get("runs") is not None:
            try:
                mode, requested = "指定次数", int(param["runs"])
            except (TypeError, ValueError):
                pass
        raw_remaining = _SESSION.get("remaining")
        remaining = requested if raw_remaining is None else int(raw_remaining)
        forced = _SESSION.get("forced_target")
        if forced is not None:      # 0 是"正常结束"的信号，不能用真假判断
            # 补药后回到队伍页时，次数往往被游戏重置；这里设回本轮算好的目标
            target = int(forced)
            log.warning("主线刷取：沿用本轮固定次数目标=%d（补药后重设）", target)
        else:
            target = plan_batch(mode, remaining, stamina)
        _SESSION["mode"] = mode
        if target <= 0:
            log.info(
                "主线刷取：没有下一批（模式=%s 剩余=%d 体力=%d 单次=%d），正常结束",
                mode, remaining, stamina, COST_PER_RUN,
            )
            _SESSION["target_runs"] = 0
            return False
        _SESSION["target_runs"] = target
        log.warning("主线刷取：模式=%s 体力=%d 剩余=%d → 本批次数=%d", mode, stamina, remaining, target)

        for attempt in range(VERIFY_ATTEMPTS):
            image = context.tasker.controller.post_screencap().wait().get()
            now = _ocr_digits(context, "主线刷取_读现在", image, ROI_STAMINA_NOW)
            after = _ocr_digits(context, "主线刷取_读扫荡后", image, ROI_STAMINA_AFTER)
            if now is None or after is None:
                log.warning("主线刷取：第%d次未读到体力/扫荡后", attempt + 1)
                time.sleep(0.5)
                continue
            _SESSION["last_now"], _SESSION["last_after"] = now, after
            # 体力不足时「扫荡后」是负数（例如 2 - 1*24 = -22），而 OCR 经常把减号丢掉。
            # 所以两种解释都试：
            #   减号保留 -> |现在 - 扫荡后|
            #   减号丢失 ->  现在 + 扫荡后
            # 取那个能整除 24、且次数落在 1..10 的解释。
            delta, current = None, None
            best_err = 99
            for cand in (abs(now - after), abs(now + after)):
                c = int(round(cand / COST_PER_RUN))
                err = abs(cand - c * COST_PER_RUN)
                if COUNT_MIN <= c <= COUNT_MAX and err <= 1 and err < best_err:
                    delta, current, best_err = cand, c, err
            if current is None:
                log.warning("主线刷取：次数反推不合理（现在=%d 扫荡后=%d），重试", now, after)
                time.sleep(0.5)
                continue
            if current == target and abs(delta - target * COST_PER_RUN) <= 1:
                log.warning("主线刷取：次数复核成功 次数=%d（现在=%d 扫荡后=%d）", current, now, after)
                # 本批跑完还剩多少：现在 - 本批次数*24。够再一次才需要下一批，
                # 这样就不必再走一遍「扫荡→队伍页」去做二次确认。
                left = now - target * COST_PER_RUN
                if mode == MODE_CONSUME_ALL:
                    _SESSION["more"] = left >= COST_PER_RUN
                else:
                    _SESSION["more"] = (remaining - target) > 0        # 本批之后还剩几次
                log.warning(
                    "主线刷取：本批 %d 次，扫荡后剩余=%d，%s",
                    target, left, "还有下一批" if _SESSION["more"] else "已榨干，不再开扫荡",
                )
                return True
            point = PLUS_POINT if current < target else MINUS_POINT
            steps = abs(current - target)
            log.warning("主线刷取：次数校正 目标=%d 当前=%d 点击%d次（现在=%d 扫荡后=%d 差=%d）",
                        target, current, steps, now, after, delta)
            for _ in range(steps):
                _click(context, *point)
                time.sleep(CLICK_DELAY)
        log.error("主线刷取：次数未到目标 %d（最后读到 现在=%s 扫荡后=%s），停止以免扫错次数",
                      target, _SESSION.get("last_now"), _SESSION.get("last_after"))
        return False

    def _batch_done(self, context) -> bool:
        """一批**真正扫完**（回到关卡详情页）后才调用：这时才扣减剩余次数。

        不能放在「设次数成功」时扣 —— 补药会让流程回到队伍页再设一次，
        那时若已经扣过，就会把补药插曲误判成「本批已完成」而直接收尾。
        """
        target = int(_SESSION.get("target_runs") or 0)
        if _SESSION.get("mode") != MODE_CONSUME_ALL and target > 0:
            raw = _SESSION.get("remaining")
            if raw is not None:
                _SESSION["remaining"] = max(0, int(raw) - target)
        if _SESSION.get("potion_pending"):
            _SESSION["potion_used"] = int(_SESSION.get("potion_used") or 0) + int(
                _SESSION.get("potion_need") or 0)
            _SESSION["potion_pending"] = False
        _SESSION["forced_target"] = None
        log.warning("主线刷取：本批记账 本批=%d 剩余=%s", target, _SESSION.get("remaining"))
        return True

    def _prepare(self, context) -> bool:
        """按每日探索的语义：**正常一律返回 True**，由「待使用体力药」的状态识别
        决定要不要进补药流程。

        只有真错误才返回 False（读不到设置 / 一次超过 50 瓶 / 连续补药 3 轮仍不够）——
        因为「不需要补药」用 False 表达会误踩 on_error 那条边。
        """
        settings = _load_settings()
        if settings is None:
            log.error("主线刷取：读不到任务设置，停止")
            return False
        _SESSION["potion_pending"] = False
        _SESSION["potion_rounds"] = 0

        if settings["potion"] != "使用":
            log.warning("主线刷取：未启用体力药，直接进入设次数")
            return True

        # 消耗完体力 + 体力药：
        #   最终体力 = 当前体力 + 瓶数 × 单瓶恢复量
        #   次数     = min(最终体力 ÷ 24, 10)
        # 设成这个次数后体力必然不足 → 点开始战斗就会打开体力选择页。
        if settings["mode"] == MODE_CONSUME_ALL:
            # 本批目标已经定过（含刚补完药回来的那轮）→ 不再重算、不再补药，
            # 否则每轮都会把药效再加一次，永远"还差 2 瓶"，形成死循环。
            if _SESSION.get("forced_target") is not None:
                log.warning("主线刷取：本批目标已定=%s（已补过药），不再重复补药",
                            _SESSION.get("forced_target"))
                _SESSION["potion_pending"] = False
                _SESSION["potion_need"] = 0
                return True

            recover = POTION_RECOVERY.get(settings["potion_type"], 10)
            stamina = int(_SESSION.get("stamina") or 0) if _SESSION.get("stamina_ok") else 0
            final = stamina + settings["potion_num"] * recover
            if final < COST_PER_RUN:
                # 休止条件：用完这些药连一次都刷不了 → 不补药，正常结束
                log.warning(
                    "主线刷取：当前体力=%d + %d瓶×%d = 最终体力=%d，连一次(%d)都刷不了 → "
                    "不补药，正常结束任务",
                    stamina, settings["potion_num"], recover, final, COST_PER_RUN)
                _SESSION["forced_target"] = 0
                _SESSION["potion_pending"] = False
                _SESSION["potion_need"] = 0
                return True
            target = max(COUNT_MIN, min(final // COST_PER_RUN, COUNT_MAX))
            _SESSION["forced_target"] = target
            used = int(_SESSION.get("potion_used") or 0)
            _SESSION["potion_need"] = max(0, min(settings["potion_num"] - used, POTION_USE_LIMIT))
            _SESSION["potion_type"] = settings["potion_type"]
            _SESSION["potion_pending"] = _SESSION["potion_need"] > 0
            log.warning(
                "主线刷取：消耗完体力+体力药 当前体力=%d + %d瓶×%d = 最终体力=%d → 次数=%d；"
                "本批用药 %d 瓶（已用 %d / 共 %d）",
                stamina, settings["potion_num"], recover, final, target,
                _SESSION["potion_need"], used, settings["potion_num"])
            return True

        if not _SESSION.get("stamina_ok"):
            log.warning("主线刷取：体力读取失败，跳过补药，直接按指定次数进行")
            return True
        stamina = int(_SESSION.get("stamina") or 0)
        raw_remaining = _SESSION.get("remaining")
        remaining = settings["runs"] if raw_remaining is None else int(raw_remaining)
        _SESSION["remaining"] = remaining
        need = remaining * COST_PER_RUN - stamina
        if need <= 0:
            log.warning("主线刷取：体力 %d 已够 %d 次（需 %d），无需补药",
                        stamina, remaining, remaining * COST_PER_RUN)
            return True

        recover = POTION_RECOVERY.get(settings["potion_type"], 10)
        count = max(1, math.ceil(need / recover))
        if count > POTION_USE_LIMIT:
            log.error("主线刷取：一次要 %d 瓶，超过单次上限 %d，停止", count, POTION_USE_LIMIT)
            return False
        if int(_SESSION.get("potion_rounds") or 0) >= 3:
            log.error("主线刷取：已连续补药 3 轮仍不够，停止以免无限补药")
            return False

        _SESSION["potion_rounds"] = int(_SESSION.get("potion_rounds") or 0) + 1
        _SESSION["potion_need"] = count
        _SESSION["potion_type"] = settings["potion_type"]
        _SESSION["potion_pending"] = True
        log.warning("主线刷取：缺 %d 体力 → 需用 %s %d 瓶", need, settings["potion_type"], count)
        return True

    def _set_potion_count(self, context) -> bool:
        target = int(_SESSION.get("potion_need") or 0)
        if target <= 0:
            log.error("主线刷取：没有待用的体力药数量")
            return False
        for attempt in range(VERIFY_ATTEMPTS):
            image = context.tasker.controller.post_screencap().wait().get()
            current = _ocr_digits(context, "主线刷取_读药量", image, ROI_POTION_COUNT)
            if current == target:
                log.warning("主线刷取：体力药数量复核成功 %d", current)
                return True
            if current is None:
                log.warning("主线刷取：第%d次未读到药量", attempt + 1)
                time.sleep(0.5)
                continue
            point = POTION_PLUS_POINT if current < target else POTION_MINUS_POINT
            steps = abs(current - target)
            log.warning("主线刷取：药量校正 目标=%d 当前=%d 点击%d次", target, current, steps)
            for _ in range(steps):
                _click(context, *point)
                time.sleep(CLICK_DELAY)
        log.error("主线刷取：体力药数量未到目标 %d", target)
        return False

    def _anchor_pick(self, context) -> bool:
        """锚定画布左上边界 → 视口 OCR 找 0/x 区域 → 点进去。

        画布会记忆上次平移位置，固定 ROI 只在某一个偏移上成立，所以必须先把
        起点变成「确定角落」，再逐个视口找目标，而不是假定目标在哪。
        """
        started = time.monotonic()

        # 进图时可能还挂着上一次的「章节选择」浮层，先关掉，免得吞掉滑动
        _close_panel(context)
        time.sleep(0.8)
        # 地图会记忆上次平移位置，且进图通常以「当前区域」居中：先在当前视口扫一次。
        # 命中就直接进去（锚定+全画布扫描慢，且平移手势可能被吞导致漏扫，实机已踩）。
        if self._scan_viewport(context, 0):
            return True

        # 等地图真正就绪再取基准帧：转场黑屏/淡入的帧接近纯色，它的均值哈希恒为全 0，
        # 会把锚定假判成「第一滑就稳定」。所以要求「不黑 + 有纹理 + 能看到困难页签」。
        ready = False
        for _ in range(8):
            image = _frame(context)
            if _mean_brightness(image) >= 20 and _frame_flat(image) >= 8:
                try:
                    probe = context.run_recognition(
                        "主线刷取_就绪检查", image,
                        pipeline_override={
                            "主线刷取_就绪检查": {
                                "recognition": "OCR",
                                "roi": list(REGION_TAB_ROI),
                                "expected": ["普通", "困难"],
                                "threshold": 0.2,
                            }
                        },
                    )
                except Exception:  # noqa: BLE001
                    probe = None
                if probe and probe.hit:
                    ready = True
                    break
            time.sleep(0.8)
        if not ready:
            log.warning("主线刷取：等地图就绪超时（画面仍黑/无纹理/读不到模式页签），继续尝试锚定")

        def _settled_frame():
            frame = _frame(context)
            if _mean_brightness(frame) < 20 or _frame_flat(frame) < 8:
                # 黑屏/淡入淡出帧接近纯色，hash 恒为全 0，不能拿来判「滑不动」
                time.sleep(0.8)
                frame = _frame(context)
            return frame

        def _pull(corner, retry, name):
            """单方向连续拖拽直到帧 hash 不再变化；返回 (步数, 连续相同帧, 最后hash)。"""
            prev = _frame_hash(_settled_frame())
            step = 0
            same_run = 1
            while step < ANCHOR_MAX_SWIPES:
                _swipe(context, corner[0], corner[1], duration=ANCHOR_SWIPE_MS)
                step += 1
                time.sleep(ANCHOR_SETTLE_S)
                cur = _frame_hash(_settled_frame())
                if cur == prev:
                    # 「没变化」不能直接当成到边界：起点落在卡片/浮层上时拖拽会被吞掉，
                    # 换一条滑动线再确认一次。
                    _swipe(context, retry[0], retry[1], duration=ANCHOR_SWIPE_MS)
                    time.sleep(ANCHOR_SETTLE_S)
                    cur = _frame_hash(_settled_frame())
                    if cur == prev:
                        same_run += 1
                        log.warning("主线刷取：%s第%d步 hash=%s(ones=%d) 未变化（已复核）",
                                    name, step, cur, cur.count("1"))
                        if same_run >= ANCHOR_STABLE_FRAMES:
                            return step, same_run, cur
                        prev = cur
                        continue
                same_run = 1
                log.warning("主线刷取：%s第%d步 hash=%s(ones=%d) 已变化",
                            name, step, cur, cur.count("1"))
                prev = cur
            return step, same_run, prev

        # 分两段锚定：先把视口一路推到最左，再一路推到最上。
        # 实测「左、上、左、上」交替拖拽会被游戏吞掉部分手势，导致锚不到位。
        left_steps, left_same, _ = _pull(CANVAS_PULL_CORNER[0], CANVAS_PULL_RETRY[0], "左移")
        up_steps, up_same, prev = _pull(CANVAS_PULL_CORNER[1], CANVAS_PULL_RETRY[1], "上移")
        swipes = left_steps + up_steps
        stable = min(left_same, up_same)
        cost = time.monotonic() - started
        anchored = left_same >= ANCHOR_STABLE_FRAMES and up_same >= ANCHOR_STABLE_FRAMES
        log.warning("主线刷取：画布锚定%s 滑动=%d 连续相同帧=%d 耗时=%.1fs hash=%s(ones=%d)",
                    "完成" if anchored else "达上限仍未稳定", swipes, stable, cost,
                    prev, prev.count("1"))
        if not anchored:
            # hash 一直不收敛 = 位置不确定，不许带着这个偏移继续扫
            log.error("主线刷取：锚定达上限(%d步)仍未稳定，画布偏移不确定，停止",
                      ANCHOR_MAX_SWIPES)
            return False

        def _reverse_ok(base_hash, name):
            """稳定后**反向挪一步**：画面必须真的变，才证明是「滑到边界」而不是「手势被吞」。

            hash 稳定只说明「拖不动」，拖不动也可能是起点压在卡片/浮层上被吞。
            反向能走说明手势通路正常、正向停住确实是边界。多种手段全失败则明确失败。
            """
            back = "right" if name == "左移" else "down"
            pool = [
                ("标准反向", CANVAS_STEPS[back]),
                ("备用反向", PAN_RETRY[back]),
                ("第三滑动线", ((640, 300), (380, 300)) if name == "左移"
                 else ((900, 620), (900, 180))),
                # 实机出现过前 3 种在同一瞬间全被转场/浮层吞掉、4s 后又恢复正常的情况，
                # 因此补一次延迟后的标准反向；仍是真实位移判据，不放宽到「盲信已到边界」。
                ("延迟复核", CANVAS_STEPS[back]),
            ]
            tries = 0
            time.sleep(1.2)
            before = _settled_frame()
            for tag, (begin, end) in pool:
                tries += 1
                if tag == "延迟复核":
                    time.sleep(3.0)
                _swipe(context, begin, end, duration=ANCHOR_SWIPE_MS)
                time.sleep(ANCHOR_SETTLE_S)
                after = _settled_frame()
                cur = _frame_hash(after)
                diff = _frame_diff(before, after)
                changed = cur != base_hash or diff >= 4.0
                log.warning("主线刷取：%s反向验证 第%d/%d种手段(%s) hash=%s(ones=%d) "
                            "灰度差=%.1f %s",
                            name, tries, len(pool), tag, cur[:16], cur.count("1"),
                            diff, "画面已变化" if changed else "画面未变化")
                if changed:
                    return True, tries
                before = after
            return False, tries

        def _back_to_corner(name):
            corner = CANVAS_PULL_CORNER[0] if name == "左移" else CANVAS_PULL_CORNER[1]
            _swipe(context, corner[0], corner[1], duration=ANCHOR_SWIPE_MS)
            time.sleep(ANCHOR_SETTLE_S)

        # 左边界：反向（向右）确认可动 → 再拖回左边界；上边界同理（反向向下）
        left_ok, left_tries = _reverse_ok(prev, "左移")
        if not left_ok:
            log.error("主线刷取：手势被吞，无法锚定 —— 左移反向验证 %d 种手段都没让画面变化",
                      left_tries)
            return False
        _back_to_corner("左移")
        corner_hash = _frame_hash(_settled_frame())
        up_ok, up_tries = _reverse_ok(corner_hash, "上移")
        if not up_ok:
            log.error("主线刷取：手势被吞，无法锚定 —— 上移反向验证 %d 种手段都没让画面变化",
                      up_tries)
            return False
        _back_to_corner("上移")
        cost = time.monotonic() - started
        log.warning("主线刷取：边界反向验证通过（左=%d种/上=%d种手段），已拖回角落，累计耗时=%.1fs",
                    left_tries, up_tries, cost)
        _SESSION["anchor"] = {"swipes": swipes, "stable": stable,
                              "reverse": {"left": left_tries, "up": up_tries},
                              "seconds": round(cost, 1), "hash": prev[:16]}

        # 锚定后复核页签：按亮度判选中态，必须仍是目标模式；不盲点（点了会重置画布）
        target_mode = _SESSION.get("mode") or TARGET_MODE
        selected, table, readable = _tab_state(context, _frame(context))
        if not readable:
            log.error("主线刷取：锚定后页签读不出（亮度差不足 %d），画布/模式状态不确定，停止",
                      TAB_BRIGHT_DIFF)
            return False
        if selected != target_mode:
            log.error("主线刷取：锚定后实际选中=%s，不是目标「%s」，停止", selected, target_mode)
            return False
        log.warning("主线刷取：页签复核通过 —— 当前「%s」（普通=%.0f/困难=%.0f）",
                    selected, table.get("普通", -1), table.get("困难", -1))

        index = 0
        direction = "right"
        for row in range(SCAN_MAX_ROWS):
            for col in range(SCAN_MAX_COLS):
                index += 1
                if self._scan_viewport(context, index):
                    return True
                if col + 1 >= SCAN_MAX_COLS:
                    break
                _image, _hash, moved = _pan(context, direction)
                if not moved:
                    log.warning("主线刷取：第%d行横移已到边界，转下一行", row + 1)
                    break
            if row + 1 >= SCAN_MAX_ROWS:
                break
            _image, _hash, moved = _pan(context, "down")
            if not moved:
                log.warning("主线刷取：纵移已到画布底部，扫描结束")
                break
            direction = "left" if direction == "right" else "right"

        log.error("主线刷取：扫完全部视口（共 %d 个），没有可进入的 0/x 区域"
                  "（找到的 0/x 卡片点击无响应，实机为未解锁状态）", index)
        return False

    def _scan_viewport(self, context, index) -> bool:
        """扫描当前视口：找「进度未满」候选（半开优先），逐个尝试进入；成功返回 True。"""
        image = _frame(context)
        items = _ocr_items(context, image)
        # 误点可能开出覆盖整屏的面板（如「奖励」），此时画面已不是地图：
        # 普通/困难 两个模式页签读不到就按一次返回，回到画布再扫。
        if not any(s >= 0.4 and ("普通" in t or "困难" in t) for t, _b, s in items):
            log.warning("主线刷取：视口%d 未见模式页签（被面板覆盖），按返回收面板", index)
            _snapshot(image, "非白名单页面")
            _back(context)
            time.sleep(1.2)
            image = _frame(context)
            items = _ocr_items(context, image)
        candidates = _progress_candidates(items)
        if not candidates:
            readable = [t for t, _b, _s in items if PROGRESS_CARD_RE.search(t)]
            clipped = _clipped_region_cards(items)
            log.warning("主线刷取：视口%d 未见进度未满的区域；可读进度=%s 贴边不可读卡=%s",
                        index, readable,
                        [f"{t}@{b[0]},{b[1]}" for t, b, _lb in clipped])
            for text, box, label in clipped:
                target = label[1] if label else box
                px = min(1240, max(40, target[0] + target[2] // 2))
                py = min(690, max(40, target[1] + target[3] // 2))
                if _enter_region(context, px, py):
                    log.warning(
                        "主线刷取：贴边卡进入区域成功 视口=%d %r 名字=%r 点击=(%d,%d)",
                        index, text, label[0] if label else None, px, py)
                    _SESSION["region"] = {
                        "text": text, "box": box, "score": 0.0,
                        "viewport": index, "click": [px, py], "hit": "贴边卡",
                    }
                    return True
                log.warning("主线刷取：贴边卡 %r 点击未进章节页（多为未解锁）", text)
            return False
        for text, box, score in candidates:
            cx = box[0] + box[2] // 2
            cy = box[1] + box[3] // 2
            label = _card_label(items, (text, box, score))
            points = []
            if label:
                lb = label[1]
                points.append((lb[0] + lb[2] // 2, lb[1] + lb[3] // 2, f"名:{label[0]}"))
            points.extend((cx + dx, cy + dy, tag) for dx, dy, tag in CARD_CLICK_OFFSETS)
            # 卡片被视口边缘切掉时，兜底偏移可能落到屏幕外，点屏幕外没有意义
            points = [p for p in points if 40 <= p[0] <= 1240 and 40 <= p[1] <= 690]
            log.warning(
                "主线刷取：视口%d 候选 text=%r box=%s 置信=%.3f 中心=(%d,%d) 标签=%r 点击候选=%s",
                index, text, box, score, cx, cy,
                label[0] if label else None,
                [f"{tag}@{px},{py}" for px, py, tag in points])
            for px, py, tag in points:
                if _enter_region(context, px, py):
                    log.warning(
                        "主线刷取：进入区域成功 视口=%d 文本=%r box=%s 置信=%.3f 点击=(%d,%d) %s",
                        index, text, box, score, px, py, tag)
                    _SESSION["region"] = {
                        "text": text, "box": box, "score": score,
                        "viewport": index, "click": [px, py], "hit": tag,
                    }
                    return True
            log.warning("主线刷取：候选 %r 全部点击点都没进章节页，换下一个候选", text)
        return False

    def finish(self, context) -> bool:
        """收尾：只打日志，不做任何点击（与每日探索的收尾同一语义）。"""
        log.info(
            "主线刷取：模式=%s 体力=%s 次数=%s 完成",
            _SESSION.get("mode"), _SESSION.get("stamina"), _SESSION.get("target_runs"),
        )
        return True


def _hit(detail=None):
    """命中返回 AnalyzeResult，未命中必须返回 None。

    注意：**不能返回 bool**。返回 False 不是 None，MaaFW 会当成"命中"，
    导致该分支永远为真（本任务的「还有下一批」就因此每次都多跑一轮）。
    与每日探索的 _hit 保持一致。
    """
    return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail=detail or {})


class MainStoryRecognition(CustomRecognition):
    """给 Pipeline 判断分支用的内存状态；只做判断，不做任何点击。"""

    def analyze(self, context, argv):
        param = _param(getattr(argv, "custom_recognition_param", None))
        expected = str(param.get("expected", ""))

        if expected == "batch:more":
            if _SESSION.get("more"):
                return _hit({"remaining": _SESSION.get("remaining")})
            return None

        if expected == "potion:pending":
            if _SESSION.get("potion_pending"):
                return _hit({"need": _SESSION.get("potion_need")})
            return None

        if expected == "potion:small":
            return _hit({"type": "小药"}) if _SESSION.get("potion_type") == "小药" else None

        if expected == "potion:large":
            return _hit({"type": "大药"}) if _SESSION.get("potion_type") == "大药" else None

        if expected == "potion:quantity_page":
            image = context.tasker.controller.post_screencap().wait().get()
            n = _ocr_digits(context, "主线刷取_药量页", image, ROI_POTION_COUNT)
            if n is not None and 1 <= n <= POTION_USE_LIMIT:
                return _hit({"count": n})
            return None

        if expected == "level:story":
            level = _SESSION.get("level") or {}
            if level.get("story"):
                return _hit({"level": level.get("text")})
            return None

        log.error("主线刷取：未知状态 %s", expected)
        return None
