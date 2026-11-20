# -*- coding: utf-8 -*-
"""翻页扫卡，抓独立的主技能 2/3 级详情帧（只读：不选料、不合成、不扣币）。

用途：模板是用 lv2_src / lv3_src 切的，同一帧自匹配不算验证；
这里抓「与模板来源不同的卡」的原生帧，做独立交叉验证。

用法：
    python tools/capture_chip_level_samples.py --address 127.0.0.1:&lt;port&gt; --levels 2,3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install"
os.environ.setdefault("MAAFW_BINARY_PATH", str(INSTALL / "runtimes" / "win-x64" / "native"))
os.environ.setdefault("TEMP", str(ROOT / ".tmp" / "chip_evidence"))
os.environ.setdefault("TMP", os.environ["TEMP"])
if str(ROOT / "agent") not in sys.path:
    sys.path.insert(0, str(ROOT / "agent"))

from maa.controller import AdbController  # noqa: E402
from maa.custom_action import CustomAction  # noqa: E402
from maa.resource import Resource  # noqa: E402
from maa.tasker import Tasker  # noqa: E402

import chip_synth  # noqa: E402
import ensure_mumu  # noqa: E402
from chip_build_select import BUTTON_ROI, _click_text, _reset_panel  # noqa: E402
from chip_build_select import _ocr  # noqa: E402
from chip_synth import (  # noqa: E402
    GRID_COLS,
    GRID_ROWS,
    _card_present,
    _close_detail,
    _detail_visible,
    _icon_point,
    _press,
    _shot,
)

log = logging.getLogger("laa.sample")
PROBE_NODE = "ChipLevelSampleProbe"
SWIPE_FROM, SWIPE_TO = (350, 470), (350, 226)
MAX_PAGES = 10

STAMP = time.strftime("%Y%m%d_%H%M%S")
OUT_DIR = ROOT / "sandbox" / f"chip_level_samples_{STAMP}"


class SampleAction(CustomAction):
    def __init__(self, levels):
        super().__init__()
        self.levels = levels
        self.found = {level: [] for level in levels}
        self.records = []

    def _width(self, frame):
        """按模板命中位置量字形宽度：ROI 纵跨整个弹窗平移范围，整块量会把别的行也算进去。"""
        x, y, w, h = chip_synth.MAIN_LEVEL_ROI
        gray = chip_synth._gray(frame)
        patch = gray[y:y + h, x:x + w]
        best, best_at = -1.0, None
        for template in chip_synth._level_templates().values():
            score, at = chip_synth._ncc_best(patch, template)
            if score > best:
                best, best_at = score, at
        if best_at is None:
            return None
        top, left = y + best_at[1], x + best_at[0]
        width = chip_synth._digit_width(gray, top, left)
        if width is None:
            return None
        return {"top": top, "left": left, "width": width}

    def _clear_filter(self, context):
        """清空筛选：不然只能看到上一轮留下的「某技能 + R5」子集。"""
        helper = chip_synth.ChipSynthAction()
        if not helper._open_filter(context):
            log.warning("[sample] 筛选面板没打开，按现有筛选继续扫")
            return False
        _reset_panel(context)
        if not _click_text(context, _shot(context), BUTTON_ROI, ["确定"], exact=True):
            log.warning("[sample] 筛选面板没找到「确定」")
            return False
        time.sleep(1.1)
        return True

    def _resort(self, context):
        """清筛选后排序会被重置，重新按技能点数排（主技能 3 级卡会排在最前）。"""
        helper = chip_synth.ChipSynthAction()
        if helper._sort_label(context) == "技能点数":
            return True
        for _ in range(2):
            box = chip_synth._find_text(context, _shot(context), chip_synth.SORT_LABEL_ROI,
                                        ["稀有度", "等级", "芯片技能", "保护", "入手顺序", "技能点数"])
            if box is not None:
                context.tasker.controller.post_click(
                    int(box[0]) + int(box[2]) // 2, int(box[1]) + int(box[3]) // 2).wait()
            else:
                context.tasker.controller.post_click(271, 113).wait()
            time.sleep(0.8)
            if _click_text(context, _shot(context), chip_synth.SORT_ITEMS_ROI, ["技能点数"]):
                time.sleep(0.9)
                if helper._sort_label(context) == "技能点数":
                    return True
        log.warning("[sample] 排序没能切回技能点数")
        return False

    def run(self, context, argv):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        _close_detail(context)
        self._clear_filter(context)
        self._resort(context)
        for _ in range(8):  # 列表会记住上次滚动位置，先拉回顶部
            chip_synth._swipe(context, SWIPE_TO, SWIPE_FROM, 700)
            time.sleep(0.35)
        for page in range(MAX_PAGES):
            if all(len(v) >= 2 for v in self.found.values()):
                break
            for row, y in enumerate(GRID_ROWS):
                for col, x in enumerate(GRID_COLS):
                    frame = _shot(context)
                    if not _card_present(frame, (x, y)):
                        continue
                    _press(context, _icon_point({"point": (x, y)}), 900, 0, 0)
                    time.sleep(1.6)
                    frame = _shot(context)
                    if not _detail_visible(context, frame):
                        _close_detail(context)
                        time.sleep(0.5)
                        continue
                    level, scores = chip_synth._match_main_level(frame)
                    if level is None:  # 弹窗动画未稳定，补拍一次
                        time.sleep(0.7)
                        frame = _shot(context)
                        level, scores = chip_synth._match_main_level(frame)
                    width = self._width(frame)
                    title = " ".join(t for t, _ in _ocr(context, frame, chip_synth.DETAIL_TITLE_ROI))
                    record = {"page": page + 1, "row": row, "col": col + 1,
                              "title": title.strip(), "level": level, "scores": scores,
                              "width": width}
                    log.info("[sample] 第%d页 行%d 列%d %s level=%s width=%s %s",
                             page + 1, row, col + 1, record["title"], level, width, scores)
                    if level in self.found and len(self.found[level]) < 2:
                        path = OUT_DIR / f"lv{level}_{len(self.found[level]) + 1}.png"
                        Image.fromarray(np.asarray(frame)[:, :, ::-1]).save(path)
                        record["frame"] = str(path)
                        self.found[level].append(record)
                    elif level is None:
                        path = OUT_DIR / f"unknown_p{page + 1}_r{row}_c{col + 1}.png"
                        Image.fromarray(np.asarray(frame)[:, :, ::-1]).save(path)
                        record["frame"] = str(path)
                    self.records.append(record)
                    _close_detail(context)
            chip_synth._swipe(context, SWIPE_FROM, SWIPE_TO, 900)
            time.sleep(0.9)
        (OUT_DIR / "samples.json").write_text(
            json.dumps({"found": self.found, "all": self.records}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        log.info("[sample] 结果目录 %s", OUT_DIR)
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default="127.0.0.1:5555")
    parser.add_argument("--levels", default="2,3")
    parser.add_argument("--on-page", action="store_true",
                        help="已经停在芯片合成页时用：跳过导航，直接跑扫描动作")
    args = parser.parse_args()
    if not args.address:
        raise SystemExit("必须指定 --address 控制器地址")
    levels = [int(v) for v in args.levels.split(",") if v.strip()]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)

    adb = ensure_mumu.find_mumu()[2]
    if not adb:
        raise SystemExit("未找到 MuMu 的 adb")
    temp = Path(os.environ["TEMP"])
    temp.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(temp / "maa-log")

    action = SampleAction(levels)
    resource = Resource()
    if not resource.post_bundle(INSTALL / "resource").wait().succeeded:
        raise RuntimeError("加载资源失败")
    if not resource.register_custom_action("chip_synth", chip_synth.ChipSynthAction()):
        raise RuntimeError("注册 chip_synth 失败")
    if not resource.register_custom_recognition("chip_synth_state",
                                                chip_synth.ChipSynthRecognition()):
        raise RuntimeError("注册 chip_synth_state 失败")
    resource.register_custom_action("chip_level_sample", action)
    controller = AdbController(str(adb), args.address, agent_path=INSTALL / "libs" / "MaaAgentBinary")
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError("连接失败")
    tasker = Tasker()
    if not tasker.bind(resource, controller) or not tasker.inited:
        raise RuntimeError("Tasker 初始化失败")
    # 唤醒节点的盲点击会把右上角图标带进动画，导致模板匹配不上；替换成空动作。
    # 仍从「芯片合成」入口进链，保证初始化节点先建会话（排序节点依赖会话）。
    if args.on_page:
        job = tasker.post_task("芯片合成_运行", {
            "芯片合成_运行": {
                "recognition": "DirectHit",
                "action": {"type": "Custom", "param": {"custom_action": "chip_level_sample"}},
                "next": [],
            }
        }).wait()
    else:
        job = tasker.post_task("芯片合成", {
            "芯片合成_唤醒": {
                "recognition": "DirectHit",
                "action": "DoNothing",
                "next": ["芯片合成_仓库图标"],
                "on_error": [],
            },
            "芯片合成_运行": {
                "recognition": "DirectHit",
                "action": {"type": "Custom", "param": {"custom_action": "chip_level_sample"}},
                "next": [],
            }
        }).wait()
    print("SAMPLE_STATUS", f"succeeded={job.succeeded}", f"raw={job.status._status.name}")
    print("OUT_DIR", OUT_DIR)
    return 0 if job.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
