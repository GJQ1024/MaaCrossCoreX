"""打包脚本：支持两种分发形态。

模式 A（默认）：只发我们自己的资源/脚本/Agent，用户自备 MFAAvalonia 壳。
  - 产物：resource/ + scripts/ + agent/ + interface.json + python/ + deps/
  - 不含 MFAAvalonia 壳，也不含 MaaFramework 原生库（runntimes/），由用户的壳提供。
  - 许可证：沿用仓库 LICENSE（MIT），零 GPL 义务。

模式 B（--with-shell）：连壳一起打包，整包按 GPL-3.0。
  - 在模式 A 基础上追加 MFAAvalonia 壳、MaaFramework 原生库（runntimes/）、
    libs/MaaAgentBinary、plugins、appsettings.json。
  - 必须附 GPL-3.0 全文 LICENSE + NOTICE（壳来源/版本 + 源码地址）。

用法：
  python install.py <version> <os> <arch> [--with-shell] [--output-dir DIR] [--mfa-dir DIR]

示例：
  python install.py v0.5.11 win x86_64                     # 模式 A -> dist/A
  python install.py v0.5.11 win x86_64 --with-shell        # 模式 B -> dist/B
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import jsonc
except ModuleNotFoundError as e:
    raise ImportError(
        "Missing dependency 'json-with-comments' (imported as 'jsonc').\n"
        f"Install it with:\n  {sys.executable} -m pip install json-with-comments\n"
        "Or add it to your project's requirements."
    ) from e

from configure import configure_ocr_model


working_dir = Path(__file__).parent.parent.resolve()

# MFAAvalonia 壳的本地来源：优先 --mfa-dir，其次 MFA_DIR 环境变量，最后评估副本。
DEFAULT_MFA_DIRS = (
    working_dir.parent / "gui-eval" / "MFA",
    working_dir / "MFA",
)

# 模式 B 整包采用 GPL-3.0；壳来源与版本与 CI 一致（.github/workflows/install.yml）。
MFA_SOURCE_REPO = "https://github.com/MAACrossCore/MCCAvalonia"
MFA_VERSION = "v0.1.3"
# 我们自己的源码仓库（与壳来源不同，不要混用）。
OUR_SOURCE_REPO = "https://github.com/GJQ1024/MaaCrossCoreX"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build MCC_Framework release package.")
    parser.add_argument("version", help="版本号，例如 v0.5.11（建议与 git tag 一致）")
    parser.add_argument("os", choices=["win", "macos", "linux", "android"])
    parser.add_argument("arch", choices=["x86_64", "aarch64"])
    parser.add_argument(
        "--with-shell",
        action="store_true",
        help="模式 B：连 MFAAvalonia 壳一起打包，整包按 GPL-3.0（默认模式 A 不含壳）",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="产物输出目录（默认 dist/A 或 dist/B）",
    )
    parser.add_argument(
        "--mfa-dir",
        default=None,
        help="MFAAvalonia 壳的本地目录（模式 B 用，默认从 gui-eval/MFA 取）",
    )
    return parser.parse_args()


args = parse_args()
version = args.version
os_name = args.os
arch = args.arch
with_shell = args.with_shell

if args.output_dir:
    install_path = Path(args.output_dir).resolve()
else:
    install_path = working_dir / "dist" / ("B" if with_shell else "A")


def get_dotnet_platform_tag() -> str:
    """自动检测当前平台并返回对应的dotnet平台标签"""
    if os_name == "win" and arch == "x86_64":
        return "win-x64"
    if os_name == "win" and arch == "aarch64":
        return "win-arm64"
    if os_name == "macos" and arch == "x86_64":
        return "osx-x64"
    if os_name == "macos" and arch == "aarch64":
        return "osx-arm64"
    if os_name == "linux" and arch == "x86_64":
        return "linux-x64"
    if os_name == "linux" and arch == "aarch64":
        return "linux-arm64"
    print("Unsupported OS or architecture.")
    print("os: [win, macos, linux, android]")
    print("arch: [x86_64, aarch64]")
    sys.exit(1)


def install_deps() -> None:
    """复制 MaaFramework 原生库到 runtimes/。

    模式 A 不调用：用户自备的 MFAAvalonia 壳已带 runtimes/。
    模式 B 调用：整包自包含。
    """
    if not (working_dir / "deps" / "bin").exists():
        print('Please download the MaaFramework to "deps" first.')
        print('请先下载 MaaFramework 到 "deps"。')
        sys.exit(1)

    if os_name == "android":
        shutil.copytree(
            working_dir / "deps" / "bin",
            install_path,
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "share" / "MaaAgentBinary",
            install_path / "MaaAgentBinary",
            dirs_exist_ok=True,
        )
    else:
        shutil.copytree(
            working_dir / "deps" / "bin",
            install_path / "runtimes" / get_dotnet_platform_tag() / "native",
            ignore=shutil.ignore_patterns(
                "*MaaDbgControlUnit*",
                "*MaaThriftControlUnit*",
                "*MaaRpc*",
                "*MaaHttp*",
                "plugins",
                "*.node",
                "*MaaPiCli*",
            ),
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "share" / "MaaAgentBinary",
            install_path / "libs" / "MaaAgentBinary",
            dirs_exist_ok=True,
        )
        shutil.copytree(
            working_dir / "deps" / "bin" / "plugins",
            install_path / "plugins" / get_dotnet_platform_tag(),
            dirs_exist_ok=True,
        )


def configure_interface_agent(interface: dict) -> None:
    if os_name == "win":
        python_exec = r"./python/python.exe"
    elif os_name == "macos":
        python_exec = r"./python/bin/python3"
    elif os_name == "linux":
        python_exec = "python3"
    else:
        return

    # pretask 的 CWD 是资源目录（install/resource/base），不是安装根目录；
    # 而 agent.child_exec 相对的是安装根目录。两者前缀不同，不能共用一个常量。
    pretask_exec = "../../" + python_exec[2:] if python_exec.startswith("./") else python_exec

    agent = interface.setdefault("agent", {})
    agent["child_exec"] = python_exec
    agent["child_args"] = ["-u", "./agent/main.py"]

    # pretask 允许是单条对象或数组；统一按数组处理，且保留每条的 args。
    # 不能像以前那样整体覆盖成单条 ensure_mumu，否则横屏预检（scripts/）会被悄悄丢掉。
    pretask = interface.get("pretask")
    if not pretask:
        return
    pretasks = pretask if isinstance(pretask, list) else [pretask]
    pretasks = [dict(item) for item in pretasks if isinstance(item, dict)]
    if not pretasks:
        return

    for item in pretasks:
        item["exec"] = pretask_exec
        item_args = item.get("args") or []
        if isinstance(item_args, str):
            item_args = [item_args]
        item["args"] = [_package_relative(arg) for arg in item_args]

    # 保留原有的「自动启动 MuMu」：缺失时补在最前面，保证启动游戏前先有 ADB 连接。
    has_ensure_mumu = any(
        "ensure_mumu.py" in arg
        for item in pretasks
        for arg in item.get("args", [])
        if isinstance(arg, str)
    )
    if not has_ensure_mumu:
        pretasks.insert(
            0,
            {
                "name": "自动启动 MuMu 12",
                "label": "仅在选择启动游戏时运行",
                "description": "自动查找 MuMu 12、启动实例并配置 ADB（不打开游戏）。",
                "exec": pretask_exec,
                "args": ["../../agent/ensure_mumu.py"],
            },
        )

    interface["pretask"] = pretasks if len(pretasks) > 1 else pretasks[0]


def _package_relative(arg: str) -> str:
    """把开发期的绝对路径映射成发布包内的相对路径。

    pretask 的 CWD 是资源目录（install/resource/base），脚本若仍指向开发目录
    （如 .../MCC_Framework/scripts/pretask_landscape.py），换台机器就静默失效。
    只处理 scripts/ 与 agent/ 两类包内目录，其余参数原样保留。
    """
    if not isinstance(arg, str) or not arg:
        return arg
    norm = arg.replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    for folder in ("scripts", "agent"):
        marker = f"/{folder}/"
        index = norm.rfind(marker)
        if index >= 0:
            return "../../" + norm[index + 1 :]
        if norm.startswith(f"{folder}/"):
            return "../../" + norm
    return arg


def install_python_runtime() -> None:
    if os_name not in ("win", "macos"):
        return

    embedded_src = (working_dir / "install" / "python").resolve()
    embedded_dst = (install_path / "python").resolve()
    if not embedded_src.is_dir():
        print("Warning: embedded Python not found; release will still require system Python.")
        return

    if embedded_src == embedded_dst:
        print(f"Embedded Python already in place: {embedded_dst}")
        return

    shutil.copytree(embedded_src, embedded_dst, dirs_exist_ok=True)


def install_agent_dependency_wheels() -> None:
    if os_name == "android":
        return

    deps_src = (working_dir / "install" / "deps").resolve()
    deps_dst = (install_path / "deps").resolve()
    if not deps_src.is_dir() or not any(deps_src.glob("*.whl")):
        print("Warning: install/deps wheel cache not found; first Agent run may download online.")
        return

    if deps_src == deps_dst:
        print(f"Agent dependency wheels already in place: {deps_dst}")
        return

    shutil.copytree(deps_src, deps_dst, dirs_exist_ok=True)


def install_resource() -> None:
    configure_ocr_model()

    shutil.copytree(
        working_dir / "assets" / "resource",
        install_path / "resource",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        working_dir / "assets" / "default",
        install_path / "default",
        dirs_exist_ok=True,
    )
    shutil.copy2(working_dir / "assets" / "interface.json", install_path)

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = jsonc.load(f)

    interface["version"] = version
    configure_interface_agent(interface)

    with open(install_path / "interface.json", "w", encoding="utf-8") as f:
        jsonc.dump(interface, f, ensure_ascii=False, indent=4)


def install_chores() -> None:
    """复制 README 与许可证。

    模式 A：沿用仓库 LICENSE（MIT），用户自备壳，零 GPL 义务。
    模式 B：整包按 GPL-3.0，写 GPL-3.0 全文 + NOTICE。
    """
    readme = working_dir / "README.md"
    if readme.is_file():
        shutil.copy2(readme, install_path / "README.md")

    if with_shell:
        _write_gpl_license()
        _write_notice()
    else:
        repo_license = working_dir / "LICENSE"
        if repo_license.is_file():
            shutil.copy2(repo_license, install_path / "LICENSE")

    appsettings_path = install_path / "appsettings.json"
    if appsettings_path.exists():
        with open(appsettings_path, "r", encoding="utf-8-sig") as f:
            appsettings = jsonc.load(f)
        # NoAutoStart="True" 只对“下一次启动”生效：壳会读它来跳过自动开跑，
        # 然后在启动过程中写回 "False"（实测 2026-11-03）。它管的是“本次启动
        # 不自动执行任务”，与模拟器自动启动无关；要每次都只开界面，入口脚本
        # 必须在每次启动前重新写回 True（见 launcher/launch-automation.ps1）。
        appsettings["NoAutoStart"] = "True"
        with open(appsettings_path, "w", encoding="utf-8") as f:
            jsonc.dump(appsettings, f, ensure_ascii=False, indent=2)


def normalize_instance_connection() -> None:
    """打包时统一实例连接口径：端口不写死，由启动器按扫包结果填充。

    MuMu 的 adb 端口每次启动都可能重新分配（历史上 16416→16417），把端口
    写进包只会连到过期端口，甚至连到别的实例（16384=AzurLane）。所以这里
    AdbSerial 一律留空，真正的值由 launcher/launch-automation.ps1 在启动前
    扫描“唯一装了交错战线”的实例后写入。

    extras.mumu.index 是实例编号而不是端口，固定为 1（JCZX）：默认 0 会指向
    AzurLane（另一项目的 is_main 实例），切到 MuMu 控制器时会连错。
    """
    path = install_path / "config" / "instances" / "default.json"
    if not path.is_file():
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        instance = jsonc.load(f)
    device = instance.get("AdbDevice")
    if not isinstance(device, dict):
        return
    device["AdbSerial"] = ""
    try:
        config = json.loads(str(device.get("Config") or "{}"))
    except json.JSONDecodeError:
        config = {}
    mumu = config.setdefault("extras", {}).setdefault("mumu", {})
    mumu["index"] = 1
    device["Config"] = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as f:
        jsonc.dump(instance, f, ensure_ascii=False, indent=2)


def install_agent() -> None:
    shutil.copytree(
        working_dir / "agent",
        install_path / "agent",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    # Windows: one-click pip install for end users (Agent / MuMu pretask)
    bat = working_dir / "Install-Agent-Deps.bat"
    if bat.is_file() and os_name == "win":
        shutil.copy2(bat, install_path / "Install-Agent-Deps.bat")

    config_dir = install_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    example = working_dir / "agent" / "orders_source.example.json"
    if example.is_file():
        shutil.copy2(example, config_dir / "orders_source.example.json")


def install_scripts() -> None:
    """把 scripts/ 打进产物：pretask（横屏预检）脚本在这里。

    脚本用 __file__ 推导项目根目录，因此不依赖开发目录或调用者的 cwd。
    """
    scripts_src = working_dir / "scripts"
    if not scripts_src.is_dir():
        print("Warning: scripts directory not found; pretask will fail in release.")
        return

    shutil.copytree(
        scripts_src,
        install_path / "scripts",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def _resolve_mfa_dir() -> Path | None:
    """定位 MFAAvalonia 壳的本地目录。"""
    if args.mfa_dir:
        candidate = Path(args.mfa_dir).resolve()
        if candidate.is_dir():
            return candidate
        print(f"Warning: --mfa-dir {candidate} not found; falling back.")
    env_dir = os.environ.get("MFA_DIR")
    if env_dir:
        candidate = Path(env_dir).resolve()
        if candidate.is_dir():
            return candidate
    for candidate in DEFAULT_MFA_DIRS:
        if candidate.is_dir():
            return candidate
    return None


def install_shell() -> None:
    """模式 B：把 MFAAvalonia 壳复制进产物。

    复用 CI 的做法（.github/workflows/install.yml）：从 Release 下载的 MFA 目录
    直接 rsync 进 install/。这里用本地已有的壳副本（gui-eval/MFA 或 --mfa-dir）。
    """
    mfa_dir = _resolve_mfa_dir()
    if mfa_dir is None:
        print(
            "Error: MFAAvalonia shell not found. "
            "Pass --mfa-dir <path> or set MFA_DIR, or place the shell under gui-eval/MFA."
        )
        sys.exit(1)

    print(f"Copying MFAAvalonia shell from {mfa_dir}")
    shutil.copytree(
        mfa_dir,
        install_path,
        dirs_exist_ok=True,
        # 壳自带的 runtimes 会被我们的 MaaFramework 原生库覆盖（install_deps 后执行），
        # 这里先不删，install_deps 会以 dirs_exist_ok 合并。
        ignore=shutil.ignore_patterns("logs", "temp", "debug", "backup", "__pycache__"),
    )


def _gpl3_text() -> str:
    """GPL-3.0 全文。优先读本地缓存，否则从 gnu.org 拉取并缓存。"""
    cache = working_dir / "tools" / ".gpl-3.0.txt"
    if cache.is_file():
        return cache.read_text(encoding="utf-8")
    try:
        import urllib.request

        with urllib.request.urlopen("https://www.gnu.org/licenses/gpl-3.0.txt", timeout=20) as resp:
            text = resp.read().decode("utf-8")
        cache.write_text(text, encoding="utf-8")
        return text
    except Exception as exc:
        print(f"Warning: failed to fetch GPL-3.0 text ({exc}); writing placeholder.")
        return (
            "GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007\n"
            "Full text: https://www.gnu.org/licenses/gpl-3.0.txt\n"
            "(本文件在打包机离线时无法写入全文，请在发版前用上述链接替换。)\n"
        )


def _write_gpl_license() -> None:
    (install_path / "LICENSE").write_text(_gpl3_text(), encoding="utf-8")


def _write_notice() -> None:
    notice = (
        "NOTICE\n"
        "======\n\n"
        f"This distribution bundles MFAAvalonia (the MaaFramework UI shell), "
        f"version {MFA_VERSION}.\n"
        f"Shell source: {MFA_SOURCE_REPO}\n\n"
        f"This project's own source: {OUR_SOURCE_REPO}\n\n"
        "The bundled shell is licensed under the GNU General Public License v3.0\n"
        "(see LICENSE). Accordingly, this entire distribution is distributed under\n"
        "the terms of the GNU General Public License v3.0.\n\n"
        "MaaFramework (the native runtime under runtimes/) is licensed under the\n"
        "GNU Lesser General Public License v3.0; its source is available at\n"
        "https://github.com/MaaXYZ/MaaFramework .\n\n"
        "Third-party components (Python, numpy, etc.) retain their own licenses;\n"
        "see their respective metadata under python/Lib/site-packages/*/licenses/.\n"
    )
    (install_path / "NOTICE").write_text(notice, encoding="utf-8")


def clean_output() -> None:
    """清空输出目录，避免上一次构建的残留混进新产物。"""
    if install_path.exists():
        shutil.rmtree(install_path)
    install_path.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    clean_output()

    # 模式 A 不复制 MaaFramework 原生库（用户壳自带）；模式 B 需要自包含。
    if with_shell:
        install_shell()
        install_deps()

    install_resource()
    install_chores()
    normalize_instance_connection()
    install_python_runtime()
    install_agent_dependency_wheels()
    install_agent()
    install_scripts()

    mode = "B (with shell, GPL-3.0)" if with_shell else "A (no shell, MIT)"
    print(f"Install to {install_path} successfully. [mode {mode}]")
