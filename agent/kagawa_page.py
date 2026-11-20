# -*- coding: utf-8 -*-
"""香川大典推关自定义：按卡面状态选下一关 + 卡面条带判定翻页。

要点：
- 卡片横向位置每帧从「右下角深蓝色对勾条」实测得到，不写死卡坐标。
  实测列表一次左滑约 357px、卡距 343px，偏移会逐页累积（5 页后约 70px），
  固定 ROI 到后面必然错位，01~04 会被读成 1/None/None/0 这类结果。
- 选关只看卡面状态：金色星标 或 蓝条上的金色对勾 = 已通关；白色挂锁 = 未解锁。
  普通模式的 01 是剧情关，没有星、只有金勾（实测 star=0 / check=271），只数星会把它当可打。
  页内从左到右第一张「未通关且未锁定」的卡就是下一关（列表本身按关卡升序），
  不再用卡号 OCR 推断起点；OCR 只用来拦 09（卡号=9 或卡名含临界/挑战）。
  09 拦截按 fail-closed：卡号与卡名都读不出来时宁可不点，按保守出口收尾，不凭猜测下点。
  锁定的关卡一律不点，所以 08 打完后不会把 09（临界挑战）当可打。
- 翻页是否生效：比较「卡面条带」平均绝对差（MAD），阈值 5.0。
  实测真翻页 13~35、同页静止 0.05（整帧平均亮度不再参与判断）。
- 是否到末页：左滑后条带无变化即认为到底；末页若只剩锁定关卡同样正常收尾。
"""

import json
import logging
import os
import re
import time
from pathlib import Path

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

import ui_common
from ui_mask import _shot

log = logging.getLogger("kagawa_page")

CARD_PITCH = 343          # 卡距（实测 33→376→719→1062）
BAR_TO_CARD_LEFT = 247    # 卡左缘 = 蓝条右缘 - 247
NUM_OFF = (18, 592, 64, 66)     # 卡号大数字，相对卡左缘（仅 OCR 日志/指定起点用）
NAME_OFF = (70, 583, 230, 44)   # 关卡名（数字右侧），相对卡左缘；只用来识别 09 临界挑战
STAR_OFF = (88, 626, 52, 26)    # 三颗星，相对卡左缘
ICON_OFF = (198, 634, 46, 32)   # 蓝条上的金勾/白锁，相对卡左缘
LAST_NORMAL_STAGE = 8           # 08 是困难最后一关，09「临界挑战」不是普通推关关卡
CLICK_DX, CLICK_Y = 110, 600
STRIP_Y0, STRIP_Y1 = 560, 715
GOLD_MIN = 25             # 金星像素：已通关实测 291~295，未通关 0
CHECK_MIN = 50            # 金勾像素：已通关实测 78~271（剧情关仅金勾无星），未通关 0
WHITE_MIN = 150           # 白锁像素：锁定实测 322~324，空条（未通关未锁定）80 左右
PAGE_DIFF = 5.0           # 卡面条带 MAD：真翻页 13~35，同页 0.05
MAX_SCREENS = 10          # 单轮扫描最多翻 10 屏，超出按失败（不再静默当全部通关）
MIN_CLICK_X = 20          # 卡面基本在屏外则不点

RIGHT = ((215, 635), (1095, 635))
LEFT = ((1095, 635), (765, 635))

_STATE = {"page": 0, "diff": 0.0, "strip": None, "at_end": False,
          "found": False, "last_num": None, "reason": None, "no_target": False}


def _swipe(context, pair, wait=1.0):
    context.tasker.controller.post_swipe(pair[0][0], pair[0][1], pair[1][0], pair[1][1], 650).wait()
    time.sleep(wait)


def _strip(image):
    """卡面条带灰度（y 560~715），作为「这一页」的指纹。"""
    return image[STRIP_Y0:STRIP_Y1, :, :].mean(axis=2)


def _mad(a, b) -> float:
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())


def _bars(image):
    """找每张卡右下角深蓝色对勾条的横向区间（会被金勾/白锁从中间截断，按 ≤32px 合并）。"""
    p = image.astype(int)
    b, g, r = p[:, :, 0], p[:, :, 1], p[:, :, 2]
    mask = (b >= 95) & ((b - r) >= 80) & ((b - g) >= 55) & (r <= 45)
    cols = mask[652:667, :].sum(axis=0)
    runs = []
    start = None
    for x, v in enumerate(cols):
        if v >= 8 and start is None:
            start = x
        elif v < 8 and start is not None:
            if x - start >= 6:
                runs.append((start, x - 1))
            start = None
    if start is not None and len(cols) - start >= 6:
        runs.append((start, len(cols) - 1))
    merged = []
    for x0, x1 in runs:
        if merged and x0 - merged[-1][1] <= 32:
            merged[-1] = (merged[-1][0], x1)
        else:
            merged.append((x0, x1))
    return merged


def _count_gold(image, left) -> int:
    x, y, w, h = left + STAR_OFF[0], STAR_OFF[1], STAR_OFF[2], STAR_OFF[3]
    p = image[y:y + h, x:x + w].astype(int)
    b, g, r = p[:, :, 0], p[:, :, 1], p[:, :, 2]
    return int(((r >= 180) & (g >= 150) & (b <= 120) & ((r - b) >= 80)).sum())


def _count_check(image, left) -> int:
    """蓝条上的金色对勾：普通模式剧情关不点星，只在这里给金勾。"""
    x, y, w, h = left + ICON_OFF[0], ICON_OFF[1], ICON_OFF[2], ICON_OFF[3]
    p = image[y:y + h, x:x + w].astype(int)
    b, g, r = p[:, :, 0], p[:, :, 1], p[:, :, 2]
    return int(((r >= 180) & (g >= 150) & (b <= 120) & ((r - b) >= 80)).sum())


def _count_white(image, left) -> int:
    x, y, w, h = left + ICON_OFF[0], ICON_OFF[1], ICON_OFF[2], ICON_OFF[3]
    p = image[y:y + h, x:x + w].astype(int)
    b, g, r = p[:, :, 0], p[:, :, 1], p[:, :, 2]
    return int(((r >= 200) & (g >= 200) & (b >= 200)).sum())


def _cards(image):
    """返回本帧可见的卡：[{left, cleared, locked}]，按屏幕从左到右。

    判定优先级：金星/金勾 = 已通关 → 白锁 = 未解锁 → 其余可打。
    """
    bars = _bars(image)[:4]
    if not bars:
        return []
    lefts = [x1 - BAR_TO_CARD_LEFT for _, x1 in bars]
    # 第 4 张卡贴屏幕右缘时蓝条被裁短，右缘不可信，按前一卡 + 卡距外推
    if len(lefts) == 4 and bars[3][1] >= 1220:
        lefts[3] = lefts[2] + CARD_PITCH
    out = []
    width = image.shape[1]
    for left in lefts:
        if left + CLICK_DX < MIN_CLICK_X:
            continue  # 卡面基本在屏外，读出来的状态不可信也不该点
        # 右缘第 4 张卡常被屏幕裁切：图标区读数会偏小（实测白锁只剩 137），不能当开放处理
        unknown = left + ICON_OFF[0] + ICON_OFF[2] > width
        star = _count_gold(image, left)
        check = 0 if star >= GOLD_MIN else _count_check(image, left)
        cleared = star >= GOLD_MIN or check >= CHECK_MIN
        white = 0 if cleared else _count_white(image, left)
        out.append({
            "left": left,
            "cleared": cleared,
            "locked": (not cleared) and white >= WHITE_MIN,
            "unknown": unknown,
            "star": star,
            "check": check,
            "white": white,
        })
    return out


def _ocr_challenge(context, image, left):
    """卡名 OCR 三态：True=09 临界挑战；False=读到文本但不像 09；None=读不到（不确定）。"""
    x, y, w, h = left + NAME_OFF[0], NAME_OFF[1], NAME_OFF[2], NAME_OFF[3]
    if x < 0:
        w += x
        x = 0
    if w <= 0:
        return None
    reco = context.run_recognition(
        "KagawaNameOCR",
        image,
        pipeline_override={
            "KagawaNameOCR": {"recognition": "OCR", "roi": [x, y, w, h], "expected": ["临界", "挑战"]}
        },
    )
    if not reco or not reco.best_result:
        return None
    text = str(getattr(reco.best_result, "text", "") or "").strip()
    if not text:
        return None
    hit = ("临界" in text) or ("挑战" in text)
    if hit:
        log.info("扫卡：卡名 OCR=%s，判定为 09 临界挑战", text)
    return hit


def _ocr_number(context, image, left):
    x, y, w, h = left + NUM_OFF[0], NUM_OFF[1], NUM_OFF[2], NUM_OFF[3]
    if x < 0:
        w += x
        x = 0
    if w <= 0:
        return None
    reco = context.run_recognition(
        "KagawaNumOCR",
        image,
        pipeline_override={
            "KagawaNumOCR": {"recognition": "OCR", "roi": [x, y, w, h], "expected": [r"\d{1,2}"]}
        },
    )
    if not reco or not reco.best_result or not hasattr(reco.best_result, "text"):
        return None
    m = re.search(r"\d{1,2}", str(reco.best_result.text))
    return int(m.group(0)) if m else None


def _reset_left(context):
    """连续右滑直到条带不再变化（最多 5 次），把列表顶到最左端。"""
    for _ in range(5):
        before = _strip(_shot(context))
        _swipe(context, RIGHT, wait=0.8)
        after = _strip(_shot(context))
        if _mad(before, after) < PAGE_DIFF:
            log.info("复位：条带不再变化，已到最左端")
            return
    log.info("复位：5 次右滑后仍有位移，按最左端处理")


def _scan_and_pick(context, start=None):
    """从最左端逐屏左扫：页内第一张「未通关且未锁定」的卡就是下一关。

    滑动带惯性（实测一次左滑约 1.5 张卡），所以不按「页序号」定位，每屏都重新识别。
    退出按 kind 分类（正常收尾 / 失败），由 _FAIL_KINDS 决定任务成败：
      正常：picked / end / locked / challenge / unsure
      失败：error（0 卡、屏数上限）/ no_target（目标关不存在）/ invalid_target（09、未解锁）
    start 非空时按卡号定位目标关（填几打几，已通关也打）：锁定/09 永不点，
    目标不存在即失败。
    """
    _STATE.update({"found": False, "last_num": None, "reason": None, "kind": None})
    target = None if start is None else int(start)
    _SWEEP["target"] = target  # 扫荡沿用同一个目标关卡；留空时扫荡会在设次数处明确失败
    if target is not None and target > LAST_NORMAL_STAGE:
        _STATE.update({"kind": "invalid_target",
                       "reason": f"目标关卡 {target} 不存在：推关范围只有 01~08，09 是特殊卡"})
        log.info("扫描：%s，按失败收尾", _STATE["reason"])
        return
    if target is not None:
        log.info("扫描：按用户指定目标关卡 %d（已通关也打）", target)
    _reset_left(context)
    for step in range(MAX_SCREENS):
        image = _shot(context)
        cards = _cards(image)
        log.info("扫描第 %d 屏：识别到 %d 张卡", step + 1, len(cards))
        if not cards:
            _STATE.update({"kind": "error", "reason": f"第 {step + 1} 屏一张卡都没识别到，按失败收尾"})
            log.info("扫描：%s", _STATE["reason"])
            return
        for idx, card in enumerate(cards):
            if card["unknown"]:
                log.info("扫描：第 %d 张卡右缘被屏幕裁切，状态未知，不点（下一屏再判）", idx + 1)
                continue
            if target is not None:
                num = _ocr_number(context, image, card["left"])
                if num != target:
                    continue
                challenge = _ocr_challenge(context, image, card["left"])
                if challenge or num == 9:
                    _STATE.update({"kind": "invalid_target",
                                   "reason": f"目标关卡 {target} 是 09 临界挑战特殊卡，不点"})
                    log.info("扫描：%s，按失败收尾", _STATE["reason"])
                    return
                if card["locked"]:
                    _STATE.update({"kind": "invalid_target",
                                   "reason": f"目标关卡 {target} 未解锁，无法开战"})
                    log.info("扫描：%s，按失败收尾", _STATE["reason"])
                    return
                x = card["left"] + CLICK_DX
                log.info("扫描：目标关卡 %d 命中（left=%d star=%d check=%d cleared=%s），点击 x=%d",
                         num, card["left"], card["star"], card["check"], card["cleared"], x)
                context.tasker.controller.post_click(x, CLICK_Y, 5).wait()
                time.sleep(1.3)
                _STATE.update({"kind": "picked", "found": True, "last_num": num})
                return
            if card["cleared"]:
                continue
            if card["locked"]:
                _STATE.update({"kind": "locked",
                               "reason": f"第 {idx + 1} 张未通关卡处于锁定状态，后续不可打"})
                log.info("扫描：%s，正常收尾", _STATE["reason"])
                return
            num = _ocr_number(context, image, card["left"])
            challenge = _ocr_challenge(context, image, card["left"])
            if challenge:
                _STATE.update({"kind": "challenge", "reason": "命中 09 临界挑战（卡名），不点"})
                log.info("扫描：%s，正常收尾", _STATE["reason"])
                return
            if num == 9:
                _STATE.update({"kind": "challenge", "reason": "卡号 OCR=9，判为 09 临界挑战，不点"})
                log.info("扫描：%s，正常收尾", _STATE["reason"])
                return
            if num is None and challenge is None:
                # fail-closed：卡号与卡名都没读出来，无法排除 09，宁可不点
                _STATE.update({"kind": "unsure",
                               "reason": "卡号与卡名 OCR 均未识别，无法排除 09，保守不点并收尾"})
                log.info("扫描：%s", _STATE["reason"])
                return
            x = card["left"] + CLICK_DX
            log.info(
                "扫描：第 %d 张未通关且未锁定（left=%d star=%d check=%d white=%d OCR=%s），点击 x=%d",
                idx + 1, card["left"], card["star"], card["check"], card["white"], num, x,
            )
            context.tasker.controller.post_click(x, CLICK_Y, 5).wait()
            time.sleep(1.3)
            _STATE.update({"kind": "picked", "found": True, "last_num": num})
            return
        moved = False
        for attempt in range(3):
            before = _strip(image)
            _swipe(context, LEFT, wait=0.9)
            image = _shot(context)
            if _mad(before, _strip(image)) >= PAGE_DIFF:
                moved = True
                break
            log.info("扫描：第 %d 屏第 %d 次左滑无位移", step + 1, attempt + 1)
        if not moved:
            if target is not None:
                _STATE.update({"kind": "no_target",
                               "reason": f"目标关卡 {target} 不在列表里（未开放或超出当前范围）"})
                log.info("扫描：%s，按失败收尾", _STATE["reason"])
                return
            _STATE.update({"kind": "end",
                           "reason": "连续 3 次左滑无位移，确认已到列表右端且本屏无可打卡"})
            log.info("扫描：%s，正常收尾", _STATE["reason"])
            return
    if target is not None:
        _STATE.update({"kind": "no_target", "reason": f"翻了 {MAX_SCREENS} 屏仍未找到目标关卡 {target}"})
    else:
        _STATE.update({"kind": "error", "reason": f"扫描达到 {MAX_SCREENS} 屏上限仍有位移，按失败收尾"})
    log.info("扫描：%s", _STATE["reason"])


_FAIL_KINDS = {"error", "no_target", "invalid_target"}


def _fail(context, reason):
    """失败收尾：原因写回选卡节点的 focus（MAA 任务详情里可读），再停任务。"""
    log.info("选卡失败：%s", reason)
    try:
        context.override_pipeline({"香川大典_祭典_选卡": {"focus": f"选卡失败：{reason}"}})
    except Exception:
        log.warning("失败原因写回 pipeline 失败（不影响停止任务）", exc_info=True)
    context.tasker.post_stop()


@AgentServer.custom_action("kagawa_scan_and_pick")
class KagawaScanAndPick(CustomAction):
    """一轮完整扫描：复位→逐屏识别→点第一张可打卡（或判定到末页/只剩 09）。

    custom_action_param: {"target": "N"} 由界面「目标关卡」覆盖写入，填几就打第几关
    （已通关也照打）；留空则按卡面金标与白锁自动挑第一张可打的卡。
    异常与「无可打关卡 / 目标关不可打」都会 post_stop 让任务按失败结束，不再静默 succeeded。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            # 没写 custom_action_param 时引擎给的是 JSON "null"，解析出来是 None
            param = json.loads(argv.custom_action_param or "{}") or {}
            given = str(param.get("target") or "").strip()
            target = int(given) if given.isdigit() else None
            _scan_and_pick(context, target)
        except Exception:
            log.exception("选卡异常：按失败停止任务，不带着未知状态继续")
            context.tasker.post_stop()
            return False
        kind = _STATE.get("kind")
        if kind in _FAIL_KINDS:
            _fail(context, _STATE.get("reason") or kind)
            return False
        log.info("选卡结束：kind=%s reason=%s", kind, _STATE.get("reason"))
        return True


@AgentServer.custom_recognition("kagawa_found")
class KagawaFound(CustomRecognition):
    """本轮是否扫到并点了可打的卡。"""

    def analyze(self, context: Context, argv):
        if _STATE.get("found"):
            return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={"found": True})
        return None


@AgentServer.custom_recognition("kagawa_last_is_8")
class KagawaLastIs8(CustomRecognition):
    """上一场打的是 08（卡号 OCR=8）→ 困难最后一关已清，直接收尾，不再去碰 09。"""

    def analyze(self, context: Context, argv):
        last = _STATE.get("last_num")
        if last is not None and int(last) == LAST_NORMAL_STAGE:
            log.info("收尾：上一场打的是 08（OCR=%s），不再扫描后续关卡", last)
            return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={"last": int(last)})
        return None


@AgentServer.custom_action("kagawa_reset")
class KagawaReset(CustomAction):
    """两次右滑把列表顶回最左端，并清空页序号。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        _swipe(context, RIGHT)
        _swipe(context, RIGHT)
        _STATE.update({"page": 0, "diff": 0.0, "strip": _strip(_shot(context)), "at_end": False})
        log.info("复位：回到最左页")
        return True


@AgentServer.custom_action("kagawa_next_page")
class KagawaNextPage(CustomAction):
    """进到下一页：复位后左滑 page+1 次；每次左滑校验位移，翻不动即认为到末页。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        prev = _STATE.get("strip")
        _swipe(context, RIGHT)
        _swipe(context, RIGHT)
        page = int(_STATE.get("page") or 0) + 1
        moved_all = True
        for idx in range(page):
            moved = False
            for attempt in range(3):
                before = _strip(_shot(context))
                _swipe(context, LEFT)
                after = _strip(_shot(context))
                if _mad(before, after) >= PAGE_DIFF:
                    moved = True
                    break
                log.info("第 %d 次左滑未产生位移（第 %d 次重试）", idx + 1, attempt + 1)
            if not moved:
                moved_all = False
                log.info("第 %d 次左滑连续无位移，判定已到列表末端", idx + 1)
                break
        cur = _strip(_shot(context))
        diff = 0.0 if prev is None else _mad(cur, prev)
        _STATE.update({"page": page, "strip": cur, "diff": float(diff), "at_end": not moved_all})
        log.info("翻页：page=%d diff=%.2f at_end=%s", page, diff, not moved_all)
        return True


@AgentServer.custom_recognition("kagawa_page_moved")
class KagawaPageMoved(CustomRecognition):
    """翻页是否真的产生了新页：卡面条带相对差，不再用整帧平均亮度。"""

    def analyze(self, context: Context, argv):
        try:
            param = json.loads(argv.custom_recognition_param or "{}") or {}
        except json.JSONDecodeError:
            param = {}
        threshold = float(param.get("threshold") or PAGE_DIFF)
        diff = float(_STATE.get("diff") or 0.0)
        if diff < threshold:
            log.info("翻页校验：卡面条带差 %.2f < %.1f，判为翻不动", diff, threshold)
            return None
        log.info("翻页校验：卡面条带差 %.2f >= %.1f，翻页生效", diff, threshold)
        return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={"diff": diff})


@AgentServer.custom_recognition("kagawa_pick_next")
class KagawaPickNext(CustomRecognition):
    """页内从左到右选第一张「未通关且未锁定」的卡；n 填了则按 n+1 找卡号。"""

    def analyze(self, context, argv):
        try:
            param = json.loads(argv.custom_recognition_param or "{}") or {}
        except json.JSONDecodeError:
            param = {}

        image = argv.image
        cards = _cards(image)
        given = str(param.get("n") or "").strip()

        if given.isdigit():
            target = int(given) + 1
            for card in cards:
                num = _ocr_number(context, image, card["left"])
                if num == target:
                    log.info("扫卡：指定起点 %s，命中 %d，点 x=%d", given, target, card["left"] + CLICK_DX)
                    return CustomRecognition.AnalyzeResult(
                        box=(card["left"] + CLICK_DX, CLICK_Y, 4, 4),
                        detail={"card": num, "given": int(given)},
                    )
            log.info("扫卡：指定起点 %s，本页 %d 张卡中无 %d", given, len(cards), target)
            return None

        for idx, card in enumerate(cards):
            if card["cleared"]:
                continue
            if card["locked"]:
                log.info("扫卡：第 %d 张起为锁定关卡（white=%d），本节不可打", idx + 1, card["white"])
                return None
            stage = int(_STATE.get("page") or 0) + idx + 1
            challenge = _ocr_challenge(context, image, card["left"])
            if challenge is not False or stage > LAST_NORMAL_STAGE:
                log.info(
                    "扫卡：第 %d 张是 09 临界挑战（序号推断=%d，名字命中=%s），不是普通推关关卡，停止",
                    idx + 1, stage, challenge,
                )
                return None
            num = _ocr_number(context, image, card["left"])
            log.info(
                "扫卡：第 %d 张未通关且未锁定（left=%d star=%d white=%d OCR=%s），点 x=%d",
                idx + 1, card["left"], card["star"], card["white"], num, card["left"] + CLICK_DX,
            )
            return CustomRecognition.AnalyzeResult(
                box=(card["left"] + CLICK_DX, CLICK_Y, 4, 4),
                detail={"card": num, "left": card["left"]},
            )

        log.info("扫卡：本页 %d 张卡均无「未通关且未锁定」，继续翻页", len(cards))
        return None

# ---------------- 扫荡 ----------------

# 读数策略（弹窗宽度会浮动，不能拿固定 ROI 硬套）：
#   1. OCR 定位「现在」「扫荡后」两个标签，以标签框为锚点读它右侧的数字；
#   2. 同时读顶部入场券计数与确认按钮「入场券-N」，用「顶部券数 - 本次消耗 == 扫荡后」
#      交叉核对（并核对弹窗「现在」== 顶部券数），一致才采信；
#   3. 不一致重读一次；仍不一致/读不出时不猜数字，降级为「每轮只扫 1 次、
#      每轮重新打开弹窗确认」的保守循环，并在日志里写明降级原因。

# 次数气泡跟着滑块走（实测 2026-11-21 00:16 帧：7 次时气泡在 x≈318~403），
# 固定窄 ROI 只在次数=1 时碰巧读到；这里横跨整条滑轨读气泡里的数字。
# 右侧「现在 / 扫荡后」的数值在 x≈650 之后，不在本 ROI 内。
SWEEP_COUNT_ROI = (240, 492, 350, 48)   # 滑块上方的次数气泡（整条滑轨跨度）
SWEEP_ROWS_ROI = (420, 470, 900, 130)   # 「现在 / 扫荡后」两行：只在这里找标签，值按标签锚定
SWEEP_VALUE_W = 150                     # 标签右侧读数值的宽度（值距标签右缘约 120px）
SWEEP_TOP_ROI = (1170, 22, 100, 40)     # 顶部入场券计数（弹窗遮罩下仍可读）
SWEEP_BTN_ROI = (800, 515, 300, 70)     # 确认按钮「入场券-N 开始战斗」
SWEEP_SETTLE_ROI = (15, 10, 430, 135)   # 结算页标题「扫荡成功」
SWEEP_PLUS = (540, 560)                 # 次数「+」
SWEEP_MINUS = (252, 560)                # 次数「−」
SWEEP_MAX_ROUNDS = 30                   # 降级单次循环的硬上限，防止死循环

_SWEEP = {"want": 0, "until_empty": False, "set": 0, "max": 0, "now": None,
          "per": 0, "left": None, "degraded": False, "done": 0, "rounds": 0,
          "reason": None, "stop": None, "degrade": None, "target": None}


def _ocr_items(context, image, roi, name, expected=None):
    """在 ROI 里跑一次 OCR，返回全部结果（不用 best_result，避免被高置信噪声截断）。"""
    x, y, w, h = (int(v) for v in roi)
    x, y = max(0, x), max(0, y)
    w, h = min(w, image.shape[1] - x), min(h, image.shape[0] - y)
    if w <= 0 or h <= 0:
        return []
    reco = context.run_recognition(
        name, image,
        pipeline_override={name: {"recognition": "OCR", "roi": [x, y, w, h],
                                  "expected": list(expected or []), "threshold": 0.2}},
    )
    return list(getattr(reco, "all_results", None) or [])


def _digits(text):
    m = re.search(r"\d{1,3}", str(text or ""))
    return int(m.group(0)) if m else None


def _box_of(item):
    box = getattr(item, "box", None)
    if not box or len(box) < 4:
        return None
    return tuple(int(v) for v in box[:4])


def _label_value(context, image, keyword, name):
    """OCR 定位标签（如「现在」），再以标签框为锚点读它右侧的数字。"""
    for item in _ocr_items(context, image, SWEEP_ROWS_ROI, name):
        text = str(getattr(item, "text", "") or "")
        if keyword not in text:
            continue
        same = _digits(text)
        if same is not None:
            return same
        box = _box_of(item)
        if not box:
            continue
        x, y, w, h = box
        roi = (x + w + 2, y - 8, SWEEP_VALUE_W, h + 16)
        for sub in _ocr_items(context, image, roi, name + "V"):
            value = _digits(getattr(sub, "text", ""))
            if value is not None:
                return value
    return None


def _read_count(context, image):
    for item in _ocr_items(context, image, SWEEP_COUNT_ROI, "KagawaSweepCount"):
        value = _digits(getattr(item, "text", ""))
        if value is not None:
            return value
    return None


def _read_top_tickets(context, image):
    for item in _ocr_items(context, image, SWEEP_TOP_ROI, "KagawaSweepTop"):
        value = _digits(getattr(item, "text", ""))
        if value is not None:
            return value
    return None


def _read_button_cost(context, image):
    """确认按钮上的「入场券-N」：返回 (N, 券标签框)。"""
    items = _ocr_items(context, image, SWEEP_BTN_ROI, "KagawaSweepBtn")
    anchor = None
    for item in items:
        if "券" in str(getattr(item, "text", "") or ""):
            anchor = item
            break
    if anchor is None:
        return None, None
    cost = _digits(getattr(anchor, "text", ""))
    box = _box_of(anchor)
    if cost is not None:
        return cost, box
    if not box:
        return None, None
    # 数字被拆成单独一项时，去「券」标签右侧 150px 内找
    for item in items:
        b = _box_of(item)
        if not b:
            continue
        if b[0] >= box[0] + box[2] - 4 and b[0] <= box[0] + box[2] + 150 and abs(b[1] - box[1]) < 30:
            value = _digits(getattr(item, "text", ""))
            if value is not None:
                return value, box
    return None, box


def _sweep_numbers_ok(cur, now, after, top, cost):
    """交叉核对：消耗 = 次数 × 单价，且 顶部券数 - 消耗 == 扫荡后、弹窗现在 == 顶部券数。"""
    if not cur or cur <= 0:
        return False, "次数读不出"
    if top is None:
        return False, "顶部入场券/弹窗「现在」读不出"
    if cost is None:
        return False, "确认按钮「入场券-N」读不出"
    if cost % cur != 0:
        return False, f"消耗 {cost} 不是次数 {cur} 的整数倍"
    if now is not None and now != top:
        return False, f"弹窗「现在」{now} 与顶部券数 {top} 不一致"
    if after is not None and after != top - cost:
        return False, f"「扫荡后」{after} ≠ 顶部 {top} - 消耗 {cost}"
    if after is None and now is None:
        return False, "「现在 / 扫荡后」都读不出，无法核对"
    return True, ""


def _read_sweep_checked(context):
    """读次数/现在/扫荡后/顶部券数/按钮消耗；不一致重读一次，仍不一致返回 None。"""
    for attempt in (1, 2):
        image = _shot(context)
        cur = _read_count(context, image)
        now = _label_value(context, image, "现在", "KagawaSweepNow")
        after = _label_value(context, image, "扫荡后", "KagawaSweepAfter")
        hud = _read_top_tickets(context, image)
        # 弹窗遮罩下的顶部 HUD 会被压暗，且右侧同一行还混有货币数字（实测 00:19 把 12 读成 2），
        # 只把 HUD 当兜底：弹窗里的「现在」才是本次扫荡的权威券数。
        top = now if now is not None else hud
        cost, _ = _read_button_cost(context, image)
        ok, why = _sweep_numbers_ok(cur, now, after, top, cost)
        if ok:
            log.info("扫荡：读数核对通过（次数=%s 现在=%s 扫荡后=%s 顶部HUD=%s 采用券数=%s 消耗=%s）",
                     cur, now, after, hud, top, cost)
            return {"cur": cur, "now": now, "after": after, "top": top, "cost": cost}
        log.info("扫荡：第 %d 次读数未通过核对（次数=%s 现在=%s 扫荡后=%s 顶部HUD=%s 采用券数=%s 消耗=%s）：%s",
                 attempt, cur, now, after, hud, top, cost, why)
    return None


def _sweep_click(context, point, wait=0.35):
    context.tasker.controller.post_click(*point, 5).wait()
    time.sleep(wait)


def _adjust_count(context, goal, cur):
    """点 +/- 把次数调到 goal，返回最终读数。"""
    guard = 0
    while cur is not None and cur < goal and guard < 40:
        _sweep_click(context, SWEEP_PLUS, 0.35)
        guard += 1
        nxt = _read_count(context, _shot(context))
        if nxt is None or nxt <= cur:
            break
        cur = nxt
    guard = 0
    while cur is not None and cur > goal and guard < 60:
        _sweep_click(context, SWEEP_MINUS, 0.3)
        guard += 1
        nxt = _read_count(context, _shot(context))
        if nxt is None or nxt >= cur:
            break
        cur = nxt
    return cur


def _force_one(context):
    """降级模式：连点「−」把次数压到最小（1 次），返回最后一次读数。"""
    cur = None
    for _ in range(12):
        cur = _read_count(context, _shot(context))
        if cur == 1:
            return 1
        _sweep_click(context, SWEEP_MINUS, 0.3)
    return cur


def _set_sweep_count(context, want, until_empty):
    """算可扫上限并把次数调到目标；读数不可信时降级为「每轮 1 次」。"""
    if _SWEEP.get("target") is None:
        _SWEEP.update({"set": 0,
                       "reason": "扫荡需要先填「目标关卡」：留空只用于推关自动选关，扫荡不自动挑关"})
        log.info("扫荡：%s", _SWEEP["reason"])
        return
    info = _read_sweep_checked(context)
    if info is None:
        cur = _force_one(context)
        image = _shot(context)
        now = _label_value(context, image, "现在", "KagawaSweepNowD")
        cost, _ = _read_button_cost(context, image)
        # 降级时已连点 12 次「−」，次数按 1 次处理；此时按钮消耗就是单次单价。
        # 用它算出本轮后还剩几张，避免票已扫空还去重开弹窗。
        per = cost if (cost and (cur in (None, 1))) else 0
        left = max(0, now - per) if (now is not None and per > 0) else None
        _SWEEP.update({
            "degraded": True, "set": 1, "now": now, "per": per, "left": left,
            "degrade": "扫荡弹窗读数交叉核对失败（现在/扫荡后/顶部券数/按钮消耗），"
                       "不猜次数，改为每轮只扫 1 次、每轮重开弹窗确认",
        })
        log.warning("扫荡：%s（压到 1 次后读数=%s，单次单价=%s，本轮后剩余=%s）",
                    _SWEEP["degrade"], cur, per, left)
        return
    cur, now, after, top, cost = (info["cur"], info["now"], info["after"],
                                  info["top"], info["cost"])
    per = cost // cur
    limit = top // per if per > 0 else 0
    _SWEEP.update({"now": top, "max": limit, "degraded": False, "degrade": None})
    goal = limit if until_empty else min(want, limit)
    if goal <= 0:
        _SWEEP.update({"set": 0,
                       "reason": f"入场券不足（现有 {top} 张，每次 {per} 张），本轮扫荡 0 次"})
        log.info("扫荡：%s", _SWEEP["reason"])
        return
    final = _adjust_count(context, goal, cur)
    if final is None:
        final = goal  # 调完读不出：按计划值记，结算页会给出真实次数
    # 本轮扫完后的预计剩余票数：用完票模式据此决定要不要再开一轮
    _SWEEP.update({"set": final, "per": per, "left": max(0, top - final * per)})
    log.info("扫荡：现有 %d 张、每次 %d 张、上限 %d 次；本次设定 %d 次%s",
             top, per, limit, final, "（用完票）" if until_empty else "")


def _settlement_visible(context):
    image = _shot(context)
    return bool(_ocr_items(context, image, SWEEP_SETTLE_ROI, "KagawaSweepSettle",
                           expected=["扫荡成功"]))


def _close_settlement(context):
    """按返回键关掉「扫荡成功」结算页；最多 3 次，关不掉只记日志。"""
    for _ in range(3):
        if not _settlement_visible(context):
            return
        context.tasker.controller.post_press_key(4).wait()
        time.sleep(1.0)
    if _settlement_visible(context):
        log.warning("扫荡：连按 3 次返回键仍能看到结算页")


@AgentServer.custom_action("kagawa_sweep_config")
class KagawaSweepConfig(CustomAction):
    """读界面「扫荡次数/把票用完」并存起来，供后面判「要不要扫」和设次数用。

    custom_action_param: {"count": "N", "until_empty": "true|false"}
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            param = json.loads(argv.custom_action_param or "{}") or {}
            raw = str(param.get("count") or "").strip()
            want = int(raw) if raw.isdigit() else 0
            until_empty = str(param.get("until_empty") or "").strip().lower() in ("true", "1", "yes")
            _SWEEP.update({"want": want, "until_empty": until_empty, "set": 0, "max": 0,
                           "now": None, "per": 0, "left": None, "degraded": False,
                           "done": 0, "rounds": 0, "reason": None, "stop": None,
                           "degrade": None})
            log.info("扫荡配置：次数=%d 用完票=%s", want, until_empty)
        except Exception:
            log.exception("扫荡配置解析异常：按失败停止任务")
            context.tasker.post_stop()
            return False
        return True


@AgentServer.custom_recognition("kagawa_sweep_wanted")
class KagawaSweepWanted(CustomRecognition):
    """要不要走扫荡链：用完票，或次数 > 0。"""

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        if _SWEEP.get("until_empty") or (_SWEEP.get("want") or 0) > 0:
            return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail=dict(_SWEEP))
        log.info("扫荡配置：次数=0 且未勾选用完票，跳过扫荡、回开战流程")
        return None


@AgentServer.custom_recognition("kagawa_sweep_ready")
class KagawaSweepReady(CustomRecognition):
    """次数已经设到 ≥1 才允许点确认。"""

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        if (_SWEEP.get("set") or 0) > 0:
            return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail=dict(_SWEEP))
        return None


@AgentServer.custom_action("kagawa_sweep_count")
class KagawaSweepCount(CustomAction):
    """在扫荡弹窗里把次数设成用户要的值 / 可扫上限（参数在 kagawa_sweep_config 里已存好）。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            _set_sweep_count(context, _SWEEP.get("want") or 0, bool(_SWEEP.get("until_empty")))
        except Exception:
            log.exception("扫荡设次数异常：按失败停止任务")
            context.tasker.post_stop()
            return False
        return True


@AgentServer.custom_action("kagawa_sweep_round_ok")
class KagawaSweepRoundOk(CustomAction):
    """一轮扫荡结算成功：关掉结算页并累计实际扫荡次数。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            _close_settlement(context)
        except Exception:
            log.warning("关闭扫荡结算页失败", exc_info=True)
        step = 1 if _SWEEP.get("degraded") else int(_SWEEP.get("set") or 0)
        if step <= 0:
            step = 1
        _SWEEP["done"] = int(_SWEEP.get("done") or 0) + step
        _SWEEP["rounds"] = int(_SWEEP.get("rounds") or 0) + 1
        log.info("扫荡：第 %d 轮结算成功，本轮 %d 次，累计 %d 次%s",
                 _SWEEP["rounds"], step, _SWEEP["done"],
                 "（降级单次）" if _SWEEP.get("degraded") else "")
        return True


@AgentServer.custom_recognition("kagawa_sweep_more")
class KagawaSweepMore(CustomRecognition):
    """一轮结算后是否再扫：用完票模式扫到票不够为止，降级模式按轮循环。"""

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        if _SWEEP.get("stop"):
            return None
        rounds = int(_SWEEP.get("rounds") or 0)
        if rounds >= SWEEP_MAX_ROUNDS:
            _SWEEP["stop"] = f"扫荡循环达到 {SWEEP_MAX_ROUNDS} 轮上限"
            log.warning("扫荡：%s", _SWEEP["stop"])
            return None
        if _SWEEP.get("degraded"):
            if _SWEEP.get("until_empty"):
                left = _SWEEP.get("left")
                per = int(_SWEEP.get("per") or 0)
                if left is not None and per > 0 and left < per:
                    _SWEEP["stop"] = f"入场券剩余 {left} 张，不足一次 {per} 张，扫荡结束"
                    log.info("扫荡：%s", _SWEEP["stop"])
                    return None
                log.info("扫荡：降级单次循环（用完票），本轮后剩余 %s 张，继续第 %d 轮",
                         left, rounds + 1)
                return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail=dict(_SWEEP))
            if int(_SWEEP.get("done") or 0) < int(_SWEEP.get("want") or 0):
                log.info("扫荡：降级单次循环（目标 %s 次，已完成 %s），继续第 %d 轮",
                         _SWEEP.get("want"), _SWEEP.get("done"), rounds + 1)
                return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail=dict(_SWEEP))
            return None
        if _SWEEP.get("until_empty"):
            left = _SWEEP.get("left")
            per = int(_SWEEP.get("per") or 0)
            if left is not None and per > 0 and left < per:
                _SWEEP["stop"] = f"入场券剩余 {left} 张，不足一次 {per} 张，扫荡结束"
                log.info("扫荡：%s", _SWEEP["stop"])
                return None
            log.info("扫荡：用完票模式，本轮后预计剩余 %s 张，重新打开弹窗继续第 %d 轮",
                     left, rounds + 1)
            return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail=dict(_SWEEP))
        return None


@AgentServer.custom_action("kagawa_sweep_blocked")
class KagawaSweepBlocked(CustomAction):
    """扫荡没开成（弹窗打不开 / 票不足 / 次数已用完）：记录停止原因，按正常结束收尾。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        reason = "扫荡没有开成"
        dismiss = False
        try:
            param = json.loads(argv.custom_action_param or "{}") or {}
            reason = str(param.get("reason") or reason)
            dismiss = str(param.get("dismiss") or "").strip().lower() in ("true", "1", "yes")
        except Exception:
            log.warning("kagawa_sweep_blocked 参数解析失败", exc_info=True)
        _SWEEP["stop"] = reason
        log.info("扫荡：%s（本轮未扫成，累计 %d 次）", reason, _SWEEP.get("done") or 0)
        if dismiss:
            try:
                context.tasker.controller.post_press_key(4).wait()
                time.sleep(0.8)
            except Exception:
                log.warning("关闭扫荡提示弹窗失败", exc_info=True)
        return True


@AgentServer.custom_action("kagawa_sweep_finish")
class KagawaSweepFinish(CustomAction):
    """扫荡收尾：关掉可能残留的结算页；一次都没扫成按失败结束。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            _close_settlement(context)
        except Exception:
            log.warning("关闭扫荡结算页失败", exc_info=True)
        done = int(_SWEEP.get("done") or 0)
        if done <= 0:
            _fail(context, _SWEEP.get("reason") or _SWEEP.get("stop") or "一次都没有扫荡成功")
            return False
        log.info("扫荡完成：实际扫荡 %d 次（目标=%s，用完票=%s，降级=%s），停止原因：%s",
                 done, _SWEEP.get("want"), _SWEEP.get("until_empty"),
                 _SWEEP.get("degraded"), _SWEEP.get("stop") or "按目标次数完成")
        return True


# ---------------- 推关子功能状态（全通自动关闭） ----------------

# 状态落在运行目录的 config/ 下：dev 跑在 MCC_Framework、打包后跑在 dist/B，
# 两边都有 config/，下次启动即可读到「推关已被自动关闭」。
_PUSH = {"closed": False, "closed_at": None}


def _push_state_file():
    return Path(os.environ.get("KAGAWA_STATE_DIR", "config")) / "kagawa_push_state.json"


def _load_push_state():
    try:
        path = _push_state_file()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8")) or {}
            _PUSH.update({"closed": bool(data.get("push_auto_closed")),
                          "closed_at": data.get("closed_at")})
    except Exception:
        log.warning("推关：状态文件读取失败，按未关闭处理", exc_info=True)


def _save_push_closed():
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        path = _push_state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"push_auto_closed": True, "closed_at": stamp},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        _PUSH.update({"closed": True, "closed_at": stamp})
        log.info("推关：确认全部通关，自动关闭推关子功能（%s）并写入 %s", stamp, path)
    except Exception:
        log.warning("推关：状态写入失败，下次仍会跑推关", exc_info=True)


@AgentServer.custom_action("kagawa_push_state")
class KagawaPushState(CustomAction):
    """读取「推关是否已被全通自动关闭」，供本次推关门禁判断。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            _load_push_state()
            log.info("推关：子功能状态=%s%s", "已自动关闭" if _PUSH["closed"] else "开启",
                     f"（{_PUSH['closed_at']} 全部通关）" if _PUSH["closed"] else "")
        except Exception:
            log.exception("推关：状态读取异常，按失败停止任务")
            context.tasker.post_stop()
            return False
        return True


@AgentServer.custom_recognition("kagawa_push_wanted")
class KagawaPushWanted(CustomRecognition):
    """本次是否真的跑推关：选了全自动且没有被「全通自动关闭」。

    custom_recognition_param: {"mode": "full|none"}
    """

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        try:
            param = json.loads(argv.custom_recognition_param or "{}") or {}
        except Exception:
            param = {}
        mode = str(param.get("mode") or "none").strip().lower()
        if mode != "full":
            return None
        if _PUSH.get("closed"):
            log.info("推关：子功能已被自动关闭（%s 全部通关），本次跳过推关，只跑勾选的扫荡",
                     _PUSH.get("closed_at") or "上次")
            return None
        log.info("推关：门禁通过，本次按全自动执行")
        return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail=dict(_PUSH))


@AgentServer.custom_action("kagawa_push_done")
class KagawaPushDone(CustomAction):
    """推关确认全部通关后调用：持久化「推关自动关闭」，供下次运行跳过。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        kind = _STATE.get("kind")
        last = _STATE.get("last_num")
        if kind in ("end", "challenge") or last == LAST_NORMAL_STAGE:
            _save_push_closed()
        else:
            log.info("推关：流程以 kind=%s 收尾，未确认全部通关，不自动关闭推关", kind)
        return True


@AgentServer.custom_action("kagawa_fail")
class KagawaFail(CustomAction):
    """通用失败出口：把 custom_action_param.reason 写进日志并停止任务。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        reason = "不可打"
        try:
            param = json.loads(argv.custom_action_param or "{}") or {}
            reason = str(param.get("reason") or reason)
        except Exception:
            log.warning("kagawa_fail 参数解析失败", exc_info=True)
        _fail(context, reason)
        return False
