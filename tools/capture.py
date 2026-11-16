# -*- coding: utf-8 -*-
"""MuMu 抓图工具（仓库版；本地入口 apps/crosscore-auto/tools/capture.py 只做桥接）。

- serial：--serial > 环境变量 ADB_SERIAL，都空明确报错（不写死端口）；
- 抓图前 device_mode.preflight（方向 mCurrentOrientation + 渲染面 + 黑屏）；
- 每张图用 device_judge.judge_frame 判定：只有"渲染面尺寸不对"或"整帧接近全黑"
  才判失败；弹窗遮挡只附注。截图失败即失败的语义保留，但失败原因精确。

用法：
    python tools/capture.py --out shots/01_home -n 3 --interval 1.0
    python tools/capture.py --restore
"""

import argparse
import datetime
import io
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

_TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS.parent / "scripts"))
sys.path.insert(0, str(_TOOLS))
import device_judge  # noqa: E402
from device_mode import (  # noqa: E402
    adb_path,
    preflight as dm_preflight,
    require_serial,
    set_mode,
)

TARGET_W, TARGET_H = device_judge.EXPECTED_W, device_judge.EXPECTED_H
APP_DIR = Path(__file__).resolve().parent.parent


def run_adb(serial: str, args: list, binary: bool = False):
    cmd = [adb_path(), "-s", serial, *args]
    r = subprocess.run(cmd, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"adb 失败: {' '.join(cmd)}\n{r.stderr.decode('utf-8', 'replace')}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def png_size(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def frame_info(data: bytes) -> dict:
    """PNG 解码成 device_judge.judge_frame 需要的 {w,h,fill,bbox_*}。"""
    im = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    h, w = im.shape[:2]
    mask = (im > device_judge.BLACK_THRESHOLD).any(axis=2)
    fill = float(mask.sum()) / (h * w)
    info = {"w": int(w), "h": int(h), "fill": fill}
    if mask.any():
        ys, xs = np.where(mask)
        info["bbox_w"] = int(xs.max() - xs.min() + 1)
        info["bbox_h"] = int(ys.max() - ys.min() + 1)
    return info


def main():
    ap = argparse.ArgumentParser(description="MuMu 1280x720 抓图工具（device_judge 判定）")
    ap.add_argument("--serial", default="", help="ADB serial；默认读 ADB_SERIAL（不写死端口）")
    ap.add_argument("--adb-path", default="", help="adb 可执行文件路径；默认读 ADB_PATH")
    ap.add_argument("--out", help="输出目录")
    ap.add_argument("-n", "--count", type=int, default=1)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--restore", action="store_true", help="还原为用户原竖屏 720x1280")
    args = ap.parse_args()

    if args.serial:
        os.environ["ADB_SERIAL"] = args.serial
    if args.adb_path:
        os.environ["ADB_PATH"] = args.adb_path
    serial = require_serial()

    subprocess.run([adb_path(), "connect", serial], capture_output=True, timeout=15)

    if args.restore:
        set_mode(device_judge.PORTRAIT_SIZE, device_judge.DENSITY)
        print(f"[capture] 已还原为用户原竖屏 {device_judge.PORTRAIT_SIZE} @ {device_judge.DENSITY}")
        return 0

    if not args.out:
        ap.error("--out 必填（或使用 --restore）")

    # 显示修正：竖→横→reset 共用切换（wm size Override 会把已横屏设备翻成
    # 竖向渲染面，实测 mCurrentOrientation 翻成 0），实现唯一在 device_judge
    device_judge.ensure_landscape_orientation(serial, adb_path(), log=print)
    info = dm_preflight()
    if not info["ok"]:
        print(f"[capture] 预检失败: {info['reason']}")
        print(f"[capture] 详情: {info}")
        print("[capture] 中止：环境不正确（弹窗/黑边不算失败，只有竖屏、尺寸不对或整帧黑屏才拦）")
        return 2
    note = f"；{info['note']}" if info.get("note") else ""
    print(f"[capture] 预检通过: 渲染面 {info['w']}x{info['h']}，渲染正常{note}")

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = APP_DIR / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    shots = []
    ok_count = 0
    for i in range(1, args.count + 1):
        if i > 1:
            time.sleep(args.interval)
        data = run_adb(serial, ["exec-out", "screencap", "-p"], binary=True)
        ts = now_iso()
        dims = png_size(data)
        if dims is None:
            lb = {"ok": False, "reason": "PNG 头解析失败"}
        else:
            lb = device_judge.judge_frame(frame_info(data))
        size_ok = bool(lb["ok"])
        fname = f"{i:03d}.png" if size_ok else f"{i:03d}_bad.png"
        (out_dir / fname).write_bytes(data)
        if size_ok:
            ok_count += 1
        w, h = dims or (0, 0)
        if not size_ok:
            extra = f" ({lb.get('reason')})"
        elif lb.get("note"):
            extra = f" ({lb['note']})"
        else:
            extra = ""
        print(f"[capture] {fname}: {w}x{h} {'OK' if size_ok else 'FAIL'}{extra}")
        shots.append({"index": i, "file": fname, "width": w, "height": h,
                      "size_ok": size_ok, "captured_at": ts,
                      "fill": lb.get("fill"), "bbox": [lb.get("bbox_w"), lb.get("bbox_h")],
                      "note": lb.get("note")})

    manifest = {
        "serial": serial,
        "created_at": now_iso(),
        "target_size": f"{TARGET_W}x{TARGET_H}",
        "shots": shots,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[capture] 完成 {ok_count}/{args.count} 张, 清单: {out_dir / 'manifest.json'}")
    if ok_count != args.count:
        print("[capture] 存在失败截图，请检查环境")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
