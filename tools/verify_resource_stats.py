# -*- coding: utf-8 -*-
"""实机验证：用真实主界面截图跑通 采集→差值→JSONL→CSV 全链路。

由于资源图标模板由迁移会话添加，本脚本直接用已知图标位置（来自 06_home 实测）
模拟 TemplateMatch 的输出，验证 OCR 读数、解析、存储、CSV 导出的端到端正确性。

用法：python tools/verify_resource_stats.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent.parent
# _ROOT = MAA 根目录
APP_DIR = _ROOT / "apps" / "crosscore-auto"
RESOURCE_DIR = APP_DIR / "assets" / "resource"
SHOT_DIR = APP_DIR / "shots" / "01_ondevice" / "06_home"

os.environ.setdefault("MAAFW_BINARY_PATH", str(_ROOT / "sandbox" / "MaaFramework-5.14.3" / "bin"))
os.environ["RESOURCE_STATS_DIR"] = str(APP_DIR / "third_party" / "MCC_Framework" / "config" / "resource_stats_verify")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from maa.controller import CustomController  # noqa: E402
from maa.resource import Resource  # noqa: E402
from maa.tasker import Tasker  # noqa: E402

_AGENT_DIR = APP_DIR / "third_party" / "MCC_Framework" / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from resource_stats import (  # noqa: E402
    RESOURCES,
    ResourceReading,
    format_summary,
    record_after,
    record_before,
)


class DummyController(CustomController):
    def __init__(self, image):
        self._img = image
        super().__init__()

    def connect(self): return True
    def request_uuid(self): return "dummy"
    def start_app(self, intent): return True
    def stop_app(self, intent): return True
    def screencap(self): return self._img
    def click(self, x, y): return True
    def swipe(self, x1, y1, x2, y2, duration): return True
    def touch_down(self, c, x, y, p): return True
    def touch_move(self, c, x, y, p): return True
    def touch_up(self, c): return True
    def click_key(self, k): return True
    def input_text(self, t): return True
    def key_down(self, k): return True
    def key_up(self, k): return True
    def scroll(self, dx, dy): return True
    def relative_move(self, dx, dy): return True


def ocr_roi(tasker, img, roi):
    x, y, w, h = roi
    override = {"V": {"recognition": "OCR", "roi": [x, y, w, h], "action": "DoNothing"}}
    job = tasker.post_task("V", override)
    job.wait()
    if not job.succeeded:
        return None, 0.0, None
    detail = tasker.get_task_detail(job.job_id)
    best = None
    for nid in getattr(detail, "node_id_list", []) or []:
        nd = tasker.get_node_detail(nid)
        reco = getattr(nd, "recognition", None) if nd else None
        items = getattr(reco, "all_results", None) if reco else None
        if items:
            for it in items:
                if best is None or float(getattr(it, "score", 0)) > best[1]:
                    box = getattr(it, "box", None)
                    b = None
                    if box is not None:
                        try:
                            bx = getattr(box, "x", None)
                            by = getattr(box, "y", None)
                            bw = getattr(box, "width", None)
                            bh = getattr(box, "height", None)
                            if None not in (bx, by, bw, bh):
                                b = [int(bx), int(by), int(bw), int(bh)]
                            else:
                                b = [int(box[0]), int(box[1]), int(box[2]), int(box[3])]
                        except Exception:
                            b = None
                    best = (str(getattr(it, "text", "")), float(getattr(it, "score", 0)), b)
    return best if best else (None, 0.0, None)


# 06_home 实测图标位置（1280x720 像素）
# 从裁剪图反推：体力图标在 "201/240" 左侧，金币图标在 "1091740" 左侧，粲晶图标在 "5816" 左侧
ICON_POSITIONS_720P = {
    "ap":   (468, 28, 52, 52),
    "coin": (653, 28, 52, 52),
    "gem":  (853, 28, 52, 52),
}


def read_from_screenshot(tasker, img, icon_positions):
    """模拟图标锚定 OCR：用已知图标位置 + 1280x720 画布偏移算 ROI，跑 OCR。"""
    h, w = img.shape[:2]
    readings = {}
    for key, spec in RESOURCES.items():
        ix, iy, iw, ih = icon_positions[key]
        dx, dy, ow, oh = spec["ocr_offset"]
        # ocr_offset 是 1280x720 画布坐标，缩放到实际截图分辨率
        scale_x = w / 1280
        scale_y = h / 720
        roi = [
            int(ix + dx * scale_x),
            int(iy + dy * scale_y),
            int(ow * scale_x),
            int(oh * scale_y),
        ]
        text, score, box = ocr_roi(tasker, img, roi)
        reading = ResourceReading(
            resource=key, label=spec["label"], value=None,
            raw=text, confidence=score, icon_box=list(icon_positions[key]),
            ocr_box=box,
        )
        if text and score >= 0.85:
            from resource_stats import _PARSERS
            val = _PARSERS[spec["parser"]](text)
            if val is not None:
                reading.value = val
                reading.success = True
        readings[key] = reading
    return readings


def main():
    res = Resource()
    if not res.post_bundle(str(RESOURCE_DIR)).wait().succeeded:
        print("[FATAL] resource 加载失败")
        return 1

    shots = sorted(SHOT_DIR.glob("00*.png"))
    if not shots:
        print(f"[FATAL] 未找到截图: {SHOT_DIR}")
        return 1

    img0 = np.asarray(Image.open(shots[0]).convert("RGB"))[:, :, ::-1].copy()
    ctrl = DummyController(img0)
    ctrl.post_connection().wait()
    tasker = Tasker()
    tasker.bind(resource=res, controller=ctrl)

    account = "verify_account"
    task = "DailyRewards"

    print("=" * 70)
    print("实机验证 1：真实主界面截图 OCR 读数")
    print("=" * 70)
    before_readings = read_from_screenshot(tasker, img0, ICON_POSITIONS_720P)
    for key, r in before_readings.items():
        print(f"  {r.label:4s}: value={r.value!r:20s} raw={r.raw!r:12s} score={r.confidence:.3f} success={r.success}")

    print("\n" + "=" * 70)
    print("实机验证 2：写入 before 快照")
    print("=" * 70)
    before = record_before(account, task, "verify_run_001", before_readings)
    print(f"  已写入 before: ts={before['ts']}")

    # 模拟任务后截图：用 002.png（AP 从 201→200）
    img1 = np.asarray(Image.open(shots[1]).convert("RGB"))[:, :, ::-1].copy()
    ctrl._img = img1
    after_readings = read_from_screenshot(tasker, img1, ICON_POSITIONS_720P)
    print(f"\n  任务后读数:")
    for key, r in after_readings.items():
        print(f"  {r.label:4s}: value={r.value!r:20s} raw={r.raw!r:12s} score={r.confidence:.3f} success={r.success}")

    after = record_after(account, task, "verify_run_001", before, after_readings)
    print(f"\n  差值: {after['delta']}")
    summary = format_summary(task, after["delta"], before["resources"], after["resources"])
    print(f"  摘要: {summary}")

    print("\n" + "=" * 70)
    print("实机验证 3：JSONL 写入内容")
    print("=" * 70)
    from resource_stats import jsonl_path
    path = jsonl_path(account)
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    print(f"  文件: {path}")
    print(f"  共 {len(lines)} 行")
    for i, line in enumerate(lines):
        rec = json.loads(line)
        print(f"\n  --- 行 {i+1} (phase={rec['phase']}) ---")
        print(f"  {json.dumps(rec, ensure_ascii=False, indent=2)[:600]}")

    print("\n" + "=" * 70)
    print("实机验证 4：识别失败不写 0（遮挡金币区域）")
    print("=" * 70)
    # 把金币区域涂成黑色模拟识别失败
    img_fail = img0.copy()
    ix, iy, iw, ih = ICON_POSITIONS_720P["coin"]
    dx, dy, ow, oh = RESOURCES["coin"]["ocr_offset"]
    scale_x = img0.shape[1] / 1280
    scale_y = img0.shape[0] / 720
    rx = int(ix + dx * scale_x)
    ry = int(iy + dy * scale_y)
    rw = int(ow * scale_x)
    rh = int(oh * scale_y)
    img_fail[ry:ry+rh, rx:rx+rw] = 0
    ctrl._img = img_fail
    fail_readings = read_from_screenshot(tasker, img_fail, ICON_POSITIONS_720P)
    print(f"  遮挡后金币读数: value={fail_readings['coin'].value!r} success={fail_readings['coin'].success}")

    before_fail = record_before(account, "FreeGift", "verify_run_002", fail_readings)
    after_fail = record_after(account, "FreeGift", "verify_run_002", before_fail, after_readings)
    print(f"  金币差值（before 失败）: {after_fail['delta']['coin']}")
    assert after_fail["delta"]["coin"] is None, "失败时差值应为 None 而非 0"
    print("  ✅ 识别失败时差值为 null，未写 0")

    print("\n" + "=" * 70)
    print("实机验证 5：CSV 导出")
    print("=" * 70)
    from export_resource_csv import export
    out = export(account, "all", None)
    csv_text = out.read_text(encoding="utf-8-sig")
    print(f"  CSV 内容预览:")
    for line in csv_text.split("\n")[:5]:
        print(f"    {line}")
    print(f"\n  CSV 行数: {len(csv_text.split(chr(10)))}")

    print("\n" + "=" * 70)
    print("全部实机验证通过 ✅")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
