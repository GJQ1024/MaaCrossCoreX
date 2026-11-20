# -*- coding: utf-8 -*-
"""基建-订单库「换抽/交付」链路：切换好友后重新定位 + 交付结果核验。

问题（换票错位）：`基建_拜访下一位好友` 原来只有一次裸 Click。切换好友是
原地换牌 —— 牌位不动、内容整换；点完箭头立刻进入下一轮识别时，识别帧还是
切换前的那一帧，交付点击的落点仍按旧牌位算，于是交付命中新好友同位置上的
另一张订单（用户看到的「要换的东西错位、换到了别的东西上」）。

本模块把「先决条件会变」的三处动作改成自定义动作，先确认页面再动手：
  infra_open_friend     选择好友 → 等标题真的变成「XX的N级订单库 M/M」并静止
  infra_friend_switch   下一位好友 → 等标题真的换人并静止；若落回自己的订单库
                        （标题无好友名且出现自身页面提示）则判好友已遍历完，正常收尾
  infra_order_scrollable 订单库下滑识别 → 右侧滑块还有下滑空间才命中
  infra_order_scroll    订单库下滑 → 下滑一屏并核对滑块位置真的移动
  infra_coin_order      普通星币订单的可交付识别 → 星币图标 + 同卡位交付按钮，
                        卡头命中稀有标记时让位给 基建_星币_可交付（稀有原判据）
  infra_deliver_order   交付 → 记录交付前的标题计数，按本次识别框点击（同帧定位）
  infra_deliver_pending 交付核验的识别 → 只有确有「已点交付、待核验」时才命中；
                        无待核验时返回未命中，让链路继续走交付/拜访好友出口，
                        避免 DirectHit 命中即终止整条任务
  infra_deliver_verify  交付核验 → 标题计数必须变化，否则判失败（不只看点击成功）
  infra_deliver_abort   缺素材走合成分支 → 清掉待核验状态

判据只用 OCR 文字（标题里的好友名 + 「M/M」计数），不新增模板阈值。
参考分辨率 1280×720（与 基建.json 一致），实际帧按 infra_mood.REF_W/H 缩放。
"""

from __future__ import annotations

import json
import logging
import re
import time

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.pipeline import JRecognitionType, JTemplateMatch

import diag
import infra_mood as mood

log = logging.getLogger("laa.infra_order")

# 订单库标题栏：自己「10级订单库：11/11」，好友「蓝色幻梦的10级订单库 8/8（…）」。
# 右端刻意停在「刷新剩余时间」倒计时之前：倒计时每秒跳字会让「连续两帧一致」永远不成立。
TITLE_ROI_REF = [300, 8, 730, 60]
OCR_NODE = "通用_OCR探针"
FRIEND_TITLE_RE = re.compile(r"(.{1,24}?)的\s*\d+\s*级\s*订单库")
COUNTER_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
# 自己订单库标题栏的自身页面提示（好友页是「XX的N级订单库」，没有这两句）
SELF_PAGE_HINTS = ("选择合适驻员", "额外获得订单")
# 订单库右侧滚动条（1280×720 参考）：滑块 x≈1240-1250，轨道 y≈124-658
SCROLL_ROI_REF = [1236, 110, 24, 575]
SCROLL_BEGIN_REF = [730, 545]      # 卡片列间隙，避开交付按钮
SCROLL_END_REF = [730, 285]
SCROLL_DURATION_MS = 500
THUMB_BRIGHT = 170
THUMB_MIN_WIDTH_REF = 2
THUMB_MIN_ROWS_REF = 40
THUMB_BOTTOM_MARGIN_REF = 36
SCROLL_SETTLE_TIMEOUT = 1.5
SCROLL_MOVE_TOLERANCE = 3

_SCROLL_STATE = {"at_bottom": False}
KIND_LABEL = {"build": "构建票", "exp": "经验", "coin": "星币"}
VARIANT_LABEL = {"rare": "稀有", "normal": "普通"}

# 订单卡「可交付」判据（1280×720）。几何与 基建_星币_可交付 的 And 子识别同参：
# 星币图标 → 稀有卡头（图标框 + (-130,-60,150,70)，阈值 0.5）
#          → 交付按钮（稀有卡头框 + (-10,190,20,-30)，阈值 0.9）。
# 普通卡没有稀有卡头，按钮按同一卡位折算到图标框：实测 4 张卡均为图标右下
# (-120,+192)、按钮 193×35，取 (-134,+180)、217×60 覆盖整块按钮且不越到邻卡。
ORDER_LIST_ROI_REF = [272, 89, 909, 603]
COIN_ICON_TEMPLATE = "Infrastructures/星币.png"
RARE_COIN_TEMPLATE = "Infrastructures/稀有星币.png"
DELIVER_BUTTON_TEMPLATE = "Infrastructures/可交付订单.png"
COIN_ICON_THRESHOLD = 0.85
RARE_COIN_THRESHOLD = 0.5
DELIVER_BUTTON_THRESHOLD = 0.9
RARE_SEARCH_OFFSET_REF = [-130, -60, 150, 70]
BUTTON_FROM_ICON_OFFSET_REF = [-134, 180]
BUTTON_SEARCH_SIZE_REF = [217, 60]
# 标题区画面差（0-255 均值）：用于标题 OCR 不可读时兜底判断「牌面已换」与「已静止」
CHANGE_THRESHOLD = 6.0
SETTLE_THRESHOLD = 1.5

_PENDING = {}


def _param(argv):
    raw = getattr(argv, "custom_action_param", None) or "{}"
    try:
        value = json.loads(raw)
        if isinstance(value, str):
            value = json.loads(value)
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _shot(context):
    return context.tasker.controller.post_screencap().wait().get()


def _scale(image):
    height, width = image.shape[:2]
    return width / float(mood.REF_W), height / float(mood.REF_H)


def _scaled_roi(roi_ref, image):
    sx, sy = _scale(image)
    return [
        int(round(roi_ref[0] * sx)),
        int(round(roi_ref[1] * sy)),
        max(1, int(round(roi_ref[2] * sx))),
        max(1, int(round(roi_ref[3] * sy))),
    ]


def _ocr(context, image, roi):
    detail = context.run_recognition(
        OCR_NODE, image,
        pipeline_override={OCR_NODE: {
            "recognition": "OCR", "roi": roi, "expected": [], "threshold": 0.2,
        }},
    )
    if not detail:
        return ""
    items = getattr(detail, "all_results", None) or []
    texts = [str(getattr(item, "text", "")) for item in items]
    if not texts:
        best = getattr(detail, "best_result", None)
        if best is not None and getattr(best, "text", None):
            texts = [str(best.text)]
    return " ".join(text for text in texts if text)


def _read_title(context):
    """读标题栏：返回好友名、交付计数（M/M）、原始文字，以及标题区像素块。"""
    image = _shot(context)
    x, y, width, height = _scaled_roi(TITLE_ROI_REF, image)
    raw = _ocr(context, image, [x, y, width, height])
    compact = re.sub(r"\s+", "", raw or "")
    friend = FRIEND_TITLE_RE.search(compact)
    counter = COUNTER_RE.search(compact)
    return {
        "raw": raw,
        "compact": compact,
        "friend": bool(friend),
        "name": friend.group(1) if friend else "",
        "counter": (int(counter.group(1)), int(counter.group(2))) if counter else None,
        "crop": image[y:y + height, x:x + width].astype("int16"),
    }


def _crop_diff(current, before):
    if current is None or before is None:
        return 0.0
    if current.shape != before.shape:
        return 255.0
    return float(abs(current - before).mean())


def _wait_settle(context, accept, max_wait, interval=0.35):
    """轮询标题：连续两次读数一致且都满足 accept 才算到位。

    返回 (是否到位, 最后一次读数)。用「连续两帧一致」代替固定延时，
    避免在新牌面还没渲染完时就开始下一轮识别。
    """
    deadline = time.time() + max_wait
    last = None
    while time.time() < deadline:
        current = _read_title(context)
        if accept(current):
            if last is not None and current["compact"] == last["compact"]:
                return True, current
            last = current
        else:
            last = None
        time.sleep(interval)
    return False, last or _read_title(context)


def _is_self_library(item):
    """是否是自己的订单库：标题无好友名，且带自身页面提示。

    好友页标题是「XX的N级订单库」，自己页是「N级订单库：M/M（选择合适驻员，额外获得订单）」。
    只认文字特征，不靠像素阈值；读不到这两句就不算回绕，避免把好友页误判成终点。
    """
    compact = item.get("compact") or ""
    return (
        not item.get("friend")
        and "订单库" in compact
        and any(hint in compact for hint in SELF_PAGE_HINTS)
    )


def _wait_switch(context, before, strict, max_wait, interval=0.35):
    """等「下一位好友」真的换人并静止，或确认已回绕到自己的订单库。

    返回 (status, 读数)，status：
      wrapped  标题无好友名且出现自身页面提示，说明好友已遍历完，落在自己的订单库；
      switched 好友名发生变化且牌面已静止（strict），或标题区画面变化且静止（非 strict）；
      timeout  仍停在同一好友页/画面未变，属于切换失败。
    """
    deadline = time.time() + max_wait
    last = None
    latest = before
    while time.time() < deadline:
        current = _read_title(context)
        latest = current
        if _is_self_library(current):
            return "wrapped", current
        if strict:
            changed = (
                current["friend"] and bool(current["counter"])
                and current["name"] != before["name"]
            )
        else:
            changed = _crop_diff(current["crop"], before["crop"]) >= CHANGE_THRESHOLD
        if changed and last is not None and _crop_diff(current["crop"], last["crop"]) < SETTLE_THRESHOLD:
            return "switched", current
        last = current if changed else None
        time.sleep(interval)
    return "timeout", latest


def _click(context, point):
    context.tasker.controller.post_click(int(point[0]), int(point[1])).wait()


def _offset_roi(box, offset):
    """把 pipeline 的 roi_offset 语义（相对基准框，宽高为增量）换成绝对 ROI。"""
    return (
        int(box[0] + offset[0]),
        int(box[1] + offset[1]),
        int(box[2] + offset[2]),
        int(box[3] + offset[3]),
    )


def _box_list(box):
    """识别框统一转 [x,y,w,h]：本版本 maa 返回列表，旧版可能是带 x/y/w/h 的 Rect。"""
    if box is None:
        return None
    if hasattr(box, "x"):
        return [int(box.x), int(box.y), int(box.w), int(box.h)]
    values = list(box)
    if len(values) != 4:
        return None
    return [int(value) for value in values]


def _match_template_all(context, image, template, roi, threshold):
    """直接跑一次模板匹配（与 pipeline 同一实现/同一资源图），返回全部命中框。

    同一帧可能有多张同类型订单卡（含已交付但图标仍在的卡），必须逐个试，
    否则第一张卡不满足时整帧会被误判为「没有可交付订单」。
    """
    detail = context.run_recognition_direct(
        JRecognitionType.TemplateMatch,
        JTemplateMatch(
            template=[template],
            roi=tuple(int(value) for value in roi),
            threshold=[float(threshold)],
        ),
        image,
    )
    if not detail or not getattr(detail, "hit", False):
        return []
    boxes = []
    for item in getattr(detail, "all_results", None) or []:
        if float(getattr(item, "score", 1.0)) < float(threshold):
            continue
        box = _box_list(getattr(item, "box", None))
        if box:
            boxes.append(box)
    if not boxes:
        best = getattr(detail, "best_result", None)
        box = _box_list(getattr(best, "box", None)) if best is not None else None
        if box is None:
            box = _box_list(getattr(detail, "box", None))
        if box:
            boxes.append(box)
    return boxes


def _match_template(context, image, template, roi, threshold):
    """单次模板匹配，命中返回第一个框。"""
    boxes = _match_template_all(context, image, template, roi, threshold)
    return boxes[0] if boxes else None


def _fmt_counter(counter):
    """计数统一写成 (M,N)，与交付记账行「(M,N) → (M,N-1)」的形式一致。"""
    if not counter:
        return "不可读"
    return "(%d,%d)" % (int(counter[0]), int(counter[1]))


def _scrollbar_metrics(image):
    """读右侧滚动条滑块：返回滑块在 ROI 内的 top/bottom 与 ROI 高度。

    滑块是窄条亮色（实测灰度 >170、宽约 10px），轨道远低于该亮度。
    找不到足够长/足够宽的亮条就返回 None（滚动条不可见）。
    """
    x, y, width, height = _scaled_roi(SCROLL_ROI_REF, image)
    crop = image[y:y + height, x:x + width]
    if crop.size == 0:
        return None
    sx, sy = _scale(image)
    min_width = max(2, int(round(THUMB_MIN_WIDTH_REF * sx)))
    min_rows = max(8, int(round(THUMB_MIN_ROWS_REF * sy)))
    gray = crop.mean(axis=2) if crop.ndim == 3 else crop
    flags = ((gray >= THUMB_BRIGHT).sum(axis=1) >= min_width).tolist()
    best = None
    start = None
    for index, flag in enumerate(flags + [False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if best is None or (index - start) > (best[1] - best[0] + 1):
                best = (start, index - 1)
            start = None
    if best is None or best[1] - best[0] + 1 < min_rows:
        return None
    return {"top": best[0], "bottom": best[1], "rows": height}


@AgentServer.custom_recognition("infra_order_scrollable")
class InfraOrderScrollableRecognition(CustomRecognition):
    """订单库右侧还有下滑空间时才命中。

    判据：右侧滚动条存在可见滑块，且滑块底端未贴到轨道底部。
    滑块消失或已到底 → 未命中，由路由继续拜访好友/切换下一位。
    """

    def analyze(self, context, argv):
        if _SCROLL_STATE["at_bottom"]:
            return None
        image = _shot(context)
        metrics = _scrollbar_metrics(image)
        if metrics is None:
            _SCROLL_STATE["at_bottom"] = True
            log.info("订单库滚动条不可见，判定已到底")
            return None
        _, sy = _scale(image)
        margin = THUMB_BOTTOM_MARGIN_REF * sy
        if metrics["bottom"] >= metrics["rows"] - 1 - margin:
            _SCROLL_STATE["at_bottom"] = True
            log.info(
                "订单库滑块已到底（bottom=%d rows=%d），停止下滑",
                metrics["bottom"], metrics["rows"],
            )
            return None
        return CustomRecognition.AnalyzeResult(
            box=(0, 0, 1, 1),
            detail={"thumb_top": metrics["top"], "thumb_bottom": metrics["bottom"]},
        )


@AgentServer.custom_action("infra_order_scroll")
class InfraOrderScrollAction(CustomAction):
    """订单库下滑一屏，并用滑块位置变化确认这次滑动真的生效。

    滑块位置未变/滚动条消失 → 判定到底，置 at_bottom，后续识别不再命中，
    由路由继续拜访好友或切换下一位；不是靠固定次数或延时结束。
    """

    def run(self, context, argv):
        image = _shot(context)
        before = _scrollbar_metrics(image)
        if before is None:
            _SCROLL_STATE["at_bottom"] = True
            log.info("订单库下滑：滚动条不可见，判定已到底")
            return True
        sx, sy = _scale(image)
        x = int(round(SCROLL_BEGIN_REF[0] * sx))
        y1 = int(round(SCROLL_BEGIN_REF[1] * sy))
        y2 = int(round(SCROLL_END_REF[1] * sy))
        context.tasker.controller.post_swipe(x, y1, x, y2, SCROLL_DURATION_MS).wait()

        after = None
        deadline = time.time() + SCROLL_SETTLE_TIMEOUT
        while time.time() < deadline:
            after = _scrollbar_metrics(_shot(context))
            if after is None:
                break
            if abs(after["top"] - before["top"]) >= SCROLL_MOVE_TOLERANCE:
                break
            time.sleep(0.15)
        if after is None or abs(after["top"] - before["top"]) < SCROLL_MOVE_TOLERANCE:
            _SCROLL_STATE["at_bottom"] = True
            log.info(
                "订单库下滑：滑块未移动（%d → %s），判定已到底",
                before["top"], None if after is None else after["top"],
            )
            return True
        log.info("订单库下滑：滑块 %d → %d，继续扫描", before["top"], after["top"])
        return True


@AgentServer.custom_action("infra_open_friend")
class InfraOpenFriendAction(CustomAction):
    """点「选择好友」里的好友行，并确认订单库标题真的切到好友牌面。

    custom_action_param:
      target_offset: [dx, dy]  相对「好友列表」识别框左上角（1280×720 参考坐标）
      attempts:      2         未到位时的补充点击次数上限（不是无脑重试，每次都要复核）
      max_wait:      6.0       每次点击后等标题到位的上限秒数
      reason:        日志标签
    """

    def run(self, context, argv):
        param = _param(argv)
        reason = str(param.get("reason") or argv.node_name)
        offset = param.get("target_offset") or [0, 0]
        attempts = max(1, int(param.get("attempts") or 2))
        max_wait = float(param.get("max_wait") or 6.0)

        box = getattr(argv, "box", None)
        if not box or not (box[2] or box[3]):
            diag.fail(
                "打开好友订单库[%s]" % reason,
                actual="节点没有识别框，无法确定好友列表里的落点",
                expect="OCR 命中「好友列表」后按其框定位好友行",
            )
            return False
        image = _shot(context)
        sx, sy = _scale(image)
        point = (box[0] + int(offset[0]) * sx, box[1] + int(offset[1]) * sy)

        before = _read_title(context)
        last = None
        for attempt in range(1, attempts + 1):
            # 先点、再按标题复核；上一次没切过去才会进入下一次补点。
            _click(context, point)
            log.info("打开好友订单库[%s] 第%d次点击 @(%d,%d)", reason, attempt, point[0], point[1])
            ok, last = _wait_settle(
                context,
                # 必须是「好友牌面」，且不是切换前那一页（同好友名不算切过去）。
                lambda item: item["friend"] and (
                    not before["friend"] or item["name"] != before["name"]
                ),
                max_wait,
            )
            if ok:
                log.info("好友订单库已就位[%s]：%s", reason, last["raw"][:80])
                _SCROLL_STATE["at_bottom"] = False
                return True
            log.warning(
                "好友订单库未就位[%s] 第%d次：标题=%s",
                reason, attempt, (last or {}).get("raw", "")[:80],
            )

        diag.fail(
            "打开好友订单库[%s]" % reason,
            actual="点击后标题仍不是「XX的N级订单库」：%s" % (last or {}).get("raw", "")[:80],
            expect="标题出现好友名与「N级订单库」",
        )
        return False


@AgentServer.custom_action("infra_friend_switch")
class InfraFriendSwitchAction(CustomAction):
    """点「下一位好友」箭头，确认标题换人并静止后才交给下一轮识别。

    custom_action_param:
      max_wait: 8.0   等标题换人的上限秒数
      reason:   日志标签
    """

    def run(self, context, argv):
        param = _param(argv)
        reason = str(param.get("reason") or argv.node_name)
        max_wait = float(param.get("max_wait") or 8.0)

        before = _read_title(context)
        strict = before["friend"] and bool(before["counter"])
        if not strict:
            log.warning(
                "切换前标题不可读（%s），改用标题区画面变化判定好友是否已换",
                before["raw"][:60],
            )

        box = getattr(argv, "box", None)
        if not box or not (box[2] or box[3]):
            diag.fail(
                "切换下一位好友[%s]" % reason,
                actual="箭头模板未命中，没有可点位置",
                expect="向右箭头模板命中",
            )
            return False
        point = (box[0] + box[2] // 2, box[1] + box[3] // 2)
        _click(context, point)
        log.info("切换下一位好友[%s] 点击 @(%d,%d) 当前=%s", reason, point[0], point[1], before["raw"][:60])

        status, after = _wait_switch(context, before, strict, max_wait)
        if status == "wrapped":
            log.info(
                "好友遍历完成[%s]：已回到自己的订单库，交由后续节点收尾（%s）",
                reason, after["raw"][:60],
            )
            _SCROLL_STATE["at_bottom"] = False
            return True
        if status == "switched":
            log.info(
                "切换好友完成[%s]：%s → %s",
                reason, before["raw"][:60], after["raw"][:60],
            )
            _SCROLL_STATE["at_bottom"] = False
            return True

        diag.fail(
            "切换下一位好友[%s]" % reason,
            actual="点击后好友名/标题区未变化：%s → %s" % (before["raw"][:60], after["raw"][:60]),
            expect="标题里的好友名发生变化并静止（标题不可读时要求标题区画面变化）",
        )
        return False


@AgentServer.custom_recognition("infra_coin_order")
class InfraCoinOrderRecognition(CustomRecognition):
    """普通星币订单的可交付识别：星币图标 + 同卡位交付按钮，且卡头没有稀有标记。

    判据取自订单卡自身可区分的特征：稀有卡卡头有「稀有」标（稀有星币.png），
    普通卡没有。稀有卡命中卡头时返回 None，仍由 基建_星币_可交付 按原判据处理，
    不会把稀有单记成普通单，也没有削弱原有稀有识别。
    """

    def analyze(self, context, argv):
        image = getattr(argv, "image", None)
        if image is None:
            return None
        node_roi = getattr(argv, "roi", None)
        if node_roi is not None and getattr(node_roi, "w", 0) > 0 and getattr(node_roi, "h", 0) > 0:
            roi = (node_roi.x, node_roi.y, node_roi.w, node_roi.h)
        else:
            roi = tuple(ORDER_LIST_ROI_REF)

        icons = _match_template_all(
            context, image, COIN_ICON_TEMPLATE, roi, COIN_ICON_THRESHOLD,
        )
        for icon in icons:
            # 稀有卡交给 基建_星币_可交付 的稀有卡头判据，这里只处理普通卡。
            rare = _match_template(
                context, image, RARE_COIN_TEMPLATE,
                _offset_roi(icon, RARE_SEARCH_OFFSET_REF), RARE_COIN_THRESHOLD,
            )
            if rare:
                continue
            button_roi = (
                icon[0] + BUTTON_FROM_ICON_OFFSET_REF[0],
                icon[1] + BUTTON_FROM_ICON_OFFSET_REF[1],
                BUTTON_SEARCH_SIZE_REF[0],
                BUTTON_SEARCH_SIZE_REF[1],
            )
            button = _match_template(
                context, image, DELIVER_BUTTON_TEMPLATE, button_roi, DELIVER_BUTTON_THRESHOLD,
            )
            if not button:
                continue
            return CustomRecognition.AnalyzeResult(
                box=tuple(button),
                detail={"variant": "normal", "icon": list(icon), "button": list(button)},
            )
        return None


@AgentServer.custom_action("infra_deliver_order")
class InfraDeliverOrderAction(CustomAction):
    """交付一张订单：先记下交付前计数，再按本次识别框点击（同帧定位，不靠序号）。

    custom_action_param:
      kind:     build / exp / coin  订单类型（只用于日志与核验）
      variant:  rare / normal       稀有/普通（coin 必填，写进日志与记账行）
      reason:   日志标签
    """

    def run(self, context, argv):
        param = _param(argv)
        reason = str(param.get("reason") or argv.node_name)
        kind = str(param.get("kind") or "")
        variant = str(param.get("variant") or "")
        label = KIND_LABEL.get(kind, kind or "未知")
        if variant in VARIANT_LABEL:
            label = "%s·%s" % (label, VARIANT_LABEL[variant])

        box = getattr(argv, "box", None)
        if not box or not (box[2] or box[3]):
            diag.fail(
                "交付订单[%s]" % reason,
                actual="没有识别框（图标/数额/可交付三项未同时命中）",
                expect="本次识别同时命中订单图标、数额与「可交付」",
            )
            return False

        title = _read_title(context)
        point = (box[0] + box[2] // 2, box[1] + box[3] // 2)
        _click(context, point)
        _PENDING.clear()
        _PENDING.update({
            "kind": kind,
            "variant": variant,
            "label": label,
            "reason": reason,
            "box": [int(value) for value in box],
            "before": title["compact"],
            "counter": title["counter"],
            "ts": time.time(),
        })
        log.info(
            "交付[%s] 点击 @(%d,%d) 框=%s 交付前标题=%s",
            label, point[0], point[1], list(box), title["raw"][:80],
        )
        return True


@AgentServer.custom_recognition("infra_deliver_pending")
class InfraDeliverPendingRecognition(CustomRecognition):
    """只在确有「已点交付、待核验」记录时才命中。

    交付核验节点原来用 DirectHit：无待核验也会命中，而该节点没有 next，
    于是「自己交付」一进来就终止，后面的交付节点与拜访好友永远轮不到。
    改成真实识别后，无待核验时返回 None（未命中），next 继续评估后续出口。
    """

    def analyze(self, context, argv):
        if not _PENDING:
            return None
        box = getattr(argv, "box", None)
        if not box or not (box[2] or box[3]):
            box = (0, 0, 1, 1)
        return CustomRecognition.AnalyzeResult(
            box=tuple(box), detail={"pending": True}
        )


@AgentServer.custom_action("infra_deliver_verify")
class InfraDeliverVerifyAction(CustomAction):
    """交付收尾确认：标题里的交付计数必须变化，否则判失败。

    custom_action_param:
      max_wait: 3.0  等计数变化的上限秒数
      fail_closed: true  计数可读且未变化时是否判失败（默认判失败）
    """

    def run(self, context, argv):
        if not _PENDING:
            # 识别已按待核验状态门禁；走到这里说明状态在识别与动作之间丢了，
            # 不能当成功放行（否则又回到「命中即终止」的假成功）。
            log.warning("交付核验[%s]：没有待核验记录，按失败处理", argv.node_name)
            return False
        param = _param(argv)
        max_wait = float(param.get("max_wait") or 3.0)
        fail_closed = bool(param.get("fail_closed", True))

        label = _PENDING.get("label", "未知")
        before_counter = _PENDING.get("counter")
        before_text = _PENDING.get("before", "")
        reason = _PENDING.get("reason", argv.node_name)

        ok, after = _wait_settle(
            context,
            lambda item: bool(item["counter"]) and (item["counter"] != before_counter),
            max_wait,
        )
        changed = bool(after and after["counter"] and after["counter"] != before_counter)
        if changed:
            log.info(
                "交付已记账[%s] %s：%s → %s", label, reason,
                _fmt_counter(before_counter), _fmt_counter(after["counter"]),
            )
            _PENDING.clear()
            return True

        if not before_counter or not (after or {}).get("counter"):
            # 计数读不出来时不做「假核验」：如实记 WARN，不据此判失败。
            log.warning(
                "交付计数不可读，无法核验[%s] %s：before=%s after=%s",
                label, reason, before_text[:60], (after or {}).get("compact", "")[:60],
            )
            _PENDING.clear()
            return True

        detail = "交付前后计数一致：%s → %s（%s）" % (
            _fmt_counter(before_counter), _fmt_counter(after["counter"]), after["raw"][:60],
        )
        diag.fail(
            "交付核验[%s]" % reason,
            actual=detail,
            expect="标题里的交付计数发生变化（订单已消费）",
        )
        _PENDING.clear()
        if fail_closed:
            return False
        log.warning("交付核验未通过，按 fail_closed=false 继续：%s", detail)
        return True


@AgentServer.custom_action("infra_deliver_abort")
class InfraDeliverAbortAction(CustomAction):
    """缺素材走合成分支：本次没有消费订单，清掉待核验状态，避免误判。"""

    def run(self, context, argv):
        if _PENDING:
            log.info(
                "交付作废[%s]：本次改走合并/合成分支，未消费订单",
                _PENDING.get("label", "未知"),
            )
            _PENDING.clear()
        return True
