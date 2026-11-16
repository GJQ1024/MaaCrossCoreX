# -*- coding: utf-8 -*-
"""末日战线（DoomsdayLine）自定义识别：战斗次数与战前队伍战力。

参数与常用 ROI 只引用 `ui_common`（次数 ROI 在 RESOURCE_ROIS["doomsday_attempts"]）。
坐标基准 1280x720；设计见 spec §2.2。
任何异常都按「未命中」处理，不抛出，避免把识别故障当成功。
"""

from __future__ import annotations

import json
import logging
import re

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition

import ui_common

log = logging.getLogger("laa.action")

# 临时 OCR 节点名（run_recognition 内部使用，不落盘）
OCR_NODE = "DoomsdayOCR"

POWER_ROI = [400, 210, 500, 120]
DEFAULT_MIN_POWER = 1000


def _param(argv) -> dict:
    """解析 custom_recognition_param；兼容 override 双重编码（同 ui_mask._param）。"""
    raw = getattr(argv, "custom_recognition_param", None)
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
        if isinstance(data, str):
            data = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _image(context, argv):
    """取当前帧；argv 自带帧优先，取不到返回 None。"""
    image = getattr(argv, "image", None)
    if image is not None:
        return image
    try:
        return context.tasker.controller.post_screencap().wait().get()
    except Exception as exc:
        log.error("末日战线：取帧失败 %s", exc)
        return None


def _ocr_texts(context, image, roi) -> list[str]:
    """ROI 内 OCR，返回全部文本；异常按空结果处理。"""
    try:
        result = context.run_recognition(
            OCR_NODE,
            image,
            pipeline_override={
                OCR_NODE: {
                    "recognition": "OCR",
                    "roi": [int(value) for value in roi],
                    "expected": [],
                }
            },
        )
    except Exception as exc:
        log.error("末日战线：OCR 调用异常 %s", exc)
        return []
    texts = []
    if not result:
        return texts
    candidates = [getattr(result, "best_result", None)]
    candidates.extend(getattr(result, "all_results", None) or [])
    for item in candidates:
        value = getattr(item, "text", None)
        if value is not None:
            texts.append(str(value))
    return texts


@AgentServer.custom_recognition("doomsday_attempts")
class DoomsdayAttemptsRecognition(CustomRecognition):
    """模拟次数：OCR 次数区，parse_count 取 cur/max。

    custom_recognition_param: {"roi": [...], "want": "positive" | "zero"}
    命中返回 AnalyzeResult(box=roi, detail={cur,max})；读不到或不满足返回 None。
    """

    def analyze(self, context, argv):
        param = _param(argv)
        roi = list(param.get("roi") or ui_common.RESOURCE_ROIS["doomsday_attempts"])[:4]
        want = str(param.get("want") or "positive").lower()
        label = str(param.get("label") or "末日战线")
        if want not in ("positive", "zero"):
            log.error("%s次数：未知 want=%r（只支持 positive/zero）", label, want)
            return None
        image = _image(context, argv)
        if image is None:
            return None

        texts = _ocr_texts(context, image, roi)
        parsed = ui_common.parse_count(texts)
        if parsed is None:
            log.error("%s次数：读不到 cur/max（OCR=%r）", label, " ".join(texts)[:80])
            return None

        cur, maximum = parsed
        hit = cur > 0 if want == "positive" else cur == 0
        log.info("%s次数 %d/%d want=%s 命中=%s", label, cur, maximum, want, hit)
        if not hit:
            return None
        return CustomRecognition.AnalyzeResult(
            box=tuple(roi),
            detail={"cur": cur, "max": maximum, "want": want},
        )


@AgentServer.custom_recognition("doomsday_team_power")
class DoomsdayTeamPowerRecognition(CustomRecognition):
    """战前队伍战力：OCR 战区取最大整数（避开「第1战队」的 1）。

    custom_recognition_param: {"roi": [...], "min_power": 1000}
    power >= min_power 命中，返回 AnalyzeResult(box=roi, detail={power})。
    读不到数字 / 低于阈值返回 None（空编队或未编入 → 上游留帧失败，不猜人、不补位）。
    """

    def analyze(self, context, argv):
        param = _param(argv)
        roi = list(param.get("roi") or POWER_ROI)[:4]
        label = str(param.get("label") or "末日战线")
        try:
            min_power = int(param.get("min_power") or DEFAULT_MIN_POWER)
        except (TypeError, ValueError):
            min_power = DEFAULT_MIN_POWER
        image = _image(context, argv)
        if image is None:
            return None

        texts = _ocr_texts(context, image, roi)
        joined = " ".join(texts)
        numbers = [int(value) for value in re.findall(r"\d+", joined)]
        if not numbers:
            log.error("%s战力：ROI 内读不到数字（OCR=%r），按未就绪处理", label, joined[:80])
            return None

        power = max(numbers)
        if power < min_power:
            log.error(
                "%s战力 %d < 阈值 %d（OCR=%r），可能空编队/未编入，不猜人、不补位",
                label, power, min_power, joined[:80],
            )
            return None

        log.info("%s战力 %d >= %d，队伍就绪", label, power, min_power)
        return CustomRecognition.AnalyzeResult(
            box=tuple(roi),
            detail={"power": power, "min_power": min_power},
        )
