"""tutorial_once 账号维度（P-014）离线回归：不连设备、不跑教程。

覆盖：
  * 新账号 -> 放行；已计数的同账号 -> 拦截；
  * 模拟换号（UID 变化）-> 判新号放行；
  * 拿不到 UID -> 跳过并记事件（fail-safe），mark 拒绝写；
  * 旧键未迁移：多账号记录 -> 跳过；单账号记录 -> 按旧计数；
  * 迁移脚本 dry-run / apply / 幂等 / 歧义退出码。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

import migrate_tutorial_state  # noqa: E402
import my_action  # noqa: E402
import tutorial_state  # noqa: E402

KEY = "tutorial_build10"
NODE = "引导_补给站_一次性闸"
INSTANCE = "inst"


class FakeContext:
    def __init__(self):
        self.overrides = []

    def override_next(self, node_name, targets):
        self.overrides.append((node_name, list(targets)))


def make_argv(param: dict):
    return SimpleNamespace(node_name=NODE, custom_action_param=json.dumps(param))


def make_param(operation: str) -> dict:
    return {
        "operation": operation,
        "key": KEY,
        "limit": 1,
        "on_done": ["教程_跳过_补给站"],
        "reason": "test",
    }


class Case:
    """一个临时目录 + 三个状态文件，按实例环境变量跑。"""

    def __init__(self, tmp: Path):
        self.state = tmp / "tutorial_state.json"
        self.cache = tmp / "account_current.json"
        self.acct = tmp / "account_switch_state.json"
        self.event = tmp / "events.jsonl"

    def write_state(self, data: dict):
        self.state.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def set_uid(self, uid):
        tutorial_state.write_account_cache(uid, 1, path=self.cache)

    def set_history(self, *uids):
        self.acct.write_text(json.dumps(
            {str(i + 1): {"uid": u} for i, u in enumerate(uids)}), encoding="utf-8")

    def run(self, operation: str):
        ctx = FakeContext()
        with patch.object(my_action, "TUTORIAL_STATE_PATH", self.state), \
                patch.object(my_action, "ACCOUNT_CACHE_PATH", self.cache), \
                patch.object(my_action, "ACCOUNT_STATE_PATH", self.acct), \
                patch.object(my_action, "TUTORIAL_EVENT_PATH", self.event):
            ok = my_action.TutorialOnceAction().run(ctx, make_argv(make_param(operation)))
        return ok, ctx

    def events(self) -> list:
        if not self.event.exists():
            return []
        return [json.loads(line) for line in
                self.event.read_text(encoding="utf-8").splitlines() if line.strip()]


def env(instance: str = INSTANCE):
    return patch.dict(os.environ, {"MFA_INSTANCE_ID": instance}, clear=False)


def test_new_account_allowed():
    with tempfile.TemporaryDirectory() as tmp, env():
        case = Case(Path(tmp))
        case.set_uid("1001")
        ok, ctx = case.run("check")
        assert ok is True and ctx.overrides == [], ctx.overrides
        assert not case.state.exists()


def test_mark_then_same_account_blocked():
    with tempfile.TemporaryDirectory() as tmp, env():
        case = Case(Path(tmp))
        case.set_uid("1001")
        ok, _ = case.run("mark")
        assert ok is True
        assert case.read_state()[KEY] == {f"{INSTANCE}:1001": 1}
        ok, ctx = case.run("check")
        assert ok is True
        assert ctx.overrides == [(NODE, ["教程_跳过_补给站"])]


def test_switch_account_is_new():
    with tempfile.TemporaryDirectory() as tmp, env():
        case = Case(Path(tmp))
        case.set_uid("1001")
        case.run("mark")
        case.set_uid("2002")  # 模拟切号：缓存改为新 UID
        ok, ctx = case.run("check")
        assert ok is True and ctx.overrides == [], "新账号不应继承上一个号的计数"


def test_unknown_uid_skips_and_records():
    with tempfile.TemporaryDirectory() as tmp, env():
        case = Case(Path(tmp))
        ok, ctx = case.run("check")
        assert ok is True
        assert ctx.overrides == [(NODE, ["教程_跳过_补给站"])]
        assert [e["event"] for e in case.events()] == ["skip_unknown"]
        assert not case.state.exists(), "未知账号不得落盘任何计数"


def test_unknown_uid_mark_refused():
    with tempfile.TemporaryDirectory() as tmp, env():
        case = Case(Path(tmp))
        ok, _ = case.run("mark")
        assert ok is False
        assert case.events()[-1]["event"] == "mark_unknown"
        assert not case.state.exists()


def test_legacy_ambiguous_skips():
    with tempfile.TemporaryDirectory() as tmp, env():
        case = Case(Path(tmp))
        case.write_state({KEY: {INSTANCE: 1}})
        case.set_history("1001", "2002")
        ok, ctx = case.run("check")
        assert ok is True
        assert ctx.overrides == [(NODE, ["教程_跳过_补给站"])]
        assert case.events()[-1]["event"] == "skip_legacy"


def test_legacy_single_account_still_works():
    with tempfile.TemporaryDirectory() as tmp, env():
        case = Case(Path(tmp))
        case.write_state({KEY: {INSTANCE: 1}})
        case.set_history("1001")
        ok, ctx = case.run("check")
        assert ok is True
        assert ctx.overrides == [(NODE, ["教程_跳过_补给站"])], "旧计数应作为已完成的基线"
        assert not any(e["event"].startswith("skip_") for e in case.events())


def test_migration_dry_run_apply_idempotent():
    with tempfile.TemporaryDirectory() as tmp, env():
        case = Case(Path(tmp))
        case.write_state({KEY: {INSTANCE: 1}})
        case.set_history("1001")
        base = ["migrate", "--state", str(case.state), "--cache", str(case.cache),
                "--account-state", str(case.acct)]

        with patch.object(sys, "argv", base):
            assert migrate_tutorial_state.main() == 0
        assert case.read_state()[KEY] == {INSTANCE: 1}, "dry-run 不得写入"

        with patch.object(sys, "argv", base + ["--apply"]):
            assert migrate_tutorial_state.main() == 0
        assert case.read_state()[KEY] == {INSTANCE: 1, f"{INSTANCE}:1001": 1}, "旧键必须保留"

        with patch.object(sys, "argv", base + ["--apply"]):
            assert migrate_tutorial_state.main() == 0
        assert case.read_state()[KEY] == {INSTANCE: 1, f"{INSTANCE}:1001": 1}, "重复跑不得重复/覆盖"


def test_migration_ambiguous_exits_2_without_write():
    with tempfile.TemporaryDirectory() as tmp, env():
        case = Case(Path(tmp))
        case.write_state({KEY: {INSTANCE: 1}})
        case.set_history("1001", "2002")
        before = case.state.read_text(encoding="utf-8")
        with patch.object(sys, "argv", [
            "migrate", "--state", str(case.state), "--cache", str(case.cache),
            "--account-state", str(case.acct), "--apply",
        ]):
            assert migrate_tutorial_state.main() == 2
        assert case.state.read_text(encoding="utf-8") == before


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"TUTORIAL_ACCOUNT_OK ({len(tests)} tests)")

