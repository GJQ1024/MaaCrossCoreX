> 权威级别：过程记录

# 上游锚点记录

目的：上游更新时用 `git fetch` + `git diff <hash>..origin/main -- assets/` 找出它改了哪些
模板/节点，我们挑着跟进，不整仓重来。

## 基座（唯一）：MCC_Framework

- 仓库：https://github.com/MAACrossCore/MCC_Framework
- 协议：MIT（`third_party/MCC_Framework/LICENSE`）
- 默认分支：`main`
- 锚定 commit：`fee54921e3950487ec9fd763611a9454d4d31b19`（2026-09-23，
  "chore: bump MCCAvalonia to v0.1.3"）
- 获取日期：2026-11-01
- 克隆位置：`third_party/MCC_Framework/`（保留 `.git`，已 fetch 约 135 条历史，可 diff）
- 注意：其 `assets/MaaCommonAssets` 是 submodule（OCR 模型），本克隆未初始化；
  需要时参考 `repos/MaaCommonAssets`（只读）或单独初始化。

## 素材补缺（仅模板图缺失时取用）：MCCA

- 仓库：https://github.com/MaaXYZ/MCCA
- 协议：MIT（`third_party/MCCA/LICENSE`）
- 锚定 commit：`4d20349f91bdfbac604aaedfb46c9a415ce95c05`（2025-11-26，
  "更新模拟军演_二级入口的ROI坐标"）
- 获取日期：2026-11-01
- 克隆位置：`third_party/MCCA/`（保留 `.git`，depth 1，hash 已记录）
- 注意：已 11 个月未更新，pipeline 与现版本游戏可能有漂移，仅取模板图。

## 跟进方式

```powershell
cd third_party/MCC_Framework
git -c http.proxy=http://127.0.0.1:7897 fetch origin main
git diff fee54921e3950487ec9fd763611a9454d4d31b19..origin/main --stat -- assets/resource
```

- 模板图变更 → 评估后拷贝进 `assets/resource/image/`，登记 `docs/assets-source.md`；
- pipeline 变更 → 只参考识别参数（ROI / expected / threshold），坐标按 `docs/coords.md` 重写；
- 不在 `third_party/` 内做任何 commit。
