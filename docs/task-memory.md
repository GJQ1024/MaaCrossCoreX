> 权威级别：权威结论

# 任务记忆 + 冷却调度

实现位置：`agent/task_memory.py`（注册于 `agent/main.py`）。
验证：`python tools/test_task_memory.py`（离线，8 项检查）。

## 概念

每个受管任务记忆三个核心字段：**上次完成时间、冷却时长、今日已执行次数**。

- **冷却（cooldown_minutes）**：两次完成之间的最小间隔。
- **每日上限（daily_limit）**：每游戏日成功次数上限；0 = 不限。
- 两者**独立判定、全部满足才放行**；跳过原因区分"冷却未到期"与"今日次数已用完"，
  并给出下次可跑时间。
- **游戏日**按刷新时刻切分（默认 05:00，可配置），不是自然日：
  当天 05:00 ~ 次日 04:59 算同一天。周任务用 refresh_weekday（0=周一）。

## 动作

| 动作 | 用途 | 行为 |
| --- | --- | --- |
| `task_memory_gate` | 任务链首门禁 | 返回 False 使节点失败，走 `on_error` 早退 |
| `task_memory_complete` | 任务链尾完成 | 写完成记录；任务中断/失败时该节点不执行，不留假记录 |

custom_action_param（挂节点时携带任务定义默认值）：

```json
{"task": "DailyRewards", "cooldown_minutes": 0, "daily_limit": 1, "refresh_hour": 5}
```

注意：经 pipeline_override 下发的 param 会被双重编码，动作内已兼容解析。

## 配置项怎么改

策略优先级：代码默认值 < pipeline param（任务定义）< **用户策略文件**（最终生效）。

用户策略文件：`config/task_memory/<实例id>.policy.json`（该目录不入库）。
两种写法，任务级优先：

```json
{
  "DailyRewards": {"daily_limit": 1, "refresh_hour": 5},
  "FreeGift": {"daily_limit": 2},
  "refresh_hour": 4
}
```

- 值带任务名的按任务覆盖；平铺字段（如上例 `refresh_hour`）做全局覆盖。
- **强制开关**：param 加 `"force": true` 或环境变量 `TASK_MEMORY_FORCE=1`。
  **强制只无视冷却，每日上限仍然拦截**。突破上限请用"重置今日计数"（见下）。

## 手动重置

- **重置某任务今日计数/记录**：删除 `config/task_memory/<实例id>.json` 里对应
  任务段（或整个文件），下次跑按空记录处理，画面识别会兜底纠正（reconcile）。
- **状态文件损坏**：自动备份为 `<文件>.corrupt` 后按空记录重建，不会卡死任务。

## 异常与时钟回拨

| 情况 | 策略 |
| --- | --- |
| 文件缺失 | 按空记录处理（正常放行） |
| 文件损坏 | 备份 `.corrupt` 后重建 |
| 时钟回拨 | 完成时间比当前时间晚超 5 分钟 → 判定回拨，**不按该"未来记录"拦截**（冷却视为到期），记 warning 日志，计数交画面真值兜底；用户改系统时间不会导致永久跳过 |
| 门禁参数解析失败 | 放行并告警（不让配置错误把任务锁死） |
| complete 参数解析失败 | 不写记录并告警 |

## 待核实

- **游戏日刷新时刻 05:00 为默认值，置信度中**：公开资料口径不一（3 点/4 点/5 点
  都有）。待下次自然跨刷新点时，用游戏内"任务刷新倒计时"文案或空态模板实机
  核实一次，再更新默认值。该值只是配置项，改起来零成本。

## pipeline 侧接入（待 pipeline 迁移完成后执行）

见研究文档 `research/task-memory-design.md` §8。链首插 `task_memory_gate`
（on_error 接早退节点），链尾插 `task_memory_complete`，免费礼包链路接
reconcile（已有 NoForFree 已领取模板）。
