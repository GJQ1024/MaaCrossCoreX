"""单张详情取证：长按第一行第一列那张卡，连拍四帧并裁出四行 ROI。

只取证，不选料、不合成、不扣币。判定逻辑与 ROI 全部复用 chip_synth，
本脚本不新增任何阈值。

用法：
    python tools/capture_chip_detail_evidence.py --address 127.0.0.1:&lt;port&gt;
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
from chip_synth import (  # noqa: E402
    DETAIL_OCR_ROI,
    DETAIL_TITLE_ROI,
    GRID_COLS,
    GRID_ROWS,
    _card_present,
    _close_detail,
    _detail_from_groups,
    _icon_point,
    _ocr,
    _press,
    _rows_from_items,
    _shot,
)

log = logging.getLogger("laa.chip_evidence")

PROBE_NODE = "ChipDetailEvidenceProbe"
SKILL = "暴怒"
QUALITY = "R5"
FRAME_COUNT = 4
FRAME_INTERVAL = 0.3


def _scored_ocr(context, image, roi):
    """带置信度的 OCR：chip_build_select._ocr 会丢掉 score，这里单独取一次。"""
    result = None
    for attempt in range(2):
        try:
            result = context.run_recognition(
                "通用_OCR探针",
                image if attempt == 0 else None,
                pipeline_override={
                    "通用_OCR探针": {"recognition": "OCR", "roi": roi, "expected": []}
                },
            )
            break
        except Exception as exc:  # noqa: BLE001 Maa C++ 层偶发抛异常，换实时帧重试
            log.warning("OCR 调用异常（第 %d 次）：%s", attempt + 1, exc)
            time.sleep(0.3)
    rows = []
    if not result:
        return rows
    seen = set()
    candidates = [getattr(result, "best_result", None)]
    candidates.extend(getattr(result, "all_results", None) or [])
    for item in candidates:
        text = getattr(item, "text", None)
        box = getattr(item, "box", None)
        if not text or not box:
            continue
        key = (str(text), tuple(int(v) for v in box))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "text": str(text),
            "box": [int(v) for v in box],
            "score": round(float(getattr(item, "score", 0.0) or 0.0), 4),
        })
    return rows


def _save(image, path: Path):
    Image.fromarray(np.asarray(image)[:, :, ::-1]).save(path)
    return str(path)


def _crop_rows(image, path: Path):
    """把四行 ROI 单独裁出来，直接看框住的是什么。"""
    x, y, w, h = DETAIL_OCR_ROI
    frame = np.asarray(image)
    height, width = frame.shape[:2]
    sx = width / 1280.0
    sy = height / 720.0
    box = (int(x * sx), int(y * sy), int((x + w) * sx), int((y + h) * sy))
    Image.fromarray(frame[box[1]:box[3], box[0]:box[2], ::-1]).save(path)
    return str(path)


class EvidenceAction(CustomAction):
    def run(self, context, argv):
        param = json.loads(getattr(argv, "custom_action_param", None) or "{}")
        out = Path(param["out"])
        out.mkdir(parents=True, exist_ok=True)
        session = chip_synth._Synth(
            {"skills": [SKILL], "qualities": [QUALITY], "threshold_raw": "2/1/1/1"},
            True,
        )
        synth = chip_synth.ChipSynthAction()
        report = {"skill": SKILL, "quality": QUALITY, "steps": [], "frames": []}

        def step(name, ok):
            report["steps"].append({"name": name, "ok": bool(ok)})
            log.info("[取证] %s → %s", name, "OK" if ok else "FAIL")
            return ok

        image = _shot(context)
        if not step("合成页有第一行第一列卡片",
                    _card_present(image, (GRID_COLS[0], GRID_ROWS[0]))):
            _write(out, report)
            return True
        if not step("筛选 暴怒 + R5", synth._apply_filter(context, session, SKILL)):
            _write(out, report)
            return True
        if not step("排序 技能点数", synth._ensure_sort(context, session)):
            _write(out, report)
            return True

        point = _icon_point({"point": (GRID_COLS[0], GRID_ROWS[0])})
        report["press_point"] = list(point)
        log.info("[取证] 长按 %s（第一行第一列）", point)
        report["press_at"] = time.time()
        _press(context, point, chip_synth.DETAIL_ATTEMPTS[0][0], 0, 0)

        for index in range(FRAME_COUNT):
            started = time.time()
            frame = _shot(context)
            full = out / f"ev_{index:02d}.png"
            crop = out / f"ev_{index:02d}_rows.png"
            title_items = _scored_ocr(context, frame, DETAIL_TITLE_ROI)
            row_items = _scored_ocr(context, frame, DETAIL_OCR_ROI)
            groups = _rows_from_items([(item["text"], item["box"]) for item in row_items])
            main_level, level_scores = chip_synth._match_main_level(frame)
            parsed = _detail_from_groups(groups, main_level)
            report["frames"].append({
                "frame": _save(frame, full),
                "rows_crop": _crop_rows(frame, crop),
                "elapsed": round(started - report.get("press_at", started), 3),
                "visible": chip_synth._detail_visible(context, frame),
                "title_ocr": title_items,
                "rows_ocr": row_items,
                "row_groups": len(groups),
                "main_level": main_level,
                "main_level_scores": level_scores,
                "parsed": parsed is not None,
                "parsed_text": chip_synth._detail_text(parsed) if parsed else None,
            })
            log.info("[取证] ev_%02d 可见=%s 分组=%d 解析=%s", index,
                     report["frames"][-1]["visible"], len(groups),
                     report["frames"][-1]["parsed_text"])
            time.sleep(FRAME_INTERVAL)

        step("点空白关闭弹窗", _close_detail(context))
        _write(out, report)
        return True


def _write(out: Path, report):
    (out / "evidence.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _nav(tasker, node):
    """按 pipeline 节点走一步，掐掉 next / on_error，避免顺带跑后续动作。"""
    job = tasker.post_task(node, {node: {"next": [], "on_error": []}}).wait()
    log.info("[取证] 导航 %s → %s", node, job.succeeded)
    return job.succeeded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default="127.0.0.1:5555")
    parser.add_argument("--adb", help="adb 路径，默认自动查找 MuMu")
    args = parser.parse_args()
    if not args.address:
        raise SystemExit("必须指定 --address 控制器地址")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    adb = Path(args.adb) if args.adb else ensure_mumu.find_mumu()[2]
    if not adb:
        raise SystemExit("未找到 MuMu 的 adb，请用 --adb 指定")

    out = ROOT / "sandbox" / ("chip_detail_evidence_%s" % time.strftime("%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(os.environ["TEMP"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(temp_dir / "maa-log")

    resource = Resource()
    if not resource.post_bundle(INSTALL / "resource").wait().succeeded:
        raise RuntimeError("加载资源失败")
    action = EvidenceAction()
    if not resource.register_custom_action("chip_evidence", action):
        raise RuntimeError("注册 chip_evidence 失败")

    controller = AdbController(str(adb), args.address,
                               agent_path=INSTALL / "libs" / "MaaAgentBinary")
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError("连接模拟器失败：%s" % args.address)
    tasker = Tasker()
    if not tasker.bind(resource, controller) or not tasker.inited:
        raise RuntimeError("初始化 Tasker 失败")

    for node in ("芯片合成_唤醒", "芯片合成_仓库图标", "芯片合成_芯片页签",
                 "芯片合成_工作台", "芯片合成_合成页签"):
        _nav(tasker, node)
        time.sleep(0.6)

    tasker.post_task(PROBE_NODE, {
        PROBE_NODE: {
            "recognition": "DirectHit",
            "action": {
                "type": "Custom",
                "param": {"custom_action": "chip_evidence",
                          "custom_action_param": {"out": str(out)}},
            },
            "next": [],
        }
    }).wait()

    tasker.post_task("通用_回主界面并结束").wait()
    print("EVIDENCE_DIR %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
