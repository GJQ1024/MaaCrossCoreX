# -*- coding: utf-8 -*-
"""芯片合成：仓库 → 芯片 → 工作台 → 合成，按主技能串行三合一。

规则（用户确认）：
- 三张同主技能、同品质芯片合成一张，产出品质与主技能名不变、部位随机、四行等级重随；
  每次消耗 210 菱形货币。
- 两档判据（任一命中即可合，其余保留）：
  档 1「主1」= 主技能等级 ≤ 主阈值-1（默认 1），完全不看副技能；
  档 2「主2副全1」= 主技能等级 ≤ 主阈值 且 三个副技能各自 ≤ 对应阈值。
- 一次只处理一种主技能；切换主技能前必须点掉 3 个材料槽的「−」。
- 已锁定/已佩戴的芯片会出现在合成列表里但点不中：不做额外判据，点击后复核槽位数量。
- 产出那张不读属性（就是一张新芯片），也不做任何排除：重扫后仍按阈值判定，
  低级产出会在下一轮继续被合掉。防「原地打转」只用货币：每轮必须正好扣掉
  「消耗道具」显示的数值，否则判定没合成成功、停下报错。
- 「消耗道具」读不到时不静默按常量 210 记账：本轮判为「成本未确认」并停止循环，
  报告里用 cost_source="ocr"|"fallback" 标明成本来源。
- 库存不足 3 张、货币不足、槽位点不动、列表扫完——一律干净收尾，不抛错。

默认真跑：面板「执行模式」选「干跑」或设 LAA_CHIP_SYNTH_REAL=0 时，才停在「合成」按钮之前、不消耗材料与货币。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from chip_build_select import (
    BUTTON_ROI,
    QUALITY_POINTS,
    _click_text,
    _find_and_click_panel_text,
    _find_text,
    _ocr,
    _panel_open,
    _reset_panel,
    _shot,
    _stable_state,
    _swipe,
)
from chip_domain import MAIN_SKILLS, SUB_SKILLS, normalize_ocr
from chip_filter_flow import instance_config_path, parse_detail_level_cluster
from stop_guard import ActionStopped, ensure_running

import diag

log = logging.getLogger("laa.chip_synth")
# 日志出口统一由 diag.setup() 配置：INFO 及以上进 logs/agent-*.log，
# 面板只收 diag.stage 写的阶段摘要（每任务 ≤10 行）。

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# chip_domain.MAIN_SKILLS 缺「震慑」（游戏面板共 37 项，见 interface.json 的
# 「芯片制造_主技能」），这里补齐，避免勾了震慑却在解析/筛选阶段被丢掉。
SYNTH_MAIN_SKILLS = MAIN_SKILLS + ("震慑",)
SYNTH_SKILLS = SYNTH_MAIN_SKILLS + SUB_SKILLS

TASK_NAME = "芯片合成"
OPT_SKILLS = "芯片合成_主技能"
OPT_THRESHOLD = "芯片合成_阈值"
# 合成台只接受 R5 芯片：品质固定，不再从面板/环境变量读取。
SYNTH_QUALITIES = ("R5",)

# 1280x720 实测（详见 specs/chip-synthesis-20261110/测绘记录.md）
SORT_LABEL_ROI = [190, 88, 260, 48]
SORT_ITEMS_ROI = [190, 140, 240, 330]
FILTER_ROI = [380, 85, 170, 55]
CAPACITY_ROI = [640, 13, 220, 53]
SYNTH_BUTTON_ROI = [800, 585, 220, 90]
CONFIRM_ROI = [700, 415, 240, 75]
CURRENCY_ROI = [1180, 8, 100, 48]

# 合成页「消耗道具：◆ 210」那一行：数字右缘实测 x≈1005，旧 ROI 宽 180（到 975）
# 把数字切掉了，OCR 只能读到标签 → 每次都回落常量。现按「标签+图标+数字」整行取。
COST_ROI = [790, 545, 230, 50]
OUTPUT_DISMISS = (160, 300)  # 「获得物品」全屏展示上的空白处，点它关掉展示

SLOT_MINUS = ((727, 163), (956, 163), (1182, 163))
GRID_COLS = (254, 376, 498)
GRID_ROWS = (228, 350, 472)
GRID_ROI = [190, 170, 380, 360]  # 卡片栅格区，翻页到底判定只看这块
# 长按必须落在芯片图标上：图标中心相对栅格单元中心的偏移（1280×720 实测）。
# 落在卡片顶栏、名字条或卡片之间的空隙都打不开详情。
ICON_OFFSET = (-4, -5)

DETAIL_TITLE_ROI = [550, 105, 200, 45]
DETAIL_OCR_ROI = [455, 210, 380, 210]
DETAIL_CLOSE = (200, 500)

# 主技能行的等级数字只有 4-8 px 宽，稳定态 OCR 会把「1」整个丢掉（只回「等级。」），
# 所以这一行改「宽度 + 模板」双条件；副技能三行仍走 OCR。
# 1280x720 真机实测：字形高 16px，右缘固定在 x=765（1/2/3 全部成立），
# 宽度「1」=4px、「2」=8px、「3」=8px —— 宽度只能分开 1 与 {2,3}，2/3 必须靠模板。
# 详情弹窗会随说明文字行数整体上下平移（实测最大下移 14px）；从待选槽长按打开的
# 弹窗还要再低 28px（主技能行 y=257 起），所以纵向跨到 214..277：
# 上边界 214 避开「攻击 +36%」白字条（y≤210），下边界 277 避开第二行（y=279 起，
# 待选槽弹窗的第二行在 y=307）——两行的等级数字都右对齐 x=765，跨行会张冠李戴。
MAIN_LEVEL_ROI = [754, 214, 16, 64]
MAIN_LEVEL_THRESHOLD = 0.85
MAIN_LEVEL_WIDTH_SPLIT = 6  # <=6 只可能是 1 级；>=7 只可能是 2/3 级
MAIN_LEVEL_TEMPLATES = "chip_main_lv%d.png"

COST_PER_SYNTH = 210
CLEAR_TRIES = 2
CLICK_TRIES = 2
SCAN_SCOPE = "first_row_only"  # 每轮只读第一行前 3 格，不翻页
SCROLL_BEGIN = (350, 470)
SCROLL_END = (350, 226)
SCROLL_DURATION = 900

_SESSION: dict = {}


def _param(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
        if isinstance(data, str):
            data = json.loads(data)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _gray(image):
    return np.asarray(image, dtype=np.float32).mean(axis=2)


def _pick(needles, texts):
    joined = " ".join(str(t) for t in texts)
    return any(n in joined for n in needles)


def _hit(detail=None):
    return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail=detail or {})


def _signal_gray(text):
    return normalize_ocr(text)


# ---------------------------------------------------------------- 主技能等级模板

_LEVEL_TEMPLATE_CACHE: dict = {}


def _resource_image_dir():
    """源码布局在 assets/resource/image，安装布局在 resource/image。"""
    for base in (PROJECT_ROOT / "assets" / "resource" / "image",
                 PROJECT_ROOT / "resource" / "image"):
        if (base / "chip_synth").is_dir():
            return base / "chip_synth"
    return PROJECT_ROOT / "assets" / "resource" / "image" / "chip_synth"


def _level_templates():
    if _LEVEL_TEMPLATE_CACHE:
        return _LEVEL_TEMPLATE_CACHE
    directory = _resource_image_dir()
    for level in (1, 2, 3):
        path = directory / (MAIN_LEVEL_TEMPLATES % level)
        try:
            with Image.open(path) as handle:
                _LEVEL_TEMPLATE_CACHE[level] = np.asarray(handle.convert("L"), dtype=np.float32)
        except OSError as exc:
            log.error("读不到主技能等级模板 %s：%s", path, exc)
    return _LEVEL_TEMPLATE_CACHE


def _ncc_best(patch, template):
    """滑动窗口 NCC，返回 (最高分, 左上角坐标)，等价 cv2.TM_CCOEFF_NORMED。"""
    th, tw = template.shape
    if patch.shape[0] < th or patch.shape[1] < tw:
        return -1.0, None
    centered = template - template.mean()
    norm = float(np.sqrt((centered * centered).sum()))
    if norm < 1e-6:
        return -1.0, None
    best, best_at = -1.0, None
    for y in range(patch.shape[0] - th + 1):
        for x in range(patch.shape[1] - tw + 1):
            window = patch[y:y + th, x:x + tw]
            local = window - window.mean()
            scale = float(np.sqrt((local * local).sum())) * norm
            if scale > 1e-6:
                score = float((local * centered).sum()) / scale
                if score > best:
                    best, best_at = score, (x, y)
    return best, best_at


def _digit_width(gray, top, left):
    """在命中位置上量字形宽度：亮列（字形）的左右缘差 +1。

    宽度是离散物理量，用来和模板结果互校：<=6 只可能是 1 级，>=7 只可能是 2/3 级。
    """
    band = gray[top + 2:top + 18, left - 6:left + 14]
    if band.size == 0:
        return None
    columns = (band > 150).sum(axis=0)
    idx = np.nonzero(columns >= 3)[0]
    if len(idx) == 0:
        return None
    return int(idx.max() - idx.min() + 1)


def _match_main_level(image, templates=None):
    """主技能行等级：模板取最高分，再用字形宽度互校；任一处对不上就判 None。

    返回 (等级或 None, 三档分数)。None 由调用方走 unknown 硬停，
    绝不退化成「读不到就当 1 级」。
    """
    templates = _level_templates() if templates is None else templates
    if not templates:
        return None, {}
    x, y, w, h = MAIN_LEVEL_ROI
    gray = _gray(image)
    patch = gray[y:y + h, x:x + w]
    scores, positions = {}, {}
    for level, template in sorted(templates.items()):
        score, at = _ncc_best(patch, template)
        scores[level] = round(score, 4)
        if at is not None:
            positions[level] = (y + at[1], x + at[0])
    level = max(scores, key=scores.get)
    if scores[level] < MAIN_LEVEL_THRESHOLD or level not in positions:
        return None, scores
    width = _digit_width(gray, *positions[level])
    if width is None or width < 3 or width > 10:
        return None, scores
    wide = width > MAIN_LEVEL_WIDTH_SPLIT
    if wide != (level > 1):  # 宽度说 1 级、模板说 2/3（或反之）→ 冲突
        return None, scores
    return level, scores


# ---------------------------------------------------------------- 配置读取

def _task_item():
    try:
        data = json.loads(instance_config_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return next((i for i in data.get("TaskItems", []) if i.get("name") == TASK_NAME), None)


def _walk_options(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        yield from _walk_options(item.get("option") or item.get("sub_options"))


def _config_options():
    """实例配置兜底：复选框存 selected_cases，输入框存 data。"""
    item = _task_item()
    found = {"skills": [], "threshold": None}
    if item is None:
        return found
    for option in _walk_options(item.get("option")):
        name = option.get("name")
        if name == OPT_SKILLS:
            found["skills"] = [str(v) for v in (option.get("selected_cases") or [])]
        elif name == OPT_THRESHOLD:
            for value in (option.get("data") or {}).values():
                if isinstance(value, str) and re.fullmatch(r"\d+/\d+/\d+/\d+", value.strip()):
                    found["threshold"] = value.strip()
    return found


def _parse_threshold(raw):
    """解析「主/副1/副2/副3」；格式不对返回 None，由调用方回落面板默认值。"""
    values = []
    for part in str(raw or "").replace("，", "/").split("/"):
        try:
            values.append(max(1, min(3, int(part.strip()))))
        except (TypeError, ValueError):
            return None
    return values if len(values) == 4 else None


def _interface_options():
    """读 interface.json 的 option（dict，以 name 为 key）。

    源码布局在 `assets/interface.json`，安装布局在根目录 `interface.json`，
    两处都找不到时返回空字典，由调用方报错。
    """
    for path in (PROJECT_ROOT / "assets" / "interface.json", PROJECT_ROOT / "interface.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        options = data.get("option")
        if isinstance(options, dict):
            return options
    log.error("读不到 interface.json 的 option：%s 及根目录副本均不可用", PROJECT_ROOT / "assets")
    return {}


def _default_threshold():
    """面板输入框默认值，来自 interface.json 的「芯片合成_阈值」。"""
    option = _interface_options().get(OPT_THRESHOLD) or {}
    for item in option.get("inputs") or []:
        value = str(item.get("default") or "").strip()
        if _parse_threshold(value) is not None:
            return value
    log.error("interface.json 未提供可用的 %s 默认值", OPT_THRESHOLD)
    return ""


def _env_list(name):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[,，]", raw) if part.strip()]


def _env_flag(name):
    """未设置返回 None；设置即视为开关，只有 0/false/no/空 视为关。"""
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() not in ("0", "false", "no", "")


def _resolve_options(param):
    config = _config_options()
    skills = [key[3:] for key, value in param.items() if key.startswith("ms_") and value]
    threshold = str(param.get("threshold") or "").strip()
    if not skills:
        skills = config["skills"]
    if not threshold:
        threshold = config["threshold"] or ""

    env_skills = _env_list("LAA_CHIP_SYNTH_SKILLS")
    if env_skills:
        skills = env_skills
    if os.environ.get("LAA_CHIP_SYNTH_THRESHOLD"):
        threshold = os.environ["LAA_CHIP_SYNTH_THRESHOLD"].strip()

    levels = _parse_threshold(threshold)
    if levels is None:
        threshold = _default_threshold()
        levels = _parse_threshold(threshold) or []
        log.warning("阈值缺失或非法，回落到 interface.json 默认：%r", threshold)

    skills = [s for s in skills if s in SYNTH_MAIN_SKILLS]
    return {
        "skills": skills,
        "qualities": list(SYNTH_QUALITIES),
        "threshold": levels,
        "threshold_raw": "/".join(str(v) for v in levels) if levels else "",
    }


# ---------------------------------------------------------------- 会话对象

class _Synth:
    """一次运行的会话：证据目录、报告、缓存。"""

    def __init__(self, options, dry_run):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.dir = PROJECT_ROOT / "sandbox" / f"chip_synth_{stamp}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.step = 0
        self.dry_run = dry_run
        self.options = options
        self.report = {
            "run_id": self.dir.name,
            "device_expected": "",
            "mode": "DRY RUN" if dry_run else "REAL",
            "dry_run": dry_run,
            "options": options,
            "skills": list(options.get("skills") or []),
            "qualities": list(options.get("qualities") or []),
            "threshold": options.get("threshold_raw", ""),
            "max_rounds": None,
            "skill_runs": [],
            "produced": [],
            "currency_before": None,
            "currency_after": None,
            "scan_truncated": False,
            "unknown_total": 0,
            "status": "running",
            "notes": [],
        }

    def flush(self):
        try:
            (self.dir / "report.json").write_text(
                json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("写报告失败：%s", exc)

    def shot(self, context, name):
        self.step += 1
        path = self.dir / f"{self.step:02d}_{name}.png"
        try:
            image = _shot(context)
            Image.fromarray(np.asarray(image)[:, :, ::-1]).save(path)
            return path.name
        except Exception as exc:  # noqa: BLE001 证据失败不阻断任务
            log.warning("保存截图 %s 失败：%s", name, exc)
            return None


# ---------------------------------------------------------------- 界面原子操作

def _slot_filled(image, index):
    """槽位右上「−」按钮是否在：空槽 0.00、满槽 ≈0.39。"""
    x, y = SLOT_MINUS[index]
    roi = _gray(image)[y - 12:y + 12, x - 22:x + 22]
    return float((roi > 190).mean()) > 0.10


def _filled_count(image):
    return sum(1 for index in range(3) if _slot_filled(image, index))


def _card_present(image, point):
    x, y = point
    return float(_gray(image)[y - 45:y + 45, x - 45:x + 45].mean()) > 85


def _cell_diff(before, after):
    return float(np.abs(_gray(before) - _gray(after)).mean())


def _crop_grid(image):
    """只取卡片栅格区做帧差，避开顶栏动画、NEW 徽章消失等全屏噪声。"""
    x, y, w, h = GRID_ROI
    return np.asarray(image)[y:y + h, x:x + w]


# (按住ms, 图标内 x 偏移, y 偏移)：第二次重试换时长、并在图标范围内换落点。
DETAIL_ATTEMPTS = ((900, 0, 0), (1300, -10, 8))
ICON_HALF = (24, 28)  # 图标在 x/y 方向的半幅（实测），重试落点不得越界
# 松手后的判定轮询：弹窗有入场动画，单帧判定会把「已打开」误判成「未出现」。
DETAIL_POLL_DEADLINE = 2.4   # 秒，单次长按后最多等这么久
DETAIL_POLL_INTERVAL = 0.4   # 秒，轮询间隔


def _icon_point(card):
    """长按落点 = 卡片里的芯片图标中心（不是栅格单元中心，也不是名字条）。"""
    return card["point"][0] + ICON_OFFSET[0], card["point"][1] + ICON_OFFSET[1]


def _press(context, point, duration, dx, dy):
    """按住不动 duration 毫秒再抬起，落点在图标内偏移 (dx, dy)。

    用 touch_down/touch_up 而不是零距离 swipe：后者在部分设备上会被当成无效手势。
    长按必须落在芯片图标上，落在卡片其它位置打不开详情。
    """
    x = point[0] + dx
    y = point[1] + dy
    controller = context.tasker.controller
    controller.post_touch_down(x, y).wait()
    time.sleep(duration / 1000.0)
    controller.post_touch_up().wait()


def _ocr_snapshot(items):
    """取证：把 OCR 结果原样转成可写进 report.json 的结构（文本 + 框）。"""
    return [{"text": text, "box": [int(v) for v in box]} for text, box in items]


def _detail_visible(context, image=None, trace=None):
    """详情弹窗是否已出现：判据是弹窗内容，不是固定等待。

    标题区认出「芯片」，或四行等级里任一行能解析出技能名/等级，即算打开。
    trace 只用于取证：把本次判定实际读到的 OCR 整段写回该 dict，不影响返回值。
    """
    image = image if image is not None else _shot(context)
    title_items = _ocr(context, image, DETAIL_TITLE_ROI)
    if trace is not None:
        trace["title"] = _ocr_snapshot(title_items)
    if _pick(["芯片"], [text for text, _ in title_items]):
        return True
    row_items = _ocr(context, image, DETAIL_OCR_ROI)
    if trace is not None:
        trace["rows"] = _ocr_snapshot(row_items)
    for group in _rows_from_items(row_items):
        if any(_skill_name(text) for text, box in group["items"]
               if int(box[0]) + int(box[2]) <= 700):
            return True
        if parse_detail_level_cluster([text for text, _ in group["items"]]) is not None:
            return True
    return False


def _open_detail(context, point, evidence=None):
    """长按打开详情：point 必须是芯片图标中心（见 _icon_point）。

    松手后按 DETAIL_POLL_DEADLINE 轮询判定弹窗内容，而不是只截一帧：弹窗有入场
    动画，单帧判定会把「已打开」误判成「未出现」，第 2 次长按又会落在已打开的
    弹窗上。两次尝试都在 deadline 内轮询不到才返回 False，由调用方记 unknown（
    fail-closed），失败帧与最后一次 OCR 写回 evidence，便于事后定位。
    """
    last_image = None
    last_trace = None
    for attempt, (duration, dx, dy) in enumerate(DETAIL_ATTEMPTS, start=1):
        if abs(dx) > ICON_HALF[0] or abs(dy) > ICON_HALF[1]:
            log.error("[芯片合成] 重试落点 (%+d,%+d) 超出芯片图标范围，拒绝落到图标外", dx, dy)
        _press(context, point, duration, dx, dy)
        started = time.monotonic()
        polls = 0
        while True:
            polls += 1
            image = _shot(context)
            trace = {} if evidence is not None else None
            if _detail_visible(context, image, trace):
                if evidence is not None:
                    # 标题命中时上面的判定不会读四行区，这里补一次只为取证
                    if "rows" not in trace:
                        trace["rows"] = _ocr_snapshot(_ocr(context, image, DETAIL_OCR_ROI))
                    evidence["open_image"] = image
                    evidence["open_ocr"] = trace
                    evidence["open_polls"] = polls
                log.info("[芯片合成] 第 %d 次长按后第 %d 次轮询判定详情已出现", attempt, polls)
                return True
            last_image, last_trace = image, trace
            if time.monotonic() - started >= DETAIL_POLL_DEADLINE:
                break
            time.sleep(DETAIL_POLL_INTERVAL)
        log.warning("[芯片合成] 长按 %dms 偏移(%+d,%+d) 后 %.1fs 内详情弹窗未出现（第 %d/%d 次）",
                    duration, dx, dy, DETAIL_POLL_DEADLINE, attempt, len(DETAIL_ATTEMPTS))
    if evidence is not None:
        evidence["fail_image"] = last_image
        evidence["fail_ocr"] = last_trace
    return False


def _save_frame(session, image, name):
    """取证：按固定文件名存整屏帧（不占 session.shot 的序号前缀）。"""
    directory = getattr(session, "dir", None)
    if image is None or directory is None:
        log.warning("[芯片合成] 取证帧 %s 未保存（无图像或无目录）", name)
        return None
    try:
        path = Path(directory) / name
        Image.fromarray(np.asarray(image)[:, :, ::-1]).save(path)
        return str(path)
    except Exception as exc:  # noqa: BLE001 取证失败不阻断任务
        log.warning("[芯片合成] 保存取证帧 %s 失败：%s", name, exc)
        return None


def _close_detail(context):
    for _ in range(3):
        context.tasker.controller.post_click(*DETAIL_CLOSE).wait()
        if not _detail_visible(context):
            return True
    return False


def _dismiss_output(context):
    """点空白关掉「获得物品」全屏展示，不读产出那张的任何属性。"""
    for _ in range(3):
        context.tasker.controller.post_click(*OUTPUT_DISMISS).wait()
        time.sleep(0.6)
        texts = [text for text, _ in _ocr(context, _shot(context), [480, 150, 340, 90])]
        if not _pick(["获得物品"], texts):
            return True
    log.warning("[芯片合成] 「获得物品」展示没关掉，继续往下走")
    return False


def _skill_name(text):
    flat = _signal_gray(text)
    if not flat:
        return None
    if flat in SYNTH_SKILLS:
        return flat
    hits = [name for name in SYNTH_SKILLS if flat.startswith(name) or name.startswith(flat)]
    if len(hits) == 1:
        return hits[0]
    hits = [name for name in SYNTH_SKILLS if flat[:1] and name.startswith(flat[:1])]
    return hits[0] if len(hits) == 1 else None


def _rows_from_items(items):
    groups = []
    for text, box in items:
        cy = float(box[1]) + float(box[3]) / 2.0
        if not (212 <= cy <= 410):
            continue
        for group in groups:
            if abs(group["y"] - cy) <= 16:
                group["items"].append((text, box))
                break
        else:
            groups.append({"y": cy, "items": [(text, box)]})
    groups.sort(key=lambda group: group["y"])
    return groups


def _detail_from_groups(groups, main_level=None):
    """主技能等级由模板匹配给出（main_level），副技能三行仍解析 OCR。"""
    if len(groups) != 4:
        return None
    rows = []
    for index, group in enumerate(groups):
        name = None
        for text, box in group["items"]:
            if int(box[0]) + int(box[2]) <= 700:
                candidate = _skill_name(text)
                if candidate:
                    name = candidate
                    break
        if index == 0:
            level = main_level
        else:
            level = parse_detail_level_cluster([text for text, _ in group["items"]])
        if name is None or level is None:
            return None
        rows.append((name, level))
    names = [name for name, _ in rows]
    levels = [level for _, level in rows]
    if names[0] not in SYNTH_MAIN_SKILLS or any(name not in SUB_SKILLS for name in names[1:]):
        return None
    if len(set(names)) != len(names) or any(level not in (1, 2, 3) for level in levels):
        return None
    return {
        "main_skill": {"name": names[0], "level": levels[0]},
        "sub_skills": [
            {"name": name, "level": level} for name, level in zip(names[1:], levels[1:])
        ],
    }


def _threshold_from_detail(detail):
    levels = [detail["main_skill"]["level"]]
    levels.extend(sub["level"] for sub in detail["sub_skills"])
    return levels


def _verdict(detail, threshold):
    """两档判据（用户口径 A）：任一档命中即可合，其余保留。

    - 档 1「主1」：主技能等级 ≤ 主阈值-1（默认 1），不看副技能。
    - 档 2「主2副全1」：主技能等级 ≤ 主阈值，且三个副技能各自 ≤ 对应阈值。
    返回 (是否命中, 档位标签, 未命中原因)。
    """
    levels = _threshold_from_detail(detail)
    if len(threshold) != 4:
        return False, "", "阈值未配置"
    main_limit = threshold[0]
    if levels[0] <= max(1, main_limit - 1):
        return True, f"主{levels[0]}", ""
    if levels[0] > main_limit:
        return False, "", f"主{levels[0]}>{main_limit}"
    for index, (level, limit) in enumerate(zip(levels[1:], threshold[1:]), start=1):
        if level > limit:
            return False, "", f"主{levels[0]} 达标但 副{index}等级{level}>{limit}"
    return True, f"主{levels[0]}副全≤{'/'.join(str(v) for v in threshold[1:])}", ""


def _signature(detail):
    return (
        detail["main_skill"]["name"],
        detail["main_skill"]["level"],
        tuple((sub["name"], sub["level"]) for sub in detail["sub_skills"]),
    )


def _detail_text(detail):
    levels = _threshold_from_detail(detail)
    subs = "/".join(f"{sub['name']}{sub['level']}" for sub in detail["sub_skills"])
    return f"{detail['main_skill']['name']}{levels[0]} [{subs}]"


class ChipSynthAction(CustomAction):
    """operation = init | ensure_sort | run"""

    def run(self, context, argv) -> bool:
        param = _param(getattr(argv, "custom_action_param", ""))
        operation = str(param.get("operation") or "")
        if not operation and ("dry_run" in param or any(k.startswith("ms_") for k in param)):
            operation = "init"
        try:
            ensure_running(context)
            if operation == "init":
                return self._init(context, argv, param)
            session = _SESSION.get("synth")
            if session is None:
                log.error("芯片合成会话未初始化")
                return False
            if operation == "ensure_sort":
                return self._ensure_sort(context, session)
            if operation == "run":
                return self._run(context, session)
            log.error("未知芯片合成操作：%s", operation)
            return False
        except ActionStopped:
            log.info("用户停止任务，芯片合成立即停止")
            return False
        except Exception:  # noqa: BLE001
            log.exception("芯片合成失败：%s", operation)
            session = _SESSION.get("synth")
            if session is not None:
                session.report["status"] = "error"
                session.flush()
            return False

    # ------------------------------------------------------------ init

    def _init(self, context, argv, param):
        options = _resolve_options(param)
        # 真跑/干跑由面板选项「芯片合成_执行模式」决定，默认真跑；
        # 环境变量 LAA_CHIP_SYNTH_REAL 只作调试覆盖（不经壳直接跑流水线时用）。
        # 历史包袱：P-100 时壳会把 custom_action_param 整体覆盖，pipeline 的 dry_run 到不了
        # agent，才临时改成「以环境变量为准」；现在壳侧是合并语义，绕路的理由已不存在。
        env_real = _env_flag("LAA_CHIP_SYNTH_REAL")
        if env_real is not None:
            dry_run = not env_real
            mode_source = "env:LAA_CHIP_SYNTH_REAL"
        else:
            dry_run = param.get("dry_run", False)
            if isinstance(dry_run, str):
                dry_run = dry_run.strip().lower() not in ("0", "false", "no", "")
            dry_run = bool(dry_run)
            mode_source = "panel:芯片合成_执行模式"
        session = _Synth(options, dry_run)
        _SESSION["synth"] = session
        env_rounds = os.environ.get("LAA_CHIP_SYNTH_MAX_ROUNDS")
        max_rounds = env_rounds if env_rounds is not None else param.get("max_rounds")
        try:
            max_rounds = int(max_rounds) if max_rounds is not None else None
        except (TypeError, ValueError):
            max_rounds = None
        session.max_rounds = max_rounds if (max_rounds and max_rounds > 0) else None
        # 自检内容同时落到 report 顶层：壳日志只转发 WRN/ERR，agent 的 INFO 看不到，
        # 截图失败中断时也要能从 report.json 看出这轮配的是什么。
        session.report["mode"] = "DRY RUN" if session.dry_run else "REAL"
        session.report["dry_run"] = session.dry_run
        session.report["mode_source"] = mode_source
        session.report["skills"] = list(options["skills"])
        session.report["qualities"] = list(options["qualities"])
        session.report["threshold"] = options["threshold_raw"]
        session.report["max_rounds"] = session.max_rounds
        ready = bool(options["skills"])
        _SESSION["status"] = "ready" if ready else "skip"
        log.info(
            "[芯片合成] 启动自检：模式=%s（来源 %s）dry_run=%s max_rounds=%s 阈值=%s 主技能=%s "
            "品质=%s 环境变量(REAL=%r MAX_ROUNDS=%r) 证据=%s",
            "DRY RUN" if session.dry_run else "REAL", mode_source, session.dry_run,
            session.max_rounds, options["threshold_raw"], options["skills"], options["qualities"],
            os.environ.get("LAA_CHIP_SYNTH_REAL"), env_rounds, session.dir,
        )
        session.report["status"] = "ready" if ready else "skip"
        session.flush()
        diag.stage("芯片合成", "模式=%s（%s）主技能=%s 品质=%s 阈值=%s"
                   % ("REAL" if not session.dry_run else "DRY RUN", mode_source,
                      options["skills"], options["qualities"], options["threshold_raw"]))
        if not ready:
            # 未勾选主技能：显式分流到跳过，不靠状态门识别失败 20s 超时再走父节点 on_error（P-097）
            context.override_next("芯片合成_初始化", ["芯片合成_跳过"])
        return True

    # ------------------------------------------------------------ 排序

    def _sort_label(self, context):
        image = _shot(context)
        texts = [text for text, _ in _ocr(context, image, SORT_LABEL_ROI)]
        for text in texts:
            if "技能点数" in text:
                return "技能点数"
            for name in ("稀有度", "等级", "芯片技能", "保护", "入手顺序"):
                if name in text:
                    return name
        return ""

    def _ensure_sort(self, context, session):
        for attempt in range(3):
            label = self._sort_label(context)
            if label == "技能点数":
                log.info("[芯片合成] 排序已是「技能点数」")
                session.shot(context, "sort_ok")
                return True
            log.info("[芯片合成] 当前排序=%r，切技能点数（第 %d 次）", label, attempt + 1)
            box = _find_text(context, _shot(context), SORT_LABEL_ROI, ["稀有度", "等级", "芯片技能", "保护", "入手顺序", "技能点数"])
            if box is not None:
                context.tasker.controller.post_click(
                    int(box[0]) + int(box[2]) // 2, int(box[1]) + int(box[3]) // 2
                ).wait()
            else:
                context.tasker.controller.post_click(271, 113).wait()
            time.sleep(0.7)
            session.shot(context, f"sort_open_{attempt + 1}")
            if not _click_text(context, _shot(context), SORT_ITEMS_ROI, ["技能点数"]):
                log.warning("[芯片合成] 下拉里没找到「技能点数」")
                time.sleep(0.4)
                continue
            time.sleep(0.8)
            if self._sort_label(context) == "技能点数":
                log.info("[芯片合成] 排序已切到「技能点数」")
                session.shot(context, "sort_done")
                return True
        log.error("[芯片合成] 排序切换 3 次未成功，安全结束")
        session.report["notes"].append("排序切换失败")
        session.report["status"] = "sort_failed"
        session.flush()
        return False

    # ------------------------------------------------------------ 主流程

    def _run(self, context, session):
        session.report["currency_before"] = self._currency(context)
        for index, skill in enumerate(session.options["skills"]):
            ensure_running(context)
            entry = {"skill": skill, "scanned": 0, "eligible": 0, "locked_skipped": 0,
                     "rounds": 0, "consumed_chips": 0, "consumed_currency": 0,
                     "outputs": [], "status": "pending", "notes": [],
                     "unknown": 0, "unknown_cards": [], "scan_truncated": False,
                     "scanned_scope": SCAN_SCOPE}
            session.report["skill_runs"].append(entry)
            log.info("[芯片合成] ===== 主技能 %s（%d/%d）=====", skill, index + 1,
                     len(session.options["skills"]))
            ok = self._process_skill(context, session, skill, entry)
            session.flush()
            if not ok:
                entry["status"] = entry["status"] if entry["status"] != "pending" else "aborted"
                session.report["status"] = "slot_stuck"
                log.error("[芯片合成] 槽位清不空，停止后续主技能（防跨技能串料）")
                diag.judge("材料槽可清空", "3 个槽都能点掉", "槽位清不空（%s）" % skill, ok=False)
                return True
        session.report["status"] = "dryrun_finished" if session.dry_run else "finished"
        session.flush()
        self._summary(session)
        runs = session.report["skill_runs"]
        if session.dry_run:
            planned_rounds = sum(int(item.get("planned_rounds") or 0) for item in runs)
            planned_chips = sum(int(item.get("planned_chips") or 0) for item in runs)
            planned_currency = sum(int(item.get("planned_currency") or 0) for item in runs)
            log.warning(
                "[芯片合成] DRY RUN：本次未执行合成（计划 %d 轮 / %d 张 / %d 货币）；"
                "队列判成功仅代表自检通过，不代表已合成",
                planned_rounds, planned_chips, planned_currency,
            )
        elif not sum(int(item.get("consumed_chips") or 0) for item in runs):
            log.warning("[芯片合成] 本次未执行合成（各主技能都没凑满 3 张可合芯片），"
                        "判成功不等同于完成，详见上方各主技能的扫描行")
        diag.done("芯片合成", "模式=%s 消耗芯片=%d 轮数=%d"
                  % ("REAL" if not session.dry_run else "DRY RUN",
                     sum(int(item.get("consumed_chips") or 0) for item in runs),
                     sum(int(item.get("rounds") or 0) for item in runs)))
        return True

    def _process_skill(self, context, session, skill, entry):
        if not self._clear_slots(context, session):
            entry["status"] = "slot_stuck"
            return False
        session.shot(context, f"clear_{skill}")

        if not self._apply_filter(context, session, skill):
            entry["status"] = "filter_failed"
            entry["notes"].append("筛选失败")
            return True
        session.shot(context, f"filtered_{skill}")

        if self._list_empty(context):
            log.info("[芯片合成] %s：当前筛选下无内容，跳过", skill)
            entry["status"] = "empty"
            return True

        candidates = self._scan(context, session, skill, entry)
        entry["initial_cards"] = list(entry["cards"])
        entry["eligible"] = len(candidates)
        count_label = (f"至少 {len(candidates)} 张" if entry.get("scan_truncated")
                       else f"{len(candidates)} 张")
        entry["eligible_label"] = count_label  # 截断时只能算下限，报告里也照此标注
        log.info("[芯片合成] %s：扫描 %d 张（unknown %d / eligible %d / 锁定跳过 %d / 截断 %s）",
                 skill, entry.get("scanned", 0), entry.get("unknown", 0),
                 len(candidates), entry.get("locked_skipped", 0),
                 bool(entry.get("scan_truncated")))
        if entry.get("unknown", 0) > 0:
            # 读不出明细的卡该不该合无从判断，样本残缺就不许推进
            entry["status"] = "incomplete"
            entry["stop_reason"] = "incomplete"
            entry["notes"].append(
                f"明细未知 {entry['unknown']} 张（详情打不开/读不出），判定不完整，本轮不推进合成"
            )
            log.warning("[芯片合成] %s：%d 张明细未知，判定不完整，停止本技能（不合成）",
                        skill, entry["unknown"])
            return True
        if entry.get("scan_truncated"):
            # 列表没看完，命中数只是下限，不能据此决定合不合
            entry["status"] = "scan_truncated"
            entry["stop_reason"] = "scan_truncated"
            entry["notes"].append("扫描到页数上限，库存未看全，判定不完整，本轮不推进合成")
            log.warning("[芯片合成] %s：扫描被截断（scan_truncated=true），不推进合成", skill)
            return True
        if len(candidates) < 3:
            entry["status"] = "stock_short"
            entry["stop_reason"] = "stock_short"
            entry["select_reason"] = "insufficient_eligible"
            log.warning(
                "[芯片合成] %s：本次无可合成芯片（eligible=%d<3：扫描 %d 张 / unknown %d / "
                "锁定跳过 %d），未执行合成",
                skill, len(candidates), entry.get("scanned", 0),
                entry.get("unknown", 0), entry.get("locked_skipped", 0),
            )
            diag.judge("可合成库存", "eligible≥3", "eligible=%d（扫描 %d / unknown %d / 锁定 %d）"
                       % (len(candidates), entry.get("scanned", 0), entry.get("unknown", 0),
                          entry.get("locked_skipped", 0)), ok=False)
            return True

        planned_rounds = len(candidates) // 3
        max_rounds = getattr(session, "max_rounds", None)
        if max_rounds:
            planned_rounds = min(planned_rounds, max_rounds)
        if session.dry_run:
            picked = self._select_three(context, session, candidates, entry)
            session.shot(context, f"dryrun_picked_{skill}")
            entry["status"] = "dryrun_ready" if len(picked) == 3 else "dryrun_short"
            entry["planned_rounds"] = planned_rounds
            entry["planned_chips"] = planned_rounds * 3
            entry["planned_currency"] = planned_rounds * COST_PER_SYNTH
            entry["picked"] = [item["detail_text"] for item in picked]
            log.info("[芯片合成] DRY RUN %s：选中 %d 张，预估 %d 轮 / %d 张 / %d 货币（每轮 %d）",
                     skill, len(picked), planned_rounds, planned_rounds * 3,
                     planned_rounds * COST_PER_SYNTH, COST_PER_SYNTH)
            return True

        # 「合完继续合」：每轮重新扫第一行，凑满 3 张就再来一轮，直到凑不满或到 max_rounds
        round_limit = max_rounds if max_rounds else max(1, len(candidates) // 3)
        for round_index in range(round_limit):
            ensure_running(context)
            currency = self._currency(context)
            if currency is None:
                entry["status"] = "currency_unreadable"
                entry["stop_reason"] = "currency_unreadable"
                entry["notes"].append("读不到顶栏货币，无法核对交易，停止")
                log.error("[芯片合成] 读不到顶栏货币，停止本轮")
                break
            if session.report["currency_before"] is None:
                session.report["currency_before"] = currency
            if currency < COST_PER_SYNTH:
                entry["notes"].append("货币不足，正常收尾")
                entry["status"] = "no_currency"
                entry["stop_reason"] = "no_currency"
                log.warning("[芯片合成] 货币 %s < %d，正常收尾", currency, COST_PER_SYNTH)
                break
            picked = self._select_three(context, session, candidates, entry)
            if len(picked) < 3:
                entry["status"] = "stock_short"
                entry["stop_reason"] = "stock_short"
                entry["select_reason"] = "select_three_short"
                log.warning("[芯片合成] %s：本轮只选中 %d 张（候选 %d 张），未执行合成",
                            skill, len(picked), len(candidates))
                break
            entry.setdefault("picked", []).extend({
                "row": item["row"] + 1,
                "col": item["col"] + 1,
                "levels": _threshold_from_detail(item["detail"]),
                "detail": item["detail_text"],
            } for item in picked)
            session.shot(context, f"picked_{skill}_{round_index + 1}")
            # 先落盘再点合成：即使随后进程中断，投料明细也已写入报告
            session.flush()
            output = self._synth_once(context, session, entry, currency, picked)
            if output is None:
                entry["status"] = "synth_failed"
                entry["stop_reason"] = "synth_failed"
                break
            entry.setdefault("states", []).append(output["state"])
            if output["state"] == "cost_unconfirmed":
                # 「消耗道具」没读出来：钱可能扣了，但成本不可信，本轮判成本未确认并停，
                # 不静默按常量记账继续合。
                entry["status"] = "cost_unconfirmed"
                entry["stop_reason"] = "cost_unconfirmed"
                entry.setdefault("cost_sources", []).append(output["cost_source"])
                entry["notes"].append(
                    f"成本未确认：未读到「消耗道具」，回落常量 {output['cost']}，"
                    f"货币实际差值 {output['delta']}，材料槽"
                    f"{'空' if output['slots_empty'] else '未空'}"
                )
                log.error("[芯片合成] 成本未确认（回落常量 %d，实际 Δ%s）；停止，不继续循环",
                          output["cost"], output["delta"])
                break
            if output["state"] != "synth_committed":
                # 货币没按「消耗道具」的数值递减，或材料槽没清空 → 这一轮没合成成功，停下报错。
                # 「同三张材料 + 货币没动 = 卡住」也落在这一条里，不另写一套判断。
                entry["status"] = "synth_failed"
                entry["stop_reason"] = "currency_mismatch"
                entry["notes"].append(
                    f"交易未确认：货币实际差值 {output['delta']}，期望 -{output['cost']}，"
                    f"材料槽{'空' if output['slots_empty'] else '未空'}"
                )
                log.error("[芯片合成] 交易未确认：实际 Δ%s，期望 -%d，材料槽%s；停止，不继续循环",
                          output["delta"], output["cost"],
                          "空" if output["slots_empty"] else "未空")
                break
            entry["rounds"] += 1
            entry["consumed_chips"] += 3
            entry["consumed_currency"] += output["cost"]
            entry["outputs"].append(output)
            session.report["produced"].append({"skill": skill, **output})
            session.flush()
            # 币值已按消耗道具递减 → 本轮确实成交。合成后列表刷新、卡片位置上移，
            # 重新筛选扫描再选；产出不记属性、不做排除，仍按阈值判定。
            self._clear_slots(context, session)
            self._apply_filter(context, session, skill)
            candidates = self._scan(context, session, skill, entry)
            entry["remaining_cards"] = list(entry["cards"])
            if entry.get("unknown", 0) > 0 or entry.get("scan_truncated"):
                entry["status"] = "incomplete"
                entry["stop_reason"] = "incomplete"
                entry["notes"].append("重扫出现明细未知/截断，判定不完整，停止后续轮次")
                log.warning("[芯片合成] 重扫明细未知 %d 张、截断 %s，停止后续轮次",
                            entry["unknown"], entry.get("scan_truncated"))
                break
            entry["status"] = "done"
            if len(candidates) < 3:
                entry["stop_reason"] = "stock_short"
                log.info("[芯片合成] %s：合完一轮后第一行凑不满 3 张（%d 张），停止循环",
                         skill, len(candidates))
                break
        else:
            entry["stop_reason"] = "max_rounds"
            entry["notes"].append(f"达到 max_rounds={round_limit}，停止循环")
            log.info("[芯片合成] %s：达到 max_rounds=%d，停止循环", skill, round_limit)
        if entry["status"] == "pending":
            entry["status"] = "done"
        return True

    # ------------------------------------------------------------ 筛选

    def _open_filter(self, context):
        for attempt in range(2):
            if not _click_text(context, _shot(context), FILTER_ROI, ["筛选"]):
                log.error("[芯片合成] 没找到筛选按钮")
                return False
            time.sleep(1.1)
            if _panel_open(context):
                return True
            log.warning("[芯片合成] 筛选面板未打开（第 %d 次），重试", attempt + 1)
            time.sleep(0.5)
        log.error("[芯片合成] 筛选面板没打开")
        return False

    def _apply_filter(self, context, session, skill):
        if not self._open_filter(context):
            return False
        if not _reset_panel(context):
            return False
        if not self._set_qualities(context, session.options["qualities"]):
            return False
        if not _find_and_click_panel_text(context, skill):
            log.error("[芯片合成] 筛选面板没找到主技能「%s」", skill)
            return False
        if not _click_text(context, _shot(context), BUTTON_ROI, ["确定"], exact=True):
            log.error("[芯片合成] 筛选面板没找到「确定」")
            return False
        time.sleep(1.1)
        return True

    def _set_qualities(self, context, wanted):
        image = _shot(context)
        box = _find_text(context, image, [150, 150, 1000, 120], ["全部"], exact=True)
        if box is not None and _stable_state(context, box) == "gold":
            context.tasker.controller.post_click(
                int(box[0]) + int(box[2]) // 2, int(box[1]) + int(box[3]) // 2
            ).wait()
            time.sleep(0.45)
        for level, point in QUALITY_POINTS.items():
            desire = level in wanted
            for _ in range(3):
                state = _stable_state(context, [point[0] - 35, point[1] - 13, 70, 26])
                if state is None:
                    break
                if (state == "gold") == desire:
                    break
                context.tasker.controller.post_click(point[0], point[1]).wait()
                time.sleep(0.4)
            state = _stable_state(context, [point[0] - 35, point[1] - 13, 70, 26])
            if state is not None and (state == "gold") != desire:
                log.error("[芯片合成] 品质 %s 置为 %s 失败（当前 %s）", level, desire, state)
                return False
        log.info("[芯片合成] 筛选品质=%s", "/".join(wanted))
        return True

    def _list_empty(self, context):
        image = _shot(context)
        texts = [text for text, _ in _ocr(context, image, [180, 180, 400, 300])]
        return _pick(["当前筛选下无内容", "无内容", "暂无"], texts)

    # ------------------------------------------------------------ 扫描

    def _read_detail(self, context, evidence=None):
        attempts = []
        level_scores = []
        image = None
        for _ in range(3):
            image = _shot(context)
            items = _ocr(context, image, DETAIL_OCR_ROI)
            main_level, scores = _match_main_level(image)
            if evidence is not None:
                attempts.append(_ocr_snapshot(items))
                level_scores.append(scores)
            detail = _detail_from_groups(_rows_from_items(items), main_level)
            if detail is not None:
                return detail
            time.sleep(0.3)
        if evidence is not None:
            evidence["read_image"] = image
            evidence["read_attempts"] = attempts
            evidence["main_level_scores"] = level_scores
        return None

    def _scan(self, context, session, skill, entry):
        """只读第一行前 3 格：列表已按技能点数升序，垃圾排在最前，没必要全量翻页。

        已知边界（接受的取舍）：排序依据是四项等级总和，主技能 1 级但副技能很高的卡
        总和偏大、会排在后面，只看前 3 张会漏掉它。
        scan_truncated / unknown 两套机制保留（本实现不翻页，scan_truncated 基本不会触发）。
        """
        entry["cards"] = []
        entry["scanned_scope"] = SCAN_SCOPE
        session.report["scanned_scope"] = SCAN_SCOPE
        candidates = []
        image = _shot(context)
        log.info("[芯片合成] %s：本次只检查前 3 张（第一行）", skill)
        cards = [
            {"point": (x, GRID_ROWS[0]), "row": 0, "col": col}
            for col, x in enumerate(GRID_COLS)
            if _card_present(image, (x, GRID_ROWS[0]))
        ]
        filled_before = _filled_count(image)
        for card in cards:
            evidence = {}
            opened = _open_detail(context, _icon_point(card), evidence)
            if not opened and _detail_visible(context, _shot(context)):
                # 判定窗口刚过才出弹窗：仍按已打开处理，不误记 unknown、不盲点关闭
                log.warning("[芯片合成] 行%d列%d 判定窗口结束后详情才出现，按已打开继续",
                            card["row"] + 1, card["col"] + 1)
                opened = True
            if not opened:
                _close_detail(context)
                row, col = card["row"] + 1, card["col"] + 1
                entry["unknown"] = entry.get("unknown", 0) + 1
                entry.setdefault("unknown_cards", []).append({
                    "row": row,
                    "col": col,
                    "reason": "详情打不开（两次长按+轮询均未出现弹窗）",
                    "fail_frame": _save_frame(
                        session, evidence.get("fail_image"), f"unknown_open_r{row}c{col}.png"),
                    "fail_ocr": evidence.get("fail_ocr"),
                })
                entry["notes"].append(f"明细未知 行{row}列{col}：详情打不开")
                log.warning("[芯片合成] %s 行%d列%d 详情打不开，记 unknown", skill, row, col)
                continue
            detail = self._read_detail(context, evidence)
            if not _close_detail(context):
                log.warning("[芯片合成] 详情弹窗关不掉，停止扫描")
                entry["notes"].append("详情弹窗关不掉")
                return candidates
            if _filled_count(_shot(context)) > filled_before:
                log.warning("[芯片合成] 关详情时误选了一张，清槽复原")
                self._clear_slots(context, session)
            entry["scanned"] += 1
            if detail is None:
                row, col = card["row"] + 1, card["col"] + 1
                entry["unknown"] = entry.get("unknown", 0) + 1
                entry.setdefault("unknown_cards", []).append({
                    "row": row,
                    "col": col,
                    "reason": "详情读不出（弹窗已开，四行解析失败）",
                    "open_frame": _save_frame(
                        session, evidence.get("open_image"), f"unknown_open_r{row}c{col}.png"),
                    "detail_frame": _save_frame(
                        session, evidence.get("read_image"), f"unknown_detail_r{row}c{col}.png"),
                    "open_ocr": evidence.get("open_ocr"),
                    "read_ocr": evidence.get("read_attempts"),
                    "main_level_scores": evidence.get("main_level_scores"),
                })
                entry["notes"].append(f"明细未知 行{card['row'] + 1}列{card['col'] + 1}：详情读不出")
                log.warning("[芯片合成] %s 行%d列%d 四行等级读不出，记 unknown", skill,
                            card["row"] + 1, card["col"] + 1)
                continue
            levels = _threshold_from_detail(detail)
            hit, tier, reason = _verdict(detail, session.options["threshold"])
            log.info("[芯片合成] %s 行%d列%d %s 等级=%s 阈值=%s → %s", skill,
                     card["row"] + 1, card["col"] + 1, _detail_text(detail),
                     "/".join(str(v) for v in levels), session.options["threshold_raw"],
                     f"命中({tier})" if hit else f"保留（{reason}）")
            entry.setdefault("cards", []).append({
                "row": card["row"] + 1,
                "col": card["col"] + 1,
                "main": detail["main_skill"]["name"],
                "levels": levels,
                "hit": hit,
                "tier": tier,
                "reason": reason,
                "detail": _detail_text(detail),
            })
            if hit:
                candidates.append({**card, "detail": detail, "detail_text": _detail_text(detail),
                                   "tier": tier})
        if len(cards) < len(GRID_COLS) or len(candidates) < len(cards):
            # 第一行凑不满 3 张命中，说明前面已经不是垃圾了，本轮到此为止，不往下翻找
            entry["notes"].append("前 3 张未全部命中，本轮结束（不往下翻找）")
            log.info("[芯片合成] %s：前 3 张未全部命中（命中 %d 张），本轮结束，不往下翻",
                     skill, len(candidates))
        return candidates

    # ------------------------------------------------------------ 选卡

    def _clear_slots(self, context, session=None):
        for index, point in enumerate(SLOT_MINUS):
            for _ in range(CLEAR_TRIES):
                if not _slot_filled(_shot(context), index):
                    break
                context.tasker.controller.post_click(point[0], point[1]).wait()
                time.sleep(0.45)
            if _slot_filled(_shot(context), index):
                log.error("[芯片合成] 槽位 %d 点 %d 次未清空", index + 1, CLEAR_TRIES)
                return False
        return True

    def _select_three(self, context, session, candidates, entry):
        if _filled_count(_shot(context)) and not self._clear_slots(context, session):
            return []
        picked = []
        for card in candidates:
            if len(picked) == 3:
                break
            for attempt in range(CLICK_TRIES):
                before = _filled_count(_shot(context))
                context.tasker.controller.post_click(card["point"][0], card["point"][1]).wait()
                time.sleep(0.45)
                if _filled_count(_shot(context)) > before:
                    picked.append(card)
                    log.info("[芯片合成] 选中 行%d列%d %s（%d/3）", card["row"] + 1,
                             card["col"] + 1, card["detail_text"], len(picked))
                    break
                if attempt + 1 >= CLICK_TRIES:
                    entry["locked_skipped"] += 1
                    entry["notes"].append(
                        f"点不中 行{card['row'] + 1}列{card['col'] + 1} {card['detail_text']}"
                    )
                    log.warning("[芯片合成] 点不中（锁定/不可用） 行%d列%d %s，跳过",
                                card["row"] + 1, card["col"] + 1, card["detail_text"])
                    session.shot(context, f"unselectable_{entry['skill']}_{card['row']}{card['col']}")
        return picked

    # ------------------------------------------------------------ 合成

    def _currency(self, context):
        image = _shot(context)
        for text, _ in _ocr(context, image, CURRENCY_ROI):
            digits = re.sub(r"\D", "", text)
            if digits:
                return int(digits)
        return None

    def _read_cost(self, context):
        """读「消耗道具」显示的实际消耗，返回 (数值, 来源)。

        来源 ocr 表示真读到了；fallback 表示只回落常量，本轮成本不可信，
        调用方必须判为「成本未确认」，不能当成完全可信的交易。
        """
        for text, _ in _ocr(context, _shot(context), COST_ROI):
            digits = re.sub(r"\D", "", text)
            if digits:
                return int(digits), "ocr"
        log.warning("[芯片合成] 读不到「消耗道具」数值，回落常量 %d（本轮成本未确认）",
                    COST_PER_SYNTH)
        return COST_PER_SYNTH, "fallback"

    def _synth_once(self, context, session, entry, currency_before, picked):
        """合一轮：投料 → 合成 → 确定 → 只用货币差额核对交易是否真的发生。

        产出那张不读属性（就是一张新芯片，记了没处用）：点空白关掉「获得物品」展示直接往下走。
        货币在合成过程中只减不增，所以「正好少了『消耗道具』显示的那个数值」= 真合成了；
        没少或变多 = 没合成功，交给调用方停下报错。
        """
        # 点「确定」之前先把投料明细留档，账本不依赖产出解读
        materials = [{
            "row": item["row"] + 1,
            "col": item["col"] + 1,
            "levels": _threshold_from_detail(item["detail"]),
            "detail": item["detail_text"],
        } for item in picked]
        cost, cost_source = self._read_cost(context)
        if not _click_text(context, _shot(context), SYNTH_BUTTON_ROI, ["合成"], exact=True):
            log.error("[芯片合成] 没找到合成按钮")
            return None
        time.sleep(0.9)
        image = _shot(context)
        texts = [text for text, _ in _ocr(context, image, CONFIRM_ROI)]
        if _pick(["确定"], texts) or _pick(["取消"], [t for t in texts]):
            session.shot(context, f"confirm_{entry['skill']}")
            if not _click_text(context, image, CONFIRM_ROI, ["确定"], exact=True):
                log.error("[芯片合成] 二次确认没找到「确定」")
                return None
        time.sleep(2.4)
        session.shot(context, f"output_{entry['skill']}")
        _dismiss_output(context)
        currency_after = self._currency(context)
        session.report["currency_after"] = currency_after
        delta = (currency_before - currency_after) if (currency_before is not None and currency_after is not None) else None
        slots_empty = _filled_count(_shot(context)) == 0
        # 成本来源必须是 ocr：回落常量时即使差额「正好 210」也不算确认
        amount_ok = bool(slots_empty and delta == cost)
        committed = bool(amount_ok and cost_source == "ocr")
        log.info("[芯片合成] 交易核对：货币 %s → %s（实际 Δ%s，期望 -%d[来源 %s]，%s）材料槽%s",
                 currency_before, currency_after, delta, cost, cost_source,
                 "差额相符" if delta == cost else "差额不符",
                 "空" if slots_empty else "未空")
        if cost_source != "ocr":
            log.error("[芯片合成] 「消耗道具」未读出，本轮成本未确认（回落常量 %d）", cost)
        elif not committed:
            log.error("[芯片合成] 点「确定」后货币差额与消耗道具不符或材料槽非空，交易未确认")
        return {
            "state": ("synth_committed" if committed
                      else "cost_unconfirmed" if cost_source != "ocr" else "failed"),
            "cost": cost,
            "cost_source": cost_source,
            "materials": materials,
            "currency_before": currency_before,
            "currency_after": currency_after,
            "delta": delta,
            "slots_empty": slots_empty,
        }

    # ------------------------------------------------------------ 汇总

    def _summary(self, session):
        total_chips = sum(item.get("consumed_chips", 0) for item in session.report["skill_runs"])
        planned = sum(item.get("planned_currency", 0) for item in session.report["skill_runs"])
        unknown = sum(item.get("unknown", 0) for item in session.report["skill_runs"])
        session.report["unknown_total"] = unknown
        # 总消耗的唯一口径：初始币值 − 当前币值（货币只减不增，累计差值就是实际消耗）
        before = session.report.get("currency_before")
        after = session.report.get("currency_after")
        total_currency = (before - after) if (before is not None and after is not None) else None
        session.report["consumed_currency"] = total_currency
        log.info("[芯片合成] 累计消耗（唯一口径）= 初始 %s − 当前 %s = %s", before, after, total_currency)
        log.info("[芯片合成] 汇总（%s）：%s", session.report["mode"], json.dumps({
            "skills": [
                {**{k: item.get(k) for k in ("skill", "scanned", "eligible", "eligible_label",
                                             "locked_skipped", "rounds", "status", "stop_reason",
                                             "scan_truncated", "scanned_scope")},
                 "unknown": item.get("unknown", 0)}
                for item in session.report["skill_runs"]
            ],
            "consumed_chips": total_chips,
            "consumed_currency": total_currency,
            "planned_currency": planned,
            "scanned_scope": session.report.get("scanned_scope", SCAN_SCOPE),
            "scan_truncated": session.report.get("scan_truncated", False),
            "unknown_total": unknown,
        }, ensure_ascii=False))
        if unknown:
            log.warning("[芯片合成] 本轮共 %d 张明细未知，判定不完整", unknown)
        session.flush()


class ChipSynthRecognition(CustomRecognition):
    """只读页面/状态，不点击。"""

    def analyze(self, context, argv):
        param = _param(getattr(argv, "custom_recognition_param", ""))
        expected = str(param.get("expected") or "")
        if expected.startswith("status:"):
            return _hit({"status": _SESSION.get("status")}) if _SESSION.get("status") == expected[7:] else None
        image = argv.image
        if expected == "page:chip":
            items = [text for text, _ in _ocr(context, image, CAPACITY_ROI)]
            return _hit({"page": "chip"}) if _pick(["/"], items) and any(
                re.search(r"\d+\s*/\s*\d+", text) for text in items
            ) else None
        if expected == "page:synth":
            items = [text for text, _ in _ocr(context, image, SYNTH_BUTTON_ROI)]
            return _hit({"page": "synth"}) if _pick(["合成"], items) else None
        return None
