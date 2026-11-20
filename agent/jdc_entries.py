import json
import time

from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

import jdc_select_character as jdc


def _param(
    argv
):

    try:

        if argv.custom_action_param:

            return json.loads(
                argv.custom_action_param
            )

    except Exception:

        pass

    return {}


class JdcEntryConfig(CustomAction):
    """记录本次任务的入场次数配置（进入次数 / 清空免费次数）。"""

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg
    ) -> bool:

        try:

            param = _param(
                argv
            )

            count = param.get(
                "count",
                0
            )

            until_empty = str(
                param.get(
                    "until_empty",
                    "false"
                )
            ).strip().lower() in (
                "true",
                "1",
                "yes",
                "on"
            )

            jdc.set_entry_config(
                count,
                until_empty
            )

            return True

        except Exception as e:

            print(
                "[角斗场] 写入入场配置失败："
                +
                repr(e)
            )

            return False


class JdcEntryDone(CustomAction):
    """一轮打完（领完全部奖励）后累计一次入场。"""

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg
    ) -> bool:

        try:

            jdc.record_entry_done()

            return True

        except Exception as e:

            print(
                "[角斗场] 记录入场次数失败："
                +
                repr(e)
            )

            return False


class JdcTeamBack(CustomAction):
    """编队页收尾：面板还在就点左上返回，已经关了就直接放行。

    旧实现是 OCR「清空队伍」+ 点返回；面板关掉后同一轮再被命中时，
    OCR 目标已消失，会空等满 20s 超时并把整条任务判失败。
    """

    TEAM_PANEL_ROI = [47, 544, 87, 80]

    BACK_ARROW = (79, 31)

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg
    ) -> bool:

        try:

            image = (
                context
                .tasker
                .controller
                .post_screencap()
                .wait()
                .get()
            )

            candidates = jdc.ocr_text_candidates(
                context,
                image,
                self.TEAM_PANEL_ROI
            )

        except Exception as e:

            print(
                "[角斗场] 编队返回：读屏失败，按面板已关处理："
                +
                repr(e)
            )

            return True

        center_x = None

        for text, cx in candidates:

            if "清空队伍" in text:
                center_x = cx
                break

        if center_x is None:

            print(
                "[角斗场] 编队面板已关闭，直接继续推关"
            )

            return True

        x = int(center_x) - 12

        try:

            context.tasker.controller.post_click(
                x,
                self.BACK_ARROW[1]
            ).wait()

            print(
                "[角斗场] 编队页返回已点击："
                f"({x},{self.BACK_ARROW[1]})"
            )

        except Exception as e:

            print(
                "[角斗场] 编队页返回点击失败："
                +
                repr(e)
            )

            return False

        time.sleep(1.5)

        return True


class JdcNeedNextEntry(CustomRecognition):
    """还差次数时才命中，供管道决定「再入场」还是「收尾回家」。"""

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg
    ):

        try:

            need = jdc.need_next_entry()

        except Exception as e:

            print(
                "[角斗场] 判断入场次数失败："
                +
                repr(e)
            )

            return None

        print(
            "[角斗场] 是否需要再入场："
            + (
                "是"
                if need
                else
                "否"
            )
            + f"（已打 {jdc.jdc_entry_done} 次）"
        )

        if not need:

            return None

        return CustomRecognition.AnalyzeResult(
            box=(
                0,
                0,
                1,
                1
            ),
            detail="need_next_entry"
        )
