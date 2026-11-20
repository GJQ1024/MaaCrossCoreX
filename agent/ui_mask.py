# -*- coding: utf-8 -*-
"""通用遮罩与六类 UI 能力的 Maa 自定义识别/动作注册。

参数、判据、阈值只在 `ui_common.py` 存一份；本文件只做 Maa 适配：
取帧、点击/滑动、复核、留帧、事件日志。

注册清单：
  ui_mask_probe / ui_button_state / ui_confirm_guard / resource_gate（识别）
  ui_mask_close / ui_mask_snapshot / smart_back / ui_swipe /
  ui_confirm_cancel / task_skip（动作）

日志统一 `laa.action`，事件流水 `logs/ui_mask_events.jsonl`。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

import ui_common

import diag

log = logging.getLogger("laa.action")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
MASK_FAIL_DIR = LOG_DIR / "mask_fail"
EVENT_PATH = LOG_DIR / "ui_mask_events.jsonl"

FULL_ROI = [0, 0, 1280, 720]
CONFIRM_ROI = [320, 280, 640, 220]
BACK_TEMPLATE = "StartUp/BackButton.png"

OCR_NODE = "UiMaskOCR"
TPL_NODE = "UiMaskTemplate"
OCR_THRESHOLD = 0.3
TEMPLATE_THRESHOLD = 0.8
# 与 mainstory_pipeline.SWEEP_ENABLED_BRIGHT 同口径，不下调。
SWEEP_ENABLED_BRIGHT = 140
# 与 my_action.VerifiedClickAction.change_threshold 默认值一致。
BACK_CHANGE_THRESHOLD = 5.0
# mainstory _frame_diff 注释口径：diff 明显 >4 视为发生位移。
SWIPE_STABLE_THRESHOLD = 4.0

CANCEL_WORDS = ("取消", "否")
OK_WORDS = ("确定", "确认", "是")


def _param(argv) -> dict:
    """解析节点参数；兼容 override 双重编码（同 my_action.cost_guard 写法）。"""
    raw = getattr(argv, "custom_action_param", None)
    if raw is None:
        raw = getattr(argv, "custom_recognition_param", None)
    if isinstance(raw, dict):
        return raw
    raw = raw or "{}"
    try:
        data = json.loads(raw)
        if isinstance(data, str):
            data = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _shot(context: Context):
    return context.tasker.controller.post_screencap().wait().get()


def _event(category: str, preset: str = "", action: str = "", attempt=None,
           count=None, result: str = "", detail: str = "") -> None:
    """追加一行事件流水；写失败不影响主流程。"""
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "category": category,
        "preset": preset,
        "action": action,
        "attempt": attempt,
        "count": count,
        "result": result,
        "detail": detail,
    }
    try:
        EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_PATH.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("UI 事件写入失败（不影响主流程）：%s", exc)


def _save_frame(context: Context, label: str):
    """失败留帧到 logs/mask_fail/；cv2 优先（同 store_purchase），PIL 兜底。"""
    try:
        MASK_FAIL_DIR.mkdir(parents=True, exist_ok=True)
        image = _shot(context)
        safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", str(label or "frame"))
        path = MASK_FAIL_DIR / f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        try:
            import cv2

            cv2.imwrite(str(path), image)
        except Exception:
            from PIL import Image

            Image.fromarray(image[:, :, ::-1]).save(path)
        return path
    except Exception as exc:  # 存帧失败不影响主流程
        log.warning("存帧失败 %s: %s", label, exc)
        return None


def _box_xywh(box):
    """把 MaaFramework box 统一成 (x, y, w, h)。"""
    if box is None:
        return None
    try:
        x = getattr(box, "x", None)
        y = getattr(box, "y", None)
        w = getattr(box, "width", None)
        h = getattr(box, "height", None)
        if None not in (x, y, w, h):
            return int(x), int(y), int(w), int(h)
        return int(box[0]), int(box[1]), int(box[2]), int(box[3])
    except (TypeError, IndexError, ValueError, AttributeError):
        return None


def _ocr_items(context: Context, image, roi) -> list[tuple[str, tuple]]:
    """ROI 内 OCR，返回 [(text, (x,y,w,h))]；异常按空结果处理。"""
    try:
        result = context.run_recognition(
            OCR_NODE,
            image,
            pipeline_override={
                OCR_NODE: {
                    "recognition": "OCR",
                    "roi": list(roi),
                    "expected": [],
                    "threshold": OCR_THRESHOLD,
                }
            },
        )
    except Exception as exc:
        log.warning("OCR 调用异常：%s", exc)
        return []
    items = []
    if not result:
        return items
    candidates = [getattr(result, "best_result", None)]
    candidates.extend(getattr(result, "all_results", None) or [])
    seen = set()
    for item in candidates:
        text = getattr(item, "text", None)
        box = _box_xywh(getattr(item, "box", None))
        if text is None or box is None:
            continue
        key = (str(text), box)
        if key in seen:
            continue
        seen.add(key)
        items.append((str(text), box))
    return items


def _ocr_first_match(context: Context, image, roi, words, threshold=OCR_THRESHOLD):
    """ROI 内 OCR（阈值由调用方给），返回首个包含 words 的 (text, box, score)。

    与 `_ocr_items` 的区别：这里必须用调用方的阈值（主界面门禁的「补给站」用
    `HOME_GUARD.anchor.ocr_threshold`=0.6，而不是全局 `OCR_THRESHOLD`=0.3），
    并把分数带回日志，便于区分「0.6 命中」与「0.3 命中」。
    """
    try:
        result = context.run_recognition(
            OCR_NODE,
            image,
            pipeline_override={
                OCR_NODE: {
                    "recognition": "OCR",
                    "roi": list(roi),
                    "expected": [],
                    "threshold": float(threshold),
                }
            },
        )
    except Exception as exc:
        log.warning("OCR 调用异常：%s", exc)
        return "", None, 0.0
    if not result:
        return "", None, 0.0
    candidates = [getattr(result, "best_result", None)]
    candidates.extend(getattr(result, "all_results", None) or [])
    for item in candidates:
        text = getattr(item, "text", None)
        box = _box_xywh(getattr(item, "box", None))
        if text is None or box is None:
            continue
        flat = str(text).replace(" ", "")
        if any(str(word) in flat for word in words):
            score = float(getattr(item, "score", 0.0) or 0.0)
            # 阈值在识别调用里已下发；这里再挡一次，避免 all_results 带出低于阈值的候选，
            # 保证 HOME_GUARD.anchor.ocr_threshold 的取值不被静默绕过。
            if score < float(threshold):
                continue
            return str(text), box, score
    return "", None, 0.0


def _template(context: Context, image, template: str, roi, threshold=TEMPLATE_THRESHOLD):
    """模板匹配，返回 (score, box)；无命中 score=0.0。"""
    try:
        result = context.run_recognition(
            TPL_NODE,
            image,
            pipeline_override={
                TPL_NODE: {
                    "recognition": "TemplateMatch",
                    "template": [str(template)],
                    "roi": list(roi),
                    "threshold": float(threshold),
                }
            },
        )
    except Exception as exc:
        log.warning("模板匹配异常：%s", exc)
        return 0.0, None
    best = getattr(result, "best_result", None) if result else None
    if best is None:
        return 0.0, None
    score = float(getattr(best, "score", 0.0) or 0.0)
    return score, _box_xywh(getattr(best, "box", None))


def _find_text_box(items, words, exclude=()):
    """在 OCR 结果里找首个包含 words 且不含 exclude 的框。"""
    for text, box in items:
        flat = text.replace(" ", "")
        if any(token in flat for token in exclude):
            continue
        if any(word in flat for word in words):
            return box, text
    return None, ""


def _dialog_text(context: Context, image, roi) -> str:
    return " ".join(text for text, _ in _ocr_items(context, image, roi))


def _confirm_hit(text: str) -> tuple[bool, str]:
    """二次确认命中：取消/否 + 确定/确认/是 同时出现，且不含白名单任一词。"""
    cancel = any(word in text for word in CANCEL_WORDS)
    confirm = any(word in text for word in OK_WORDS)
    white = next((word for word in ui_common.CONFIRM_WHITELIST if word in text), None)
    if white:
        return False, f"白名单：{white}"
    if cancel and confirm:
        return True, "非白名单二次确认"
    return False, "缺少取消/确定配对"


def _normalize_mask_spec(spec: dict) -> dict:
    """模板类 preset 没写 verify_template 时，用同 roi/模板/0.8 复核消失。"""
    out = dict(spec)
    if out.get("kind") == "template" and not out.get("verify_template"):
        out["verify_template"] = {
            "roi": list(out.get("roi") or FULL_ROI),
            "template": out["template"],
            "threshold": TEMPLATE_THRESHOLD,
        }
    if out.get("verify_absent") and not out.get("verify_roi"):
        out["verify_roi"] = list(out.get("roi") or FULL_ROI)
    return out


def _mask_spec(param: dict, argv) -> tuple[str, dict | None]:
    """preset 优先查 MASK_SPECS；否则按内联写法（spec §1.4 展开稿）组装。"""
    preset = str(param.get("preset") or "").strip()
    if preset:
        spec = ui_common.MASK_SPECS.get(preset)
        if spec is None:
            log.error("遮罩关闭：未登记的 preset %s", preset)
            return preset, None
        return preset, _normalize_mask_spec(spec)

    name = str(param.get("mask") or getattr(argv, "node_name", "") or "mask")
    verify_ocr = param.get("verify_ocr") or {}
    verify_tpl = param.get("verify_template") or {}
    if not isinstance(verify_ocr, dict):
        verify_ocr = {}
    if not isinstance(verify_tpl, dict):
        verify_tpl = {}
    spec = {
        "kind": "template" if (param.get("template") or verify_tpl.get("template")) else "ocr",
        "roi": list(param.get("roi") or getattr(argv, "roi", None) or FULL_ROI),
        "template": param.get("template") or verify_tpl.get("template"),
        "target": param.get("target"),
        "target_offset": param.get("target_offset"),
        "confirm": param.get("confirm"),
        "verify_roi": verify_ocr.get("roi") or param.get("roi") or FULL_ROI,
        "verify_absent": verify_ocr.get("absent") or param.get("verify_absent"),
        "verify_template": verify_tpl or None,
        "retries": int(param.get("retries") or 0),
        "delay": float(param.get("delay") or 1.0),
    }
    return name, _normalize_mask_spec(spec)


def _probe_box(context: Context, image, spec: dict, threshold=TEMPLATE_THRESHOLD):
    """按遮罩配置定位识别框：OCR 命中项或模板命中框。

    模板分支必须同时校验 `score >= threshold`：引擎可能返回低于阈值的
    相似模板框（实测子页 0.5 分），只看「框非空」会把无遮罩的页面当成有遮罩，
    后续 `_resolve_point` 又取不到可点坐标，白耗一轮。存在性判定统一走
    `_probe_preset`，本函数只负责「已确认存在后取框」。
    """
    roi = list(spec.get("roi") or FULL_ROI)
    if spec.get("kind") == "template":
        score, box = _template(context, image, spec.get("template"), roi, float(threshold))
        if box is None or score < float(threshold):
            return None
        return box
    items = _ocr_items(context, image, roi)
    expected = [str(value) for value in (spec.get("expected") or [])]
    for text, box in items:
        if any(token in text for token in expected):
            return box
    return None


def _resolve_point(context: Context, image, spec: dict):
    """确定点击点：confirm 文本框中心 > target 绝对点 > target_offset > 识别框中心。"""
    confirm = spec.get("confirm")
    if confirm:
        roi = list(confirm.get("roi") or FULL_ROI)
        words = [str(value) for value in (confirm.get("expected") or ["确定"])]
        items = _ocr_items(context, image, roi)
        box, text = _find_text_box(items, words, exclude=("上限", "仓库", "是否"))
        if box is None:
            log.error("遮罩[%s] confirm 区未找到按钮（OCR=%r）", spec.get("mask") or "confirm",
                      " ".join(t for t, _ in items)[:60])
            return None
        return int(box[0] + box[2] // 2), int(box[1] + box[3] // 2)
    target = spec.get("target")
    if target:
        return int(target[0]), int(target[1])
    offset = spec.get("target_offset")
    if offset:
        box = _probe_box(context, image, spec)
        if box is None:
            return None
        return int(box[0] + offset[0]), int(box[1] + offset[1])
    if spec.get("kind") == "template":
        score, box = _template(context, image, spec.get("template"), list(spec.get("roi") or FULL_ROI))
        if box is None or score < TEMPLATE_THRESHOLD:
            return None
        return int(box[0] + box[2] // 2), int(box[1] + box[3] // 2)
    box = _probe_box(context, image, spec)
    if box is None:
        return None
    return int(box[0] + box[2] // 2), int(box[1] + box[3] // 2)


def _verify_closed(context: Context, spec: dict) -> tuple[bool, str]:
    """复核遮罩是否消失：verify_absent 全部不在 / 模板分数低于阈值。"""
    absent = [str(value) for value in (spec.get("verify_absent") or [])]
    if absent:
        joined = _dialog_text(context, _shot(context), spec.get("verify_roi") or spec.get("roi") or FULL_ROI)
        remain = [token for token in absent if token in joined]
        if remain:
            return False, f"仍含 {'/'.join(remain)}（OCR={joined[:60]}）"
        return True, "标题已消失"
    vtpl = spec.get("verify_template")
    if vtpl:
        threshold = float(vtpl.get("threshold") or TEMPLATE_THRESHOLD)
        score, box = _template(
            context, _shot(context), vtpl.get("template"),
            list(vtpl.get("roi") or spec.get("roi") or FULL_ROI), threshold,
        )
        if box is not None and score >= threshold:
            return False, f"模板仍命中 score={score:.3f}（阈值 {threshold}）"
        return True, f"模板已消失 score={score:.3f}"
    return True, "未配置复核（纯空白点击）"


def _probe_preset(context: Context, image, preset: str):
    """单种遮罩探测：命中返回 (box, detail)，未命中 box 为 None。"""
    spec = ui_common.MASK_SPECS.get(preset)
    if spec is None:
        log.error("ui_mask_probe：未登记的遮罩 preset %r", preset)
        return None, {"preset": preset, "error": "unregistered"}
    if spec.get("kind") == "template":
        score, box = _template(
            context, image, spec.get("template"), spec.get("roi") or FULL_ROI, TEMPLATE_THRESHOLD
        )
        detail = {"preset": preset, "kind": "template", "score": round(score, 3)}
        if box is None or score < TEMPLATE_THRESHOLD:
            return None, detail
        return box, detail
    items = _ocr_items(context, image, spec.get("roi") or FULL_ROI)
    expected = [str(value) for value in (spec.get("expected") or [])]
    for text, box in items:
        if any(token in text for token in expected):
            return box, {"preset": preset, "kind": "ocr", "text": text}
    return None, {"preset": preset, "kind": "ocr"}


def _residual_after_close(context: Context) -> str:
    """关掉一个遮罩后复核：新截一帧按 any 再探，返回仍命中的 preset 名；一屏干净返回 "none"。

    这是「点完之后怎么确认真的没了」的判定：关掉当前遮罩不等于这一屏干净，
    还要看有没有第二种遮罩（剩下的交给调用方那一圈 [JumpBack]通用-遮罩处理 继续关）。
    """
    try:
        image = _shot(context)
    except Exception as exc:  # noqa: BLE001
        return "shot_error:%s" % exc.__class__.__name__
    for item in ui_common.MASK_ANY_ORDER:
        box, _detail = _probe_preset(context, image, item)
        if box is not None:
            return item
    return "none"


@AgentServer.custom_recognition("ui_mask_probe")
class UiMaskProbeRecognition(CustomRecognition):
    """按 preset 识别遮罩是否存在：OCR 文本命中或模板命中即返回识别框。

    preset="any" 时按 ui_common.MASK_ANY_ORDER 逐个探测，命中即返回，
    供 通用-遮罩处理 做识别闸门（DirectHit 恒命中会在 JumpBack 链里死循环）。
    """

    def analyze(self, context, argv):
        param = _param(argv)
        preset = str(param.get("mask") or param.get("preset") or "").strip()
        image = getattr(argv, "image", None)
        if image is None:
            image = _shot(context)
        if preset == "any":
            order = ui_common.MASK_ANY_ORDER
        else:
            order = [preset]
        for item in order:
            box, detail = _probe_preset(context, image, item)
            if box is None:
                continue
            log.info("遮罩探测[%s] 命中 %s", item, detail)
            _event("mask", item, "probe", None, None, "hit", str(detail)[:120])
            return CustomRecognition.AnalyzeResult(box=box, detail=detail)
        return None


def _close_mask(context: Context, preset: str, spec: dict) -> tuple[bool, str]:
    """按遮罩配置点击并复核消失；ui_mask_close 与 ensure_main 共用这一份实现。

    返回 (是否关掉, 最后一次复核读数说明)；不在这里留帧，由调用方决定证据口径。
    """
    retries = max(0, int(spec.get("retries") or 0))
    delay = float(spec.get("delay") or 1.0)
    attempts = 1 + retries
    detail = "未执行点击"
    for attempt in range(1, attempts + 1):
        image = _shot(context)
        point = _resolve_point(context, image, spec)
        if point is None:
            log.error("遮罩[%s] 第%d次无可用点击坐标", preset, attempt)
            _event("mask", preset, "click", attempt, None, "failed", "无点击坐标")
            return False, "无点击坐标"
        context.tasker.controller.post_click(point[0], point[1]).wait()
        log.info("遮罩[%s] 第%d次点击 @(%d,%d)", preset, attempt, point[0], point[1])
        time.sleep(delay)
        ok, detail = _verify_closed(context, spec)
        if ok:
            residual = _residual_after_close(context)
            log.info("遮罩[%s] 已关闭（第%d次）：%s；残余 %s", preset, attempt, detail, residual)
            _event(
                "mask", preset, "click", attempt, None, "closed",
                f"point={point} {detail} residual={residual}",
            )
            return True, detail
        log.warning("遮罩[%s] 第%d次未关闭：%s", preset, attempt, detail)
        _event("mask", preset, "click", attempt, None, "retry", f"point={point} {detail}")
    return False, detail


@AgentServer.custom_action("ui_mask_close")
class UiMaskCloseAction(CustomAction):
    """关闭遮罩并复核消失；全部失败留帧 + log.error，返回 False。

    custom_action_param: {"preset": "item_popup"} 或内联
    {"mask": "...", "target": [...], "verify_ocr": {...}, "verify_template": {...}}。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv)
        preset, spec = _mask_spec(param, argv)
        if spec is None:
            return False
        if spec.get("action") == "wait_login":
            log.error("遮罩[%s] 属登录恢复（禁止点取消），不在 ui_mask_close 内点击", preset)
            return False

        attempts = 1 + max(0, int(spec.get("retries") or 0))
        ok, detail = _close_mask(context, preset, spec)
        if ok:
            return True

        path = _save_frame(context, preset)
        log.error("遮罩[%s] 关闭失败，已留帧 %s", preset, path)
        _event("mask", preset, "click", attempts, None, "failed", f"留帧 {path}")
        diag.fail("遮罩关闭", actual="第 %d 次点击后复核仍未关闭；最后读数 %s" % (attempts, detail),
                  expect="关闭后复核读数低于阈值", frame=str(path), step="遮罩[%s]" % preset)
        return False


@AgentServer.custom_action("ui_mask_snapshot")
class UiMaskSnapshotAction(CustomAction):
    """只留证据：抓帧存 logs/mask_fail/，写事件，返回 True（不改变任务结果）。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv)
        label = str(param.get("label") or "mask_fail")
        reason = str(param.get("reason") or "")
        path = _save_frame(context, label)
        log.warning("遮罩留帧[%s]：%s -> %s", label, reason, path)
        _event("mask", label, "snapshot", None, None, "saved" if path else "failed", reason)
        return True


@AgentServer.custom_action("smart_back")
class SmartBackAction(CustomAction):
    """按 BACK_TABLE 查表返回；个人面板只点空白，禁止 BACK（会退出游戏）。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv)
        page = str(param.get("page") or "").strip()
        image = _shot(context)
        joined = _dialog_text(context, image, FULL_ROI)

        if page:
            entry = next((item for item in ui_common.BACK_TABLE if item["id"] == page), None)
            if entry is None:
                log.error("smart_back：未登记的页面 %r", page)
                return False
            return self._execute(context, entry)

        for entry in ui_common.BACK_TABLE:
            anchors = entry.get("anchor") or []
            if not anchors:
                continue
            # 任一锚点命中即按该页处理：漏检会走到 BACK，对个人面板是禁止动作。
            if any(token in joined for token in anchors):
                log.info("返回查表命中页面：%s", entry["id"])
                return self._execute(context, entry)
        return self._back_key(context)

    def _execute(self, context: Context, entry: dict) -> bool:
        page_id = entry["id"]
        action = entry["action"]
        if action == "click_blank":
            point = entry["point"]
            context.tasker.controller.post_click(int(point[0]), int(point[1])).wait()
            log.info("返回[%s] 点空白 @(%d,%d)，禁止 BACK", page_id, point[0], point[1])
            time.sleep(0.8)
            after = _dialog_text(context, _shot(context), FULL_ROI)
            if not any(token in after for token in entry.get("anchor") or []):
                _event("back", page_id, action, None, None, "closed", "面板已关闭")
                return True
            log.error("返回[%s] 点空白后面板仍在，不改用 BACK", page_id)
            _event("back", page_id, action, None, None, "failed", "面板未关闭且不改用 BACK")
            return False

        if action == "click_text":
            items = _ocr_items(context, _shot(context), FULL_ROI)
            box, text = _find_text_box(items, [entry["text"]])
            if box is None:
                log.error("返回[%s] 未找到文本「%s」", page_id, entry["text"])
                _event("back", page_id, action, None, None, "failed", f"未找到{entry['text']}")
                return False
            x = box[0] + box[2] // 2
            y = box[1] + box[3] // 2
            context.tasker.controller.post_click(x, y).wait()
            log.info("返回[%s] 点击「%s」@(%d,%d)", page_id, text, x, y)
            time.sleep(0.8)
            after = _dialog_text(context, _shot(context), FULL_ROI)
            gone = not any(token in after for token in entry.get("anchor") or [])
            _event("back", page_id, action, None, None, "closed" if gone else "failed",
                   f"点击「{text}」")
            return gone

        if action == "click_back_template":
            image = _shot(context)
            score, box = _template(context, image, BACK_TEMPLATE, FULL_ROI)
            if box is None or score < TEMPLATE_THRESHOLD:
                log.error("返回[%s] 未命中返回模板 %s（score=%.3f）", page_id, BACK_TEMPLATE, score)
                _event("back", page_id, action, None, None, "failed", f"模板 score={score:.3f}")
                return False
            x = box[0] + box[2] // 2
            y = box[1] + box[3] // 2
            context.tasker.controller.post_click(x, y).wait()
            log.info("返回[%s] 点击返回模板 @(%d,%d)", page_id, x, y)
            time.sleep(1.0)
            after_score, _after_box = _template(context, _shot(context), BACK_TEMPLATE, FULL_ROI)
            ok = after_score < TEMPLATE_THRESHOLD
            _event("back", page_id, action, None, None, "closed" if ok else "failed",
                   f"点击后 score={after_score:.3f}")
            return ok

        if action == "keyevent_back":
            return self._back_key(context)

        log.error("smart_back：未实现的动作 %r（page=%s）", action, page_id)
        return False

    def _back_key(self, context: Context) -> bool:
        """无锚点兜底 KEYCODE_BACK；点后必须复核画面变化，不变判失败。"""
        before = _shot(context)
        context.tasker.controller.post_press_key(4).wait()
        time.sleep(0.8)
        diff = ui_common.frame_diff(before, _shot(context))
        ok = diff >= BACK_CHANGE_THRESHOLD
        log.warning("返回[sub_panel] BACK 后画面差 %.2f（阈值 %.1f）", diff, BACK_CHANGE_THRESHOLD)
        _event("back", "sub_panel", "keyevent_back", None, None,
               "closed" if ok else "failed", f"画面差 {diff:.2f}")
        return ok


@AgentServer.custom_action("ui_swipe")
class UiSwipeAction(CustomAction):
    """按 SWIPE_PRESETS 执行滑动；scroll_top 连续两帧稳定即停。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv)
        preset = str(param.get("preset") or "slow_left")
        spec = ui_common.SWIPE_PRESETS.get(preset)
        if spec is None:
            log.error("ui_swipe：未登记的 preset %r", preset)
            return False

        begin = spec["begin"]
        end = spec["end"]
        duration = int(spec["duration"])
        repeat = max(1, int(spec.get("repeat") or 1))
        stable_stop = bool(spec.get("stop_on_stable"))
        stable_hits = 0
        count = 0
        for index in range(repeat):
            before = _shot(context)
            context.tasker.controller.post_swipe(
                int(begin[0]), int(begin[1]), int(end[0]), int(end[1]), duration
            ).wait()
            time.sleep(0.8)
            diff = ui_common.frame_diff(before, _shot(context))
            count += 1
            stable = diff < SWIPE_STABLE_THRESHOLD
            log.info("滑动[%s] 第%d次 画面差 %.2f 稳定=%s", preset, count, diff, stable)
            _event("swipe", preset, "swipe", index + 1, count,
                   "stable" if stable else "moved", f"diff={diff:.2f}")
            if stable_stop:
                stable_hits = stable_hits + 1 if stable else 0
                if stable_hits >= 2:
                    log.info("滑动[%s] 连续两帧稳定，提前停止（共%d次）", preset, count)
                    break
        log.info("滑动[%s] 结束，共%d次", preset, count)
        return True


@AgentServer.custom_recognition("ui_button_state")
class UiButtonStateRecognition(CustomRecognition):
    """按钮可点/灰态：bright 模式按亮度阈值，color 模式按选项通道差。"""

    def analyze(self, context, argv):
        param = _param(argv)
        preset = str(param.get("preset") or "")
        mode = str(param.get("mode") or "bright").lower()
        want = str(param.get("want") or "enabled").lower()
        image = getattr(argv, "image", None)
        if image is None:
            image = _shot(context)

        point = param.get("point")
        roi = param.get("roi")
        if roi is None and point is not None:
            x, y = int(point[0]), int(point[1])
            roi = [x - 35, y - 13, 70, 26]
        if roi is None:
            log.error("ui_button_state：缺少 roi/point（preset=%s），无法判态", preset)
            return None
        roi = [int(value) for value in roi]

        brightness = ui_common.mean_brightness(image, roi)
        threshold = None
        if mode == "color":
            sample = list(point) if point is not None else [
                roi[0] + roi[2] // 2, roi[1] + roi[3] // 2,
            ]
            state = ui_common.read_option_state(image, sample)
            if want == "disabled":
                hit = state == "gray"
            else:
                hit = state == "gold"
        else:
            threshold = float(param.get("threshold") or SWEEP_ENABLED_BRIGHT)
            state = "bright" if brightness >= threshold else "gray"
            if want == "disabled":
                hit = state == "gray"
            else:
                hit = state == "bright"

        detail = {
            "preset": preset,
            "mode": mode,
            "state": state,
            "brightness": round(brightness, 1),
            "threshold": threshold,
            "want": want,
        }
        log.info(
            "按钮判态[%s] mode=%s state=%s brightness=%.1f threshold=%s want=%s",
            preset, mode, state, brightness, threshold, want,
        )
        _event("state", preset, "probe", None, None, str(state),
               f"brightness={brightness:.1f} threshold={threshold}")
        if not hit:
            return None
        return CustomRecognition.AnalyzeResult(box=tuple(roi), detail=detail)


@AgentServer.custom_recognition("ui_confirm_guard")
class UiConfirmGuardRecognition(CustomRecognition):
    """二次确认框守卫：取消/否 + 确定/确认/是 同时出现，且不含白名单词。"""

    def analyze(self, context, argv):
        image = getattr(argv, "image", None)
        if image is None:
            image = _shot(context)
        text = _dialog_text(context, image, CONFIRM_ROI)
        hit, reason = _confirm_hit(text)
        if not hit:
            return None
        log.warning("二次确认框命中（%s）：%s", reason, text[:80])
        _event("confirm", "dialog", "guard", None, None, "hit", text[:160])
        return CustomRecognition.AnalyzeResult(
            box=tuple(CONFIRM_ROI),
            detail={"dialog_text": text, "reason": reason},
        )


@AgentServer.custom_action("ui_confirm_cancel")
class UiConfirmCancelAction(CustomAction):
    """白名单外的二次确认一律点「取消」，点后复核对话框消失。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        box = _box_xywh(getattr(argv, "box", None))
        roi = list(box) if box else list(CONFIRM_ROI)
        items = _ocr_items(context, _shot(context), roi)
        target = None
        for text, item_box in items:
            flat = text.replace(" ", "")
            # 跳过「是否…」这类对话框正文，避免点到描述文字。
            if "是否" in flat:
                continue
            if "取消" in flat or flat in ("否", "不要"):
                target = item_box
                break
        if target is None:
            path = _save_frame(context, "confirm_no_cancel")
            log.error("二次确认取消：识别框内未找到「取消/否」，已留帧 %s", path)
            _event("confirm", "dialog", "cancel", None, None, "failed", f"未找到取消按钮，留帧 {path}")
            return False

        x = target[0] + target[2] // 2
        y = target[1] + target[3] // 2
        context.tasker.controller.post_click(x, y).wait()
        log.warning("二次确认默认取消：点击「取消」@(%d,%d)", x, y)
        time.sleep(0.8)

        after = _dialog_text(context, _shot(context), roi)
        still_hit, reason = _confirm_hit(after)
        if still_hit:
            path = _save_frame(context, "confirm_failed")
            log.error("二次确认取消后对话框仍在（%s），已留帧 %s", reason, path)
            _event("confirm", "dialog", "cancel", None, None, "failed", f"{reason}，留帧 {path}")
            return False
        _event("confirm", "dialog", "cancel", None, None, "closed", reason)
        return True


@AgentServer.custom_recognition("resource_gate")
class ResourceGateRecognition(CustomRecognition):
    """体力/次数闸：OCR RESOURCE_ROIS，cur >= need 命中，否则未命中。"""

    def analyze(self, context, argv):
        param = _param(argv)
        preset = str(param.get("preset") or "")
        roi = ui_common.RESOURCE_ROIS.get(preset)
        if roi is None:
            log.error("resource_gate：未登记的资源 preset %r", preset)
            return None
        try:
            need = int(param.get("need") or 1)
        except (TypeError, ValueError):
            need = 1

        image = getattr(argv, "image", None)
        if image is None:
            image = _shot(context)
        joined = _dialog_text(context, image, roi)
        parsed = ui_common.parse_count(joined)
        if parsed is None:
            log.error("资源[%s] 读不到 cur/max（OCR=%r）", preset, joined[:60])
            _event("resource", preset, "gate", None, None, "unreadable", joined[:120])
            return None

        cur, maximum = parsed
        enough = cur >= need
        log.info("资源[%s] %d/%d need=%d 足够=%s", preset, cur, maximum, need, enough)
        _event("resource", preset, "gate", None, cur,
               "pass" if enough else "skip", f"{cur}/{maximum} need={need}")
        if not enough:
            return None
        return CustomRecognition.AnalyzeResult(
            box=tuple(roi),
            detail={"preset": preset, "cur": cur, "max": maximum, "need": need},
        )


@AgentServer.custom_action("task_skip")
class TaskSkipAction(CustomAction):
    """资源不足时终端跳过：只写 WARNING + 事件，不购买、不兑换。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        param = _param(argv)
        reason = str(param.get("reason") or "体力/次数不足，跳过并记录；不购买、不兑换")
        log.warning("任务跳过（不购买、不兑换）：%s", reason)
        _event("resource", str(param.get("preset") or ""), "skip", None, None, "skip", reason)
        return True


def _guard_overlay(context: Context, image):
    """顶层遮罩判据：按 blocker_presets 逐个探测，命中即返回 (preset, detail)。

    主界面的 anchor（右下「补给站」）与顶栏资源图标都在底栏/顶栏，付费/公告类模态弹窗
    只盖中央，两者同帧可见并不等于无弹窗。这里复用通用遮罩判据做一次同帧探测：
    命中任一遮罩就说明页面顶层还有东西，判 not_main，由恢复阶梯关闭后重判。
    """
    spec = ui_common.HOME_GUARD
    for preset in spec.get("blocker_presets") or ui_common.MASK_ANY_ORDER:
        if preset not in ui_common.MASK_SPECS:
            continue
        box, detail = _probe_preset(context, image, preset)
        if box is not None:
            return preset, detail
    return "", {}


def _guard_main_state(context: Context, image):
    """主界面判据：右下「补给站」OCR 命中 且 顶栏至少一个资源图标命中 且 无顶层遮罩。

    单看 anchor/图标都可能误判（订单库/基地会保留部分顶栏元素；模态弹窗也只盖中央），
    故三者取与；返回值第二项是给日志/事件用的读数明细，失败时直接进 diag.fail。
    """
    spec = ui_common.HOME_GUARD
    anchor = spec["anchor"]
    threshold = float(anchor.get("ocr_threshold") or OCR_THRESHOLD)
    anchor_text, _box, anchor_score = _ocr_first_match(
        context, image, anchor["roi"], anchor["expected"], threshold
    )
    icons = {}
    for template in spec["resource_templates"]:
        score, _box = _template(context, image, template, spec["resource_roi"],
                                spec["resource_threshold"])
        icons[template] = round(score, 3)
    icon_ok = any(score >= spec["resource_threshold"] for score in icons.values())
    detail = {
        "anchor": anchor_text,
        "anchor_ok": bool(anchor_text),
        "anchor_score": round(anchor_score, 3),
        "anchor_threshold": threshold,
        "icons": icons,
        "icon_ok": icon_ok,
    }
    if not (anchor_text and icon_ok):
        return False, detail

    overlay, overlay_detail = _guard_overlay(context, image)
    detail["overlay"] = overlay
    if overlay:
        detail["overlay_detail"] = overlay_detail
    return not overlay, detail


def _guard_clock_box(context: Context, image):
    """待机页时钟：与 通用-待机唤醒 同 roi/正则/阈值，命中返回识别框。"""
    spec = ui_common.HOME_GUARD["standby"]
    try:
        result = context.run_recognition(
            OCR_NODE,
            image,
            pipeline_override={
                OCR_NODE: {
                    "recognition": "OCR",
                    "roi": list(spec["roi"]),
                    "expected": [spec["pattern"]],
                    "threshold": float(spec["threshold"]),
                }
            },
        )
    except Exception as exc:
        log.warning("主界面门禁：待机时钟 OCR 异常 %s", exc)
        return None
    best = getattr(result, "best_result", None) if result else None
    return _box_xywh(getattr(best, "box", None)) if best is not None else None


@AgentServer.custom_action("ensure_main")
class EnsureMainAction(CustomAction):
    """任务入口主界面门禁：一次性确认 + 有界自救，替代 JumpBack 前置的 next 重评。

    背景（2026-11-17）：芯片_资源前/基建_资源前 是 DirectHit 必中节点，父节点末尾
    [JumpBack]子任务_进入首页 永远走不到；上次把 JumpBack 前置后回跳会重评父节点
    next，首页标志命中即死循环（701 次空转 10 分钟）。改为本动作内闭环：
      每轮先判主界面；否则按 待机点时钟 → 关遮罩 → 取消二次确认 → 点首页按钮
      → 点左上返回箭头 自救一轮；最多 max_rounds 轮。
    绝不盲按 BACK（主界面按 BACK 会退到 B 站游戏中心）；仍失败则留帧 + diag.fail
    + 跑收尾任务后返回 False，外层 error_handling 保持到任务结束（判失败）。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        spec = ui_common.HOME_GUARD
        param = _param(argv)
        label = str(param.get("label") or getattr(argv, "node_name", "") or "ensure_main")
        cleanup = str(param.get("cleanup_entry") or "").strip()
        try:
            max_rounds = max(1, int(param.get("max_rounds") or spec["max_rounds"]))
        except (TypeError, ValueError):
            max_rounds = int(spec["max_rounds"])
        step_wait = float(spec["step_wait"])
        threshold = float(spec["template_threshold"])

        log.info("主界面门禁[%s] 开始：最多 %d 轮", label, max_rounds)
        detail = {}
        for round_no in range(1, max_rounds + 1):
            image = _shot(context)
            ok, detail = _guard_main_state(context, image)
            log.info("主界面门禁[%s] 第%d轮判据：%s", label, round_no, detail)
            _event("guard", label, "probe", round_no, None,
                   "main" if ok else "not_main", str(detail)[:180])
            if ok:
                log.info("主界面门禁[%s] 已确认在主界面（第%d轮）", label, round_no)
                return True

            clock = _guard_clock_box(context, image)
            if clock is not None:
                x, y = clock[0] + clock[2] // 2, clock[1] + clock[3] // 2
                context.tasker.controller.post_click(x, y).wait()
                log.info("主界面门禁[%s] 待机页命中，点时钟 @(%d,%d)", label, x, y)
                _event("guard", label, "standby_clock", round_no, None, "click", f"@({x},{y})")
                time.sleep(step_wait)
                continue

            mask = None
            for preset_name in ui_common.MASK_ANY_ORDER:
                if preset_name not in ui_common.MASK_SPECS:
                    continue
                # 与 _guard_overlay 同一口径：存在性只看 _probe_preset（内部校验
                # score >= TEMPLATE_THRESHOLD），不再用只取框的 _probe_box。
                box, _probe_detail = _probe_preset(context, image, preset_name)
                if box is not None:
                    mask = (preset_name, _normalize_mask_spec(ui_common.MASK_SPECS[preset_name]))
                    break
            if mask is not None:
                closed, mask_detail = _close_mask(context, mask[0], mask[1])
                log.info("主界面门禁[%s] 遮罩[%s] 关闭=%s（%s）",
                         label, mask[0], closed, mask_detail)
                _event("guard", label, "mask", round_no, None,
                       "closed" if closed else "failed", f"{mask[0]} {mask_detail}"[:160])
                time.sleep(step_wait)
                continue

            dialog = _dialog_text(context, image, CONFIRM_ROI)
            hit, reason = _confirm_hit(dialog)
            if hit:
                items = _ocr_items(context, image, CONFIRM_ROI)
                cancel_box = None
                for text, box in items:
                    flat = text.replace(" ", "")
                    if "是否" in flat:
                        continue
                    if "取消" in flat or flat in ("否", "不要"):
                        cancel_box = box
                        break
                if cancel_box is not None:
                    x = cancel_box[0] + cancel_box[2] // 2
                    y = cancel_box[1] + cancel_box[3] // 2
                    context.tasker.controller.post_click(x, y).wait()
                    log.warning("主界面门禁[%s] 二次确认默认取消 @(%d,%d)（%s）",
                                label, x, y, reason)
                    _event("guard", label, "confirm_cancel", round_no, None, "click",
                           f"@({x},{y}) {reason}")
                    time.sleep(step_wait)
                    continue

            navigated = False
            for template in spec["home_templates"]:
                score, box = _template(context, image, template, spec["home_roi"], threshold)
                if box is None or score < threshold:
                    continue
                x, y = box[0] + box[2] // 2, box[1] + box[3] // 2
                context.tasker.controller.post_click(x, y).wait()
                log.info("主界面门禁[%s] 点首页按钮[%s] score=%.3f @(%d,%d)",
                         label, template, score, x, y)
                _event("guard", label, "home_button", round_no, None, "click",
                       f"{template} score={score:.3f} @({x},{y})")
                time.sleep(step_wait)
                navigated = True
                break
            if navigated:
                continue

            score, box = _template(context, image, spec["back_template"],
                                   spec["back_roi"], threshold)
            if box is not None and score >= threshold:
                x, y = box[0] + box[2] // 2, box[1] + box[3] // 2
                context.tasker.controller.post_click(x, y).wait()
                log.info("主界面门禁[%s] 点左上返回箭头 score=%.3f @(%d,%d)",
                         label, score, x, y)
                _event("guard", label, "back_arrow", round_no, None, "click",
                       f"score={score:.3f} @({x},{y})")
                time.sleep(step_wait)
                continue

            # 无任何可用锚点：不盲按 BACK，只等一帧再看（可能是转场/加载中）
            log.warning("主界面门禁[%s] 第%d轮无可点锚点，等待 %.1fs 后复看",
                        label, round_no, step_wait)
            _event("guard", label, "wait", round_no, None, "no_anchor", "不盲按 BACK")
            time.sleep(step_wait)

        path = _save_frame(context, label + "_未回主界面")
        try:
            log.error("主界面门禁[%s] %d 轮后仍未回到主界面，留帧 %s", label, max_rounds, path)
            _event("guard", label, "probe", max_rounds, None, "failed", f"留帧 {path}")
            diag.fail("进入任务前未回到主界面", actual=str(detail),
                      expect="补给站 OCR（≥HOME_GUARD.anchor.ocr_threshold）+ 顶栏资源图标 + 无顶层遮罩同帧命中",
                      frame=str(path), step=label)
        except Exception as exc:  # noqa: BLE001 诊断写入异常不得吞掉失败
            log.error("主界面门禁[%s] 失败路径记录异常（仍判失败）：%s", label, exc)
        finally:
            if cleanup:
                try:
                    context.run_task(cleanup)
                except Exception as exc:  # noqa: BLE001
                    log.error("主界面门禁[%s] 收尾「%s」异常：%s", label, cleanup, exc)
        return False
