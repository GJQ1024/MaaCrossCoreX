# -*- coding: utf-8 -*-
"""crosscore-auto 资源校验脚本。

用 MaaFramework 真实加载 assets/resource，并做静态规则检查：
1. Resource.post_bundle 加载成功；engine 解析出的节点集合覆盖我们定义的全部节点；
2. pipeline 中引用的模板图都存在于 image/；
3. next / on_error / interrupt 引用（含 [JumpBack] 前缀）都能解析到已定义节点；
4. [Anchor]X 引用（target / roi / next）对应的锚点有节点设置；
5. 坐标规范硬检查：任何节点不得出现数组形式的绝对 target（见 docs/coords.md §4）；
6. interface.json 的 task entry 都存在。

用法（在主仓库根目录 MCC_Framework 下执行；需带 maa 包的 Python）：
    <带 maa 的 python> tools/validate_resource.py
    <带 maa 的 python> tools/validate_resource.py --resource assets/resource
退出码 0 = 全部通过。

环境变量：
    MAAFW_BINARY_PATH   MaaFramework 原生库 bin 目录；缺省时向上搜索 sandbox/MaaFramework-*/bin。
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
RESOURCE_DIR = APP_DIR / "assets" / "resource"
IMAGE_DIR = RESOURCE_DIR / "image"
PIPELINE_DIR = RESOURCE_DIR / "pipeline"
INTERFACE_JSON = APP_DIR / "assets" / "interface.json"

# 坐标规范（零绝对 target）只约束本轮迁移落地的文件；
# 上游其余 pipeline 原样保留（含其自身的绝对 target 写法），待后续逐个改造。
MIGRATED_FILES = {
    "启动游戏.json",
    "领取奖励.json",
    "关闭游戏.json",
    "重连网络.json",
    "通用.json",
    "界面导航.json",
    "每日探索.json",
    "通用-扫荡.json",
    "通用-出击选择.json",
    "模拟军演.json",
    # 第 4 批：绝对 target 已清零（含 Swipe 相对化），后续不得回退
    "基建.json",
    "活动-夏活.json",
    "创生微粒刷取.json",
}

# 已迁移文件里、本次仍保留绝对 target 的豁免节点（custom 识别链路，
# 识别框语义由 agent 决定，留待对应批次一并处理）。
SKIP_NODES = {
    "每日探索_芯片结算确认",  # custom_recognition=daily_chip_reward_state，芯片奖励链路，待芯片批次
}


def _find_framework_bin(start: Path):
    """从 start 向上搜索 sandbox/MaaFramework-*/bin，返回第一个命中的 Path 或 None。"""
    for parent in [start, *start.parents]:
        sandbox = parent / "sandbox"
        if sandbox.is_dir():
            candidates = sorted(sandbox.glob("MaaFramework-*/bin"), reverse=True)
            for c in candidates:
                if c.is_dir():
                    return c
    return None


# 引擎二进制：优先用环境变量 MAAFW_BINARY_PATH；缺省时向上搜索 sandbox/MaaFramework-*/bin。
_fw_bin = _find_framework_bin(APP_DIR)
if _fw_bin is not None:
    os.environ.setdefault("MAAFW_BINARY_PATH", str(_fw_bin))

from maa.resource import Resource  # noqa: E402

REF_PREFIX = re.compile(r"^\[(JumpBack|Anchor|Mid)\]")

errors = []
warnings = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def iter_ref_strings(node):
    """取出节点里所有引用其他节点/锚点的字符串。"""
    for key in ("next", "on_error", "interrupt"):
        for item in as_list(node.get(key)):
            if isinstance(item, str):
                yield key, item
            elif isinstance(item, dict):  # NodeAttr 形式 {"name": ...}
                name = item.get("name")
                if isinstance(name, str):
                    yield key, name
    for key in ("target", "roi"):
        v = node.get(key)
        if isinstance(v, str):
            yield key, v
        act = node.get("action")
        if isinstance(act, dict):
            v2 = act.get("param", {}).get(key) if isinstance(act.get("param"), dict) else None
            if isinstance(v2, str):
                yield key, v2


def iter_templates(node):
    for key in ("template",):
        for t in as_list(node.get(key)):
            if isinstance(t, str):
                yield t
    reco = node.get("recognition")
    if isinstance(reco, dict):
        param = reco.get("param", {})
        for t in as_list(param.get("template")):
            if isinstance(t, str):
                yield t


def iter_absolute_targets(node):
    """数组形式的 target 即绝对坐标（docs/coords.md §4 禁止）。"""
    t = node.get("target")
    if isinstance(t, list):
        yield "target"
    act = node.get("action")
    if isinstance(act, dict) and isinstance(act.get("param"), dict):
        if isinstance(act["param"].get("target"), list):
            yield "action.param.target"


def collect_anchor_names(node):
    a = node.get("anchor")
    if isinstance(a, str):
        yield a
    elif isinstance(a, list):
        for x in a:
            if isinstance(x, str):
                yield x
    elif isinstance(a, dict):
        for k in a:
            yield k


def main():
    global RESOURCE_DIR, IMAGE_DIR, PIPELINE_DIR, INTERFACE_JSON
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--resource",
        default=None,
        help="覆盖资源目录（例：assets/resource）；缺省用本仓库 assets/resource",
    )
    args = ap.parse_args()
    if args.resource:
        RESOURCE_DIR = Path(args.resource).resolve()
        IMAGE_DIR = RESOURCE_DIR / "image"
        PIPELINE_DIR = RESOURCE_DIR / "pipeline"
        sibling_iface = RESOURCE_DIR.parent / "interface.json"
        if sibling_iface.is_file():
            INTERFACE_JSON = sibling_iface

    print(f"[validate] app dir : {APP_DIR}")
    print(f"[validate] resource: {RESOURCE_DIR}")
    print(f"[validate] maa bin : {os.environ.get('MAAFW_BINARY_PATH', '(unset)')}")

    # ---- 0. 目录结构 ----
    for d in (IMAGE_DIR, PIPELINE_DIR):
        if not d.is_dir():
            err(f"缺少目录: {d}")
    if errors:
        return finish()

    # ---- 1. 解析全部 pipeline JSON ----
    nodes = {}
    json_files = sorted(PIPELINE_DIR.rglob("*.json"))
    print(f"[validate] pipeline files: {len(json_files)}")
    for f in json_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            err(f"JSON 解析失败 {f.name}: {e}")
            continue
        for name, node in data.items():
            if name.startswith("$__"):
                continue
            if not isinstance(node, dict):
                err(f"{f.name}: 节点 {name} 不是对象")
                continue
            if name in nodes:
                warn(f"节点名重复定义: {name}（{f.name} 与 {nodes[name]['__file__']}）")
            node["__file__"] = f.name
            nodes[name] = node
    print(f"[validate] nodes defined : {len(nodes)}")

    # ---- 2. MaaFramework 真实加载 ----
    res = Resource()
    job = res.post_bundle(str(RESOURCE_DIR))
    job.wait()
    if not job.succeeded:
        err("Resource.post_bundle 加载失败")
        return finish()
    engine_nodes = set(res.node_list)
    print(f"[validate] engine nodes  : {len(engine_nodes)}")
    for name in nodes:
        if name not in engine_nodes:
            err(f"引擎未解析到节点: {name}（{nodes[name]['__file__']}）")

    # ---- 3. 引用 / 模板 / 锚点 / 绝对坐标检查 ----
    anchors_defined = set()
    for name, node in nodes.items():
        anchors_defined.update(collect_anchor_names(node))
    print(f"[validate] anchors       : {sorted(anchors_defined)}")

    for name, node in nodes.items():
        src = f"{node['__file__']}::{name}"
        for key, ref in iter_ref_strings(node):
            m = REF_PREFIX.match(ref)
            if m:
                kind = m.group(1)
                bare = ref[m.end():]
                if kind == "Anchor":
                    if bare not in anchors_defined:
                        err(f"{src}: [{key}] 引用未设置的锚点 [Anchor]{bare}")
                else:
                    if bare not in nodes:
                        err(f"{src}: [{key}] 引用未定义节点 [{kind}]{bare}")
            else:
                if ref not in nodes:
                    err(f"{src}: [{key}] 引用未定义节点 {ref}")
        for t in iter_templates(node):
            if not (IMAGE_DIR / t).is_file():
                err(f"{src}: 模板图缺失 image/{t}")
        for where in iter_absolute_targets(node):
            if name in SKIP_NODES:
                warn(f"{src}: 存在绝对坐标 {where}（豁免节点，见 SKIP_NODES）")
            elif node["__file__"] in MIGRATED_FILES:
                err(f"{src}: 存在绝对坐标 {where}（违反 docs/coords.md §4）")
            else:
                warn(f"{src}: 存在绝对坐标 {where}（上游未迁移文件，暂不约束）")

    # ---- 4. interface.json 任务入口 ----
    if INTERFACE_JSON.is_file():
        iface = json.loads(INTERFACE_JSON.read_text(encoding="utf-8"))
        for task in iface.get("task", []):
            entry = task.get("entry")
            if entry not in nodes:
                err(f"interface.json: 任务 {task.get('name')} 的 entry {entry!r} 不存在")
        print(f"[validate] tasks         : {[t.get('name') for t in iface.get('task', [])]}")
    else:
        err("缺少 assets/interface.json")

    return finish()


def finish():
    for w in warnings:
        print(f"  WARN: {w}")
    if errors:
        print(f"\n[validate] FAILED: {len(errors)} error(s)")
        for e in errors:
            print(f"  ERROR: {e}")
        return 1
    print("\n[validate] PASS: 资源加载成功，引用/模板/锚点/坐标规范全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
