> 权威级别：过程记录

# 资源统计 MVP

实现位置：`agent/resource_stats.py`（已注册于 `agent/main.py`）。
验证：`python tools/test_resource_stats.py`（26 项单元测试）+ `python tools/verify_resource_stats.py`（真实截图端到端）。

## 范围

只做四件事，不扩展成仪表盘：

1. **采集**：主界面顶栏三项资源（体力 / 金币 / 粲晶），用**图标锚定 ROI**做 OCR。
2. **收益口径**：任务开始前、结束后各抓一次，差值 = after − before。
3. **存储**：JSON Lines，按账号分文件。
4. **输出**：任务结束日志打一行摘要 + 导出 CSV。

## 核心原则

- **图标锚定**：先 `TemplateMatch` 定位资源图标，再以图标边界 + 固定偏移计算 OCR 区域。
  不同界面资源条位置会漂移，固定整屏坐标不可靠。
- **识别失败不写 0**：OCR 无结果、置信度低于阈值（0.85）、或数字解析失败时，
  该资源记 `null`，差值也为 `null`，绝不把失败当成 0 拉低曲线。
- **差值独立配对**：每次任务 `run_id` 独立配对 before/after，不复用上一任务的余量。

## 坐标基准

**统一使用 1280×720 归一化画布**（与全项目一致，见 `docs/coords.md` §1）。

- `ocr_offset`、TemplateMatch 的 `roi`、OCR 的 `roi` 均为 1280×720 画布坐标。
- MaaFramework 的 `run_recognition` 在 `pipeline_override` 中收到的 `roi` 按 1280×720 解释，
  运行时自动映射到实际截图分辨率，**不调用 `viewport.scale_roi`**（那是 1920 基准的遗留换算）。
- 不混用两套基准：换分辨率时由框架统一映射，避免 1.5 倍偏差。

## 资源定义

`RESOURCES` 字典（1280×720 画布坐标）：

| key | label | template | ocr_offset (dx, dy, w, h) | parser |
| --- | --- | --- | --- | --- |
| `ap` | 体力 | `HomeResourceAp.png` | (35, -4, 120, 33) | `parse_ap` |
| `coin` | 金币 | `HomeResourceCoin.png` | (37, -4, 133, 33) | `parse_int` |
| `gem` | 粲晶 | `HomeResourceGem.png` | (35, -4, 107, 33) | `parse_int` |

- `ocr_offset` 是相对于图标左上角的 OCR 区域偏移（1280×720 画布）。
- `parse_ap` 解析 `current/max`（兼容全角斜杠 `／`），`parse_int` 解析纯数字（去逗号/空格）。

## 动作

| 动作 | 用途 | 行为 |
| --- | --- | --- |
| `resource_stats` (phase=before) | 任务链首 | 抓截图读数，写 before 快照，生成 `run_id` 存入会话 |
| `resource_stats` (phase=after) | 任务链尾 | 抓截图读数，按同任务名匹配最近 before，算差值写 after，打摘要 |

custom_action_param：

```json
{"task": "DailyRewards", "phase": "before"}
```

- `account` 可显式传入；缺省取 `MAA_INSTANCE_CONFIG` 文件名或 `MFA_INSTANCE_ID`，再缺省 `default`。
- after 若找不到可配对的 before，返回 False，不写假记录。

## 数据存储

- 目录：`config/resource_stats/<account>.jsonl`（可用环境变量 `RESOURCE_STATS_DIR` 覆盖）。
- 该目录在 `.gitignore` 中（`config` 已忽略），不入库。
- 每行一条记录，`phase` 为 `before` 或 `after`。

### before 记录

```json
{
  "schema": 1,
  "ts": "2026-11-01T23:18:11",
  "account": "JCZX",
  "task": "DailyRewards",
  "run_id": "20261101231811123",
  "phase": "before",
  "resources": {
    "ap": {"resource": "ap", "label": "体力", "value": {"current": 201, "max": 240}, "raw": "201/240", "confidence": 0.998, "success": true, "icon_box": [468, 28, 52, 52], "ocr_box": [542, 34, 77, 18]},
    "coin": {"resource": "coin", "label": "金币", "value": 1091740, "raw": "1091740", "confidence": 0.999, "success": true, "icon_box": [653, 28, 52, 52], "ocr_box": [728, 34, 78, 18]},
    "gem": {"resource": "gem", "label": "粲晶", "value": 5816, "raw": "5816", "confidence": 0.999, "success": true, "icon_box": [853, 28, 52, 52], "ocr_box": [924, 34, 49, 19]}
  }
}
```

### after 记录（多 delta 与 before_ts）

```json
{
  "schema": 1,
  "ts": "2026-11-01T23:18:12",
  "account": "JCZX",
  "task": "DailyRewards",
  "run_id": "20261101231811123",
  "phase": "after",
  "resources": { "...": "同 before 结构" },
  "delta": {"ap": -1, "coin": 0, "gem": 0},
  "before_ts": "2026-11-01T23:18:11"
}
```

- 任一资源 before 或 after 失败，其 `delta` 为 `null`。
- 体力 `delta` 取 `current` 的差值。

## 日志摘要

after 阶段写入后打一行：

```
[资源统计] 本次 DailyRewards：体力 201→200，金币 +1240，粲晶 +3
```

某项识别失败时显示 `体力 识别失败`。

## CSV 导出

```bash
python tools/export_resource_csv.py [account] [--date YYYY-MM-DD|all] [--out path.csv]
```

- `account` 缺省为 `default`（或 `MFA_INSTANCE_ID`）。
- `--date` 缺省今天，传 `all` 导出全部。
- 输出缺省 `config/resource_stats/<account>_<date>.csv`，UTF-8 BOM（Excel 可直接打开）。

字段：`ts, account, task, run_id, phase, ap_before, ap_after, ap_delta, coin_before, coin_after, coin_delta, gem_before, gem_after, gem_delta`。

识别失败的资源在 before/after/delta 列留空（不写 0）。

## 注册到 AgentServer

已在 `agent/main.py` 注册（`9368bb6` 之后追加，未改动任务记忆注册行）：

```python
from resource_stats import ResourceStatsAction
AgentServer.custom_action("resource_stats")(ResourceStatsAction)
```

## 如何在 pipeline 里挂节点

在需要统计的任务**首尾各插一个** `resource_stats` custom action 节点。

任务首节点（进入任务前）：

```json
{
  "your_task_before": {
    "action": "CustomAction",
    "custom_action": "resource_stats",
    "custom_action_param": "{\"task\": \"DailyRewards\", \"phase\": \"before\"}",
    "next": ["your_task_main"]
  }
}
```

任务尾节点（任务完成后）：

```json
{
  "your_task_after": {
    "action": "CustomAction",
    "custom_action": "resource_stats",
    "custom_action_param": "{\"task\": \"DailyRewards\", \"phase\": \"after\"}",
    "next": []
  }
}
```

param 字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `task` | 是 | 任务名，before/after 必须一致才能配对；建议与 `task_memory` 同名 |
| `phase` | 是 | `before` 或 `after` |
| `account` | 否 | 账号标识；缺省取 `MAA_INSTANCE_CONFIG` 文件名或 `MFA_INSTANCE_ID`，再缺省 `default` |

注意：
- `custom_action_param` 经 pipeline 下发时会被双重 JSON 编码，动作内已兼容解析。
- after 阶段按同 `task` 名匹配最近一条 before；若找不到可配对的 before，返回 False，不写假记录。
- 建议接入的任务：`StartUp`、`DailyRewards`、`FreeGift`、每日探索、周本等。

## 数据清理方式

数据位于 `config/resource_stats/`（已被 `.gitignore` 忽略，不入库）。

- **删除某账号全部数据**：删除 `config/resource_stats/<account>.jsonl`。
- **删除某账号指定日期**：编辑 `<account>.jsonl`，删除对应 `ts` 开头的行（JSONL 每行独立，可直接删行）。
- **清空全部数据**：删除整个 `config/resource_stats/` 目录（下次运行自动重建）。
- **CSV 导出文件**：`config/resource_stats/<account>_<date>.csv` 可直接删除，不影响原始 JSONL。
- 数据损坏时：JSONL 按行解析，单行损坏不影响其他行；导出脚本会跳过解析失败的行。

## 前置依赖（迁移会话负责）

以下资源由迁移会话添加，本模块不改动 `assets/`：

1. 三个图标模板，放 `assets/resource/image/`：
   - `HomeResourceAp.png`（体力图标）
   - `HomeResourceCoin.png`（金币图标）
   - `HomeResourceGem.png`（粲晶图标）
2. 在需要统计的任务首尾各插一个 `resource_stats` 节点：
   - 任务首节点：`{"task": "<任务名>", "phase": "before"}`
   - 任务尾节点：`{"task": "<任务名>", "phase": "after"}`

## 挂接顺序（2026-11-02 迁移会话定稿，固定约定）

与 `task_memory` 同链接入时，节点顺序固定为：`门禁(gate) → 资源前(before) → 任务链 → 记忆完成(complete) → 回主界面 → 资源后(after)`。

- **before 在 gate 之后**：门禁拦截时不产生孤儿 before（after 按同任务名配最近 before，孤儿会污染下一次配对）。
- **complete 在回主界面之前**：记忆完成在任务业务终点（如《通行证_结束》《礼包_退出》《首页标志1》）即写入，避免被后续回主界面导航的偶发失败污染。
- **after 必须在主界面**：资源统计 OCR 顶栏，任务链尾（如《通行证_结束》）往往仍在深层页读不到顶栏，必须在「回到主界面并确认」之后触发 after。实机 2026-11-02 证据：挂在《通行证_结束》后三项全识别失败，移到回主界面后三项全 success。
- **共享出口注意**：`启动游戏` 的成功出口《首页标志1》同时被各任务的回主界面导航经过。导航流触发的 `启动_资源后` 因会话中无配对 before 会返回 False（不写 JSONL），节点 `on_error=[启动_记忆完成]` 兜底，导航任务不受影响。
- 已接入任务与挂点：
  - `启动游戏`：`启动_资源前`（entry，主界面就在）/ `启动_资源后` 挂《首页标志1》后（启动成功出口=主界面）
  - `每日免费礼包`：`礼包_资源前` 挂门禁后 / `礼包_资源后` 挂《礼包_退出》后
  - `领取奖励`：`领奖_资源前` 挂门禁后 / `领奖_资源后` 挂《领奖_回主界面 → 领奖_确认主界面读资源》后（任务尾在通行证深层页，需先回主界面）
