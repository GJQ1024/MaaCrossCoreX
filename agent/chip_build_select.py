"""芯片改造：按「筛选面板」结构选片 + 「暂无芯片」正常收尾。

两个自定义动作（纯类，不依赖 AgentServer，便于 tools/run_task.py 进程内注册）：

- chip_filter_select：在候选列表页打开筛选面板，按任务面板选项勾选
  芯片品质 / 芯片类型 / 佩戴类型 / 主技能，确定后点第一张卡。
  品质可多选：按「品质外层 × 主技能内层」展开成组逐组处理，每轮只选一个
  品质 + 一个主技能、只改一张芯片，组的进度写在 custom_action_param.cursor
  里（context.override_pipeline 回写），下一轮从该组继续。
  某项在面板里找不到、或筛选后无候选，记 error 并跳过该项（不顶替别的芯片）。
- chip_no_target：列表出现「暂无芯片」时判断改造槽占用，常态跳过并正常结束，
  仅当改造槽可读且为 0/4 时判真异常失败；mode=slot_guard 时用于每轮改造前的
  槽位预检（槽位已满则记「槽位不足，未改造」并正常结束）。
"""
import json
import logging
import os
import re
import time
from pathlib import Path

from maa.context import Context
from maa.custom_action import CustomAction

import ui_common

log = logging.getLogger("laa.chip_build")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHIP_TARGET_PATH = PROJECT_ROOT / "config" / "chip_target.json"

PANEL_ROI = [170, 95, 960, 470]
TOP_HEADING_ROI = [380, 120, 520, 60]
HEADER_ROI = [0, 20, 760, 60]
PANEL_TITLE_ROI = [0, 55, 760, 75]
BUTTON_ROI = [150, 540, 1000, 170]
LIST_ROI = [0, 80, 1280, 640]
SLOT_ROI = [980, 100, 200, 60]

# 滑动必须落在面板可滚动的选项区（y≈470）且避开选项文字列：
# 用右边缘 x=1100（第 4 列文字右界≈998），否则起点压在选项上会被判成点击、误选别的技能。
SCROLL_X = 1100
SCROLL_BEGIN = (SCROLL_X, 470)
SCROLL_END = (SCROLL_X, 310)

# 筛选面板固定坐标（1280x720，重置并回顶后的实测值）。
# 芯片品质 / 芯片类型 在面板首屏可见，用固定点避免 OCR 误命中半透明面板后的卡片角标。
QUALITY_POINTS = {
    "R1": (471, 206),
    "R2": (640, 207),
    "R3": (809, 207),
    "R4": (977, 207),
    "R5": (471, 264),
}
TYPE_POINTS = {
    "运算芯片": (469, 389),
    "反馈芯片": (469, 445),
    "控制芯片": (639, 388),
    "存储芯片": (807, 388),
    "输入芯片": (977, 389),
}


def _param(argv) -> dict:
    raw = getattr(argv, "custom_action_param", None) or "{}"
    try:
        data = json.loads(raw)
        if isinstance(data, str):
            data = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _ocr(context: Context, image, roi):
    result = None
    for attempt in range(2):
        try:
            result = context.run_recognition(
                "通用_OCR探针",
                image if attempt == 0 else None,
                pipeline_override={
                    "通用_OCR探针": {"recognition": "OCR", "roi": roi, "expected": []}
                },
            )
            break
        except Exception as exc:  # Maa C++ 层偶发抛 0xe06d7363，换实时截图重试一次
            log.warning("OCR 调用异常（第 %d 次）：%s", attempt + 1, exc)
            time.sleep(0.3)
    items = []
    if not result:
        return items
    covered = set()
    candidates = [getattr(result, "best_result", None)]
    candidates.extend(getattr(result, "all_results", None) or [])
    for item in candidates:
        text = getattr(item, "text", None)
        box = getattr(item, "box", None)
        if not text or not box:
            continue
        key = (str(text), tuple(box))
        if key in covered:
            continue
        covered.add(key)
        items.append((str(text), list(box)))
    return items


def _shot(context: Context):
    return context.tasker.controller.post_screencap().wait().get()


def _find_text(context: Context, image, roi, needles, exact=False):
    """在 ROI 内找到任一目标文本，返回其 box（不点击）。"""
    for text, box in _ocr(context, image, roi):
        flat = text.replace(" ", "")
        for needle in needles:
            ok = flat == needle if exact else (needle in flat)
            if ok:
                return box
    return None


def _click_text(context: Context, image, roi, needles, exact=False):
    """在 ROI 内找到任一目标文本并点击，返回命中的文本。"""
    for text, box in _ocr(context, image, roi):
        flat = text.replace(" ", "")
        for needle in needles:
            ok = flat == needle if exact else (needle in flat)
            if ok:
                x = int(box[0]) + int(box[2]) // 2
                y = int(box[1]) + int(box[3]) // 2
                context.tasker.controller.post_click(x, y).wait()
                log.info("点击筛选项「%s」@(%d,%d)", text, x, y)
                return text
    return ""


def _swipe(context: Context, begin, end, duration=400):
    context.tasker.controller.post_swipe(
        int(begin[0]), int(begin[1]), int(end[0]), int(end[1]), int(duration)
    ).wait()


def _color_state(image, box):
    """读取一处选项文字的颜色状态：gold（已选中）/ gray（未选中）/ flash（按压白闪）。

    Maa 截图是 BGR：选中文字≈[64,175,233]（视觉为金色），未选中≈[141,138,138]。
    点按瞬间整块会刷白（≈[224,227,232]），必须等它消退后再判定，否则会把正在
    切换的项误判成未选中而反复重点——重复点击等于把刚勾上的又取消掉。
    判据与阈值已集中在 ui_common.read_option_state，本函数只做委托。
    """
    return ui_common.read_option_state(image, box)


def _selected(image, box):
    return _color_state(image, box) == "gold"


def _stable_state(context: Context, box, tries: int = 8, interval: float = 0.4):
    """连续采样直到脱离按压白闪，返回 gold/gray；一直不稳定则返回 None。"""
    for _ in range(tries):
        state = _color_state(_shot(context), box)
        if state in ("gold", "gray"):
            return state
        time.sleep(interval)
    return None


def _selected_at(context: Context, point, w: int = 70, h: int = 26):
    """按固定坐标采样同一个选中判定，用于 chip 品质/类型这类坐标点击。"""
    box = [int(point[0]) - w // 2, int(point[1]) - h // 2, w, h]
    return _stable_state(context, box)


def _click_point(context: Context, point, label: str, attempts: int = 3) -> bool:
    """坐标点击并等白闪消退后回读选中态；只有稳定为未选中才重点。"""
    box = [int(point[0]) - 35, int(point[1]) - 13, 70, 26]
    for index in range(attempts):
        context.tasker.controller.post_click(int(point[0]), int(point[1])).wait()
        state = _stable_state(context, box)
        if state == "gold":
            log.info("点击筛选面板「%s」@(%d,%d) 已选中", label, int(point[0]), int(point[1]))
            return True
        if state is None:
            log.warning("「%s」选中态读不到，按已点击继续", label)
            return True
        log.warning("「%s」第 %d 次点击未生效，重试", label, index + 1)
    log.error("筛选面板「%s」点击 %d 次仍未选中", label, attempts)
    return False



def _panel_open(context: Context) -> bool:
    """面板标题在 y≈87（HEADER_ROI 之外），用独立标题 ROI 判断。"""
    image = _shot(context)
    texts = " ".join(text for text, _ in _ocr(context, image, PANEL_TITLE_ROI))
    if "筛选范围" in texts:
        return True
    bottom = " ".join(text for text, _ in _ocr(context, image, [150, 540, 1000, 160]))
    return "重置" in bottom and "确定" in bottom


def _verify_text_at(context: Context, box, name: str) -> bool:
    """点前在命中框附近重新认字，确认画面没有把目标挪走。"""
    x, y, w, h = [int(v) for v in box]
    roi = [max(0, x - 30), max(0, y - 12), w + 60, h + 24]
    for text, _ in _ocr(context, _shot(context), roi):
        flat = text.replace(" ", "")
        if flat == name or name.startswith(flat) or flat.startswith(name):
            return True
    return False


def _find_and_click_panel_text(context: Context, name: str, max_steps: int = 14) -> bool:
    """在筛选面板里按名字找选项：回到顶部后逐屏下滑，点中后等白闪消退再复核。

    复核只看点击位置的颜色（点选后面板不滚动），不再按文字重新查找：选中后的
    文字 OCR 常把「震慑」读成「震慨」，按文本找不到就会被当成失败重点，
    反而把刚勾上的项取消掉。
    """
    def find_box(image=None):
        image = image if image is not None else _shot(context)
        for text, box in _ocr(context, image, PANEL_ROI):
            flat = text.replace(" ", "")
            if flat == name:
                return text, box, image
            # OCR 常把「震慑」只回传首字「震」（框仍是整词宽≈41px）：允许单字前缀，
            # 但要求框宽≥30，滤掉滚动中途截到的碎字噪声（这类框只有≈21px）。
            if name.startswith(flat) and (len(flat) >= 2 or int(box[2]) >= 30):
                return text, box, image
            if flat.startswith(name) and len(flat) >= 2:
                return text, box, image
        return None, None, image

    for attempt in range(4):
        if not _scroll_panel_to_top(context):
            log.warning("「%s」查找前未能确认面板回顶，按当前视野继续", name)
        for _ in range(max_steps):
            text, box, image = find_box()
            if box is None:
                _swipe(context, SCROLL_BEGIN, SCROLL_END, 1200)
                time.sleep(0.6)
                continue
            if _color_state(image, box) == "gold":
                log.info("「%s」已是选中态（OCR=%r），跳过点击", name, text)
                return True
            x = int(box[0]) + int(box[2]) // 2
            # OCR 只回传首字、框只有单字宽时，两字词的正中在框右缘
            if int(box[2]) < 30:
                x = int(box[0]) + int(box[2])
            y = int(box[1]) + int(box[3]) // 2
            # 面板有滚动吸附动画，而整屏 OCR 要几秒：命中框取自动画中途的帧，
            # 等到真正点击时画面已经吸到下一档（实测把「震慑」点成了「压力」）。
            # 点前在小窗内重新认字，确认原处仍是目标才点。
            if not _verify_text_at(context, box, name):
                log.warning("「%s」命中框在点击前已移位，重新查找", name)
                time.sleep(0.4)
                continue
            for click_round in range(2):
                context.tasker.controller.post_click(x, y).wait()
                state = _stable_state(context, box)
                if state == "gold":
                    log.info("筛选面板命中「%s」OCR=%r @(%d,%d) 复核选中=True", name, text, x, y)
                    return True
                log.info(
                    "筛选面板命中「%s」OCR=%r @(%d,%d) 第 %d 次点击后状态=%s",
                    name, text, x, y, click_round + 1, state,
                )
                if state != "gray":
                    break
            _swipe(context, SCROLL_BEGIN, SCROLL_END, 1200)
            time.sleep(0.6)
    log.error("筛选面板内「%s」多次点击仍未进入选中态", name)
    return False



def _heading_visible(context: Context) -> bool:
    return _find_text(context, _shot(context), TOP_HEADING_ROI, ["芯片品质"]) is not None


def _scroll_panel_to_top(context: Context, max_steps: int = 14) -> bool:
    """面板会记住上次滚动位置；以「芯片品质」标题回到视野作为回顶判据。

    滑动改用 600ms 慢拖：300ms 的滑动会触发惯性甩动，一甩就跨过整个分区，
    截图落在滚动中途时坐标全错。命中标题后还要静置复看一次，避免把甩动
    过程中的瞬时帧当成已回顶。
    """
    for _ in range(max_steps):
        if _heading_visible(context):
            time.sleep(0.25)
            if _heading_visible(context):
                return True
        _swipe(context, SCROLL_END, SCROLL_BEGIN, 600)
        time.sleep(0.35)
    time.sleep(0.25)
    return _heading_visible(context)


def _reset_panel(context: Context) -> bool:
    """点「重置」并确认品质行回到「全部」选中，避免沿用上一轮残留筛选。"""
    for attempt in range(3):
        _click_text(context, _shot(context), BUTTON_ROI, ["重置"], exact=True)
        time.sleep(0.5)
        if not _scroll_panel_to_top(context):
            log.warning("面板未能滚回顶部，第 %d 次重试", attempt + 1)
            time.sleep(0.4)
            continue
        image = _shot(context)
        box = _find_text(context, image, [150, 150, 1000, 120], ["全部"], exact=True)
        if box is not None and _stable_state(context, box) == "gold":
            log.info("筛选面板已重置（芯片品质=全部 选中）")
            return True
        log.warning("重置后品质行未回到「全部」，第 %d 次重试", attempt + 1)
        time.sleep(0.4)
    log.error("筛选面板重置失败，拒绝沿用残留筛选继续")
    return False



def _load_config_targets() -> list:
    try:
        data = json.loads(CHIP_TARGET_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        values = data.get("targets")
        if isinstance(values, list):
            return [str(item) for item in values if str(item).strip()]
        name = str(data.get("name") or "").strip()
        if name:
            return [name]
    return []


def collect_targets(param: dict) -> list:
    """面板勾选（t_* 键）优先；未显式传 targets 时回退 config/chip_target.json。

    显式传 targets=[]（或空字符串）表示「不建造」，直接返回 []，不再回退配置。
    """
    has_explicit_targets = "targets" in param
    targets = [str(item) for item in (param.get("targets") or []) if str(item).strip()]
    for key, value in param.items():
        if key.startswith("t_") and value:
            targets.append(key[2:])
    deduped = []
    for name in targets:
        if name not in deduped:
            deduped.append(name)
    if deduped:
        return deduped
    if has_explicit_targets:
        return []
    return _load_config_targets()


def collect_levels(param: dict) -> list:
    """面板勾选（q_* 键）优先，其次 levels 默认值；返回空列表表示不加品质条件。"""
    levels = [key[2:] for key, value in param.items() if key.startswith("q_") and value]
    if not levels:
        raw = param.get("levels") or []
        if isinstance(raw, str):
            levels = [raw]
        elif isinstance(raw, (list, tuple)):
            levels = [str(item) for item in raw]
    deduped = []
    for name in levels:
        name = name.strip()
        if name and name not in deduped:
            deduped.append(name)
    return deduped


def _persist_cursor(context: Context, node_name: str, param: dict, cursor: int) -> None:
    """把处理进度写回本节点的 custom_action_param，供下一轮（改造完成后回到筛选）续跑。"""
    new_param = dict(param)
    new_param["cursor"] = int(cursor)
    try:
        context.override_pipeline(
            {node_name: {"action": {"param": {"custom_action_param": new_param}}}}
        )
    except Exception as exc:  # 写回失败不阻断本轮，只是下一轮会从头再来
        log.warning("写回主技能处理进度失败（%s）", exc)


class ChipFilterSelectAction(CustomAction):
    """按筛选面板结构逐项选片；未配置目标时跳过建造段并正常结束。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv)
        targets = collect_targets(param)
        if not targets:
            log.info("未配置任何目标芯片，跳过建造段并正常结束")
            return True
        if not any(key.startswith("t_") for key in param):
            log.info("任务面板未勾选芯片，回退配置文件目标：%s", targets)

        levels = collect_levels(param) or ["全部"]
        chip_type = str(param.get("chip_type") or "全部")
        wear = str(param.get("wear") or "全部")
        try:
            cursor = max(0, int(param.get("cursor") or 0))
        except (TypeError, ValueError):
            cursor = 0

        pairs = [(level, name) for level in levels for name in targets]
        if cursor >= len(pairs):
            log.info("品质 × 主技能已全部处理完（共 %d 组），回退任务结束", len(pairs))
            context.override_next(argv.node_name, ["任务结束"])
            return True

        log.info(
            "目标芯片=%s 品质=%s 类型=%s 佩戴=%s，共 %d 组（品质外层 × 主技能内层），本轮从第 %d 组开始",
            targets, levels, chip_type, wear, len(pairs), cursor + 1,
        )

        skipped = []
        while cursor < len(pairs):
            level, name = pairs[cursor]
            if not _ensure_panel(context):
                return False
            if not _reset_panel(context):
                return False
            if not _apply_filters(context, level, chip_type, wear):
                return False
            if not _find_and_click_panel_text(context, name):
                log.error(
                    "筛选面板未找到主技能「%s」（品质 %s），跳过该组（不顶替其他芯片）",
                    name, level,
                )
                skipped.append(f"{level}×{name}")
                cursor += 1
                continue
            if not _confirm_panel(context):
                return False
            cursor += 1
            image = _shot(context)
            list_text = " ".join(text for text, _ in _ocr(context, image, LIST_ROI))
            if "暂无芯片" in list_text:
                log.error(
                    "品质 %s + 主技能「%s」无候选芯片，跳过该组（继续下一组）", level, name,
                )
                skipped.append(f"{level}×{name}")
                continue
            _persist_cursor(context, argv.node_name, param, cursor)
            point = param.get("card_point") or [170, 175]
            context.tasker.controller.post_click(int(point[0]), int(point[1])).wait()
            log.info(
                "品质 %s + 主技能「%s」命中，点选第一张芯片 @(%d,%d)%s",
                level, name, int(point[0]), int(point[1]),
                f"（本轮跳过 {skipped}）" if skipped else "",
            )
            time.sleep(1.0)
            return True

        _persist_cursor(context, argv.node_name, param, cursor)
        if skipped:
            log.info("本轮跳过 %d 项：%s", len(skipped), skipped)
        image = _shot(context)
        list_text = " ".join(text for text, _ in _ocr(context, image, LIST_ROI))
        if "暂无芯片" in list_text:
            log.info("筛选结果为空 → 交由「暂无芯片」按改造槽占用收尾")
            return False
        _close_panel(context)
        log.info("没有可继续处理的目标芯片，正常结束")
        context.override_next(argv.node_name, ["任务结束"])
        return True


def _ensure_panel(context: Context) -> bool:
    """确保筛选面板已打开；「筛选」按钮偶发首次点击不落地，逐次重点并复核。"""
    if _panel_open(context):
        return True
    for attempt in range(4):
        image = _shot(context)
        if not _click_text(context, image, HEADER_ROI, ["筛选"]):
            log.error("候选列表页未找到「筛选」入口")
            return False
        time.sleep(1.2)
        if _panel_open(context):
            log.info("筛选面板已打开（第 %d 次点击后）", attempt + 1)
            return True
        log.info("等待筛选面板打开（第 %d 次点击后）", attempt + 1)
    log.error("筛选面板未打开（OCR 未见「筛选范围」/重置+确定）")
    return False


def _apply_filters(context: Context, level: str, chip_type: str, wear: str) -> bool:
    """品质/类型用固定坐标，佩戴类型走 OCR 查找面板文字。"""
    if level and level != "全部":
        point = QUALITY_POINTS.get(level)
        if not point:
            log.error("未知的芯片品质 %s（界面仅 R1-R5）", level)
            return False
        if not _click_point(context, point, f"芯片品质 {level}"):
            return False
    if chip_type and chip_type != "全部":
        point = TYPE_POINTS.get(chip_type)
        if not point:
            log.error("未知的芯片类型 %s", chip_type)
            return False
        if not _click_point(context, point, f"芯片类型 {chip_type}"):
            return False
    if wear and wear != "全部":
        if not _find_and_click_panel_text(context, wear):
            log.error("筛选面板未找到佩戴类型 %s", wear)
            return False
    return True


def _confirm_panel(context: Context) -> bool:
    image = _shot(context)
    if os.environ.get("CHIP_SCAN_DEBUG"):
        sel_items = [t for t, b in _ocr(context, image, PANEL_ROI) if _selected(image, b) is True]
        log.info("DEBUG 点确定前面板选中项: %s", sel_items)
    if not _click_text(context, image, BUTTON_ROI, ["确定"], exact=True):
        log.error("筛选面板未找到「确定」")
        return False
    for _ in range(4):
        time.sleep(0.6)
        if not _panel_open(context):
            time.sleep(0.4)
            return True
    log.error("点击「确定」后面板仍未关闭")
    return False


def _close_panel(context: Context) -> None:
    """收尾用：面板还开着就点「确定」关掉，失败不阻断。"""
    try:
        if _panel_open(context):
            _click_text(context, _shot(context), BUTTON_ROI, ["确定"], exact=True)
            time.sleep(0.6)
    except Exception as exc:
        log.warning("关闭筛选面板失败（%s）", exc)



class ChipNoTargetAction(CustomAction):
    """「暂无芯片」收尾：改造槽有占用即常态跳过；0/4 才是真异常。

    mode=slot_guard 时只做槽位预检：已满则记「槽位不足，未改造」并结束本轮。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv)
        roi = param.get("slots_roi") or SLOT_ROI
        cleanup = str(param.get("cleanup_entry") or "任务结束")
        mode = str(param.get("mode") or "no_target")
        image = _shot(context)
        texts = [text for text, _ in _ocr(context, image, roi)]
        joined = " ".join(texts)
        match = re.search(r"(\d{1,2})\s*[/／]\s*([1-9])", joined)
        if not match:
            if mode == "slot_guard":
                log.warning("未能确认改造槽状态（OCR=%r），按有余量继续", joined[:80])
                return True
            log.warning("未能确认改造槽状态（OCR=%r），按常态跳过本次建造", joined[:80])
            return True
        occupied, total = int(match.group(1)), int(match.group(2))
        if mode == "slot_guard":
            if occupied >= total:
                log.info("槽位不足，未改造（可改造 %d/%d），正常结束", occupied, total)
                return False
            log.info("改造槽 %d/%d 仍有余量，继续处理下一项", occupied, total)
            return True
        if occupied <= 0:
            log.error(
                "改造槽 %d/%d 为空且费用闸已放行，仍无候选芯片：按异常判失败"
                "（检查筛选/等级或界面数据未加载）", occupied, total,
            )
            try:
                context.run_task(cleanup)
            except Exception as exc:
                log.error("失败收尾「%s」异常：%s", cleanup, exc)
            return False
        log.info("无可改造芯片，跳过本次建造（可改造 %d/%d）", occupied, total)
        return True
