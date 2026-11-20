#!/usr/bin/env python3
"""提交前作用域闸门：只允许提交本次声明的文件，并阻止与他会话占用冲突。

设计约束：
- 只读 git 索引与 .tmp/ 下的本地约定文件，不修改工作树、不改写历史。
- 默认不安装 git hook；安装需显式 --install，且已有 hook 时默认拒绝覆盖。
- 开关：存在 .tmp/scope_guard.enabled 才执行检查；否则 hook 直接放行并打印提示。
- 口径：--hook 在非 git 目录退出 2；找不到可用的 python 由 hook 退出 1（均为阻断，不静默放行）。

约定文件（均在仓库根 .tmp/ 下，已被 .gitignore 的 `*.tmp` 忽略）：
  .tmp/commit_scope.txt                 每行一个路径，支持 # 注释、*?[] 通配符
  .tmp/session_claims/<session>.json    {"session": "...", "updated_at": "...", "paths": [...]}
  .tmp/session_claims/current           当前会话名（可选）

路径归一化：`\\`→`/`、去 `./`、合并重复 `/`；Windows 下一律 casefold；
声明以 `/` 结尾表示目录，按前缀匹配。绝对路径与含 `..` 的声明一律拒绝。

用法：
  python tools/ci/pre_commit_scope.py --hook           # git hook 调用
  python tools/ci/pre_commit_scope.py --install        # 安装 hook（不覆盖已有）
  python tools/ci/pre_commit_scope.py --uninstall      # 卸载本工具的 hook
  python tools/ci/pre_commit_scope.py --enable/--disable
  python tools/ci/pre_commit_scope.py --status
  python tools/ci/pre_commit_scope.py --claim <paths...> / --release
  python tools/ci/pre_commit_scope.py --scope-set <paths...>   # 覆盖写入本次提交清单
  python tools/ci/pre_commit_scope.py --self-test      # 临时仓库内验证全部判据
  python tools/ci/pre_commit_scope.py --ci             # 仅自检 + 确认 .tmp 未入库
  python tools/ci/pre_commit_scope.py --ci --base <ref># 额外校验 base...HEAD 改动集（需 scope 清单）
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# 必须与 hook_shim() 第一行注释逐字一致，否则安装/卸载/状态会互相认不出
HOOK_MARKER = "# 由 tools/ci/pre_commit_scope.py --install 生成"
CLAIM_TTL_SECONDS = 30 * 60
TMP_DIR = ".tmp"
CLAIMS_DIR = f"{TMP_DIR}/session_claims"
SCOPE_FILE = f"{TMP_DIR}/commit_scope.txt"
ENABLE_FILE = f"{TMP_DIR}/scope_guard.enabled"
CURRENT_SESSION_FILE = f"{CLAIMS_DIR}/current"

TEXT_SUFFIXES = {
    ".py", ".json", ".md", ".txt", ".yml", ".yaml", ".cs", ".csproj",
    ".sh", ".toml", ".ini", ".cfg", ".xml", ".html", ".css", ".js",
}
CRLF_ALLOWED_SUFFIXES = {".bat", ".cmd", ".ps1", ".sln", ".vcxproj", ".props", ".targets"}

CASE_INSENSITIVE = os.name == "nt"


class Violation(Exception):
    pass


def run_git(args: list[str], cwd: Path, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c", "core.quotepath=false",
            # 让 CR 视为行尾的一部分，CRLF 交给专门的检查分支报告，避免被 --check 抢先报成空白
            "-c", "core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
    )


def normalize_path(raw: str) -> str:
    """统一声明/实际路径：分隔符、./、重复斜杠、Windows 大小写。"""
    p = str(raw).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/{2,}", "/", p)
    if p.endswith("/"):
        p = p.rstrip("/") + "/"
    return p.casefold() if CASE_INSENSITIVE else p


def is_absolute_path(raw: str) -> bool:
    p = str(raw).strip().replace("\\", "/")
    return p.startswith("/") or bool(re.match(r"^[A-Za-z]:/", p))


def has_glob(entry: str) -> bool:
    return any(ch in entry for ch in "*?[")


def split_entry(entry: str) -> tuple[str, bool]:
    e = normalize_path(entry)
    return e.rstrip("/"), e.endswith("/")


def declared_hit(declared: str, actual: str) -> bool:
    """声明是否命中实际路径：目录按前缀，通配符按 fnmatch，否则精确相等。"""
    d, is_dir = split_entry(declared)
    if not d:
        return False
    a = normalize_path(actual)
    if is_dir:
        return a == d or a.startswith(d + "/")
    if has_glob(d):
        return fnmatch.fnmatchcase(a, d)
    return a == d


def exact_declared(declared: str, actual: str) -> bool:
    """删除项只接受精确路径，目录前缀与通配符都不算。"""
    d, is_dir = split_entry(declared)
    return (not is_dir) and (not has_glob(d)) and d == normalize_path(actual)


def repo_root(cwd: Path) -> Path:
    p = run_git(["rev-parse", "--show-toplevel"], cwd)
    if p.returncode != 0 or not p.stdout.strip():
        raise Violation("当前目录不在 git 仓库内")
    return Path(p.stdout.strip())


def staged_entries(root: Path) -> tuple[list[str], list[str]]:
    """返回 (全部 staged 路径, staged 删除路径)。"""
    p = run_git(["diff", "--cached", "--name-status", "-z", "--no-renames"], root)
    if p.returncode != 0:
        raise Violation(f"读取暂存区失败：{p.stderr.strip()}")
    parts = [x for x in p.stdout.split("\0") if x]
    staged: list[str] = []
    deleted: list[str] = []
    i = 0
    while i + 1 < len(parts):
        status, path = parts[i], parts[i + 1]
        staged.append(path)
        if status.startswith("D"):
            deleted.append(path)
        i += 2
    return staged, deleted


def read_scope(root: Path) -> list[str]:
    f = root / SCOPE_FILE
    if not f.exists():
        raise Violation(
            f"缺少 {SCOPE_FILE}：先在 .tmp/commit_scope.txt 写本次允许提交的路径（每行一个）"
        )
    entries = []
    # utf-8-sig：兼容 Windows 记事本保存的 UTF-8 BOM，避免首行路径失配
    for line in f.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if is_absolute_path(line):
            raise Violation(f"{SCOPE_FILE} 不允许绝对路径：{line}")
        entries.append(line)
    if not entries:
        raise Violation(
            f"{SCOPE_FILE} 为空：本次没有声明任何可提交路径。"
            "开关启用后空清单会拒绝所有提交，请先写清单再提交。"
        )
    return entries


def scope_is_empty(root: Path) -> bool:
    """按 read_scope 的同口径判断清单是否有效（忽略空行与注释）。"""
    f = root / SCOPE_FILE
    if not f.exists():
        return True
    for line in f.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return False
    return True


def resolve_session(cli_session: str | None, root: Path) -> str:
    if cli_session:
        return cli_session
    env = os.environ.get("SCOPE_SESSION_ID")
    if env:
        return env
    f = root / CURRENT_SESSION_FILE
    if f.exists():
        name = f.read_text(encoding="utf-8").strip()
        if name:
            return name
    return "default"


def warn_default_session(session: str) -> None:
    if session == "default":
        print(
            "[scope-guard] 警告：会话名落到 default。所有未设置会话名的会话会被视为同一会话，"
            "跨会话冲突检测失效。请设置环境变量 SCOPE_SESSION_ID，或把会话名写入 "
            f"{CURRENT_SESSION_FILE}。",
            file=sys.stderr,
        )


def parse_ts(value) -> float | None:
    # bool 是 int 的子类，必须在数值判断前排除，否则 True 会被当成 1.0 时间戳
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def load_claims(root: Path, own_session: str, strict: bool) -> tuple[list[dict], list[str]]:
    """读取占用声明。strict=True 时任何解析异常都 fail-closed。"""
    active: list[dict] = []
    warnings: list[str] = []
    d = root / CLAIMS_DIR
    if not d.is_dir():
        return active, warnings
    now = time.time()
    own = normalize_path(own_session)
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("顶层不是 JSON 对象")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            if strict:
                raise Violation(f"占用声明解析失败（fail-closed）：{f.name}：{exc}")
            warnings.append(f"占用声明解析失败，状态查询模式忽略：{f.name}")
            continue

        session = str(data.get("session") or f.stem)
        # 先取原值做类型校验，再处理缺省；不能用 `or []`，否则 {} / "" / 0 会被短路成空列表而绕过 fail-closed
        raw_paths = data.get("paths")
        # 缺字段与 null 都属声明不完整，fail-closed；显式 paths: [] 是合法空声明
        if "paths" not in data or raw_paths is None:
            if strict:
                raise Violation(
                    f"占用声明缺少 paths 字段或为 null（fail-closed）：{f.name}"
                )
            warnings.append(f"占用声明缺少 paths 或为 null，状态查询模式忽略：{f.name}")
            continue
        if not isinstance(raw_paths, list) or any(not isinstance(p, str) for p in raw_paths):
            if strict:
                raise Violation(
                    f"占用声明 paths 必须是字符串数组（fail-closed）：{f.name}"
                    f"（实际类型 {type(raw_paths).__name__}）"
                )
            warnings.append(f"占用声明 paths 非字符串数组，状态查询模式忽略：{f.name}")
            continue
        else:
            paths = raw_paths

        ts = parse_ts(data.get("updated_at"))
        if ts is None:
            if strict:
                raise Violation(f"占用声明时间戳无效（fail-closed）：{f.name}")
            warnings.append(f"占用声明时间戳无效，状态查询模式忽略：{f.name}")
            continue
        if ts > now + 60:
            warnings.append(f"占用声明时间戳在未来，已钳到当前时间：{f.name}")
            ts = now
        if now - ts > CLAIM_TTL_SECONDS:
            continue
        if normalize_path(session) == own:
            continue

        active.append({
            "session": session,
            "paths": paths,
            "updated_at": ts,
            "file": f.name,
        })
    return active, warnings


def check_scope(root: Path, session: str) -> int:
    staged, deleted = staged_entries(root)
    if not staged:
        print("[scope-guard] 暂存区为空，放行。")
        return 0

    scope = read_scope(root)

    in_tmp = [p for p in staged if normalize_path(p) == TMP_DIR or normalize_path(p).startswith(TMP_DIR + "/")]
    if in_tmp:
        raise Violation(".tmp/ 属于本地约定文件，禁止提交（请从暂存区移除）")

    outside = [p for p in staged if not any(declared_hit(e, p) for e in scope)]
    if outside:
        lines = "\n".join(f"  - {p}" for p in outside)
        raise Violation(
            "以下暂存文件不在 .tmp/commit_scope.txt 声明的范围内：\n" + lines
            + "\n\n只允许 `git add -- <明确路径>`，禁止 `git add -A` / `git commit -a`。"
        )

    bad_deletes = [p for p in deleted if not any(exact_declared(e, p) for e in scope)]
    if bad_deletes:
        lines = "\n".join(f"  - {p}" for p in bad_deletes)
        raise Violation(
            "以下删除项没有被显式列进 commit_scope.txt（删除项不接受目录或通配符）：\n" + lines
        )

    claims, warnings = load_claims(root, session, strict=True)
    for w in warnings:
        print(f"[scope-guard] 警告：{w}", file=sys.stderr)
    conflicts: list[str] = []
    for claim in claims:
        for claimed in claim["paths"]:
            for p in staged:
                if declared_hit(claimed, p):
                    when = datetime.fromtimestamp(claim["updated_at"], tz=timezone.utc).isoformat()
                    conflicts.append(
                        f"  - {p} 已被会话 `{claim['session']}` 声明"
                        f"（声明项 {claimed}，更新于 {when}）"
                    )
    if conflicts:
        raise Violation("暂存文件与其他会话的有效占用声明冲突：\n" + "\n".join(conflicts))

    p = run_git(["diff", "--cached", "--check"], root)
    if p.returncode != 0:
        detail = (p.stdout or "") + (p.stderr or "")
        raise Violation("`git diff --cached --check` 未通过（空白/冲突标记）：\n" + detail.strip())

    crlf_files = []
    for path in staged:
        suffix = Path(path).suffix.lower()
        if suffix in CRLF_ALLOWED_SUFFIXES or suffix not in TEXT_SUFFIXES:
            continue
        blob = run_git(["show", f":{path}"], root, text=False)
        if blob.returncode != 0:
            continue
        content = blob.stdout or b""
        if b"\r\n" not in content:
            continue
        head = run_git(["show", f"HEAD:{path}"], root, text=False)
        if head.returncode == 0 and b"\r\n" not in (head.stdout or b""):
            crlf_files.append(path)
        elif head.returncode != 0:
            crlf_files.append(path)
    if crlf_files:
        lines = "\n".join(f"  - {p}" for p in crlf_files)
        raise Violation(
            "以下文本文件在索引中是 CRLF，而仓库/HEAD 使用 LF（会污染整文件 diff）：\n" + lines
            + "\n请转成 LF 后重新 `git add -- <路径>`；仅 .bat/.cmd/.ps1/.sln 等允许 CRLF。"
        )

    print(f"[scope-guard] 通过：{len(staged)} 个暂存文件均在作用域内，无占用冲突。")
    return 0


def hook_path(root: Path) -> Path:
    p = run_git(["rev-parse", "--git-path", "hooks/pre-commit"], root)
    if p.returncode != 0:
        raise Violation("无法定位 .git/hooks 目录")
    path = Path(p.stdout.strip())
    return path if path.is_absolute() else (root / path)


def hook_shim() -> str:
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}，删除请用 --uninstall\n"
        "repo=\"$(git rev-parse --show-toplevel)\" || { "
        "echo '[scope-guard] 不在 git 仓库内，拒绝提交（exit 2）' >&2; exit 2; }\n"
        "if command -v python >/dev/null 2>&1; then PY=python\n"
        "elif command -v py >/dev/null 2>&1; then PY=py\n"
        "else echo '[scope-guard] 未找到 python，无法执行提交闸门（exit 1）；"
        "如确需提交请 git commit --no-verify 并在回报中说明' >&2; exit 1; fi\n"
        "\"$PY\" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' "
        "|| { echo '[scope-guard] python 版本低于 3.10，无法执行闸门（exit 1）' >&2; exit 1; }\n"
        f"exec \"$PY\" \"$repo/tools/ci/pre_commit_scope.py\" --hook\n"
    )


def install_hook(root: Path) -> int:
    path = hook_path(root)
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARKER not in text:
            print(f"[scope-guard] 已存在其他 pre-commit hook：{path}")
            print("[scope-guard] 未覆盖。请人工合并，或将原 hook 改为调用本脚本。")
            return 2
        print(f"[scope-guard] hook 已安装：{path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hook_shim(), encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    print(f"[scope-guard] 已安装 hook：{path}")
    print("[scope-guard] 默认开关关闭：执行 `python tools/ci/pre_commit_scope.py --enable` 才生效。")
    return 0


def uninstall_hook(root: Path) -> int:
    path = hook_path(root)
    if not path.exists():
        print("[scope-guard] 没有安装 hook。")
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    if HOOK_MARKER not in text:
        print(f"[scope-guard] {path} 不是本工具生成的，拒绝删除。")
        return 2
    path.unlink()
    print(f"[scope-guard] 已卸载 hook：{path}")
    return 0


def set_enabled(root: Path, enabled: bool) -> int:
    f = root / ENABLE_FILE
    if enabled:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("enabled\n", encoding="utf-8", newline="\n")
        print(f"[scope-guard] 已启用（{ENABLE_FILE}）")
        # 与 read_scope 同口径：只有注释/空行也算空清单
        if scope_is_empty(root):
            print(
                f"[scope-guard] 警告：{SCOPE_FILE} 缺失或为空（只有注释也算空），"
                "启用后所有提交都会被拒绝。"
                "请先写入本次允许提交的路径。",
                file=sys.stderr,
            )
    elif f.exists():
        f.unlink()
        print(f"[scope-guard] 已关闭（删除 {ENABLE_FILE}）")
    else:
        print("[scope-guard] 当前已是关闭状态")
    return 0


def status(root: Path) -> int:
    hp = hook_path(root)
    installed = hp.exists() and HOOK_MARKER in hp.read_text(encoding="utf-8", errors="replace")
    enabled = (root / ENABLE_FILE).exists()
    scope = root / SCOPE_FILE
    session = resolve_session(None, root)
    claims, warnings = load_claims(root, session, strict=False)
    print(f"仓库根      : {root}")
    print(f"hook 已安装 : {'是' if installed else '否'}  ({hp})")
    print(f"开关        : {'启用' if enabled else '关闭'}  ({ENABLE_FILE})")
    print(f"作用域文件  : {'存在' if scope.exists() else '缺失'}  ({SCOPE_FILE})")
    print(f"当前会话    : {session}")
    print(f"其他有效声明: {len(claims)} 条")
    for c in claims:
        print(f"  - {c['session']}: {len(c['paths'])} 条路径  ({c['file']})")
    for w in warnings:
        print(f"  ! {w}", file=sys.stderr)
    warn_default_session(session)
    return 0


def write_claim(root: Path, session: str, paths: list[str]) -> int:
    warn_default_session(session)
    clean: list[str] = []
    for raw in paths:
        if is_absolute_path(raw):
            raise Violation(f"--claim 不接受绝对路径：{raw}")
        n = normalize_path(raw)
        if not n or n == "/":
            raise Violation(f"--claim 路径为空：{raw!r}")
        if ".." in n.rstrip("/").split("/"):
            raise Violation(f"--claim 不接受 .. 路径：{raw}")
        if not n.endswith("/") and (root / n).is_dir():
            print(
                f"[scope-guard] 提示：{raw} 是目录，目录声明建议写成 {n}/ 以启用前缀匹配。",
                file=sys.stderr,
            )
        clean.append(n)
    d = root / CLAIMS_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session}.json"
    existing: list[str] = []
    if f.exists():
        try:
            existing = list(json.loads(f.read_text(encoding="utf-8")).get("paths") or [])
        except (OSError, json.JSONDecodeError):
            existing = []
    merged = sorted({*(normalize_path(x) for x in existing), *clean})
    data = {
        "session": session,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "paths": merged,
    }
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"[scope-guard] 已声明 {len(merged)} 条路径 → {f}")
    return 0


def set_scope(root: Path, paths: list[str]) -> int:
    """覆盖写入本次提交清单，避免手写文件时漏项或残留上次内容。"""
    clean: list[str] = []
    for raw in paths:
        if is_absolute_path(raw):
            raise Violation(f"--scope-set 不接受绝对路径：{raw}")
        n = normalize_path(raw)
        if not n or n == "/":
            raise Violation(f"--scope-set 路径为空：{raw!r}")
        if ".." in n.rstrip("/").split("/"):
            raise Violation(f"--scope-set 不接受 .. 路径：{raw}")
        clean.append(n)
    f = root / SCOPE_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(clean) + "\n", encoding="utf-8", newline="\n")
    print(f"[scope-guard] 已覆盖写入 {len(clean)} 条路径 → {f}")
    return 0


def release_claim(root: Path, session: str) -> int:
    f = root / CLAIMS_DIR / f"{session}.json"
    if f.exists():
        f.unlink()
        print(f"[scope-guard] 已释放占用声明：{f}")
    else:
        print("[scope-guard] 没有找到本会话的占用声明。")
    return 0


def ci_check(root: Path, base: str | None) -> int:
    tracked = run_git(["ls-files", TMP_DIR], root)
    if tracked.returncode == 0 and tracked.stdout.strip():
        print("[scope-guard] CI 失败：.tmp/ 下的本地约定文件被跟踪入库：")
        print(tracked.stdout)
        return 1

    if base:
        scope = read_scope(root)
        diff = run_git(["diff", "--name-only", f"{base}...HEAD"], root)
        if diff.returncode != 0:
            print(f"[scope-guard] 无法读取改动集 {base}...HEAD：{diff.stderr.strip()}")
            return 2
        deleted_diff = run_git(
            ["diff", "--name-only", "--diff-filter=D", f"{base}...HEAD"], root
        )
        deleted = {p for p in deleted_diff.stdout.splitlines() if p.strip()}
        changed = [x for x in diff.stdout.splitlines() if x.strip()]
        outside = [
            p for p in changed
            if p not in deleted and not any(declared_hit(e, p) for e in scope)
        ]
        bad_deletes = [
            p for p in changed
            if p in deleted and not any(exact_declared(e, p) for e in scope)
        ]
        if outside or bad_deletes:
            print(f"[scope-guard] 改动集超出 {SCOPE_FILE} 范围：")
            for p in outside:
                print(f"  - {p}")
            for p in bad_deletes:
                print(f"  - {p}（删除项必须精确声明，不接受目录或通配符）")
            return 1
        print(f"[scope-guard] CI 通过：.tmp 未入库，{base}...HEAD 改动集在作用域内。")
        return 0

    rc = self_test()
    if rc != 0:
        return rc
    print(
        "[scope-guard] CI 通过：.tmp 未入库，脚本自检全绿。"
        "本模式只验证闸门自身，不校验 PR 改动集；需要拦截改动集请使用 --base <ref>。"
    )
    return 0


def self_test() -> int:
    """在临时仓库中复现全部判据与已知绕过写法，返回 0 表示全部按预期触发。"""
    script = Path(__file__).resolve()
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mccx-scope-selftest-") as tmp:
        repo = Path(tmp)
        run_git(["init", "-q"], repo)
        run_git(["config", "user.email", "selftest@example.invalid"], repo)
        run_git(["config", "user.name", "scope-selftest"], repo)
        run_git(["config", "core.autocrlf", "false"], repo)
        (repo / TMP_DIR / "session_claims").mkdir(parents=True)
        # hook shim 会按仓库根定位 tools/ci/pre_commit_scope.py，临时仓库需放一份
        (repo / "tools" / "ci").mkdir(parents=True)
        shutil.copyfile(script, repo / "tools" / "ci" / "pre_commit_scope.py")

        def put(rel: str, text: str):
            """显式 LF 写入，避免 Windows 上 write_text 把 \\n 翻成 CRLF。"""
            (repo / rel).write_text(text, encoding="utf-8", newline="\n")

        def call(*extra_args: str, session: str | None = "me") -> subprocess.CompletedProcess:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            if session is None:
                env.pop("SCOPE_SESSION_ID", None)
            else:
                env["SCOPE_SESSION_ID"] = session
            return subprocess.run(
                [sys.executable, str(script), *extra_args],
                cwd=str(repo), capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=env,
            )

        def expect(name: str, result: subprocess.CompletedProcess, code: int, needle: str):
            out = (result.stdout or "") + (result.stderr or "")
            if result.returncode != code or needle not in out:
                failures.append(
                    f"{name}: 期望 exit={code} 且包含 {needle!r}，实际 exit={result.returncode}\n{out}"
                )

        def write_claim_json(name: str, session: str, paths, updated_at=None):
            put(
                f"{CLAIMS_DIR}/{name}.json",
                json.dumps({
                    "session": session,
                    "updated_at": updated_at if updated_at is not None
                    else datetime.now(timezone.utc).isoformat(),
                    "paths": paths,
                }),
            )

        def reset_stage():
            run_git(["reset", "-q"], repo)
            put(SCOPE_FILE, "")
            for f in (repo / CLAIMS_DIR).glob("*.json"):
                f.unlink()

        # 0) 初始提交与开关
        put(ENABLE_FILE, "enabled\n")
        put("keep.txt", "ok\n")
        run_git(["add", "--", "keep.txt"], repo)
        run_git(["commit", "-q", "-m", "init"], repo)

        # 1) 作用域外文件
        reset_stage()
        put(SCOPE_FILE, "allowed.txt\n")
        put("outside.txt", "x\n")
        run_git(["add", "--", "outside.txt"], repo)
        expect("作用域外", call("--hook"), 1, "不在 .tmp/commit_scope.txt")

        # 2) 与会话占用冲突
        reset_stage()
        put(SCOPE_FILE, "claimed.txt\n")
        put("claimed.txt", "x\n")
        run_git(["add", "--", "claimed.txt"], repo)
        write_claim_json("other", "other", ["claimed.txt"])
        expect("占用冲突", call("--hook"), 1, "已被会话 `other` 声明")

        # 3) 过期声明不阻塞
        reset_stage()
        put(SCOPE_FILE, "claimed.txt\n")
        put("claimed.txt", "x\n")
        run_git(["add", "--", "claimed.txt"], repo)
        write_claim_json("stale", "stale", ["claimed.txt"], time.time() - CLAIM_TTL_SECONDS - 60)
        expect("过期声明", call("--hook"), 0, "通过")

        # 4) 行尾空白（diff --check）
        reset_stage()
        put(SCOPE_FILE, "trail.txt\n")
        put("trail.txt", "bad \n")
        run_git(["add", "--", "trail.txt"], repo)
        expect("行尾空白", call("--hook"), 1, "diff --cached --check")

        # 5) CRLF
        reset_stage()
        put(SCOPE_FILE, "crlf.py\n")
        (repo / "crlf.py").write_bytes(b"print(1)\r\n")
        run_git(["add", "--", "crlf.py"], repo)
        expect("CRLF", call("--hook"), 1, "CRLF")

        # 6) 删除项未显式声明
        reset_stage()
        run_git(["rm", "-q", "--cached", "keep.txt"], repo)
        put(SCOPE_FILE, "*.txt\n")
        expect("删除项未显式声明", call("--hook"), 1, "删除项")

        # 7) 正常删除
        reset_stage()
        put(SCOPE_FILE, "keep.txt\n")
        run_git(["rm", "-q", "keep.txt"], repo)
        expect("正常删除", call("--hook"), 0, "通过")

        # 8) 缺少作用域文件
        reset_stage()
        (repo / SCOPE_FILE).unlink()
        put("outside.txt", "x\n")
        run_git(["add", "--", "outside.txt"], repo)
        expect("缺少作用域", call("--hook"), 1, "缺少 .tmp/commit_scope.txt")

        # 9) 空清单：启用后拒绝一切提交
        reset_stage()
        put(SCOPE_FILE, "# only comment\n")
        put("outside.txt", "x\n")
        run_git(["add", "--", "outside.txt"], repo)
        expect("空清单", call("--hook"), 1, "为空")

        # 10) 声明路径写法：反斜杠 / ./ / 目录前缀 / Windows 大小写
        variants = ["agent/foo.py", "agent\\foo.py", "./agent/foo.py", "agent/"]
        if CASE_INSENSITIVE:
            variants.append("Agent/foo.py")
        for idx, declared in enumerate(variants):
            reset_stage()
            put(SCOPE_FILE, "agent/foo.py\n")
            (repo / "agent").mkdir(exist_ok=True)
            put("agent/foo.py", "x\n")
            run_git(["add", "--", "agent/foo.py"], repo)
            write_claim_json(f"other{idx}", "other", [declared])
            expect(f"声明写法 {declared}", call("--hook"), 1, "已被会话 `other` 声明")

        # 10b) 目录不误伤：声明别的前缀应放行
        reset_stage()
        put(SCOPE_FILE, "agent/foo.py\n")
        put("agent/foo.py", "x\n")
        run_git(["add", "--", "agent/foo.py"], repo)
        write_claim_json("otherx", "other", ["agent2/"])
        expect("目录不误伤", call("--hook"), 0, "通过")

        # 11) 声明解析失败：fail-closed
        reset_stage()
        put(SCOPE_FILE, "agent/foo.py\n")
        put("agent/foo.py", "x\n")
        run_git(["add", "--", "agent/foo.py"], repo)
        put(f"{CLAIMS_DIR}/broken.json", "{not json")
        expect("声明解析失败", call("--hook"), 1, "fail-closed")

        # 12) 未来时间戳：钳到当前并告警
        reset_stage()
        put(SCOPE_FILE, "agent/foo.py\n")
        put("agent/foo.py", "x\n")
        run_git(["add", "--", "agent/foo.py"], repo)
        write_claim_json("future", "other", ["agent/foo.py"],
                         datetime.now(timezone.utc).timestamp() + 86400 * 365)
        expect("未来时间戳", call("--hook"), 1, "未来")

        # 13) default 会话必须告警
        reset_stage()
        put(SCOPE_FILE, "ok.txt\n")
        put("ok.txt", "x\n")
        run_git(["add", "--", "ok.txt"], repo)
        expect("default 告警", call("--hook", session=None), 0, "default")

        # 14) 非 git 目录：--hook 阻断
        with tempfile.TemporaryDirectory(prefix="mccx-notgit-") as outside:
            r = subprocess.run(
                [sys.executable, str(script), "--hook"], cwd=outside,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "SCOPE_SESSION_ID": "me"},
            )
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 2 or "不在 git 仓库" not in out:
                failures.append(f"非 git 目录：期望 exit=2，实际 {r.returncode}\n{out}")

        # 15) --claim 拒绝绝对路径
        r = call("--claim", "C:/abs/path.py")
        if r.returncode == 0 or "绝对路径" not in ((r.stdout or "") + (r.stderr or "")):
            failures.append(f"--claim 绝对路径：期望拒绝，实际 exit={r.returncode}\n{r.stdout}{r.stderr}")

        # 16) install/status/uninstall 往返
        hp = repo / ".git" / "hooks" / "pre-commit"
        r = call("--install")
        text = hp.read_text(encoding="utf-8", errors="replace") if hp.exists() else ""
        if r.returncode != 0 or HOOK_MARKER not in text:
            failures.append(f"install：exit={r.returncode}，hook 内容未含标记\n{r.stdout}{r.stderr}")
        expect("status 识别安装", call("--status"), 0, "hook 已安装 : 是")
        expect("重复 install 幂等", call("--install"), 0, "hook 已安装")
        # 16b) 关闭开关时真实 git commit 只提示并放行（在临时仓库验证，不动真实仓提交）
        (repo / ENABLE_FILE).unlink()
        put("outside2.txt", "x\n")
        run_git(["add", "--", "outside2.txt"], repo)
        committed = subprocess.run(
            ["git", "commit", "-m", "disabled pass"], cwd=str(repo),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        committed_out = (committed.stdout or "") + (committed.stderr or "")
        if committed.returncode != 0 or "开关未启用" not in committed_out:
            failures.append(
                f"关闭开关放行：期望 exit=0 且有提示，实际 {committed.returncode}\n{committed_out}"
            )
        put(ENABLE_FILE, "enabled\n")
        # 16c) 启用但清单为空：真实 git commit 必须被拒（临时仓库）
        put(SCOPE_FILE, "# comment only\n")
        put("outside3.txt", "x\n")
        run_git(["add", "--", "outside3.txt"], repo)
        rejected = subprocess.run(
            ["git", "commit", "-m", "empty scope"], cwd=str(repo),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        rejected_out = (rejected.stdout or "") + (rejected.stderr or "")
        if rejected.returncode == 0 or "为空" not in rejected_out:
            failures.append(
                f"空清单真实提交：期望被拒且提示为空，实际 {rejected.returncode}\n{rejected_out}"
            )
        run_git(["reset", "-q"], repo)
        r = call("--uninstall")
        if r.returncode != 0 or hp.exists():
            failures.append(f"uninstall：exit={r.returncode}，hook 仍存在={hp.exists()}")
        expect("status 识别卸载", call("--status"), 0, "hook 已安装 : 否")

        # 17) CI 必须拦住被跟踪的 .tmp
        reset_stage()
        put(f"{TMP_DIR}/tracked.txt", "x\n")
        run_git(["add", "--", f"{TMP_DIR}/tracked.txt"], repo)
        run_git(["commit", "-q", "-m", "track tmp"], repo)
        # 用 --base HEAD 走改动集分支，避免 --ci 无 base 时递归调用自检
        expect("CI 拦住 .tmp 入库", call("--ci", "--base", "HEAD"), 1, "被跟踪入库")

        # 18) --ci --base 校验改动集（先把 .tmp 从跟踪中移除，避免命中上一条判据）
        run_git(["rm", "-q", "--cached", f"{TMP_DIR}/tracked.txt"], repo)
        run_git(["commit", "-q", "-m", "untrack tmp"], repo)
        reset_stage()
        put("bad.txt", "x\n")
        run_git(["add", "--", "bad.txt"], repo)
        run_git(["commit", "-q", "-m", "bad change"], repo)
        put(SCOPE_FILE, "good.txt\n")
        expect("CI 改动集越界", call("--ci", "--base", "HEAD~1"), 1, "改动集超出")

        # 19) paths 类型绕过：{} / "" / 0 必须 fail-closed（不能被 `or []` 短路）
        for label, value in [("dict", {}), ("str", ""), ("int", 0)]:
            reset_stage()
            put(SCOPE_FILE, "agent/foo.py\n")
            put("agent/foo.py", "x\n")
            run_git(["add", "--", "agent/foo.py"], repo)
            put(f"{CLAIMS_DIR}/badtype.json", json.dumps({
                "session": "other",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "paths": value,
            }))
            expect(f"paths 类型 {label}", call("--hook"), 1, "fail-closed")

        # 20) updated_at=true 不能被当成 1.0 时间戳
        reset_stage()
        put(SCOPE_FILE, "agent/foo.py\n")
        put("agent/foo.py", "x\n")
        run_git(["add", "--", "agent/foo.py"], repo)
        put(f"{CLAIMS_DIR}/boolts.json", json.dumps({
            "session": "other",
            "updated_at": True,
            "paths": ["agent/foo.py"],
        }))
        expect("updated_at 布尔", call("--hook"), 1, "时间戳无效")

        # 21) --claim 落到 default 会话必须告警
        reset_stage()
        expect("claim default 告警", call("--claim", "agent/foo.py", session=None), 0, "default")
        (repo / CLAIMS_DIR / "default.json").unlink(missing_ok=True)

        # 22) 只有注释的清单，--enable 必须告警
        reset_stage()
        put(SCOPE_FILE, "# only comment\n")
        expect("注释清单启用告警", call("--enable"), 0, "所有提交都会被拒绝")
        call("--disable")

        # 23) UTF-8 BOM 清单首行不能失配（上一条测试关闭了开关，这里重新启用）
        reset_stage()
        put(ENABLE_FILE, "enabled\n")
        (repo / SCOPE_FILE).write_bytes(b"\xef\xbb\xbfbom.txt\n")
        put("bom.txt", "x\n")
        run_git(["add", "--", "bom.txt"], repo)
        expect("BOM 清单", call("--hook"), 0, "通过")

        # 24) CI 删除项不能被通配覆盖
        reset_stage()
        put("del.txt", "x\n")
        run_git(["add", "--", "del.txt"], repo)
        run_git(["commit", "-q", "-m", "add del"], repo)
        run_git(["rm", "-q", "del.txt"], repo)
        run_git(["commit", "-q", "-m", "delete del"], repo)
        put(SCOPE_FILE, "*.txt\n")
        expect("CI 删除项不能被通配覆盖", call("--ci", "--base", "HEAD~1"), 1, "删除项必须精确声明")

        # 25) --claim 裸目录名给提示
        (repo / "pkg").mkdir(exist_ok=True)
        expect("claim 目录提示", call("--claim", "pkg"), 0, "目录声明建议")
        (repo / CLAIMS_DIR / "me.json").unlink(missing_ok=True)

        # 26) --scope-set 覆盖写入清单
        r = call("--scope-set", "s1.txt", "s2.txt")
        content = (repo / SCOPE_FILE).read_text(encoding="utf-8")
        if r.returncode != 0 or "s1.txt" not in content or "s2.txt" not in content:
            failures.append(f"--scope-set 写入失败：exit={r.returncode}\n{r.stdout}{r.stderr}")

        # 27) 不得留可被外部环境变量削弱的自检开关（token 拆分拼接，避免自证）
        token = "SCOPE_GUARD_IN_" + "SELFTEST"
        if token in script.read_text(encoding="utf-8"):
            failures.append(f"源码仍包含可被外部环境变量削弱的 {token}")

        # 27) 缺少 paths 字段：fail-closed
        reset_stage()
        put(SCOPE_FILE, "agent/foo.py\n")
        put("agent/foo.py", "x\n")
        run_git(["add", "--", "agent/foo.py"], repo)
        put(f"{CLAIMS_DIR}/nopaths.json", json.dumps({
            "session": "other",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
        expect("缺少 paths 字段", call("--hook"), 1, "缺少 paths")

        # 28) paths 为 null：fail-closed
        reset_stage()
        put(SCOPE_FILE, "agent/foo.py\n")
        put("agent/foo.py", "x\n")
        run_git(["add", "--", "agent/foo.py"], repo)
        put(f"{CLAIMS_DIR}/nullpaths.json", json.dumps({
            "session": "other",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "paths": None,
        }))
        expect("paths 为 null", call("--hook"), 1, "为 null")

        # 29) paths: [] 是合法空声明，不得误伤
        reset_stage()
        put(SCOPE_FILE, "agent/foo.py\n")
        put("agent/foo.py", "x\n")
        run_git(["add", "--", "agent/foo.py"], repo)
        put(f"{CLAIMS_DIR}/emptypaths.json", json.dumps({
            "session": "other",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "paths": [],
        }))
        expect("paths 空数组合法", call("--hook"), 0, "通过")

    if failures:
        print("SELF-TEST FAILED")
        for f in failures:
            print("\n" + f)
        return 1
    print(
        "SELF-TEST PASSED：作用域外/占用冲突/过期声明/行尾空白/CRLF/删除项/正常删除/"
        "缺少作用域/空清单/声明写法/目录不误伤/解析失败/未来时间戳/default 告警/"
        "非 git 目录/绝对路径/install 往返/CI 拦截 .tmp/CI base 改动集/"
        "paths 类型/updated_at 布尔/claim default 告警/注释清单启用告警/BOM 清单/"
        "CI 删除项精确匹配/claim 目录提示/无环境变量自检开关/"
        "缺少 paths 字段/paths 为 null/paths 空数组合法/关闭开关放行/"
        "空清单真实提交被拒/--scope-set 写清单，共 33 组"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="提交前作用域闸门")
    ap.add_argument("--hook", action="store_true", help="git pre-commit hook 模式")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--enable", action="store_true")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--claim", nargs="+", metavar="PATH")
    ap.add_argument("--scope-set", nargs="+", metavar="PATH", dest="scope_set")
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--ci", action="store_true")
    ap.add_argument("--base", default=None, help="配合 --ci：校验 base...HEAD 改动集")
    ap.add_argument("--session", default=None)
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    try:
        root = repo_root(Path.cwd())
    except Violation as exc:
        if args.hook:
            print(f"[scope-guard] {exc}，拒绝继续（exit 2）。", file=sys.stderr)
            return 2
        print(f"[scope-guard] {exc}", file=sys.stderr)
        return 2

    if args.ci:
        try:
            return ci_check(root, args.base)
        except Violation as exc:
            print(f"[scope-guard] CI 配置错误：{exc}", file=sys.stderr)
            return 2

    session = resolve_session(args.session, root)

    try:
        if args.install:
            return install_hook(root)
        if args.uninstall:
            return uninstall_hook(root)
        if args.enable:
            return set_enabled(root, True)
        if args.disable:
            return set_enabled(root, False)
        if args.status:
            return status(root)
        if args.claim:
            return write_claim(root, session, args.claim)
        if args.scope_set:
            return set_scope(root, args.scope_set)
        if args.release:
            return release_claim(root, session)
    except Violation as exc:
        print(f"[scope-guard] 操作失败：{exc}", file=sys.stderr)
        return 2

    # 默认/hook 模式
    if args.hook:
        warn_default_session(session)
        if not (root / ENABLE_FILE).exists():
            print(
                f"[scope-guard] 开关未启用（缺 {ENABLE_FILE}），放行。"
                "启用：python tools/ci/pre_commit_scope.py --enable"
            )
            return 0
    try:
        return check_scope(root, session)
    except Violation as exc:
        print("\n[scope-guard] 提交被拒绝：\n" + str(exc) + "\n", file=sys.stderr)
        print(
            "[scope-guard] 处理方式：修订 .tmp/commit_scope.txt（只列本次文件），"
            "或 `git restore --staged -- <路径>` 移除不属于本次的暂存项。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
