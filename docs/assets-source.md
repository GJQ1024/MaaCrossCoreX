> 权威级别：权威结论

# 素材来源登记

第一版所需 13 张模板图**全部来自 MCC_Framework**（MIT），MCCA 本次未取用任何素材。
来源 commit 见 `docs/upstream.md`。

| 文件 | 来源 | 用途节点 |
|---|---|---|
| `image/StartUp/NERI.png` | MCC_Framework | `启动角色界面` |
| `image/StartUp/CloseAnnouncement.png` | MCC_Framework | `子任务_关闭公告` |
| `image/StartUp/ZNGG.png` | MCC_Framework | `子任务_关闭周年公告` |
| `image/StartUp/HomeFlag1.png` | MCC_Framework | `首页标志2` |
| `image/StartUp/HomeButton.png` / `HomeButton1.png` | MCC_Framework | `子任务_点击首页按钮` |
| `image/StartUp/BackButton.png` | MCC_Framework | `子任务_点击返回` |
| `image/StartUp/DailyTask.png` | MCC_Framework | `每日任务`、`通行证` |
| `image/StartUp/NoDailyTask.png` | MCC_Framework | `无每日任务` |
| `image/StartUp/NoMail.png` | MCC_Framework | `无邮件` |
| `image/StartUp/NoForFree.png` | ~~MCC_Framework~~ 已按 3.3.1 重裁（2026-11-01，`gift_after_claim5` 第一位付费礼包「¥6」价格符号 55×40） | `礼包_已领取` |
| `image/Infrastructures/mail.png` | ~~MCC_Framework~~ 已按 3.3.1 重截（2026-11-01，`06_home` 主界面信封图标 38×30） | `邮件` |
| `image/一键领取.png` | ~~MCC_Framework~~ 已按 3.3.1 重截（2026-11-01，`08_dailytask` 每日任务页右上按钮 124×37） | `每日任务_一键领取` |
| `image/StartUp/ClosePromo.png` | 自截 3.3.1（2026-11-01，`06_home` 星贸凭证弹窗右上 X 36×36） | `子任务_关闭促销弹窗` |
| `image/Infrastructures/Infr_Recourse2.png` / `Infr_Recourse3.png` | 自截 2026-11-18 21:35 MuMu 1280×720 帧（基地总览、三处资源点满仓 MAX 态；紫晶 60×38 @(513,444)、红晶 60×38 @(578,444)）。原 `Infr_Recourse.png` 只认粉晶，紫/红同帧仅 -0.0028 / -0.1392 | `基建_收取资源`、`基建_收取资源_不换班` |

## 3.3.1 实机比对备注（2026-11-01，详见 shots/01_ondevice/match_report.json）

- 通过（保留上游模板）：NERI 0.999、HomeFlag1 0.870、HomeButton 0.9995、HomeButton1 0.950、
  BackButton 0.998、DailyTask 0.979（命中主界面底部任务图标）；CloseAnnouncement 0.893
  命中的是星贸凭证弹窗 X（本次无普通公告弹窗，未能验证真公告场景）。
- 未出现对应界面/状态、无法比对（保留上游模板）：ZNGG（周年公告未弹）、NoMail（邮箱非空）、
  NoDailyTask（无可证状态）、NoForFree（免费礼包未领）。
- `一键领取.png` 已重裁自每日任务页；注意 3.3.1 通行证任务页无「一键领取」按钮，
  节点 `通行证_一键领取` 的 roi [900,260,360,130] 与流程需按 3.3.1 重设计（按钮实际位于每日任务页右上 ~[1082,88]）。

## 待实机补齐（当前占位，未引用缺失文件）

以下识别当前依赖上游 720p 旧截图或纯 OCR，游戏 3.3.1 实机验证后可能需要重截：

- `启动_唤醒待机页面`：纯 OCR 时钟正则，无模板，待实机确认点按唤醒有效；
- `礼包_已领取` 用的 `NoForFree.png` 已于 2026-11-01 按 3.3.1 重裁为第一位付费礼包「¥6」符号，实跑匹配 1.000；
- 周年公告 `ZNGG.png` 为周年活动特供，活动结束后可能不再出现（节点保留无害）；
- OCR 模型：`assets/resource/model/` 目前为空，运行 OCR 识别前需放入
  `MaaCommonAssets/OCR/ppocr_v4/zh_cn`（可从 `repos/MaaCommonAssets` 只读拷贝）。
