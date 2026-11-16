# -*- coding: utf-8 -*-
"""离线识别判据体检：抽取全部 pipeline 识别节点，并在已有真实帧上回归模板匹配分。

只读 assets/resource 与本地帧目录，不启动实例、不连 adb、不跑 pipeline。

用法（任意带 opencv/numpy 的 Python）：
    python tools/audit_recognition.py --static --out sandbox/audit
    python tools/audit_recognition.py --match --out sandbox/audit
    python tools/audit_recognition.py --match --frames <目录> --limit-per-dir 40
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOURCE_DIR = os.path.join(APP_DIR, "assets", "resource")
IMAGE_DIR = os.path.join(RESOURCE_DIR, "image")
PIPELINE_DIRS = [
    os.path.join(RESOURCE_DIR, "pipeline"),
    os.path.join(RESOURCE_DIR, "bilibili", "pipeline"),
]

DEFAULT_FRAME_DIRS = [
    os.path.join(APP_DIR, "..", "..", "shots"),
    os.path.join(APP_DIR, "..", "..", "sandbox"),
    os.path.join(APP_DIR, "..", "..", "sandbox", "evidence"),
    os.path.join(APP_DIR, "evidence"),
]

TEMPLATE_TYPES = {"TemplateMatch"}
COLOR_TYPES = {"ColorMatch"}
SKIP_KEYS = ("$__",)


def iter_pipeline_files():
    for d in PIPELINE_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for name in sorted(files):
                if name.endswith(".json"):
                    yield os.path.join(root, name)


def iter_nodes(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return
    for name, node in data.items():
        if name.startswith(SKIP_KEYS) or not isinstance(node, dict):
            continue
        yield name, node


def rel(path):
    return os.path.relpath(path, APP_DIR)


def load_nodes():
    rows = []
    for path in iter_pipeline_files():
        for name, node in iter_nodes(path):
            reco = node.get("recognition") or {}
            # 兼容两种写法：新式 {type, param} 与旧式 "Type" + 参数平铺在节点上
            if isinstance(reco, str):
                rtype = reco
                param = node
            else:
                rtype = reco.get("type") or "DirectHit"
                param = dict(node)
                param.update(reco.get("param") or {})
            rows.append(
                {
                    "file": rel(path),
                    "node": name,
                    "type": rtype,
                    "param": param,
                    "node_json": node,
                }
            )
    return rows


def template_paths(template):
    names = template if isinstance(template, list) else [template]
    out = []
    for n in names:
        p = os.path.join(IMAGE_DIR, n)
        out.append(p)
    return out


def color_ranges(param):
    lower = param.get("lower")
    upper = param.get("upper")
    if isinstance(lower, list) and isinstance(upper, list):
        if isinstance(lower[0], list):
            return list(zip(lower, upper))
        return [(lower, upper)]
    # 兼容 [[r,g,b], ...] 上下限成对写法之外的异常，直接原样返回
    return []


HSV_BGR_METHODS = {4: "RGB", 40: "HSV", 6: "HSV_S", 5: "HSV_V"}


def static_report(rows, outdir):
    os.makedirs(outdir, exist_ok=True)
    typed = defaultdict(int)
    for row in rows:
        typed[row["type"]] += 1

    tm = [r for r in rows if r["type"] in TEMPLATE_TYPES]
    cm = [r for r in rows if r["type"] in COLOR_TYPES]

    tpath = os.path.join(outdir, "template_nodes.csv")
    with open(tpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "文件",
                "节点",
                "模板",
                "阈值",
                "阈值写法",
                "roi",
                "method",
                "green_mask",
                "预筛",
                "模板图存在",
                "模板尺寸",
                "roi尺寸",
                "尺寸风险",
            ]
        )
        for r in tm:
            p = r["param"]
            tpl = p.get("template", "")
            thr = p.get("threshold")
            thr_txt = "缺省(0.7)" if thr is None else str(thr)
            names = tpl if isinstance(tpl, list) else [tpl]
            missing = [n for n in names if not os.path.isfile(os.path.join(IMAGE_DIR, n))]
            sizes = []
            for n in names:
                tp = os.path.join(IMAGE_DIR, n)
                if os.path.isfile(tp):
                    try:
                        from PIL import Image

                        with Image.open(tp) as im:
                            sizes.append(f"{n}:{im.width}x{im.height}")
                    except Exception:
                        pass
            roi = p.get("roi")
            size_risk = ""
            if isinstance(roi, list) and len(roi) == 4 and sizes:
                try:
                    from PIL import Image

                    tw = th = 0
                    for n in names:
                        tp = os.path.join(IMAGE_DIR, n)
                        if os.path.isfile(tp):
                            with Image.open(tp) as im:
                                tw = max(tw, im.width)
                                th = max(th, im.height)
                    if tw > roi[2] or th > roi[3]:
                        size_risk = f"模板{tw}x{th}超出roi{roi[2]}x{roi[3]}"
                except Exception:
                    pass
            w.writerow(
                [
                    r["file"],
                    r["node"],
                    "|".join(str(n) for n in names),
                    "" if thr is None else thr,
                    thr_txt,
                    json.dumps(p.get("roi"), ensure_ascii=False) if p.get("roi") else "",
                    p.get("method", 5),
                    p.get("green_mask", False),
                    json.dumps(p.get("order_by"), ensure_ascii=False) if p.get("order_by") else "",
                    "缺失:" + ",".join(missing) if missing else "有",
                    ";".join(sizes),
                    f"{roi[2]}x{roi[3]}" if isinstance(roi, list) and len(roi) == 4 else "",
                    size_risk,
                ]
            )

    cpath = os.path.join(outdir, "color_nodes.csv")
    with open(cpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "文件",
                "节点",
                "method",
                "颜色空间",
                "roi",
                "roi面积",
                "lower",
                "upper",
                "count",
                "count下限",
                "count上限",
                "connected",
                "风险",
            ]
        )
        for r in cm:
            p = r["param"]
            roi = p.get("roi")
            area = roi[2] * roi[3] if isinstance(roi, list) and len(roi) == 4 else None
            cnt = p.get("count")
            if isinstance(cnt, list):
                lo, hi = (cnt + [None, None])[:2]
            elif cnt is None:
                lo, hi = None, None
            else:
                lo, hi = cnt, cnt
            method = p.get("method", 4)
            risks = []
            if lo is None:
                risks.append("缺count下限")
            if hi is None:
                risks.append("缺count上限")
            if p.get("connected"):
                risks.append("connected=true(只取最大连通块)")
            if lo is not None and roi and area and lo >= area:
                risks.append("count下限≥roi面积")
            w.writerow(
                [
                    r["file"],
                    r["node"],
                    method,
                    HSV_BGR_METHODS.get(method, "?"),
                    json.dumps(roi, ensure_ascii=False) if roi else "",
                    area or "",
                    json.dumps(p.get("lower"), ensure_ascii=False) if p.get("lower") else "",
                    json.dumps(p.get("upper"), ensure_ascii=False) if p.get("upper") else "",
                    json.dumps(cnt, ensure_ascii=False) if cnt is not None else "",
                    "" if lo is None else lo,
                    "" if hi is None else hi,
                    p.get("connected", False),
                    ";".join(risks),
                ]
            )

    summary = {
        "pipeline_files": len(list(iter_pipeline_files())),
        "nodes_total": len(rows),
        "by_type": dict(sorted(typed.items(), key=lambda kv: -kv[1])),
        "template_nodes": len(tm),
        "template_nodes_default_threshold": sum(1 for r in tm if r["param"].get("threshold") is None),
        "color_nodes": len(cm),
    }
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("静态清单:", tpath)
    print("颜色清单:", cpath)
    return summary


def collect_frames(frame_dirs, limit_per_dir=0, sample_every=1):
    """只收集 1280x720 帧路径，逐帧惰性解码，避免整库读入内存。

    sample_every>1 时按目录抽样（保留首尾帧），用于给全量模板回归提速；
    抽样只会漏掉“更好的正例帧”，使余量估计偏保守，不会把 P0 误判成 OK。
    """
    import cv2
    import numpy as np

    frames = []
    for d in frame_dirs:
        d = os.path.abspath(d)
        if not os.path.isdir(d):
            continue
        seen = 0
        for root, _dirs, files in os.walk(d):
            bucket = []
            for name in sorted(files):
                if not name.lower().endswith(".png"):
                    continue
                p = os.path.join(root, name)
                try:
                    img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
                except Exception:
                    continue
                if img is None:
                    continue
                h, w = img.shape[:2]
                if (w, h) != (1280, 720):
                    continue
                bucket.append(p)
            if sample_every > 1 and len(bucket) > sample_every:
                picked = bucket[::sample_every]
                if bucket[-1] not in picked:
                    picked.append(bucket[-1])
            else:
                picked = bucket
            for p in picked:
                if limit_per_dir and seen >= limit_per_dir:
                    break
                frames.append(p)
                seen += 1
            if limit_per_dir and seen >= limit_per_dir:
                break
    return frames


def read_frame(path):
    import cv2
    import numpy as np

    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def match_templates(rows, frames, outdir, thr_margin_p0=0.05, thr_margin_p1=0.15):
    import cv2
    import numpy as np

    os.makedirs(outdir, exist_ok=True)

    # 先把所有模板图读进内存（体积小），帧逐张惰性读取（避免整库几十 GB）
    specs = []
    for r in [x for x in rows if x["type"] in TEMPLATE_TYPES]:
        p = r["param"]
        names = p.get("template")
        if not names:
            continue
        names = names if isinstance(names, list) else [names]
        tpls = []
        for n in names:
            tpath = os.path.join(IMAGE_DIR, n)
            if not os.path.isfile(tpath):
                continue
            tpl = cv2.imdecode(np.fromfile(tpath, dtype=np.uint8), cv2.IMREAD_COLOR)
            if tpl is not None:
                tpls.append(tpl)
        specs.append(
            {
                "row": r,
                "names": names,
                "tpls": tpls,
                "thr": 0.7 if p.get("threshold") is None else float(p.get("threshold")),
                "roi": p.get("roi"),
                "best": (-2.0, "", None),
                "fired": [],
            }
        )

    for fi, fpath in enumerate(frames, 1):
        img = read_frame(fpath)
        if img is None:
            continue
        for sp in specs:
            roi = sp["roi"]
            crop = img
            ox, oy = 0, 0
            if isinstance(roi, list) and len(roi) == 4:
                x, y, w, h = roi
                x = max(0, min(img.shape[1] - 1, int(x)))
                y = max(0, min(img.shape[0] - 1, int(y)))
                w = max(1, min(img.shape[1] - x, int(w)))
                h = max(1, min(img.shape[0] - y, int(h)))
                crop = img[y : y + h, x : x + w]
                ox, oy = x, y
            local_best = -2.0
            local_loc = None
            for tpl in sp["tpls"]:
                th, tw = tpl.shape[:2]
                if crop.shape[0] < th or crop.shape[1] < tw:
                    continue
                try:
                    res = cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)
                    _mn, _mx, _mloc, mloc = cv2.minMaxLoc(res)
                except cv2.error:
                    continue
                score = float(_mx)
                if score > local_best:
                    local_best = score
                    local_loc = (ox + mloc[0], oy + mloc[1])
            if local_best > sp["best"][0]:
                sp["best"] = (local_best, fpath, local_loc)
            if local_best >= sp["thr"]:
                sp["fired"].append((local_best, fpath))
        if fi % 25 == 0 or fi == len(frames):
            print(f"  帧进度 {fi}/{len(frames)}", flush=True)

    result_rows = []
    for sp in specs:
        r = sp["row"]
        names = sp["names"]
        thr = sp["thr"]
        best = sp["best"]
        fired = sorted(sp["fired"])
        margin = best[0] - thr
        if not fired:
            level = "无帧可验"
            action = "仅记录"
            frame_note = "无命中帧"
            worst_txt = ""
            worst_fired_margin = None
            same_dir = ""
        else:
            best_dir = os.path.dirname(best[1])
            # 只取「与最高分帧同目录」的命中帧做最差真实正例，避免其它页面的偶然高分污染
            same_dir_fired = [s for s, f in fired if os.path.dirname(f) == best_dir]
            worst_fired_margin = (min(same_dir_fired) - thr) if same_dir_fired else None
            level = "P1"
            if worst_fired_margin is not None and worst_fired_margin < thr_margin_p0:
                level = "P0"
            elif margin <= thr_margin_p1:
                level = "P1"
            else:
                level = "OK"
            action = "待改" if level == "P0" else "仅记录"
            frame_note = rel(best[1])
            worst_txt = (
                f"{min(same_dir_fired):.4f}@{rel(best_dir)}"
                if same_dir_fired
                else "(同目录无其它命中)"
            )
            same_dir = rel(best_dir)
        result_rows.append(
            [
                r["file"],
                r["node"],
                "|".join(names),
                f"{thr:.2f}",
                f"{best[0]:.4f}" if best[1] else "",
                frame_note,
                f"{margin:+.4f}" if best[1] else "",
                len(fired),
                worst_txt,
                f"{worst_fired_margin:+.4f}" if worst_fired_margin is not None else "",
                same_dir,
                level,
                action,
            ]
        )
    path = os.path.join(outdir, "template_match.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "文件",
                "节点",
                "模板",
                "阈值",
                "最高分",
                "最高分帧",
                "最优余量",
                "命中帧数",
                "同目录最差命中",
                "同目录最差余量",
                "最高分目录",
                "等级",
                "处置",
            ]
        )
        w.writerows(result_rows)
    print("匹配清单:", path)
    return result_rows


def color_mask(img, lower, upper, method):
    import numpy as np

    # method 4 = RGBCount，MAA 的 lower/upper 为 RGB 顺序，帧为 BGR
    if method != 4:
        return None
    bgr_lo = np.array([lower[2], lower[1], lower[0]], dtype=np.uint8)
    bgr_hi = np.array([upper[2], upper[1], upper[0]], dtype=np.uint8)
    return np.all((img >= bgr_lo) & (img <= bgr_hi), axis=2).astype(np.uint8)


def match_colors(rows, frames, outdir):
    import cv2
    import numpy as np

    os.makedirs(outdir, exist_ok=True)
    cm = [r for r in rows if r["type"] in COLOR_TYPES]
    result_rows = []
    for r in cm:
        p = r["param"]
        method = p.get("method", 4)
        if method != 4:
            result_rows.append([r["file"], r["node"], method, "非RGBCount未验", "", "", "", "", "", "", "无帧可验", "仅记录"])
            continue
        roi = p.get("roi")
        counts = []
        fire = 0
        for fpath in frames:
            img = read_frame(fpath)
            if img is None:
                continue
            crop = img
            if isinstance(roi, list) and len(roi) == 4:
                x, y, w, h = roi
                crop = img[y : y + h, x : x + w]
            mask = color_mask(crop, p.get("lower"), p.get("upper"), method)
            if mask is None:
                continue
            if p.get("connected") and mask.any():
                n, labels, stats, _cent = cv2.connectedComponentsWithStats(mask, 8)
                cnt = int(stats[1:, cv2.CC_STAT_AREA].max()) if n > 1 else 0
            else:
                cnt = int(np.count_nonzero(mask))
            counts.append((cnt, fpath))
        counts.sort()
        vals = [c for c, _ in counts]
        lo = p.get("count")
        # MAA 默认 count=1，且单值只当下限；缺省=「≥1 像素」
        if isinstance(lo, list):
            cmin, cmax = lo[0], lo[1]
        elif lo is None:
            cmin, cmax = 1, None
        else:
            cmin = lo
            cmax = None
        for c, _f in counts:
            if (cmin is None or c >= cmin) and (cmax is None or c <= cmax):
                fire += 1
        nonzero = [c for c in vals if c >= 1]
        med = vals[len(vals) // 2] if vals else None
        level = "P0" if cmax is None else ("P1" if fire > 0.3 * len(frames) else "OK")
        result_rows.append(
            [
                r["file"],
                r["node"],
                method,
                f"roi={roi}",
                f"min={vals[0]}" if vals else "",
                f"median={med}" if vals else "",
                f"max={vals[-1]}" if vals else "",
                f"非零帧 {len(nonzero)}/{len(frames)}",
                f"非零min={nonzero[0]} max={nonzero[-1]}" if nonzero else "",
                f"配置=[{cmin},{cmax}]",
                f"命中帧 {fire}/{len(frames)}",
                level,
                "仅记录",
            ]
        )
        print(f"  color {r['node']}: {level} fire={fire}/{len(frames)} min={vals[0] if vals else '-'} med={med}")

    path = os.path.join(outdir, "color_match.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "文件",
                "节点",
                "method",
                "roi",
                "帧内最小count",
                "中位",
                "最大",
                "非零帧",
                "非零区间",
                "配置范围",
                "命中帧",
                "等级",
                "处置",
            ]
        )
        w.writerows(result_rows)
    print("颜色回归:", path)
    return result_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", action="store_true")
    ap.add_argument("--match", action="store_true")
    ap.add_argument("--color-match", action="store_true")
    ap.add_argument("--out", default=os.path.join(APP_DIR, "sandbox", "audit"))
    ap.add_argument("--frames", action="append", default=[])
    ap.add_argument("--limit-per-dir", type=int, default=0)
    ap.add_argument("--sample-every", type=int, default=1)
    args = ap.parse_args()
    if not (args.static or args.match or args.color_match):
        args.static = args.match = args.color_match = True

    rows = load_nodes()
    frames = None
    if args.static:
        static_report(rows, args.out)
    if args.match or args.color_match:
        frame_dirs = args.frames or DEFAULT_FRAME_DIRS
        frames = collect_frames(frame_dirs, args.limit_per_dir, args.sample_every)
        print(f"可用 1280x720 帧: {len(frames)}")
    if args.match:
        match_templates(rows, frames, args.out)
    if args.color_match:
        match_colors(rows, frames, args.out)


if __name__ == "__main__":
    main()
