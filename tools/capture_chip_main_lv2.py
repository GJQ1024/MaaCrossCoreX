"""翻页找主技能 2 级的卡，长按开详情截原生帧（只读，不选料/不合成/不扣币）。

模板比对在本地用 numpy NCC 做，和 MaaFW 的 TM_CCOEFF_NORMED 等价，
便于先离线确认分数再落到生产代码。

用法：
    python tools/capture_chip_main_lv2.py --address 127.0.0.1:16416
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
TPL_DIR = ROOT / "assets" / "resource" / "image" / "chip_synth"
OUT_DIR = ROOT / "sandbox" / "chip_detail_evidence_20261111_025342"
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

log = logging.getLogger("laa.lv2")
PROBE_NODE = "ChipLv2FinderProbe"
LEVEL_ROI = (740, 218, 40, 37)   # 主技能行等级数字搜索窗
SWIPE_FROM, SWIPE_TO = (350, 470), (350, 226)
MAX_PAGES = 8


def _ncc_max(patch, tpl):
    th, tw = tpl.shape
    ih, iw = patch.shape
    t = tpl - tpl.mean()
    tn = np.sqrt((t * t).sum())
    best = -1.0
    for y in range(ih - th + 1):
        for x in range(iw - tw + 1):
            win = patch[y:y + th, x:x + tw]
            w = win - win.mean()
            d = np.sqrt((w * w).sum()) * tn
            if d > 1e-6:
                best = max(best, float((w * t).sum() / d))
    return round(best, 4)


def _gray(image):
    return np.asarray(image, dtype=np.float32).mean(axis=2)


class FinderAction(CustomAction):
    def run(self, context, argv):
        tpls = {
            k: np.asarray(Image.open(TPL_DIR / ("chip_main_%s.png" % k)).convert("L"),
                          dtype=np.float32)
            for k in ("lv1", "lv2", "lv3")
        }
        _close_detail(context)
        result = {}
        for page in range(MAX_PAGES):
            for row, y in enumerate(GRID_ROWS):
                for col, x in enumerate(GRID_COLS):
                    frame = _shot(context)
                    if not _card_present(frame, (x, y)):
                        continue
                    _press(context, _icon_point({"point": (x, y)}), 900, 0, 0)
                    time.sleep(1.1)
                    frame = _shot(context)
                    if not _detail_visible(context, frame):
                        _close_detail(context)
                        continue
                    crop = _gray(frame)[LEVEL_ROI[1]:LEVEL_ROI[1] + LEVEL_ROI[3],
                                        LEVEL_ROI[0]:LEVEL_ROI[0] + LEVEL_ROI[2]]
                    scores = {k: _ncc_max(crop, t) for k, t in tpls.items()}
                    log.info("[lv2] 第%d页 行%d 列%d 分数 %s", page + 1, row, col + 1, scores)
                    if scores["lv2"] >= 0.9 and scores["lv2"] == max(scores.values()):
                        path = OUT_DIR / "lv2_src.png"
                        Image.fromarray(np.asarray(frame)[:, :, ::-1]).save(path)
                        result = {"page": page + 1, "row": row, "col": col,
                                  "scores": scores, "frame": str(path)}
                        _close_detail(context)
                        (OUT_DIR / "lv2_find.json").write_text(
                            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                        log.info("[lv2] 命中 %s", result)
                        return True
                    _close_detail(context)
            chip_synth._swipe(context, SWIPE_FROM, SWIPE_TO, 900)
            time.sleep(0.9)
        log.warning("[lv2] %d 页内没找到主技能 2 级的卡", MAX_PAGES)
        (OUT_DIR / "lv2_find.json").write_text(json.dumps({"found": False}), encoding="utf-8")
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default="127.0.0.1:16416")
    args = parser.parse_args()
    if args.address != "127.0.0.1:16416":
        raise SystemExit("控制器必须是 127.0.0.1:16416")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)

    adb = ensure_mumu.find_mumu()[2]
    if not adb:
        raise SystemExit("未找到 MuMu 的 adb")
    temp = Path(os.environ["TEMP"])
    temp.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(temp / "maa-log")

    resource = Resource()
    if not resource.post_bundle(INSTALL / "resource").wait().succeeded:
        raise RuntimeError("加载资源失败")
    resource.register_custom_action("chip_lv2_find", FinderAction())
    controller = AdbController(str(adb), args.address, agent_path=INSTALL / "libs" / "MaaAgentBinary")
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError("连接失败")
    tasker = Tasker()
    if not tasker.bind(resource, controller) or not tasker.inited:
        raise RuntimeError("Tasker 初始化失败")
    tasker.post_task(PROBE_NODE, {
        PROBE_NODE: {
            "recognition": "DirectHit",
            "action": {"type": "Custom", "param": {"custom_action": "chip_lv2_find"}},
            "next": [],
        }
    }).wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
