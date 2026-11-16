# -*- coding: utf-8 -*-
"""战斗 HUD「自动战斗」前置：**确保**开关处于开启态（三段式纠正闸）。

2026-11-03 定稿。自动战斗状态是用户可改、且会被上一次手动操作改变的 UI 状态，
不能假定「上次是开态就还开着」，因此本识别不是「验证已开」，而是主动纠正：

- 已开：按钮区域通过「颜色 + 形状」双重校验（连续 2 帧）-> 直接通过，**不点击**。
- 关态：AutoOff 模板连续 2 帧命中（阈值 0.9）-> 点击图标中心 -> 0.9s ->
  重新抓帧，要求开态双重校验连续 2 帧通过；仍关再点一次（最多 MAX_CLICKS 次），
  两次仍不成才判失败。
- 识别不到按钮（BOSS 演出中/HUD 未出现）：**不是失败**，继续抓帧等待；
  总预算 >=60s（CONFIRM_BUDGET_SEC），预算内 HUD 出现即回到上面两条。
  只有预算耗尽后依然既判不到开态、也点不到按钮才判失败。

HUD 首现时刻通过 HUD_FIRST_SEEN 暴露给 agent 侧 60s 战斗看门狗
（arena_pipeline.py: page:battle_timeout）。

## 判据（2026-11-03 离线实测，1280×720 基准）

### 第一维：颜色

ROI `[1096,34,78,46]`（与旧模板框完全一致）。琥珀/开态像素区间：

- R 160–255、G 110–230、B 30–180，且 R-B >= 45、G-B >= 20；
- 兼容 RGB/BGR：同时计算通道互换后的 BGR 分支，任一分支成立即计为琥珀像素；
- ROI 内琥珀像素总数 `[120, 1000]`：下限覆盖按钮实心区域、排除孤立噪点；
  上限排除整片金色特效页（实测 `poll_088.png` 为 3588）。

### 第二维：形状（最大连通块 + 主体块联合包围盒）

只靠计数挡不住「落在 120–1000 区间内的大面积金色」——实测 `poll_093/100–119`
（金色战斗横幅）为 774，计数会误判为开态。因此再对掩码做 8 邻域连通域分析：

1. 最大连通块面积 `60–130`：正例两半箭头各 79–85 px；金色横幅最大块 528、
   整片金色 3588、主界面噪点 28、列表碎点 4。
2. 「主体块」联合包围盒：只取面积 >= 最大块 50% 的连通块（开态图标是**两个
   等大的环状箭头**，碎片/金色文字条会被这一刀切掉），用它们的并集外接框
   代替单块框。单块框不可用——最大块会随两半箭头谁大谁小而左右翻转，
   正例实测宽高比 0.500–2.111，与负例 `poll_093` 的 0.632 直接重叠。
   联合框实测：宽 20–24、高 22–23、宽高比 0.909–1.091。
   - 宽 17–28、高 18–27（正例实测上下各留 >=15% 余量）；
   - 宽高比 0.77–1.26（正例 0.909–1.091，各留 15%）；
   - 填充率 0.26–0.42（环形图标不是实心块，正例 0.305–0.366，留 15%）：
     实心金块填充率 1.0、金色横幅 0.579、列表碎点 0.067 都被排除。
3. 连续 2 帧全部达标才通过（CONFIRM_FRAMES），保持原有复核节奏。

实机帧实测（同一 ROI，1280×720）：

| 帧 | 状态 | 琥珀像素 | 最大块面积 | 主体框 | 宽高比 | 填充率 |
| --- | --- | --- | --- | --- | --- | --- |
| `04_observe_b.png` | 开态 | 161 | 81 | 24×22 | 1.091 | 0.305 |
| `39_hud_hit.png` | 开态 | 166 | 84 | 21×22 | 0.955 | 0.359 |
| `20_b_02.png` | 开态 | 165 | 83 | 24×22 | 1.091 | 0.313 |
| `poll_049.png` | 开态 | 163 | 82 | 22×22 | 1.000 | 0.337 |
| `poll_053.png` | 开态 | 163 | 84 | 24×22 | 1.091 | 0.309 |
| `poll_063.png` | 开态 | 196 | 85 | 21×23 | 0.913 | 0.348 |
| `poll_047.png` | 关态 | 0 | – | – | – | – |
| `poll_088.png` | 整片金色 | 3588 | 3588 | 78×46 | 1.696 | 1.000 |
| `poll_093.png` / `poll_100–119.png` | 金色横幅 | 774 | 528 | 24×38 | 0.632 | 0.579 |
| `15_arena_list_pre.png` | 对手列表 | 21 | 4 | 10×15 | 0.667 | 0.067 |
| `92_main_final.png` | 主界面 | 65 | 28 | 12×14 | 0.857 | 0.250 |

`poll_088.png` 被面积上限（3588 > 130）、宽 78 > 28、高 46 > 27、宽高比 1.696 > 1.26、
填充率 1.0 > 0.42 同时挡掉；`poll_093.png` 被面积上限（528 > 130）、高 38 > 27、
宽高比 0.632 < 0.77、填充率 0.579 > 0.42 挡掉。两者都不在计数区间外，是形状判据
真正起作用的样本。

`AutoOn.png` 不再参与判定（旧模板分数 0.802 vs 阈值 0.8 仅 0.002 余量，已废弃）；
`AutoOff.png` 仍用于关态识别（实测 0.962 vs 阈值 0.9，余量 0.062）。

## 为什么不用 MaaFramework 原生 ColorMatch

原生 `ColorMatch` 只能给出「颜色区间 + 像素总数 + 联合外接框（`connected: false`）」，
没有「最大连通块面积 / 连通块个数 / 填充率」这类图元，也无法在 pipeline 里表达
宽高比约束；本判据必须逐连通块判断，故保留 agent 侧 CustomRecognition。
`_amber_metrics` 返回不到框时只回 `None`，绝不回 `(0,0,0,0)`（P-004 的已知缺陷）。
"""

from __future__ import annotations

import logging
import time
from collections import deque

import numpy as np

from maa.custom_recognition import CustomRecognition

from stop_guard import ActionStopped, ensure_running

log = logging.getLogger("combat.autobattle")

# 1280×720 基准：战斗 HUD 右上角自动战斗图标（帮助 ? / 快进 / 自动战斗 / 暂停 中的第三个）
ROI_1280 = (1096, 34, 78, 46)
# 第一维：琥珀像素计数区间。下限覆盖按钮实心区域；上限排除整片金色特效页。
AMBER_MIN_COUNT = 120
AMBER_MAX_COUNT = 1000
# 琥珀色区间（RGB 分支；BGR 分支自动镜像）。区间边界距实测极值 >=13 级。
AMBER_R_MIN, AMBER_R_MAX = 160, 255
AMBER_G_MIN, AMBER_G_MAX = 110, 230
AMBER_B_MIN, AMBER_B_MAX = 30, 180
AMBER_RB_MIN = 45
AMBER_GB_MIN = 20
# 第二维：形状。基准值按 1280×720 实测，运行时按 ROI 实际尺寸缩放。
MAJOR_FRACTION = 0.5          # 只把面积 >= 最大块 50% 的块算作「主体块」
BLOCK_MIN_AREA = 60           # 最大连通块面积下限（正例 79–85）
BLOCK_MAX_AREA = 130          # 最大连通块面积上限（金色横幅 528 / 整片金 3588）
ICON_MIN_WIDTH, ICON_MAX_WIDTH = 17, 28      # 主体框宽（正例 20–24）
ICON_MIN_HEIGHT, ICON_MAX_HEIGHT = 18, 27    # 主体框高（正例 22–23）
ICON_MIN_ASPECT, ICON_MAX_ASPECT = 0.77, 1.26  # 宽高比（正例 0.909–1.091）
ICON_MIN_FILL, ICON_MAX_FILL = 0.26, 0.42      # 填充率（正例 0.305–0.366）
# 关态辅助判据：模板匹配（实测正确态 0.962，阈值 0.9 留 0.062 余量）
OFF_TEMPLATE = "Battle/AutoOff.png"
OFF_THRESHOLD = 0.9
# 同一判据需连续命中多少帧才下结论
CONFIRM_FRAMES = 2
# 帧间隔（秒）：两次复核之间重新抓帧的最小间隔
CONFIRM_FRAME_GAP_SEC = 0.4
# 未识别到按钮时放慢抓帧，避免演出期刷屏
IDLE_FRAME_GAP_SEC = 1.0
# 总等待预算（秒）：必须 >=60，覆盖「演出关闭后仍需加载」的最坏情况
CONFIRM_BUDGET_SEC = 65.0
CLICK_SETTLE_SEC = 0.9
# 最多点击次数：复核仍关态时只允许再点一次，防止在错误页连点
MAX_CLICKS = 2

# 战斗 HUD 首次出现（time.monotonic）；供 arena_pipeline 的 60s 看门狗起算。
HUD_FIRST_SEEN = None


def reset_hud_clock() -> None:
    """每次发起挑战前清零，避免沿用上一场的首现时间。"""
    global HUD_FIRST_SEEN
    HUD_FIRST_SEEN = None


def hud_first_seen():
    """返回本场战斗 HUD 首次出现的时间戳；尚未出现返回 None。"""
    return HUD_FIRST_SEEN


def _mark_hud_seen() -> None:
    global HUD_FIRST_SEEN
    if HUD_FIRST_SEEN is None:
        HUD_FIRST_SEEN = time.monotonic()


class CombatAutoStateRecognition(CustomRecognition):
    """确保自动战斗为开态；仅总预算耗尽仍无法确认时返回失败。"""

    def analyze(self, context, argv):
        expected = "on"
        param = getattr(argv, "custom_recognition_param", None)
        if isinstance(param, dict):
            expected = str(param.get("expected", "on"))
        try:
            ensure_running(context)
        except ActionStopped:
            return None

        image = argv.image
        deadline = time.monotonic() + CONFIRM_BUDGET_SEC
        clicked = 0
        last = {}
        last_off = None
        on_streak = 0
        off_streak = 0
        while True:
            metrics = self._amber_metrics(image)
            amber = metrics["count"]
            # HUD 首现沿用旧的「琥珀计数进入区间」口径，看门狗计时不受形状判据影响。
            if AMBER_MIN_COUNT <= amber <= AMBER_MAX_COUNT:
                _mark_hud_seen()
            on_hit = metrics["ok"]
            off = None
            if on_hit:
                on_streak += 1
                off_streak = 0
                last = metrics
                box = metrics["box"]
            elif AMBER_MIN_COUNT <= amber <= AMBER_MAX_COUNT:
                # 颜色在区间但形状不过（金色横幅/特效）——不是关态，继续观察。
                on_streak = 0
                off_streak = 0
                last = metrics
                off = self._match_off(context, image)
            else:
                on_streak = 0
                off = self._match_off(context, image)
                if off is not None:
                    off_streak += 1
                    on_streak = 0
                    box, score = off
                    last_off = round(score, 3)
                else:
                    off_streak = 0

            # 分支 A：已开 -> 直接通过，不点击。
            if on_hit and on_streak >= CONFIRM_FRAMES:
                log.info(
                    "自动战斗已开启（点击 %d 次，连续 %d 帧颜色+形状达标：琥珀 %d，"
                    "最大块 %d，主体框 %s，宽高比 %.3f，填充率 %.3f）",
                    clicked, on_streak, amber, metrics["largest_area"],
                    last.get("box_size"), metrics["aspect"], metrics["fill"],
                )
                return CustomRecognition.AnalyzeResult(
                    box=tuple(box),
                    detail={
                        "state": "on",
                        "clicked": clicked,
                        "amber_count": amber,
                        "block_area": metrics["largest_area"],
                        "icon_box": metrics["box_size"],
                        "aspect": metrics["aspect"],
                        "fill": metrics["fill"],
                        "off_score": last_off,
                        "frames": on_streak,
                        "hud_first_seen": HUD_FIRST_SEEN,
                    },
                )

            # 分支 B：关态 -> 点击纠正并复核。
            if off is not None and off_streak >= CONFIRM_FRAMES and expected == "on":
                if clicked >= MAX_CLICKS:
                    log.error(
                        "自动战斗仍为关闭（已点击 %d 次，AutoOff 连续 %d 帧命中，分数 %.3f），"
                        "纠正失败",
                        clicked, off_streak, score,
                    )
                    return None
                cx = box[0] + box[2] // 2
                cy = box[1] + box[3] // 2
                ensure_running(context)
                context.tasker.controller.post_click(cx, cy).wait()
                clicked += 1
                log.info(
                    "自动战斗当前关闭（AutoOff 连续 %d 帧分数 %.3f），"
                    "点击 (%d,%d) 开启并复核（第 %d 次）",
                    off_streak, score, cx, cy, clicked,
                )
                on_streak = 0
                off_streak = 0
                time.sleep(CLICK_SETTLE_SEC)
                image = context.tasker.controller.post_screencap().wait().get()
                ensure_running(context)
                continue

            # 分支 C：识别不到 -> 预算内继续等待，不判失败。
            if time.monotonic() >= deadline:
                if clicked:
                    log.error(
                        "自动战斗纠正失败：已点击 %d 次，%.0f 秒预算耗尽仍未复核为开态"
                        "（最后琥珀像素=%d，形状=%s，off_score=%s）",
                        clicked, CONFIRM_BUDGET_SEC, last.get("count", 0),
                        last.get("reason"), last_off,
                    )
                else:
                    log.error(
                        "战斗 HUD 未确认：%.0f 秒内开态颜色+形状未连续 %d 帧达标、"
                        "AutoOff 也未连续 %d 帧命中（最后琥珀像素=%d，形状=%s，off_score=%s）",
                        CONFIRM_BUDGET_SEC, CONFIRM_FRAMES, CONFIRM_FRAMES,
                        last.get("count", 0), last.get("reason"), last_off,
                    )
                return None

            gap = (
                CONFIRM_FRAME_GAP_SEC
                if (on_hit or off is not None)
                else IDLE_FRAME_GAP_SEC
            )
            time.sleep(gap)
            image = context.tasker.controller.post_screencap().wait().get()
            ensure_running(context)

    def _roi_box(self, image):
        h, w = image.shape[:2]
        sx, sy = w / 1280.0, h / 720.0
        return (
            int(ROI_1280[0] * sx), int(ROI_1280[1] * sy),
            int(ROI_1280[2] * sx), int(ROI_1280[3] * sy),
        )

    def _amber_metrics(self, image):
        """颜色 + 形状双重校验；返回计数、最大连通块与主体框，未通过时带 `reason`。

        框为空 / 像素不足时 `box` 为 None（绝不返回 (0,0,0,0)）。
        """
        result = {
            "count": 0, "ok": False, "reason": "no_pixels", "box": None,
            "box_size": None, "largest_area": 0, "aspect": 0.0, "fill": 0.0,
        }
        try:
            x, y, w, h = self._roi_box(image)
            mask = self._amber_mask(image, x, y, w, h)
        except Exception:
            log.warning("自动战斗琥珀色统计失败", exc_info=True)
            return result

        count = int(mask.sum())
        result["count"] = count
        if count <= 0:
            return result
        sx = w / float(ROI_1280[2])
        sy = h / float(ROI_1280[3])
        area_scale = sx * sy

        comps = self._components(mask)
        largest = max(comp["area"] for comp in comps)
        result["largest_area"] = largest
        if count < AMBER_MIN_COUNT:
            result["reason"] = "count_low"
            return result
        if count > AMBER_MAX_COUNT:
            result["reason"] = "count_high"
            return result
        if largest < BLOCK_MIN_AREA * area_scale:
            result["reason"] = "block_small"
            return result
        if largest > BLOCK_MAX_AREA * area_scale:
            result["reason"] = "block_large"
            return result

        # 主体块 = 面积 >= 最大块 50% 的连通块；开态图标是两个等大的环状箭头，
        # 碎片与金色文字条会被这一刀切掉。用并集外接框，避免单块随两半箭头翻转。
        majors = [c for c in comps if c["area"] >= largest * MAJOR_FRACTION]
        x0 = min(c["x"] for c in majors)
        y0 = min(c["y"] for c in majors)
        x1 = max(c["x"] + c["w"] - 1 for c in majors)
        y1 = max(c["y"] + c["h"] - 1 for c in majors)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        major_px = sum(c["area"] for c in majors)
        aspect = bw / float(bh)
        fill = major_px / float(bw * bh)
        result.update({
            "box": (x + x0, y + y0, bw, bh),
            "box_size": (bw, bh),
            "aspect": round(aspect, 3),
            "fill": round(fill, 4),
            "n_major": len(majors),
            "n_components": len(comps),
        })
        if not (ICON_MIN_WIDTH * sx <= bw <= ICON_MAX_WIDTH * sx):
            result["reason"] = "width"
        elif not (ICON_MIN_HEIGHT * sy <= bh <= ICON_MAX_HEIGHT * sy):
            result["reason"] = "height"
        elif not (ICON_MIN_ASPECT <= aspect <= ICON_MAX_ASPECT):
            result["reason"] = "aspect"
        elif not (ICON_MIN_FILL <= fill <= ICON_MAX_FILL):
            result["reason"] = "fill"
        else:
            result["reason"] = "pass"
            result["ok"] = True
        return result

    def _amber_mask(self, image, x, y, w, h):
        """ROI 内琥珀掩码；同时兼容 RGB / BGR 通道顺序（任一分支成立即算）。"""
        crop = image[y:y + h, x:x + w, :3].astype("int16")
        if crop.size == 0 or crop.ndim < 3 or crop.shape[2] < 3:
            return np.zeros((0, 0), dtype=bool)
        c0, c1, c2 = crop[:, :, 0], crop[:, :, 1], crop[:, :, 2]
        rgb = (
            (c0 >= AMBER_R_MIN) & (c0 <= AMBER_R_MAX)
            & (c1 >= AMBER_G_MIN) & (c1 <= AMBER_G_MAX)
            & (c2 >= AMBER_B_MIN) & (c2 <= AMBER_B_MAX)
            & ((c0 - c2) >= AMBER_RB_MIN) & ((c1 - c2) >= AMBER_GB_MIN)
        )
        bgr = (
            (c2 >= AMBER_R_MIN) & (c2 <= AMBER_R_MAX)
            & (c1 >= AMBER_G_MIN) & (c1 <= AMBER_G_MAX)
            & (c0 >= AMBER_B_MIN) & (c0 <= AMBER_B_MAX)
            & ((c2 - c0) >= AMBER_RB_MIN) & ((c1 - c0) >= AMBER_GB_MIN)
        )
        return rgb | bgr

    @staticmethod
    def _components(mask):
        """8 邻域连通域（ROI 只有 78×46，纯 Python 足够快）。"""
        height, width = mask.shape
        seen = np.zeros_like(mask, dtype=bool)
        comps = []
        for yy in range(height):
            row = mask[yy]
            for xx in range(width):
                if not row[xx] or seen[yy, xx]:
                    continue
                queue = deque([(yy, xx)])
                seen[yy, xx] = True
                ys_min = ys_max = yy
                xs_min = xs_max = xx
                area = 0
                while queue:
                    cy, cx = queue.popleft()
                    area += 1
                    if cy < ys_min:
                        ys_min = cy
                    elif cy > ys_max:
                        ys_max = cy
                    if cx < xs_min:
                        xs_min = cx
                    elif cx > xs_max:
                        xs_max = cx
                    for dy in (-1, 0, 1):
                        ny = cy + dy
                        if ny < 0 or ny >= height:
                            continue
                        for dx in (-1, 0, 1):
                            nx = cx + dx
                            if 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                                seen[ny, nx] = True
                                queue.append((ny, nx))
                comps.append({
                    "area": area, "x": xs_min, "y": ys_min,
                    "w": xs_max - xs_min + 1, "h": ys_max - ys_min + 1,
                })
        return comps

    def _match_off(self, context, image):
        x, y, w, h = self._roi_box(image)
        try:
            rd = context.run_recognition(
                "战斗_自动战斗关态",
                image,
                pipeline_override={
                    "战斗_自动战斗关态": {
                        "recognition": "TemplateMatch",
                        "roi": [x, y, w, h],
                        "template": OFF_TEMPLATE,
                        "threshold": OFF_THRESHOLD,
                        "rate_limit": 0,
                    }
                },
            )
        except Exception:
            log.warning("自动战斗关态模板识别失败", exc_info=True)
            return None
        if rd and rd.hit and rd.best_result:
            return tuple(rd.best_result.box), float(rd.best_result.score)
        return None
