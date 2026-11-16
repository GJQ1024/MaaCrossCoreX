"""邮件附件领取引擎（独立任务）。

用户口径（2026-11-06）：
- 只领附件：不删邮件、不做任何清理；不点公告、不点外链。
- 优先「一键领取」；一键不存在 / 点完仍有待领取时，逐封走单封路径。
- 成功判据必须三重：待领取计数归 0 + 单封按钮「领取」变「删除」 + 底部奖励条「已领取」。
  任一不成立按失败或「部分成功」退出，不假成功。
- 无附件邮件（资源溢出等）不当作可领取：右侧按钮不是「领取」就跳过；
  若按钮是「领取」但点击后计数不降，则标记为无效并跳过，绝不在同一封上反复点。
- 支持列表翻页（滚动），并限制单次运行最多领取封数 / 滚动次数。
- 幂等（P-050）：沿用 store_run 标记，5 分钟内重复执行直接跳过，避免重复进入邮箱。

实测坐标（1280x720，2026-11-06）：
- 顶部「待领取邮件：N」约 x1060-1210 / y135-156
- 「一键领取」约 (313, 637)；单封按钮约 (1131, 640)
- 底部奖励条「已领取」约 x480-540 / y600-645
- 获得物品弹窗：点空白 (250, 300) 关闭
- 列表首行标题 y≈206，其后 y≈287/351/416/481/546
"""
import json
import logging
import os
import time

from maa.context import Context
from maa.custom_action import CustomAction

from store_purchase import (
    RUN_DIR,
    _begin_run,
    _center,
    _finish_run,
    _first_number,
    _ocr,
    _recent_marker,
    _save_frame,
)

log = logging.getLogger("laa.mail")

RUN_KEY = "mail_claim"
LOCK_PATH = RUN_DIR / "mail_claim.lock"
TRACE_PATH = RUN_DIR.parent / "mail_claim_runs.log"
LOCK_STALE_SEC = 600

# 门禁与执行在同一进程内传递标记（run_task --with-agent / MFAAgent 都是单进程）
_ACTIVE_MARKER = None
_ACTIVE_LOCK = False

PENDING_ROI = [1000, 110, 280, 85]
# P-071：数字 0 的 OCR 不稳定（两次实机读成「（」「C」），标签本身可读），
# 因此 0 改为整串模板判定；N>0 只对数字域做窄 ROI OCR。
# 离线四帧实测：正例（03:59 / 03:11 / 52_mailbox）label=1.0000 zero=1.0000，
# 负例（好友页）label=0.3425 zero=0.3198；阈值取 0.90，不随失败下调。
PENDING_TPL_THRESHOLD = 0.90
PENDING_DIGIT_ROI = [1192, 134, 34, 30]
TPL_PENDING_LABEL = "Mail/PendingLabel.png"
TPL_PENDING_ZERO = "Mail/PendingZero.png"
ONE_CLICK_ROI = [200, 610, 280, 60]
BUTTON_ROI = [1040, 605, 210, 70]
REWARD_ROI = [420, 575, 240, 120]

ROW_TITLE_YS = [197, 263, 331, 398, 465, 532]
ROW_CLICK_X = 220
ROW_TITLE_ROI = [60, 0, 340, 52]

POPUP_ROI = [280, 180, 720, 380]
POPUP_TAP = (250, 300)
POPUP_TOKENS = ("获得物品", "点击空白处", "点击任意处")

LIST_SWIPE = ((230, 560), (230, 210), 600)


def _tap(context: Context, x: int, y: int):
    context.tasker.controller.post_click(int(x), int(y)).wait()


def _trace(msg: str):
    """运行轨迹落盘（仅追加一行），便于并发/异常场景事后取证。"""
    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def _join(items) -> str:
    return " ".join(text for text, _box in items)


def _template_score(context: Context, template: str, roi) -> float:
    """模板匹配分数；无命中返回 0.0。"""
    image = context.tasker.controller.post_screencap().wait().get()
    result = context.run_recognition(
        "通用_模板探针", image,
        pipeline_override={
            "通用_模板探针": {
                "recognition": "TemplateMatch",
                "roi": roi,
                "template": [template],
            }
        },
    )
    best = getattr(result, "best_result", None) if result else None
    score = getattr(best, "score", None)
    return float(score) if score is not None else 0.0


def _read_pending(context: Context):
    """返回 (待领取数量, 依据)；读不到数量时第一项为 None。

    P-071：数字 0 的 OCR 不稳定（两次实机分别读成「（」「C」），标签本身可读），
    改为确定性读法：
      1) 「待领取邮件：0」整串模板命中 → 0，不经 OCR；
      2) 只有标签模板命中（说明 N>0）→ 仅对数字域做窄 ROI OCR；
      3) 两者都不中 → 不在邮件页 / 计数不可读，返回 None。
    阈值固定，不因读不到而放宽。
    """
    score_zero = _template_score(context, TPL_PENDING_ZERO, PENDING_ROI)
    if score_zero >= PENDING_TPL_THRESHOLD:
        return 0, f"tpl:zero={score_zero:.4f}"

    score_label = _template_score(context, TPL_PENDING_LABEL, PENDING_ROI)
    if score_label < PENDING_TPL_THRESHOLD:
        return None, f"tpl:none zero={score_zero:.4f} label={score_label:.4f}"

    texts = [text for text, _box in _ocr(context, PENDING_DIGIT_ROI)]
    for text in texts:
        value = _first_number(text)
        if value is not None:
            return value, f"tpl:label={score_label:.4f} digit={value}"
    return None, f"tpl:label={score_label:.4f} digit-unreadable raw={' '.join(texts)!r}"


def _read_pending_retry(context: Context, attempts: int = 3):
    """读计数；被「获得物品」弹窗挡住时点空白后重读。"""
    raw = ""
    for _ in range(attempts):
        value, raw = _read_pending(context)
        if value is not None:
            return value, raw
        if _close_reward_popup(context):
            continue
        time.sleep(0.6)
    return None, raw


def _wait_mail_page(context: Context, timeout: float = 12.0):
    """等待邮件页真正就绪（OCR 能读到待领取计数），最多 timeout 秒。"""
    deadline = time.time() + timeout
    raw = ""
    attempt = 0
    while True:
        value, raw = _read_pending(context)
        if value is not None:
            return value, raw
        attempt += 1
        if attempt % 3 == 0:
            _close_reward_popup(context)
        if time.time() >= deadline:
            return None, raw
        time.sleep(0.8)


def _acquire_lock() -> bool:
    """跨进程互斥：同一时间只允许一个邮件领取进程进入邮箱。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE_SEC:
                log.warning("[邮件] 清理陈旧锁 %s", LOCK_PATH.name)
                LOCK_PATH.unlink()
                fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                return False
        except OSError:
            return False
    try:
        os.write(fd, f"pid={os.getpid()} at={time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
    finally:
        os.close(fd)
    return True


def _release_lock():
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def _abort_active():
    """失败/中断清理：结束本进程的幂等标记并释放锁，避免 5 分钟假跳过。"""
    global _ACTIVE_MARKER, _ACTIVE_LOCK
    if _ACTIVE_MARKER is not None:
        _finish_run(_ACTIVE_MARKER, 0, "aborted_before_exec")
        _ACTIVE_MARKER = None
    if _ACTIVE_LOCK:
        _release_lock()
        _ACTIVE_LOCK = False


def _read_button(context: Context) -> str:
    return _join(_ocr(context, BUTTON_ROI))


def _read_reward(context: Context) -> str:
    return _join(_ocr(context, REWARD_ROI))


def _close_reward_popup(context: Context) -> bool:
    text = _join(_ocr(context, POPUP_ROI))
    if any(token in text for token in POPUP_TOKENS):
        _tap(context, *POPUP_TAP)
        time.sleep(0.8)
        return True
    return False


def _find_one_click(context: Context):
    for text, box in _ocr(context, ONE_CLICK_ROI):
        if "一键领取" in text:
            center = _center(box)
            if center:
                return center
    return None


def _row_title(context: Context, y: int) -> str:
    roi = [ROW_TITLE_ROI[0], max(0, y - 26), ROW_TITLE_ROI[2], ROW_TITLE_ROI[3]]
    return _join(_ocr(context, roi))


def _scroll_list(context: Context) -> bool:
    """列表上滑一页；返回画面是否发生变化。"""
    image = context.tasker.controller.post_screencap().wait().get()
    before = image.tobytes()
    (x1, y1), (x2, y2), duration = LIST_SWIPE
    context.tasker.controller.post_swipe(x1, y1, x2, y2, duration).wait()
    time.sleep(0.9)
    image2 = context.tasker.controller.post_screencap().wait().get()
    return image2.tobytes() != before


def _button_is_claimable(text: str) -> bool:
    return "领取" in text and "已领取" not in text and "删除" not in text


def _verify_bulk_done(context: Context) -> bool:
    """一键领取后待领取=0 的佐证：至少一封「删除」+ 底部「已领取」，且无残留「领取」。"""
    seen_deleted = False
    seen_claimed = False
    for y in ROW_TITLE_YS:
        _tap(context, ROW_CLICK_X, y)
        time.sleep(0.5)
        btn = _read_button(context)
        reward = _read_reward(context)
        if _button_is_claimable(btn):
            log.error("[邮件] 一键领取后仍发现「领取」按钮（行 y=%s）：%r", y, btn)
            return False
        if "删除" in btn:
            seen_deleted = True
        if "已领取" in reward:
            seen_claimed = True
    return seen_deleted and seen_claimed


def _claim_selected(context: Context):
    """对当前选中邮件点「领取」并做三重校验。

    返回 (status, detail)：
      ok   —— 计数 -1 + 按钮变「删除」+ 底部「已领取」
      noop —— 点击后计数不变、按钮仍是「领取」、底部无「已领取」：视为无附件/无效，跳过
      fail —— 其它任何不一致：不假成功
    """
    before, _ = _read_pending_retry(context)
    if before is None:
        return "fail", "待领取计数不可读"

    center = None
    for text, box in _ocr(context, BUTTON_ROI):
        if _button_is_claimable(text):
            center = _center(box)
            break
    if not center:
        return "noop", f"未找到「领取」按钮（before={before}）"

    _tap(context, *center)
    time.sleep(1.2)
    _close_reward_popup(context)

    after, _ = _read_pending_retry(context)
    btn = _read_button(context)
    reward = _read_reward(context)
    detail = f"before={before} after={after} btn={btn!r} reward={reward!r}"

    if after is not None and after == before - 1 and "删除" in btn and "已领取" in reward:
        return "ok", detail
    if after == before and _button_is_claimable(btn) and "已领取" not in reward:
        return "noop", detail
    return "fail", detail


def _route_skip(context: Context, reason: str) -> bool:
    """幂等跳过：把「邮件领取_门禁」的 next 覆盖成「邮件领取_跳过」终端。

    不能用 return False 表达跳过：on_error 同时承载「后续节点识别超时」，
    两者混在一起会把导航失败算成跳过（2026-11-10 假成功）。
    """
    try:
        if context.override_next("邮件领取_门禁", ["邮件领取_跳过"]):
            _trace(f"gate skip routed reason={reason}")
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("[邮件] 跳过分流覆盖异常（%s）：%s", reason, exc)
    log.error("[邮件] 跳过分流覆盖失败（%s），按失败处理", reason)
    return False


class MailClaimGateAction(CustomAction):
    """进邮箱之前的幂等门禁：5 分钟内已有记录 / 已有进程在跑 → 直接跳过，不碰设备。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _ACTIVE_MARKER, _ACTIVE_LOCK

        # pipeline 重入（例如锚点未命中后回退再进来）时，本进程已经放行过，
        # 直接继续，不能把自己的标记误判成「另一进程/重跑」。
        if _ACTIVE_MARKER is not None and _ACTIVE_LOCK:
            log.info("[邮件] 本次任务已通过幂等门禁，继续执行")
            _trace("gate reenter pass")
            return True

        recent = _recent_marker(RUN_KEY)
        if recent:
            log.info("[邮件] 5 分钟内已有执行记录（%s），跳过，不进入邮箱", recent.name)
            _trace(f"gate skip recent={recent.name}")
            return _route_skip(context, f"recent={recent.name}")
        if not _acquire_lock():
            log.info("[邮件] 另一进程正在执行邮件领取，跳过（并发互斥），不进入邮箱")
            _trace("gate skip lock")
            return _route_skip(context, "lock")
        _ACTIVE_LOCK = True
        _ACTIVE_MARKER = _begin_run(RUN_KEY)
        log.info("[邮件] 幂等门禁放行（%s），开始进入邮箱", _ACTIVE_MARKER.name)
        _trace(f"gate pass marker={_ACTIVE_MARKER.name}")
        return True


class MailClaimAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _ACTIVE_MARKER, _ACTIVE_LOCK

        raw = getattr(argv, "custom_action_param", None) or "{}"
        try:
            param = json.loads(raw)
            if isinstance(param, str):
                param = json.loads(param)
        except (json.JSONDecodeError, TypeError):
            param = {}
        if not isinstance(param, dict):
            param = {}

        if param.get("abort"):
            _abort_active()
            log.info("[邮件] 失败清理：已结束幂等标记并释放锁")
            _trace("abort cleanup")
            return True

        max_claims = int(param.get("max_claims") or 20)
        max_scrolls = int(param.get("max_scrolls") or 6)
        dry_run = bool(param.get("dry_run"))

        marker = None
        owns_lock = False
        if not dry_run:
            if _ACTIVE_MARKER is not None:
                marker = _ACTIVE_MARKER
                _ACTIVE_MARKER = None
                owns_lock = _ACTIVE_LOCK
                _ACTIVE_LOCK = False
            else:
                # 兜底：没有经过 gate（例如直接单节点执行）时自行加锁
                recent = _recent_marker(RUN_KEY)
                if recent:
                    log.info(
                        "[邮件] 疑似重跑，跳过（发现 %s，300 秒内），不重复进入邮箱",
                        recent.name,
                    )
                    return True
                if not _acquire_lock():
                    log.info("[邮件] 另一进程正在执行邮件领取，跳过（并发互斥）")
                    return True
                owns_lock = True
                marker = _begin_run(RUN_KEY)

        claimed_total = 0
        note = ""

        try:
            initial, raw_text = _wait_mail_page(context)
            if initial is None:
                log.error("[邮件] 无法读取待领取计数（依据=%r），判定失败，避免假成功", raw_text)
                _trace(f"enter pending_unreadable raw={raw_text!r}")
                note = "pending_unreadable"
                return False

            _save_frame(context, "enter")
            log.warning("[邮件] 进入邮箱：待领取邮件=%s（%s）", initial, raw_text)
            _trace(
                f"enter pending={initial} raw={raw_text!r} "
                f"state={'可领' if initial else '无可领'}"
            )

            if initial == 0:
                log.info("[邮件] 本次结果：实际领取 0 封，跳过 0 封，待领取=0（无待领取附件，不做任何点击）")
                _trace("empty path success (no claim clicks) state=无可领")
                note = "empty claimed=0 skipped=0"
                return True

            pending = initial
            skipped_passive = 0
            skipped_invalid = 0

            # 1) 优先一键领取（最多 3 次；计数不降即判定不可用）
            for _ in range(3):
                if pending == 0:
                    break
                center = _find_one_click(context)
                if not center:
                    log.info("[邮件] 未发现「一键领取」按钮，转逐封")
                    break
                _tap(context, *center)
                time.sleep(1.4)
                _close_reward_popup(context)
                after, raw_after = _read_pending_retry(context)
                if after is None:
                    log.warning("[邮件] 一键领取后计数不可读（%r），转逐封兜底", raw_after)
                    break
                if after < pending:
                    log.info("[邮件] 一键领取：待领取 %s -> %s", pending, after)
                    _trace(f"oneclick {pending}->{after} state=可领")
                    pending = after
                else:
                    log.info("[邮件] 一键领取点击后计数未下降（%s），判定一键不可用，转逐封", pending)
                    _trace(f"oneclick no progress pending={pending}")
                    break

            if pending == 0:
                if _verify_bulk_done(context):
                    claimed_total = initial
                    log.info(
                        "[邮件] 一键领取完成，三重佐证成立：待领取=0 + 按钮「删除」 + 底部「已领取」"
                    )
                    note = f"one_click total={claimed_total}"
                    return True
                log.error(
                    "[邮件] 一键领取后待领取=0，但未取得「删除」/「已领取」佐证，判定失败（不假成功）"
                )
                note = "bulk_verify_failed"
                return False

            # 2) 逐封兜底
            single_claims = 0
            tried = set()
            skipped_titles = set()
            scrolls = 0

            while pending > 0 and single_claims < max_claims:
                progressed = False
                for y in ROW_TITLE_YS:
                    if pending == 0 or single_claims >= max_claims:
                        break
                    key = (scrolls, y)
                    if key in tried:
                        continue
                    tried.add(key)

                    title = _row_title(context, y)
                    if title and title in skipped_titles:
                        continue

                    _tap(context, ROW_CLICK_X, y)
                    time.sleep(0.6)
                    btn = _read_button(context)
                    reward = _read_reward(context)

                    if not _button_is_claimable(btn):
                        log.info(
                            "[邮件] 跳过无附件/已领取：y=%s 标题=%r 按钮=%r 底部=%r",
                            y, title, btn, reward,
                        )
                        _trace(f"skip y={y} title={title!r} btn={btn!r} state=灰态")
                        skipped_passive += 1
                        continue

                    status, detail = _claim_selected(context)
                    if status == "ok":
                        single_claims += 1
                        pending, _ = _read_pending_retry(context)
                        if pending is None:
                            log.error("[邮件] 领取后计数不可读，判定失败：%s", detail)
                            _trace(f"single pending_unreadable {detail}")
                            note = f"pending_unreadable_after {detail}"
                            return False
                        progressed = True
                        log.info(
                            "[邮件] 单封领取成功：y=%s 标题=%r 待领取=%s，按钮=删除，底部=已领取（%s）",
                            y, title, pending, detail,
                        )
                        _trace(f"single ok y={y} pending={pending} {detail}")
                        continue

                    if status == "noop":
                        if title:
                            skipped_titles.add(title)
                        log.warning(
                            "[邮件] 判定为无附件/无效邮件，跳过且不再点：y=%s 标题=%r（%s）",
                            y, title, detail,
                        )
                        skipped_invalid += 1
                        _trace(f"single noop y={y} title={title!r}")
                        continue

                    log.error("[邮件] 单封领取校验失败：y=%s 标题=%r（%s）", y, title, detail)
                    _trace(f"single fail y={y} title={title!r} detail={detail}")
                    note = f"single_verify_failed {detail}"
                    return False

                if pending == 0:
                    break
                if progressed:
                    continue
                if scrolls >= max_scrolls:
                    break
                if not _scroll_list(context):
                    log.info("[邮件] 列表滚动无变化，停止翻页")
                    break
                scrolls += 1
                log.info("[邮件] 已翻页 %s/%s", scrolls, max_scrolls)

            claimed_total = max(0, initial - pending)
            if pending == 0:
                log.info(
                    "[邮件] 本次结果：实际领取 %s 封（单封 %s），跳过 %s 封（无附件/已领取 %s，无效 %s），待领取=0",
                    claimed_total, single_claims,
                    skipped_passive + skipped_invalid, skipped_passive, skipped_invalid,
                )
                _trace(
                    f"done ok total={claimed_total} single={single_claims} "
                    f"skipped_passive={skipped_passive} skipped_invalid={skipped_invalid}"
                )
                note = (
                    f"ok single={single_claims} total={claimed_total} "
                    f"skipped={skipped_passive + skipped_invalid}"
                )
                return True

            log.warning(
                "[邮件] 本次结果：实际领取 %s 封，跳过 %s 封（无附件/已领取 %s，无效 %s），"
                "待领取=%s（未清零，判失败）",
                claimed_total, skipped_passive + skipped_invalid,
                skipped_passive, skipped_invalid, pending,
            )
            note = (
                f"partial single={single_claims} pending={pending} "
                f"skipped={skipped_passive + skipped_invalid}"
            )
            return False
        finally:
            _finish_run(marker, claimed_total, note)
            if owns_lock:
                _release_lock()
