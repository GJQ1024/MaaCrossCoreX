import json
import logging
import time
from pathlib import Path

import diag
import tutorial_state
from chip_build_select import ChipFilterSelectAction, ChipNoTargetAction
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

log = logging.getLogger("laa.action")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TUTORIAL_STATE_PATH = tutorial_state.STATE_PATH
ACCOUNT_CACHE_PATH = tutorial_state.ACCOUNT_CACHE_PATH
ACCOUNT_STATE_PATH = tutorial_state.ACCOUNT_STATE_PATH
TUTORIAL_EVENT_PATH = tutorial_state.EVENT_PATH


@AgentServer.custom_action("my_action_111")
class MyCustomAction(CustomAction):

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:

        print("my_action_111 is running!")

        return True


@AgentServer.custom_action("task_fail")
class TaskFailAction(CustomAction):
    """明确失败出口：custom_action_param = {"reason": "...", "cleanup_entry": "..."}。

    用于取代「无 next 的终端节点」：识别不到预期页面时让任务判失败，
    而不是点一下任意文本就报成功（假成功）。返回 False 即失败。

    cleanup_entry：可选的收尾入口。框架里 PipelineTask::run 以
    `!error_handling` 作返回值，而任何一次识别成功都会把 error_handling
    清掉——所以「失败后再走 next/on_error 收尾链」最终会被判成成功。
    这里改为在动作内用 context.run_task 先跑完收尾（回主界面），再返回
    False 并让节点 on_error 为空，外层的 error_handling 才会保持到结尾，
    做到「任务判失败 + 设备已干净收尾」。
    """

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        raw = getattr(argv, "custom_action_param", None) or "{}"
        param: dict = {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            if isinstance(parsed, dict):
                param = parsed
            reason = str(param.get("reason") or "未提供原因")
        except (json.JSONDecodeError, TypeError):
            reason = str(raw)
        log.error("任务明确失败：%s", reason)

        cleanup = str(param.get("cleanup_entry") or "").strip()
        if cleanup:
            try:
                context.run_task(cleanup)
            except Exception as exc:
                log.error("失败收尾「%s」执行异常：%s", cleanup, exc)
        return False


@AgentServer.custom_action("cost_guard")
class CostGuardAction(CustomAction):
    """付费货币闸：OCR 指定区域，命中 deny 且未命中 allow 时改道。

    custom_action_param:
      roi:   [x, y, w, h]（默认全屏）
      allow: ["构建蓝图", ...]  命中即放行（优先于 deny）
      deny:  ["粲晶", "星贸凭证", "¥", "$", ...]
      unknown: "allow" | "deny"（都没命中时如何处理，默认 allow）
      on_deny: ["节点"]（改道目标；为空则返回 False 让节点失败）
      reason: 日志原因
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        raw = getattr(argv, "custom_action_param", None) or "{}"
        try:
            param = json.loads(raw)
            if isinstance(param, str):
                param = json.loads(param)
        except (json.JSONDecodeError, TypeError):
            param = {}
        if not isinstance(param, dict):
            param = {}
        roi = list(param.get("roi") or [0, 0, 1280, 720])
        allow = [str(item) for item in (param.get("allow") or [])]
        deny = [str(item) for item in (param.get("deny") or [])]
        unknown = str(param.get("unknown") or "allow").lower()
        on_deny = param.get("on_deny") or []
        if isinstance(on_deny, str):
            on_deny = [on_deny]
        reason = str(param.get("reason") or "命中付费货币关键词")

        image = context.tasker.controller.post_screencap().wait().get()
        result = context.run_recognition(
            "通用_OCR探针", image,
            pipeline_override={
                "通用_OCR探针": {
                    "recognition": "OCR",
                    "roi": roi,
                    "expected": [],
                }
            },
        )
        texts = []
        items = []
        if result:
            candidates = [getattr(result, "best_result", None)]
            candidates.extend(getattr(result, "all_results", None) or [])
            for item in candidates:
                value = getattr(item, "text", None)
                if value is not None:
                    texts.append(str(value))
                    items.append((str(value), getattr(item, "box", None)))
        joined = " ".join(texts)
        hit_allow = any(key in joined for key in allow)
        hit_deny = any(key in joined for key in deny)

        if hit_allow:
            if param.get("click_allow"):
                for value, box in items:
                    if any(key in value for key in allow):
                        if box:
                            x = int(box[0]) + int(box[2]) // 2
                            y = int(box[1]) + int(box[3]) // 2
                            context.tasker.controller.post_click(x, y).wait()
                            log.info("费用闸按白名单点击：%s @(%d,%d)", value, x, y)
                            break
            log.info("费用闸放行：%s", joined[:120])
            return True
        if hit_deny or unknown == "deny":
            log.error("费用闸拦截（%s）：%s", reason, joined[:120])
            if on_deny:
                context.override_next(argv.node_name, list(on_deny))
                return True
            return False
        log.info("费用闸未命中关键词，放行：%s", joined[:120])
        return True


@AgentServer.custom_action("verified_click")
class VerifiedClickAction(CustomAction):
    """点击后复核；未生效再点（总次数 = 1 + retries），日志记重试次数。

    P-070/P-073：识别成功、坐标在按钮内、等待冻结已通过，但 UI 无变化，
    同区域手动点击有效 —— 判定为 MaaTouch 单次注入丢失。这里做「点后复核、
    未生效再点一次」，属重试而非兜底；仍未生效则判失败，不静默成功。

    custom_action_param:
      target_offset: [dx, dy]   相对识别框左上角的偏移；缺省为识别框中心
      target:        [x, y]     可选，绝对坐标（仅遗留节点使用）
      verify_roi_offset: [dx, dy, w, h]  复核区域相对识别框（优先于 verify_roi）
      verify_roi:    [x, y, w, h]  复核区域，默认全屏
      verify:        {"gone": ["文本", ...]}  全部消失即生效
      verify_change: true       或：verify_roi 画面变化即生效
      change_threshold: 5.0     画面变化的平均像素差阈值（0-255）
      retries:       1          未生效时的补充点击次数
      delay:         0.9        点击后到复核的等待秒数
      on_fail:       ["节点", ...]  重试耗尽后的改道兜底（失败已记 diag，不判整任务失败）
      reason:        日志标签
    """

    @staticmethod
    def _ocr_texts(context, roi):
        image = context.tasker.controller.post_screencap().wait().get()
        result = context.run_recognition(
            "通用_OCR探针", image,
            pipeline_override={
                "通用_OCR探针": {
                    "recognition": "OCR",
                    "roi": roi,
                    "expected": [],
                }
            },
        )
        texts = []
        if result:
            candidates = [getattr(result, "best_result", None)]
            candidates.extend(getattr(result, "all_results", None) or [])
            for item in candidates:
                value = getattr(item, "text", None)
                if value is not None:
                    texts.append(str(value))
        return " ".join(texts)

    @staticmethod
    def _roi_snapshot(context, roi):
        image = context.tasker.controller.post_screencap().wait().get()
        height, width = image.shape[:2]
        x, y, w, h = [int(v) for v in roi]
        x, y = max(0, min(x, width - 1)), max(0, min(y, height - 1))
        w, h = max(1, w), max(1, h)
        return image[y:min(y + h, height), x:min(x + w, width)].astype("int16").copy()

    def _resolve_verify_roi(self, param, box, reason):
        """复核区域：verify_roi_offset（相对识别框）> verify_roi > 全屏。

        P-111：基建资源点的复核区必须跟着识别框走。写死 verify_roi 时，
        资源点整体平移后复核的是旧位置，会把「没点到」误判成「领到了」。
        """
        offset = param.get("verify_roi_offset")
        if offset:
            if not (box and (box[2] or box[3])):
                log.error("复核点击[%s] 给了 verify_roi_offset 但没有识别框", reason)
                return None
            roi = [
                box[0] + int(offset[0]),
                box[1] + int(offset[1]),
                int(offset[2]),
                int(offset[3]),
            ]
        elif param.get("verify_roi"):
            roi = [int(v) for v in param["verify_roi"]]
        else:
            roi = [0, 0, 1280, 720]
        x, y, w, h = roi
        return [max(0, x), max(0, y), max(1, w), max(1, h)]

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        raw = getattr(argv, "custom_action_param", None) or "{}"
        try:
            param = json.loads(raw)
            if isinstance(param, str):
                param = json.loads(param)
        except (json.JSONDecodeError, TypeError):
            param = {}
        if not isinstance(param, dict):
            param = {}

        reason = str(param.get("reason") or argv.node_name)
        retries = max(0, int(param.get("retries") or 0))
        delay = float(param.get("delay") or 0.9)
        gone = [str(item) for item in ((param.get("verify") or {}).get("gone") or [])]
        change_threshold = float(param.get("change_threshold") or 5.0)
        on_fail = [str(item) for item in (param.get("on_fail") or [])]

        box = getattr(argv, "box", None)
        if param.get("target"):
            x, y = [int(v) for v in param["target"]]
        elif box and (box[2] or box[3]):
            offset = param.get("target_offset") or [box[2] / 2, box[3] / 2]
            x = int(box[0] + offset[0])
            y = int(box[1] + offset[1])
        else:
            log.error("复核点击[%s] 无可用点击坐标（识别框为空且未给 target）", reason)
            return False

        verify_roi = self._resolve_verify_roi(param, box, reason)
        if verify_roi is None:
            return False

        def effective(before):
            if gone:
                joined = self._ocr_texts(context, verify_roi)
                if all(token not in joined for token in gone):
                    return True, f"gone 成立（'{joined[:60]}'）"
                return False, f"'{joined[:60]}' 仍含 {'/'.join(gone)}"
            after = self._roi_snapshot(context, verify_roi)
            diff = float(abs(after - before).mean())
            return diff >= change_threshold, f"画面差 {diff:.2f} (阈值 {change_threshold:.1f})"

        attempts = 1 + retries
        before = None if gone else self._roi_snapshot(context, verify_roi)
        for attempt in range(1, attempts + 1):
            context.tasker.controller.post_click(x, y).wait()
            time.sleep(delay)
            ok, detail = effective(before)
            if ok:
                log.warning(
                    "复核点击[%s] 第%d次生效，retries_used=%d @(%d,%d) %s",
                    reason, attempt, attempt - 1, x, y, detail,
                )
                return True
            log.warning("复核点击[%s] 第%d次未生效 @(%d,%d) %s", reason, attempt, x, y, detail)
            if not gone and attempt < attempts:
                before = self._roi_snapshot(context, verify_roi)
        log.error(
            "复核点击[%s] 重试 %d 次仍未生效 @(%d,%d) box=%s roi=%s %s",
            reason, retries, x, y, list(box) if box is not None else None, verify_roi, detail,
        )
        diag.fail(
            "复核点击[%s]" % reason,
            actual="点击 %d 次后复核区无变化 @(%d,%d) box=%s roi=%s（%s）" % (
                attempts, x, y, list(box) if box is not None else None, verify_roi, detail,
            ),
            expect="点击后目标区域发生变化",
        )
        if on_fail:
            log.error("复核点击[%s] 走兜底改道：%s", reason, " → ".join(on_fail))
            context.override_next(argv.node_name, list(on_fail))
            return True
        return False


@AgentServer.custom_action("tutorial_once")
class TutorialOnceAction(CustomAction):
    """教程一次性闸：跨任务持久化计数，防止重跑教程时重复免费十连。

    状态键为 ``实例:账号UID``；旧键 ``实例`` 仅在「本机只记录过一个账号」时按可读基线使用。
    拿不到账号 UID、或旧键未迁移且归属不明时，**不猜**：跳过并记事件（fail-safe）。

    custom_action_param:
      operation:   "check" | "mark"
      key:         计数键，默认 tutorial_build10
      limit:       check 时的上限，默认 1
      on_done:     check 达到上限时的改道目标
      on_unknown:  账号未知 / 旧键归属不明时的改道目标（缺省复用 on_done）
      reason:      日志说明
    """

    @staticmethod
    def _targets(param: dict, name: str) -> list:
        value = param.get(name) or []
        return [value] if isinstance(value, str) else list(value)

    def _skip_unknown(self, context, argv, param, key, status, detail) -> bool:
        """账号维度不确定：跳过教程段并记录，绝不当作「已完成」或「未完成」去跑。"""
        tutorial_state.record_event(f"skip_{status}", key, detail, TUTORIAL_EVENT_PATH)
        targets = self._targets(param, "on_unknown") or self._targets(param, "on_done")
        log.error("一次性闸跳过（%s）：%s %s，改道 %s", detail, key, status, targets)
        if targets:
            context.override_next(argv.node_name, targets)
            return True
        return False

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        raw = getattr(argv, "custom_action_param", None) or "{}"
        try:
            param = json.loads(raw)
            if isinstance(param, str):
                param = json.loads(param)
        except (json.JSONDecodeError, TypeError):
            param = {}
        if not isinstance(param, dict):
            param = {}

        operation = str(param.get("operation") or "check").lower()
        key = str(param.get("key") or "tutorial_build10")
        limit = int(param.get("limit") or 1)
        instance = tutorial_state.instance_key()
        uid, uid_source = tutorial_state.current_uid(ACCOUNT_CACHE_PATH, ACCOUNT_STATE_PATH)

        data = tutorial_state.load_json(TUTORIAL_STATE_PATH)
        counts = data.get(key)
        if not isinstance(counts, dict):
            counts = {}
        count, status = tutorial_state.resolve_count(
            counts, instance, uid, tutorial_state.account_candidates(ACCOUNT_STATE_PATH)
        )

        if status not in ("ok", "new", "legacy-ok"):
            detail = {
                "unknown": "拿不到当前账号 UID",
                "legacy": "旧实例键未迁移且归属不明（先跑 scripts/migrate_tutorial_state.py --apply）",
                "corrupt": "计数不是整数",
            }.get(status, f"状态异常 {status}")
            if operation == "mark":
                tutorial_state.record_event(f"mark_{status}", key, detail, TUTORIAL_EVENT_PATH)
                log.error("一次性闸拒绝记数：%s（%s）", detail, key)
                return False
            return self._skip_unknown(context, argv, param, key, status, detail)

        if status == "legacy-ok":
            log.warning("一次性闸使用旧键（本机仅一个账号 uid=%s）：%s=%d", uid, key, count)
        log.info("一次性闸账号键：%s:%s（来源 %s）", instance, uid, uid_source)

        if operation == "check":
            if count >= limit:
                on_done = self._targets(param, "on_done")
                log.warning(
                    "一次性闸拦截（%s）：%s=%d/%d，改道 %s",
                    param.get("reason") or "已达上限", key, count, limit, on_done,
                )
                if on_done:
                    context.override_next(argv.node_name, list(on_done))
                    return True
                return False
            log.info("一次性闸放行：%s=%d/%d", key, count, limit)
            return True

        if operation == "mark":
            counts[tutorial_state.composite_key(instance, uid)] = count + 1
            data[key] = counts
            tutorial_state.save_json(TUTORIAL_STATE_PATH, data)
            log.info("一次性闸计数：%s=%d（%s）", key, count + 1, param.get("reason") or "")
            return True

        log.error("tutorial_once 未知操作：%s", operation)
        return False


@AgentServer.custom_action("chip_filter_select")
class ChipFilterSelect(ChipFilterSelectAction):
    pass


@AgentServer.custom_action("chip_no_target")
class ChipNoTarget(ChipNoTargetAction):
    pass
