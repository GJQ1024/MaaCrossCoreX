from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from maa.context import Context
from maa.custom_action import CustomAction

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "account_switch.py"
TIMEOUT = 3600


class AccountSwitchAction(CustomAction):
    """把面板选项（切换数量 / 勾选账号）翻译成 scripts/account_switch.py 的参数。

    默认不切换：count=0 且未勾选任何账号时直接成功返回，不触碰设备。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            param = json.loads(getattr(argv, "custom_action_param", None) or "{}")
        except (TypeError, json.JSONDecodeError):
            print("[account_switch] 参数不是合法 JSON，已中止（不切换）", flush=True)
            return False

        picks = [i for i in range(1, 9) if bool(param.get(f"pick_{i}"))]
        try:
            count = int(param.get("count") or 0)
        except (TypeError, ValueError):
            count = 0

        if not picks and count <= 0:
            print("[account_switch] 不切换（count=0 且未勾选账号）", flush=True)
            return True

        cmd = [sys.executable, str(SCRIPT)]
        if picks:
            cmd += ["--indices", ",".join(map(str, picks))]
        else:
            cmd += ["--count", str(count)]

        print("[account_switch] 执行：" + " ".join(cmd), flush=True)
        try:
            proc = subprocess.run(
                cmd, cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(f"[account_switch] 超时（{TIMEOUT}s），已中止", flush=True)
            return False

        for line in (proc.stdout or "").splitlines():
            print("[account_switch] " + line, flush=True)
        if proc.returncode != 0:
            print(f"[account_switch] 失败，退出码 {proc.returncode}", flush=True)
            if proc.stderr:
                print("[account_switch] stderr: " + proc.stderr[-2000:], flush=True)
            return False
        return True
