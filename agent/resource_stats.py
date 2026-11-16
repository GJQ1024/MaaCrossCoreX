# -*- coding: utf-8 -*-
"""资源统计 MVP：主界面顶栏三项资源（体力/金币/粲晶）的 OCR 采集与差值记录。

核心原则：
1. 图标锚定 ROI：先 TemplateMatch 定位资源图标，再以图标边界 + 固定偏移计算 OCR 区域，
   避免不同界面资源条位置漂移导致的固定坐标失效。
2. 识别失败不写 0：OCR 无结果、置信度低于阈值、或数字解析失败时，该资源记 null，
   差值也为 null，绝不把失败当成 0 拉低曲线。
3. 差值口径：after - before，每次任务 run_id 独立配对，不复用上一任务的余量。
4. 存储：JSON Lines，按账号分文件，位于 config/resource_stats/<account>.jsonl。

注册说明：本模块不自行注册到 AgentServer；待 main.py 合并窗口打开后，由调用方执行：
    from resource_stats import ResourceStatsAction
    AgentServer.custom_action("resource_stats")(ResourceStatsAction)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from maa.custom_action import CustomAction
from maa.context import Context

log = logging.getLogger("laa.resource_stats")

SCHEMA_VERSION = 1
CONFIDENCE_THRESHOLD = 0.85  # 低于此置信度的 OCR 结果视为识别失败

# 坐标基准：与全项目一致，统一使用 1280x720 归一化画布（见 docs/coords.md §1）。
# MaaFramework 的 run_recognition 在 pipeline_override 中收到的 roi 按 1280x720 解释，
# 运行时自动映射到实际截图分辨率，因此此处不调用 viewport.scale_roi（那是 1920 基准的遗留换算）。
CANVAS_W, CANVAS_H = 1280, 720

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 顶栏资源定义（1280x720 画布坐标）
# template: 图标模板路径（相对 assets/resource/image）
# ocr_offset: 从图标左上角 (x, y) 到 OCR 区域左上角的偏移 (dx, dy, w, h)，1280x720 画布
# parser: 文本解析函数名
RESOURCES = {
    "ap": {
        "label": "体力",
        "template": "HomeResourceAp.png",
        # 图标右侧紧邻 current/max 文本，如 "201/240"
        "ocr_offset": (35, -4, 120, 33),
        "parser": "parse_ap",
    },
    "coin": {
        "label": "金币",
        "template": "HomeResourceCoin.png",
        "ocr_offset": (37, -4, 133, 33),
        "parser": "parse_int",
    },
    "gem": {
        "label": "粲晶",
        "template": "HomeResourceGem.png",
        "ocr_offset": (35, -4, 107, 33),
        "parser": "parse_int",
    },
}

# 单次任务会话：phase=before 时存读数，phase=after 时取差值
_SESSION: dict[str, Any] = {}
_SESSION_LOCK = threading.Lock()

# 动作实例强引用（与 task_memory 同理）
_INSTANCES: list = []


# ---------- 账号与路径 ----------

def _account_id() -> str:
    """与 task_memory 同款实例来源；缺省 default。"""
    configured = os.environ.get("MAA_INSTANCE_CONFIG")
    if configured:
        return Path(configured).stem
    instance_id = os.environ.get("MFA_INSTANCE_ID", "").strip()
    return instance_id or "default"


def data_dir() -> Path:
    """数据目录：默认 <克隆根>/config/resource_stats，可用 RESOURCE_STATS_DIR 覆盖。"""
    override = os.environ.get("RESOURCE_STATS_DIR")
    return Path(override) if override else PROJECT_ROOT / "config" / "resource_stats"


def jsonl_path(account: str | None = None) -> Path:
    return data_dir() / f"{account or _account_id()}.jsonl"


# ---------- 数字解析 ----------

def parse_int(text: str) -> int | None:
    """解析纯数字串，去除逗号/空格。失败返回 None。"""
    if text is None:
        return None
    normalized = re.sub(r"[\s,，]", "", str(text))
    if not normalized:
        return None
    match = re.fullmatch(r"\d+", normalized)
    return int(match.group(0)) if match else None


def parse_ap(text: str) -> dict | None:
    """解析 'current/max' 格式体力。失败返回 None。"""
    if text is None:
        return None
    normalized = re.sub(r"\s+", "", str(text))
    match = re.fullmatch(r"(\d+)\s*[/／]\s*(\d+)", normalized)
    if not match:
        # 容错：有些截图可能只显示 current
        single = re.fullmatch(r"\d+", normalized)
        if single:
            return {"current": int(single.group(0)), "max": None}
        return None
    return {"current": int(match.group(1)), "max": int(match.group(2))}


_PARSERS = {
    "parse_int": parse_int,
    "parse_ap": parse_ap,
}


# ---------- OCR 读数 ----------

@dataclass
class ResourceReading:
    """单次资源读数。失败时 value 为 None，debug 保留原始 OCR 信息。"""
    resource: str
    label: str
    value: int | dict | None
    raw: str | None = None
    confidence: float | None = None
    icon_box: list | None = None
    ocr_box: list | None = None
    success: bool = False

    def to_dict(self) -> dict:
        d = {
            "resource": self.resource,
            "label": self.label,
            "value": self.value,
            "raw": self.raw,
            "confidence": self.confidence,
            "success": self.success,
        }
        if self.icon_box is not None:
            d["icon_box"] = list(self.icon_box)
        if self.ocr_box is not None:
            d["ocr_box"] = list(self.ocr_box)
        return d


def _box_to_xywh(box) -> tuple[int, int, int, int] | None:
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
    except (TypeError, IndexError, ValueError):
        return None


def _match_icon(context: Context, image, template_name: str) -> tuple[int, int, int, int] | None:
    """TemplateMatch 定位图标，返回 (x, y, w, h) 或 None。"""
    node = f"ResourceIcon_{template_name}"
    override = {
        node: {
            "recognition": "TemplateMatch",
            "template": template_name,
            "roi": [0, 0, CANVAS_W, 120],  # 顶栏区域（1280x720 画布）
            "threshold": 0.7,
        }
    }
    detail = context.run_recognition(node, image, pipeline_override=override)
    result = getattr(detail, "best_result", None) if detail else None
    return _box_to_xywh(getattr(result, "box", None)) if result else None


def _ocr_number(context: Context, image, roi: list[int]) -> tuple[str | None, float | None, list | None]:
    """对指定 ROI 跑 OCR，返回 (text, score, box)。"""
    node = "ResourceOCR"
    override = {
        node: {
            "recognition": "OCR",
            "roi": roi,
            "threshold": 0.2,
        }
    }
    detail = context.run_recognition(node, image, pipeline_override=override)
    items = getattr(detail, "all_results", None) if detail else None
    if not items:
        return None, None, None
    # 取置信度最高的一条
    best = max(items, key=lambda it: float(getattr(it, "score", 0)))
    box = _box_to_xywh(getattr(best, "box", None))
    return str(getattr(best, "text", "")), float(getattr(best, "score", 0)), box


def read_resources(context: Context, image) -> dict[str, ResourceReading]:
    """从截图读取三项资源。每项独立判定成功/失败。"""
    readings: dict[str, ResourceReading] = {}
    for key, spec in RESOURCES.items():
        reading = ResourceReading(resource=key, label=spec["label"], value=None)
        icon_box = _match_icon(context, image, spec["template"])
        if icon_box is None:
            log.debug("资源 %s 图标未匹配到", spec["label"])
            readings[key] = reading
            continue
        reading.icon_box = list(icon_box)
        # 图标锚定 OCR ROI（1280x720 画布坐标，run_recognition 自动映射到实际分辨率）
        ix, iy, iw, ih = icon_box
        dx, dy, ow, oh = spec["ocr_offset"]
        ocr_roi = [ix + dx, iy + dy, ow, oh]
        text, score, ocr_box = _ocr_number(context, image, ocr_roi)
        reading.raw = text
        reading.confidence = score
        reading.ocr_box = ocr_box
        if text is None or score is None or score < CONFIDENCE_THRESHOLD:
            log.info("资源 %s OCR 失败：text=%r score=%s", spec["label"], text, score)
            readings[key] = reading
            continue
        parser = _PARSERS[spec["parser"]]
        value = parser(text)
        if value is None:
            log.info("资源 %s 数字解析失败：text=%r", spec["label"], text)
            readings[key] = reading
            continue
        reading.value = value
        reading.success = True
        readings[key] = reading
    return readings


# ---------- 存储 ----------

def _jsonl_append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


@dataclass
class TaskSnapshot:
    ts: str
    resources: dict[str, ResourceReading]

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "resources": {k: v.to_dict() for k, v in self.resources.items()},
        }


def record_before(account: str, task: str, run_id: str, readings: dict[str, ResourceReading]) -> dict:
    """记录任务前快照，返回写入的记录。"""
    ts = datetime.now().isoformat(timespec="seconds")
    record = {
        "schema": SCHEMA_VERSION,
        "ts": ts,
        "account": account,
        "task": task,
        "run_id": run_id,
        "phase": "before",
        "resources": {k: v.to_dict() for k, v in readings.items()},
    }
    _jsonl_append(jsonl_path(account), record)
    return record


def record_after(account: str, task: str, run_id: str, before: dict, readings: dict[str, ResourceReading]) -> dict:
    """记录任务后快照 + 差值，返回写入的记录。"""
    ts = datetime.now().isoformat(timespec="seconds")
    before_resources = before.get("resources", {})
    delta: dict[str, Any] = {}
    for key in RESOURCES:
        before_val = before_resources.get(key, {}).get("value")
        after_reading = readings.get(key)
        after_val = after_reading.value if after_reading else None
        # 体力的差值取 current
        if key == "ap":
            bv = before_val.get("current") if isinstance(before_val, dict) else None
            av = after_val.get("current") if isinstance(after_val, dict) else None
        else:
            bv = before_val
            av = after_val
        if bv is None or av is None:
            delta[key] = None
        else:
            delta[key] = av - bv
    record = {
        "schema": SCHEMA_VERSION,
        "ts": ts,
        "account": account,
        "task": task,
        "run_id": run_id,
        "phase": "after",
        "resources": {k: v.to_dict() for k, v in readings.items()},
        "delta": delta,
        "before_ts": before.get("ts"),
    }
    _jsonl_append(jsonl_path(account), record)
    return record


def format_summary(task: str, delta: dict[str, Any], before: dict, after: dict) -> str:
    """生成日志摘要行，如：本次 +1240 金币, +3 粲晶, 体力 201→200"""
    parts = []
    for key, spec in RESOURCES.items():
        d = delta.get(key)
        if d is None:
            parts.append(f"{spec['label']} 识别失败")
            continue
        if key == "ap":
            bv = before.get("ap", {}).get("value")
            av = after.get("ap", {}).get("value")
            bc = bv.get("current") if isinstance(bv, dict) else "?"
            ac = av.get("current") if isinstance(av, dict) else "?"
            parts.append(f"{spec['label']} {bc}→{ac}")
        else:
            sign = "+" if d > 0 else ""
            parts.append(f"{spec['label']} {sign}{d}")
    return f"本次 {task}：{'，'.join(parts)}"


# ---------- 自定义动作 ----------

class ResourceStatsAction(CustomAction):
    """资源统计动作。param: {task, phase: 'before'|'after', account?}"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            raw = getattr(argv, "custom_action_param", None) or "{}"
            param = json.loads(raw)
            if isinstance(param, str):
                param = json.loads(param)
            task = str(param.get("task") or "unknown")
            phase = str(param.get("phase") or "before")
            account = param.get("account") or _account_id()

            image = context.tasker.controller.post_screencap().wait().get()
            readings = read_resources(context, image)

            if phase == "before":
                run_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
                record = record_before(account, task, run_id, readings)
                with _SESSION_LOCK:
                    _SESSION[run_id] = record
                log.info("[资源统计] %s 任务前快照已记录 run_id=%s", task, run_id)
                return True

            if phase == "after":
                # 取同任务的最近一条 before；无配对即失败，不写假记录
                with _SESSION_LOCK:
                    before = None
                    for rec in reversed(list(_SESSION.values())):
                        if rec.get("task") == task:
                            before = rec
                            break
                    if before is None:
                        log.error(
                            "[资源统计] 任务 %s 没有可配对的任务前快照，跳过差值计算",
                            task,
                        )
                        return False
                    run_id = before["run_id"]
                    _SESSION.pop(run_id, None)

                record = record_after(account, task, run_id, before, readings)
                summary = format_summary(
                    task, record["delta"],
                    before["resources"], record["resources"],
                )
                log.info("[资源统计] %s", summary)
                return True

            log.error("[资源统计] 未知 phase: %s", phase)
            return False
        except Exception:
            log.exception("[资源统计] 动作执行失败")
            return False


_INSTANCES.append(ResourceStatsAction())
