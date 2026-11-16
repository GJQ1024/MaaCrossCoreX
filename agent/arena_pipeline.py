# -*- coding: utf-8 -*-
"""Atomic arena recognition and calculation capabilities for Pipeline."""

from __future__ import annotations

import json
import logging
import time

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from arena_loop import (
    ACTION_CHALLENGE,
    ACTION_REFRESH,
    ACTION_RETRY_COUNTER,
    ACTION_STOP_CUSTOM_TARGET,
    ACTION_STOP_REFRESH_EMPTY,
    ACTION_STOP_SIM_EMPTY,
    FALLBACK_REMAINING,
    REPEAT_CUSTOM,
    ROI_OWN_DEPLOYMENT,
    ROI_REFRESH,
    ROI_SIM,
    ArenaLoop,
    allows_fallback_row,
    candidate_meets_requirements,
    choose_challenge_row,
    decide_arena_action,
)
from stop_guard import ActionStopped, ensure_running
from viewport import scale_roi

from combat_autobattle import hud_first_seen, reset_hud_clock


log = logging.getLogger("arena.pipeline")
_SESSION = {}

# 单场挑战从判定 challenge 起，到结算完成的最长等待（秒）
BATTLE_SETTLE_TIMEOUT_SEC = 180
# 战斗超时看门狗：从「战斗 HUD 首次出现」起算，60 秒仍未进结算就撤退。
# 游戏侧 bug：超过回合上限会一直卡住不出结算，必须有 agent 侧兜底。
BATTLE_WATCHDOG_SEC = 60
# HUD 迟迟不出现时的兜底起算：挑战点击后给加载/演出的宽限时间（秒）
HUD_APPEAR_GRACE_SEC = 75
# 单轮最多主动撤退次数；超过则按 finish 收尾，避免「打不动→撤退→刷新→再打」死循环
MAX_RETREATS_PER_ROUND = 2
# 撤退链走完仍退不出去时，重启游戏进程只允许一次，避免「重启→再卡→再重启」循环
MAX_GAME_RESTARTS_PER_ROUND = 1
# 游戏包名识别关键字：认包不认端口（渠道包可能带后缀，如 .bilibili）
PACKAGE_KEYWORD = "crosscore"
# 整个竞技场 Pipeline 任务总时长上限（秒）
PIPELINE_RUN_TIMEOUT_SEC = 1200

# 撤退点击链（1280×720 实机实测，证据 evidence/arena_20261103/）
PAUSE_BTN_1280 = (1215, 53)          # HUD 右上最右 ⏸
EXIT_BATTLE_1280 = (272, 359)        # 暂停菜单「退出战斗」
EXIT_CONFIRM_1280 = (796, 449)       # 确认弹窗「确定」
ROI_PAUSE_MENU_1280 = [150, 320, 260, 80]      # 暂停菜单「退出战斗/设置/返回战斗」
ROI_EXIT_DIALOG_1280 = [300, 330, 680, 230]    # 退出确认弹窗「是否退出/不扣除」


def _param(raw):
    try:
        return json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _hit(detail=None):
    return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail=detail or {})


def _hit_box(box, detail=None):
    """返回命中元素的真实识别框。

    2026-11-02 坐标迁移：Pipeline 里 `target: true` / `target: "节点名"` 依赖
    本自定义识别吐出的 box。原先一律给 (0,0,1,1)，导致下游只能写绝对 target；
    现在把页面标题/按钮的实际框返回，下游即可用 target_offset 相对偏移。
    box 为 None 时退回旧的 (0,0,1,1)（只影响相对点击，不影响页面判定）。
    """
    if box is None:
        return _hit(detail)
    return CustomRecognition.AnalyzeResult(box=tuple(box), detail=detail or {})


def _ocr_box(context, image, override_name, node, roi=None, threshold=None):
    """按 pipeline 节点跑一次 OCR/模板，返回 best_result.box（1280 基准 ROI 直接传入）。

    注意：此处的 roi 是 **1280×720 基准**，由 MaaFramework 自动映射到实际分辨率，
    不要再经过 viewport.scale_roi（那是给 1920 基准的旧 ROI 用的）。
    """
    override = {node: {}}
    if roi is not None:
        override[node]["roi"] = roi
    if threshold is not None:
        override[node]["threshold"] = threshold
    try:
        rd = context.run_recognition(node, image, pipeline_override=override)
    except Exception:
        log.warning("竞技场页面框读取失败：%s", node, exc_info=True)
        return None
    if rd and rd.hit and rd.best_result:
        return tuple(rd.best_result.box)
    return None


def _probe_ocr(context, image, name, roi, expected, threshold=0.2):
    """用临时节点名做一次 OCR（节点不必存在于资源里，同 page:skip 的写法）。"""
    try:
        rd = context.run_recognition(
            name, image,
            pipeline_override={
                name: {
                    "recognition": "OCR",
                    "roi": roi,
                    "expected": expected,
                    "threshold": threshold,
                }
            },
        )
    except Exception:
        log.warning("竞技场临时 OCR 失败：%s", name, exc_info=True)
        return None
    if rd and rd.hit and rd.best_result:
        return tuple(rd.best_result.box)
    return None


# 挑战确认页右下角「挑战」按钮：1280 基准。
# 实测 2026-11-02：挑战文字框 [760,637,51,28]（中心 (786,651)）。
# 旧 ROI [780,615,130,70] 左边界切掉「挑」字，OCR 直接不命中 → 会退化成 (0,0,1,1)，
# 用 target:true 时点到屏幕左上角，必须放宽到完整包住文字。
ROI_CONFIRM_BTN_TEXT = [745, 625, 140, 55]


def _confirm_button_box(context, image):
    """取挑战按钮本体框：优先模板框，取不到退回 OCR 文字框（均 1280 基准）。

    模板 `竞技场_挑战按钮框` 取自原始帧 [756,622,165,46]；OCR 文本兜底避免
    模板因光晕/分辨率变化命中失败时退化成 (0,0,1,1) 导致点击屏幕左上角。
    """
    box = _ocr_box(context, image, "竞技场_挑战按钮框", "竞技场_挑战按钮框")
    if box is not None:
        return box
    return _probe_ocr(
        context, image, "ArenaConfirmButtonOCR", ROI_CONFIRM_BTN_TEXT, ["挑战"], 0.2
    )


def _click_1280(context, point, image):
    """按 1280×720 基准坐标点击（实机截图同为 1280，缩放仅作保护）。"""
    h, w = image.shape[:2]
    x = int(point[0] * w / 1280.0)
    y = int(point[1] * h / 720.0)
    context.tasker.controller.post_click(x, y).wait()
    log.info("点击 1280 基准坐标 %s -> 实际 (%d,%d)", tuple(point), x, y)


def _same_frame(a, b):
    try:
        return a is not None and b is not None and a.shape == b.shape and a.tobytes() == b.tobytes()
    except Exception:
        return False


def _game_package(context):
    """用 pm list packages 找唯一的 crosscore 系包（认包不认端口）。

    0 个或多命中都返回 None：不猜实例、不按端口选包。
    """
    try:
        output = (
            context.tasker.controller.post_shell("pm list packages", timeout=15000)
            .wait()
            .get()
        )
    except Exception:
        log.warning("重启兜底：pm list packages 执行失败", exc_info=True)
        return None
    packages = [
        line.strip().split(":", 1)[-1]
        for line in (output or "").splitlines()
        if PACKAGE_KEYWORD in line.lower()
    ]
    packages = [name for name in packages if name]
    if len(packages) != 1:
        log.error(
            "重启兜底：crosscore 系包命中 %d 个（%s），拒绝重启", len(packages), packages
        )
        return None
    return packages[0]


def _restart_game(context):
    """兜底重启游戏进程：am force-stop + monkey（与 P-010 同手段）。

    只在竞技场「撤退链走完仍退不出去」这一条分支调用；正常「画面在变但没进展」
    不得调用。重启成功返回 True，之后由 Pipeline 收尾回主界面。
    """
    package = _game_package(context)
    if package is None:
        return False
    try:
        context.tasker.controller.post_shell(
            "am force-stop %s" % package, timeout=20000
        ).wait()
        time.sleep(1.0)
        context.tasker.controller.post_shell(
            "monkey -p %s -c android.intent.category.LAUNCHER 1" % package,
            timeout=20000,
        ).wait()
    except Exception:
        log.error("重启兜底：force-stop/monkey 执行失败", exc_info=True)
        return False
    log.info("重启兜底：已重启游戏进程 %s", package)
    return True


def _retreat_abort(context, reason):
    """撤退链走完仍退不出去：按兜底重启游戏，并把出口交给 Pipeline。

    返回 True 表示已重启；返回 False 表示本轮已重启过或重启失败，按失败收尾。
    """
    log.error("撤退链走完仍退不出去：%s", reason)
    restarts = int(_SESSION.get("restarts", 0))
    if restarts >= MAX_GAME_RESTARTS_PER_ROUND:
        _SESSION["decision"] = "fail"
        _SESSION["fail_reason"] = "撤退链走完仍退不出去，且本轮已重启过游戏"
        return False
    _SESSION["restarts"] = restarts + 1
    if not _restart_game(context):
        _SESSION["decision"] = "fail"
        _SESSION["fail_reason"] = "撤退链走完仍退不出去，重启游戏失败"
        return False
    _SESSION["decision"] = "stop_battle_restart"
    _SESSION["completion_reason"] = "撤退链走完仍退不出去，已重启游戏"
    return True


def _watchdog_deadline():
    """战斗看门狗截止时刻：HUD 首现 + 60s；HUD 不出现时按点击后宽限兜底。"""
    ts = hud_first_seen()
    if ts is not None:
        _SESSION["hud_first_seen"] = ts
    base = _SESSION.get("hud_first_seen")
    if base is None:
        base = (_SESSION.get("battle_click_at") or time.monotonic()) + HUD_APPEAR_GRACE_SEC
    deadline = base + BATTLE_WATCHDOG_SEC
    hard = _SESSION.get("battle_deadline")
    if hard is not None:
        deadline = min(deadline, hard)
    return deadline


def _pause_menu_visible(context, image):
    return _probe_ocr(
        context, image, "ArenaPauseMenuOCR", ROI_PAUSE_MENU_1280,
        ["退出战斗", "设置", "返回战斗"], 0.2,
    ) is not None


def _exit_dialog_visible(context, image):
    return _probe_ocr(
        context, image, "ArenaExitDialogOCR", ROI_EXIT_DIALOG_1280,
        ["是否退出", "不扣除", "退出战斗"], 0.2,
    ) is not None


def _retreat_battle(context, engine):
    """执行 暂停 -> 退出战斗 -> 确认退出 -> 回列表 -> 刷新。

    识别不到暂停菜单/确认弹窗时，先间隔 >=3s 抓两帧，hash 相同才判定为静态页并
    重试一次；仍不确认则**绝不在无弹窗时点确定**（误点可能真的判负）。
    撤退链走完仍退不出去（任一环节失败或确认后 40s 未回列表）才允许重启游戏：
    由 `_retreat_abort` 兜底，重启成功置 decision=stop_battle_restart 交 Pipeline 收尾。
    """
    image = engine._shot(context)
    if image is None:
        log.error("撤退：无法抓取战斗画面")
        return False
    # 2026-11-09 实机证据（P-059）：看门狗在已回到对手列表时触发，暂停按钮 (1215,53)
    # 在列表页无效，撤退链三次失败后走 _retreat_abort 重启游戏。已不在战斗页就不撤退。
    if engine._is_arena_list(context, image):
        log.warning("撤退跳过：当前已是对手列表（看门狗在非战斗页触发，不重启游戏）")
        _SESSION["watchdog_armed"] = False
        _SESSION["battle_deadline"] = None
        _SESSION["decision"] = "evaluate"
        return True
    sim_before = engine._counter_current(
        context, image, "ArenaReadChallenges", ROI_SIM, 10
    )
    log.info("战斗超时撤退开始：撤退前模拟次数 OCR=%s", sim_before)

    # 1) 暂停
    opened = False
    for attempt in range(2):
        _click_1280(context, PAUSE_BTN_1280, image)
        time.sleep(1.0)
        image = engine._shot(context)
        if _pause_menu_visible(context, image):
            opened = True
            break
        time.sleep(3.0)
        image2 = engine._shot(context)
        if _same_frame(image, image2) and not _pause_menu_visible(context, image2):
            log.warning("撤退：点暂停后仍无暂停菜单（两帧相同），重试第 %d 次", attempt + 2)
            image = image2
            continue
        image = image2
        if _pause_menu_visible(context, image):
            opened = True
            break
        log.warning("撤退：暂停菜单未确认（画面仍在变化），重试第 %d 次", attempt + 2)
    if not opened:
        return _retreat_abort(context, "两次点暂停均未出现暂停菜单（不盲点）")

    # 2) 退出战斗（必须确认弹窗出现后才允许点确定）
    dialog = False
    for attempt in range(2):
        _click_1280(context, EXIT_BATTLE_1280, image)
        time.sleep(1.0)
        image = engine._shot(context)
        if _exit_dialog_visible(context, image):
            dialog = True
            break
        time.sleep(3.0)
        image2 = engine._shot(context)
        if _same_frame(image, image2) and not _exit_dialog_visible(context, image2):
            log.warning("撤退：点退出后仍无确认弹窗（两帧相同），重试第 %d 次", attempt + 2)
            image = image2
            continue
        image = image2
        if _exit_dialog_visible(context, image):
            dialog = True
            break
        log.warning("撤退：退出确认弹窗未确认（画面仍在变化），重试第 %d 次", attempt + 2)
    if not dialog:
        return _retreat_abort(context, "退出确认弹窗未识别（禁止盲点确定，误点可能判负）")

    # 3) 确认退出
    _click_1280(context, EXIT_CONFIRM_1280, image)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        time.sleep(1.0)
        image = engine._shot(context)
        if image is not None and engine._is_arena_list(context, image):
            break
    else:
        return _retreat_abort(context, "确认退出后 40 秒内未回到对手列表")

    sim_after = engine._counter_current(
        context, image, "ArenaReadChallenges", ROI_SIM, 10
    )
    refreshes = engine._counter_current(
        context, image, "ArenaReadRefresh", ROI_REFRESH, 15
    )
    _SESSION.setdefault("retreat_log", []).append({
        "before": sim_before, "after": sim_after, "refreshes": refreshes,
    })
    log.info(
        "战斗超时撤退完成：模拟次数 撤退前=%s、撤退后=%s（实测不扣次数）；剩余刷新=%s",
        sim_before, sim_after, refreshes,
    )

    # 4) 刷新对手（免费每日 15 次）；刷新不可用时直接重新判定
    if refreshes is not None and refreshes > 0:
        if engine._click_node(context, "ArenaRefresh"):
            log.info("撤退后已刷新对手，准备重新挑战")
            time.sleep(1.2)
        else:
            log.warning("撤退后刷新按钮未识别，跳过刷新直接重新判定")
    else:
        log.info("撤退后刷新次数为 %s，跳过刷新直接重新判定", refreshes)

    _SESSION["watchdog_armed"] = False
    _SESSION["battle_deadline"] = None
    _SESSION["decision"] = "evaluate"
    return True


class ArenaPipelineAction(CustomAction):
    """Perform one arena calculation/state mutation; Pipeline owns navigation."""

    def run(self, context, argv) -> bool:
        params = _param(argv.custom_action_param)
        operation = str(params.get("operation", ""))
        try:
            ensure_running(context)
            if operation == "init":
                engine = ArenaLoop()
                options = {
                    "repeat": engine._repeat(),
                    "target": engine._target(),
                    "max_power": engine._max_power(),
                    "min_points": engine._min_points(),
                    "fallback_threshold": engine._fallback_threshold(),
                    "fallback_points": engine._fallback_points(),
                }
                _SESSION.clear()
                _SESSION.update({
                    "engine": engine,
                    "repeat": options["repeat"],
                    "target": options["target"],
                    "max_power": options["max_power"],
                    "min_points": options["min_points"],
                    "fallback_threshold": options["fallback_threshold"],
                    "fallback_points": options["fallback_points"],
                    "row": 1,
                    "challenged": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "pending_result": None,
                    "decision": "navigate",
                    "completion_reason": None,
                    "victory_seen": False,
                    "reward_seen": False,
                    "deadline": time.monotonic() + PIPELINE_RUN_TIMEOUT_SEC,
                    "target_validated": False,
                    "confirm_attempts": 0,
                    "battle_deadline": None,
                    "retreats": 0,
                    "restarts": 0,
                    "watchdog_armed": False,
                    "battle_click_at": None,
                    "hud_first_seen": None,
                    "retreat_log": [],
                })
                reset_hud_clock()
                threshold = options["fallback_threshold"]
                log.info(
                    "Pipeline初始化竞技场：重复=%s，目标=%d，可挑战最高战力=%d，最低挑战积分=%d，"
                    "兜底阈值=%s，兜底最低积分=%d",
                    options["repeat"], options["target"], options["max_power"],
                    options["min_points"],
                    (
                        "从不" if threshold is None
                        else "剩余刷新<=剩余挑战次数" if threshold == FALLBACK_REMAINING
                        else "剩余刷新<=%d" % threshold
                    ),
                    options["fallback_points"],
                )
                return True

            engine = _SESSION.get("engine")
            if engine is None:
                log.error("竞技场Pipeline会话尚未初始化")
                return False

            if operation == "capture_own_power":
                own = engine._stable_num(
                    context, "ArenaReadOwnPower", ROI_OWN_DEPLOYMENT,
                    "部署页自己战力", 1000, 999999, attempts=5,
                )
                if own is None:
                    log.error("无法准确识别己方战力，禁止盲目挑战")
                    return False
                _SESSION["own"] = own
                log.info("本次竞技场缓存己方战力=%d，后续不重复读取", own)
                return True

            if operation == "evaluate":
                if time.monotonic() >= _SESSION["deadline"]:
                    _SESSION["decision"] = "fail"
                    _SESSION["fail_reason"] = (
                        f"竞技场Pipeline运行超过{PIPELINE_RUN_TIMEOUT_SEC // 60}分钟"
                    )
                    return True
                image = engine._shot(context)
                if not engine._is_arena_list(context, image):
                    _SESSION["decision"] = "navigate"
                    return True
                refreshes = engine._counter_current(
                    context, image, "ArenaReadRefresh", ROI_REFRESH, 15
                )
                simulations = engine._counter_current(
                    context, image, "ArenaReadChallenges", ROI_SIM, 10
                )
                if simulations is None:
                    _SESSION["decision"] = ACTION_RETRY_COUNTER
                    return True
                if simulations == 0 and not engine._confirm_zero_counter(
                    context, "ArenaReadChallenges", ROI_SIM, 10, "模拟次数"
                ):
                    _SESSION["decision"] = ACTION_RETRY_COUNTER
                    return True
                if refreshes == 0 and not engine._confirm_zero_counter(
                    context, "ArenaReadRefresh", ROI_REFRESH, 15, "刷新次数"
                ):
                    _SESSION["decision"] = ACTION_RETRY_COUNTER
                    return True

                _SESSION["target_validated"] = True

                max_power = _SESSION["max_power"]
                min_points = _SESSION["min_points"]
                row1 = engine._read_row(context, image, 1)
                rows = {1: row1}
                row1_ok = candidate_meets_requirements(
                    max_power, row1["power"], row1["points"], min_points,
                )

                # 第一段：只看第 1 位，够最低挑战积分就打。
                # 第二段（兜底）：剩余刷新次数 <= 阈值时进入，按 1 -> 2 -> 3 位
                # 找第一个满足放宽门槛的对手，目的是把当天次数清完。
                fallback_points = None
                if not row1_ok:
                    threshold = _SESSION["fallback_threshold"]
                    if allows_fallback_row(refreshes, threshold, simulations):
                        fallback_points = _SESSION["fallback_points"]
                        for index in (2, 3):
                            rows[index] = engine._read_row(context, image, index)
                        log.info(
                            "第1位不达标（战力=%s 积分=%s，上限=%s 最低积分=%s），"
                            "剩余刷新%s<=阈值%s 进入兜底，门槛降为%s分，检查第2、3位",
                            row1["power"], row1["points"], max_power, min_points,
                            refreshes,
                            "剩余挑战%s" % simulations if threshold == FALLBACK_REMAINING
                            else threshold,
                            fallback_points,
                        )
                    else:
                        log.info(
                            "第1位不达标（战力=%s 积分=%s，上限=%s 最低积分=%s），"
                            "剩余刷新%s 未到兜底阈值%s，只刷新不挑战2、3位",
                            row1["power"], row1["points"], max_power, min_points,
                            refreshes, "从不" if threshold is None else threshold,
                        )

                chosen = choose_challenge_row(rows, max_power, min_points, fallback_points)
                candidate_ok = chosen is not None
                _SESSION["row"] = chosen if chosen is not None else 1
                top = rows.get(_SESSION["row"], row1)

                decision = decide_arena_action(
                    simulations, refreshes, candidate_ok, _SESSION["repeat"],
                    _SESSION["challenged"], _SESSION["target"],
                )
                _SESSION.update({
                    "decision": decision,
                    "simulations": simulations,
                    "refreshes": refreshes,
                    "top": top,
                })
                if decision == ACTION_CHALLENGE:
                    _SESSION["confirm_attempts"] = 1
                    _SESSION["battle_deadline"] = (
                        time.monotonic() + BATTLE_SETTLE_TIMEOUT_SEC
                    )
                    _SESSION["pending_result"] = None
                    _SESSION["victory_seen"] = False
                    _SESSION["reward_seen"] = False
                    # 看门狗从本场 HUD 首现起算；这里清空上一场并上膛。
                    _SESSION["watchdog_armed"] = True
                    _SESSION["battle_click_at"] = time.monotonic()
                    _SESSION["hud_first_seen"] = None
                    reset_hud_clock()
                if decision in (
                    ACTION_STOP_SIM_EMPTY, ACTION_STOP_REFRESH_EMPTY,
                    ACTION_STOP_CUSTOM_TARGET,
                ):
                    _SESSION["completion_reason"] = decision
                log.info(
                    "Pipeline竞技场判定：模拟=%s，刷新=%s，%s，对手战力=%s，积分=%s，结果=%s",
                    simulations, refreshes,
                    "选定=第%d位" % chosen if chosen is not None else "选定=无（本轮不挑战）",
                    top["power"], top["points"], decision,
                )
                return True

            if operation == "refresh":
                if not engine._click_node(context, "ArenaRefresh"):
                    log.error("未能识别并点击竞技场刷新按钮")
                    return False
                return True

            if operation == "click_challenge":
                # 按选定位次点对手卡片；之后的链路（确认页/结算/奖励）与第一位完全一致。
                row_index = _SESSION.get("row", 1)
                if not engine._click_challenge_row(context, row_index):
                    log.error("点击第%s位对手卡片时被用户中止", row_index)
                    return False
                return True

            if operation == "dismiss_buy":
                # 次数用完后点挑战会直接弹「模拟次数购买」：点击已生效，只关不买。
                row_index = _SESSION.get("row", 1)
                engine._dismiss_buy_dialog(context)
                _SESSION["completion_reason"] = ACTION_STOP_SIM_EMPTY
                _SESSION["decision"] = "stop_sim_empty"
                log.info(
                    "第%s位点击已生效但模拟次数已用完（页面为模拟次数购买），按次数归零结束，未做任何购买",
                    row_index,
                )
                return True

            if operation == "retreat_battle":
                log.info(
                    "战斗超过 %.0f 秒未结算，执行第 %s/%d 次主动撤退",
                    BATTLE_WATCHDOG_SEC,
                    _SESSION.get("retreats", 0), MAX_RETREATS_PER_ROUND,
                )
                if not _retreat_battle(context, engine):
                    return False
                return True

            if operation == "mark_result":
                result = str(params.get("result", ""))
                if result not in ("success", "failure"):
                    log.error("竞技场结算结果参数无效：%s", result)
                    return False
                previous = _SESSION.get("pending_result")
                if previous is not None and previous != result:
                    log.error("竞技场同一轮出现冲突结算：%s -> %s", previous, result)
                    return False
                _SESSION["pending_result"] = result
                if result == "success":
                    _SESSION["victory_seen"] = True
                    log.info("识别到竞技场战斗胜利，记录本次成功")
                else:
                    log.info("识别到竞技场战斗失败，记录本次失败并继续后续挑战")
                return True

            if operation == "mark_reward":
                _SESSION["reward_seen"] = True
                if _SESSION.get("pending_result") is None:
                    # 部分设备的胜利标题动画很短；获得物品仍是可靠的成功结算证据。
                    _SESSION["pending_result"] = "success"
                    _SESSION["victory_seen"] = True
                    log.info("未捕获胜利标题，但已由竞技场奖励页确认本次成功")
                return True

            if operation == "mark_challenge":
                result = _SESSION.get("pending_result")
                if result not in ("success", "failure"):
                    log.error("竞技场返回列表但缺少本轮胜负结果，拒绝生成错误统计")
                    return False
                _SESSION["challenged"] += 1
                if result == "success":
                    _SESSION["succeeded"] += 1
                else:
                    _SESSION["failed"] += 1
                _SESSION["pending_result"] = None
                _SESSION["victory_seen"] = False
                _SESSION["reward_seen"] = False
                # 单次挑战已确认收尾，看门狗必须解除；否则回列表后的判定/刷新阶段
                # 仍会到点触发撤退链（P-059 就在这里把游戏重启了）。
                _SESSION["watchdog_armed"] = False
                _SESSION["battle_deadline"] = None
                _SESSION["decision"] = "evaluate"
                log.info(
                    "Pipeline确认单次挑战%s，累计挑战=%d、成功=%d、失败=%d",
                    "成功" if result == "success" else "失败",
                    _SESSION["challenged"], _SESSION["succeeded"], _SESSION["failed"],
                )
                return True

            if operation == "finish":
                reason = _SESSION.get("completion_reason")
                if reason == ACTION_STOP_REFRESH_EMPTY:
                    log.info(
                        "刷新次数归零，当前第一位不符合要求，剩余挑战次数（%s）次",
                        _SESSION.get("simulations", "未知"),
                    )
                log.info(
                    "竞技场共挑战%d次，成功%d次，失败%d次",
                    _SESSION["challenged"], _SESSION["succeeded"], _SESSION["failed"],
                )
                log.info("竞技场Pipeline结束原因=%s", reason)
                return reason is not None

            if operation == "fail":
                reason = params.get("reason") or _SESSION.get("fail_reason", "未知原因")
                log.error("竞技场Pipeline失败：%s", reason)
                return False

            log.error("未知竞技场原子操作：%s", operation)
            return False
        except ActionStopped:
            log.info("用户停止任务，竞技场原子操作立即停止")
            return False
        except Exception:
            log.exception("竞技场原子操作失败：%s", operation)
            return False


class ArenaPipelineRecognition(CustomRecognition):
    """Expose arena page and decision facts; never performs a click."""

    def analyze(self, context, argv):
        expected = str(_param(argv.custom_recognition_param).get("expected", ""))
        engine = _SESSION.get("engine")
        if expected.startswith("decision:"):
            value = expected[9:]
            return _hit({"decision": value}) if _SESSION.get("decision") == value else None
        if expected == "state:own_missing":
            if _SESSION.get("own") is None:
                return _hit_box(
                    _ocr_box(context, argv.image, "ArenaDeployButton", "ArenaDeployButton"),
                    {"own": None},
                )
            return None
        if expected == "state:own_ready":
            return _hit({"own": _SESSION.get("own")}) if _SESSION.get("own") is not None else None
        if engine is None:
            return None

        image = argv.image
        if expected == "page:arena" and engine._is_arena_list(context, image):
            return _hit_box(
                _ocr_box(context, image, "ArenaDeployButton", "ArenaDeployButton"),
                {"page": "arena"},
            )
        if expected == "page:buy_attempts" and engine._is_buy_attempts_dialog(context, image):
            # 今日模拟次数用完后点挑战会直接弹该框：说明点击已生效。
            return _hit_box(
                _ocr_box(context, image, "ArenaBuyTitle", "ArenaBuyTitle"),
                {"page": "buy_attempts", "row": _SESSION.get("row", 1)},
            )
        if expected == "page:confirm" and engine._is_challenge_confirm(context, image):
            return _hit_box(
                _confirm_button_box(context, image),
                {"page": "confirm"},
            )
        if expected == "page:victory" and engine._is_victory_page(context, image):
            box = _ocr_box(context, image, "ArenaResult", "ArenaResult", [13, 13, 400, 147], 0.15)
            if box is None:
                box = _ocr_box(context, image, "ArenaResult", "ArenaResult", [0, 0, 1100, 340], 0.15)
            return _hit_box(box, {"page": "victory"})
        if expected == "page:defeat" and engine._is_defeat_page(context, image):
            return _hit_box(
                _ocr_box(context, image, "ArenaDefeat", "ArenaDefeat", [42, 39, 332, 92], 0.2),
                {"page": "defeat"},
            )
        if expected == "page:reward" and engine._is_reward_page(
            context, image, allow_color_fallback=_SESSION.get("victory_seen", False)
        ):
            box = _ocr_box(context, image, "ArenaReward", "ArenaReward", [534, 170, 200, 56])
            if box is None:
                box = _ocr_box(context, image, "ArenaReward", "ArenaReward", [600, 150, 760, 330], 0.15)
            return _hit_box(box, {"page": "reward"})
        if expected == "page:battle_complete":
            result = _SESSION.get("pending_result")
            # 2026-11-09 实机证据：胜利页关闭后游戏直接回对手列表，不经过奖励浮层，
            # 只看 reward_seen 会让「已确认胜负 + 已回列表」永远命不中（P-059）。
            # pending_result 本身来自 page:victory / page:defeat 的识别结果，可直接作为已结算依据。
            settled = _SESSION.get("reward_seen") or result in ("success", "failure")
            if settled and engine._is_arena_list(context, image):
                return _hit({"page": "arena", "settled": True, "result": result})
            return None
        if expected == "page:settlement_confirm":
            if _SESSION.get("pending_result") and engine._is_challenge_confirm(context, image):
                return _hit_box(
                    _confirm_button_box(context, image),
                    {"page": "confirm"},
                )
            return None
        if expected == "page:confirm_retry":
            if (
                not _SESSION.get("victory_seen")
                and not _SESSION.get("reward_seen")
                and _SESSION.get("confirm_attempts", 0) < 3
                and engine._is_challenge_confirm(context, image)
            ):
                _SESSION["confirm_attempts"] += 1
                return _hit({"attempt": _SESSION["confirm_attempts"]})
            return None
        if expected == "page:confirm_failed":
            if (
                _SESSION.get("confirm_attempts", 0) >= 3
                and engine._is_challenge_confirm(context, image)
            ):
                _SESSION["fail_reason"] = "挑战确认按钮连续三次未生效"
                _SESSION["decision"] = "fail"
                return _hit({"attempts": _SESSION["confirm_attempts"]})
            return None
        if expected == "page:battle_timeout":
            if not _SESSION.get("watchdog_armed"):
                return None
            # 2026-11-09 实跑证据（00:26:28 胜利、00:26:32 看门狗）：胜负页已确认后
            # 还在结算离页，看门狗只负责「战斗里出不来」，不能在结算阶段撤退。
            # 解除看门狗，把收敛交给 结算完成/奖励 节点。
            if _SESSION.get("pending_result") in ("success", "failure"):
                _SESSION["watchdog_armed"] = False
                _SESSION["battle_deadline"] = None
                log.info("看门狗解除：本轮胜负已确认，等待结算离页")
                return None
            deadline = _watchdog_deadline()
            if time.monotonic() < deadline:
                return None
            retreats = int(_SESSION.get("retreats", 0))
            _SESSION["watchdog_armed"] = False
            _SESSION["battle_deadline"] = None
            if retreats < MAX_RETREATS_PER_ROUND:
                _SESSION["retreats"] = retreats + 1
                _SESSION["decision"] = "retreat"
                log.warning(
                    "战斗超时看门狗触发：HUD 首现（%s）后超过 %.0f 秒仍未结算，"
                    "第 %d/%d 次主动撤退",
                    _SESSION.get("hud_first_seen"), BATTLE_WATCHDOG_SEC,
                    retreats + 1, MAX_RETREATS_PER_ROUND,
                )
                return _hit({"timeout": True, "retreat": retreats + 1})
            _SESSION["decision"] = "stop_battle_timeout"
            _SESSION["completion_reason"] = "连续超回合，放弃本轮"
            log.error(
                "战斗超时看门狗：已连续撤退 %d 次仍未结算，按「连续超回合」结束本轮",
                retreats,
            )
            return _hit({"timeout": True, "retreat": retreats, "giveup": True})
        if expected == "page:skip":
            detail = context.run_recognition(
                "ArenaPipelineSkipOCR", image,
                pipeline_override={
                    "ArenaPipelineSkipOCR": {
                        "recognition": "OCR",
                        "roi": scale_roi(image, [1580, 0, 330, 170]),
                        "expected": ["跳过"],
                        "threshold": 0.2,
                    }
                },
            )
            if detail and detail.hit and detail.best_result:
                box = detail.best_result.box
                return CustomRecognition.AnalyzeResult(box=tuple(box), detail={"page": "skip"})
        return None
