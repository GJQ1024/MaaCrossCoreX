#!/usr/bin/env python3
"""产物一致性检查：壳版本、资源版本、agent 四份副本。

背景：仓库里有四类落地产物，都是 gitignore 的生成物，靠人工同步，容易漂移：
  - 源：assets/resource、agent/*.py、assets/interface.json（受版本控制，唯一基准）
  - 开发运行目录：install/（tools/install.py 产出，也是本机自用跑任务的地方）
  - 发布包目录：dist/A（资源包）、dist/B（整包）
  - 壳构建产物：<工作区>/work/MFAAvalonia/bin/.../（外部目录，不在本仓库内）

判据（只读，不改任何文件）：
  1) 壳：以最新的 MFAAvalonia.Core.dll 为基准，比对 install/、dist/*、release zip 里的同名文件；
     同时检查补丁标记 RefreshTaskGroups 是否存在（上游原版没有这个方法）。
  2) 资源：以 assets/resource 为基准，比对 install/、dist/A、dist/B 的文件集合与内容哈希。
  3) agent：以 agent/*.py 为基准，比对三份副本的 .py 哈希；忽略 __pycache__、*.bak_*、运行态 json。
  4) 版本号：四份 interface.json 的 version 字段与文件哈希。

退出码：0 全部一致；1 存在漂移；2 无法定位基准（壳构建产物或依赖目录缺失）。

用法：
  python tools/ci/check_consistency.py
  python tools/ci/check_consistency.py --json
  python tools/ci/check_consistency.py --release-dir D:/path/to/release
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

SHELL_CORE_REL = "libs/MFAAvalonia.Core.dll"
PATCH_MARKER = b"RefreshTaskGroups"
IGNORED_AGENT_SUFFIXES = (".pyc", ".bak", ".cmd")
IGNORED_DIRS = {"__pycache__"}

RESOURCE_COPIES = ("install", "dist/A", "dist/B")
AGENT_COPIES = ("install/agent", "dist/A/agent", "dist/B/agent")
INTERFACE_COPIES = (
    "assets/interface.json",
    "install/interface.json",
    "dist/A/interface.json",
    "dist/B/interface.json",
)


class Finding:
    def __init__(self, section: str, level: str, message: str):
        self.section = section
        self.level = level  # ok / drift / info
        self.message = message

    def as_dict(self) -> dict:
        return {"section": self.section, "level": self.level, "message": self.message}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mtime_str(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def find_workspace_root(start: Path) -> Path | None:
    """壳构建产物在仓库之外的工作区里，逐级向上找 work/MFAAvalonia/bin。"""
    cur = start
    for _ in range(6):
        if (cur / "work" / "MFAAvalonia" / "bin").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def find_shell_core(bin_dir: Path) -> Path | None:
    """取最新构建的 MFAAvalonia.Core.dll。

    构建目录（bin/AnyCPU/Release）把 Core 放在根目录，发布形态（install/dist）放在 libs/ 下，
    两种都认，避免只认 libs/ 时把构建产物漏掉。
    """
    hits = [p for p in bin_dir.glob("**/MFAAvalonia.Core.dll") if p.is_file()]
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


def shell_fingerprint(path: Path) -> dict:
    data = path.read_bytes()
    return {
        "path": str(path),
        "size": len(data),
        "sha256": sha256_bytes(data),
        "patched": PATCH_MARKER in data,
        "mtime": mtime_str(path),
    }


def zip_fingerprint(zip_path: Path, member: str) -> dict | None:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n.replace("\\", "/").endswith(member)]
            if not names:
                return None
            data = zf.read(names[0])
    except (OSError, zipfile.BadZipFile):
        return None
    return {
        "path": f"{zip_path.name}:{names[0]}",
        "size": len(data),
        "sha256": sha256_bytes(data),
        "patched": PATCH_MARKER in data,
        "mtime": "zip",
    }


def check_shell(root: Path, ws: Path | None, release_dir: Path | None) -> list[Finding]:
    out: list[Finding] = []
    probes: list[dict] = []
    baseline: dict | None = None
    if ws:
        build = find_shell_core(ws / "work" / "MFAAvalonia" / "bin")
        if build:
            probe = {**shell_fingerprint(build), "label": f"work({build.relative_to(ws).as_posix()})"}
            baseline = probe
            probes.append(probe)
    for rel in ("install", "dist/A", "dist/B", "dist/clean-shell"):
        p = root / rel / SHELL_CORE_REL
        if p.is_file():
            probes.append({**shell_fingerprint(p), "label": rel})
    if release_dir and release_dir.is_dir():
        for zp in sorted(release_dir.glob("*.zip")):
            fp = zip_fingerprint(zp, SHELL_CORE_REL)
            if fp:
                probes.append({**fp, "label": f"zip:{zp.name}"})

    if not probes:
        out.append(Finding("shell", "drift", "没有找到任何壳副本"))
        return out

    if baseline is None:
        candidates = [p for p in probes if p["mtime"] != "zip"] or probes
        base = max(candidates, key=lambda x: x["mtime"] if x["mtime"] != "zip" else "")
        out.append(
            Finding(
                "shell",
                "drift",
                f"未找到壳构建产物（work/MFAAvalonia/bin 下无 MFAAvalonia.Core.dll），"
                f"暂以 {base['label']} 为参照，无法判断发布包是否为自有构建",
            )
        )
    else:
        base = baseline
    base_sha = base["sha256"]
    base_label = base["label"]
    out.append(Finding("shell", "info", f"基准 = {base_label} sha256={base_sha[:12]} {base['size']}B mtime={base['mtime']}"))
    for p in probes:
        if p is base:
            continue
        same = p["sha256"] == base_sha
        tag = "一致" if same else "不一致"
        out.append(
            Finding(
                "shell",
                "ok" if same else "drift",
                f"{p['label']}: {tag} sha256={p['sha256'][:12]} {p['size']}B mtime={p['mtime']}"
                f" 补丁标记={'有' if p['patched'] else '无'}",
            )
        )
        if not p["patched"] and p["patched"] != base["patched"]:
            out.append(Finding("shell", "drift", f"{p['label']}: 缺少补丁标记 RefreshTaskGroups，疑似上游原版壳"))
    return out


def tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in root.rglob("*"):
        if f.is_file():
            out[f.relative_to(root).as_posix()] = sha256_file(f)
    return out


def check_resource(root: Path, limit: int) -> list[Finding]:
    out: list[Finding] = []
    src_dir = root / "assets" / "resource"
    if not src_dir.is_dir():
        return [Finding("resource", "drift", "缺少 assets/resource，无法确定基准")]
    src = tree_hashes(src_dir)
    out.append(Finding("resource", "info", f"基准 assets/resource：{len(src)} 个文件"))
    for copy in RESOURCE_COPIES:
        d = root / copy / "resource"
        if not d.is_dir():
            out.append(Finding("resource", "drift", f"{copy}/resource 不存在"))
            continue
        cur = tree_hashes(d)
        only_src = sorted(set(src) - set(cur))
        only_copy = sorted(set(cur) - set(src))
        diff = sorted(k for k in set(src) & set(cur) if src[k] != cur[k])
        if not (only_src or only_copy or diff):
            out.append(Finding("resource", "ok", f"{copy}/resource 与源一致（{len(cur)} 个文件）"))
            continue
        out.append(
            Finding(
                "resource",
                "drift",
                f"{copy}/resource 漂移：源有副本无 {len(only_src)}，副本有源无 {len(only_copy)}，内容不同 {len(diff)}",
            )
        )
        for name in only_src[:limit]:
            out.append(Finding("resource", "drift", f"  源有副本无: {name}"))
        for name in only_copy[:limit]:
            out.append(Finding("resource", "drift", f"  副本有源无: {name}"))
        for name in diff[:limit]:
            out.append(Finding("resource", "drift", f"  内容不同: {name}"))
        extra = len(only_src) + len(only_copy) + len(diff) - min(len(only_src), limit) - min(len(only_copy), limit) - min(len(diff), limit)
        if extra > 0:
            out.append(Finding("resource", "drift", f"  ……另有 {extra} 项未列出"))
    return out


def agent_py(d: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in d.rglob("*.py"):
        if IGNORED_DIRS & set(f.relative_to(d).parts):
            continue
        out[f.relative_to(d).as_posix()] = sha256_file(f)
    return out


def check_agent(root: Path, limit: int) -> list[Finding]:
    out: list[Finding] = []
    src = agent_py(root / "agent")
    if not src:
        return [Finding("agent", "drift", "缺少 agent/*.py，无法确定基准")]
    out.append(Finding("agent", "info", f"基准 agent/：{len(src)} 个 .py"))
    for copy in AGENT_COPIES:
        d = root / copy
        if not d.is_dir():
            out.append(Finding("agent", "drift", f"{copy} 不存在"))
            continue
        cur = agent_py(root / copy)
        only_src = sorted(set(src) - set(cur))
        only_copy = sorted(set(cur) - set(src))
        diff = sorted(k for k in set(src) & set(cur) if src[k] != cur[k])
        if not (only_src or only_copy or diff):
            out.append(Finding("agent", "ok", f"{copy} 与源一致（{len(cur)} 个 .py）"))
            continue
        out.append(
            Finding(
                "agent",
                "drift",
                f"{copy} 漂移：源有副本无 {len(only_src)}，副本有源无 {len(only_copy)}，内容不同 {len(diff)}",
            )
        )
        for name in only_src[:limit]:
            out.append(Finding("agent", "drift", f"  源有副本无: {name}"))
        for name in only_copy[:limit]:
            out.append(Finding("agent", "drift", f"  副本有源无: {name}"))
        for name in diff[:limit]:
            out.append(Finding("agent", "drift", f"  内容不同: {name}"))
    return out


def check_interface_version(root: Path) -> list[Finding]:
    out: list[Finding] = []
    versions: dict[str, list[str]] = {}
    for rel in INTERFACE_COPIES:
        p = root / rel
        if not p.is_file():
            out.append(Finding("version", "drift", f"{rel} 不存在"))
            continue
        data = p.read_bytes()
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            out.append(Finding("version", "drift", f"{rel} 解析失败：{exc}"))
            continue
        ver = str(doc.get("version") or "")
        sha = sha256_bytes(data)[:12]
        versions.setdefault(ver, []).append(rel)
        out.append(Finding("version", "info", f"{rel}: version={ver or '<缺失>'} sha256={sha}"))
    if len(versions) > 1:
        detail = "；".join(f"{v or '<缺失>'} ← {', '.join(paths)}" for v, paths in sorted(versions.items()))
        out.append(Finding("version", "drift", f"interface.json 版本号不一致：{detail}"))
    elif versions:
        (ver, paths), = versions.items()
        out.append(Finding("version", "ok", f"四份 interface.json 版本号一致：{ver or '<缺失>'}（{len(paths)} 份）"))
    return out


def render(findings: list[Finding]) -> None:
    titles = {"shell": "壳版本", "resource": "资源", "agent": "agent 副本", "version": "接口版本"}
    for section in ("shell", "resource", "agent", "version"):
        rows = [f for f in findings if f.section == section]
        if not rows:
            continue
        print(f"\n== {titles.get(section, section)} ==")
        for f in rows:
            mark = {"ok": "[OK]  ", "drift": "[DRIFT]", "info": "[INFO]"}[f.level]
            print(f"{mark} {f.message}")


def main() -> int:
    ap = argparse.ArgumentParser(description="产物一致性检查（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--root", default=None, help="仓库根（默认按脚本位置推断）")
    ap.add_argument("--release-dir", default=None, help="release zip 目录（默认自动找 <工作区>/work/release）")
    ap.add_argument("--max-list", type=int, default=20, help="每类漂移最多列出的文件名")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else repo_root()
    if not (root / "assets").is_dir():
        print(f"[check] 仓库根不合法：{root}", file=sys.stderr)
        return 2

    ws = find_workspace_root(root)
    release_dir = Path(args.release_dir).resolve() if args.release_dir else (ws / "work" / "release" if ws else None)

    findings: list[Finding] = []
    findings += check_shell(root, ws, release_dir)
    findings += check_resource(root, args.max_list)
    findings += check_agent(root, args.max_list)
    findings += check_interface_version(root)

    drifts = [f for f in findings if f.level == "drift"]
    if args.json:
        print(json.dumps(
            {
                "root": str(root),
                "workspace": str(ws) if ws else None,
                "release_dir": str(release_dir) if release_dir else None,
                "drift_count": len(drifts),
                "findings": [f.as_dict() for f in findings],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(f"仓库根: {root}")
        print(f"工作区: {ws if ws else '<未找到>'}")
        print(f"发布目录: {release_dir if release_dir else '<未找到>'}")
        render(findings)
        print(f"\n结论: {'一致' if not drifts else f'发现 {len(drifts)} 项漂移'}")

    if any(f.level == "drift" and "未找到壳构建产物" in f.message for f in findings):
        return 2
    return 0 if not drifts else 1


if __name__ == "__main__":
    sys.exit(main())
