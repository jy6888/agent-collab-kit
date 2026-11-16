# 项目模型与语义更新协议

## 事实源

- `PROJECT.md`：愿景、成功结果、范围、非目标和长期约束。
- `ROADMAP.md`：里程碑及 accepted/active/completed/abandoned/superseded 状态。
- `STATUS.md`：当前阶段、活动任务、阻塞和可执行下一步。
- `FINDINGS.md`：有证据、跨任务有效的项目事实。
- `DECISIONS.md`：用户或架构决定及接受、拒绝、替代历史。
- `tasks/<id>/`：单任务计划、机械/人工进度和局部发现。
- `.codex/planning/index.json`：可重建缓存，不得覆盖 Markdown。
- `.codex/planning/transactions/`：未完成语义事务的恢复证据；存在 prepared 项时不得继续写长期语义。
- `docs/project/snapshots/`：显式检查点的非 canonical 副本；恢复时不默认读取。

## 权威顺序

1. 用户最新且明确适用于本项目的指令。
2. accepted 项目决定。
3. accepted/active 项目目标和路线。
4. 活动任务计划。
5. 仓库与测试证明的实施事实。
6. 代理建议、旧 Handoff 和机器缓存。

代理可以用直接证据记录 observed/verified finding，但只能把新目标或路线写成 proposed。只有用户明确授权才能接受、拒绝、放弃或替代项目方向。

## 状态机

- Goal/Milestone：`proposed -> accepted -> active -> blocked/completed/abandoned/superseded`，具体转换由命令校验。accepted、abandoned、superseded 需要用户权威；completed 需要证据。
- Finding：`observed -> verified/disputed/invalidated/superseded`；事实不自动改变路线。
- Decision：`proposed -> accepted/rejected/superseded`；终态转换需要用户权威。
- Task：`active/blocked/paused/ready_for_review -> completed/cancelled`；completed 还要验收、发现分类和项目对账。

所有长期语义命令默认 dry-run，并要求目标文件 `expected_revision`。revision 不匹配时停止并重读；不做自动语义合并。

## 必须执行语义更新的检查点

- 首次确认项目长期目标：初始化 PROJECT、ROADMAP、STATUS。
- 用户改变目标、范围、非目标或长期约束：同一轮、下一次实质代码修改前更新。
- 用户放弃或替代方向：更新 DECISIONS、ROADMAP、STATUS，保留旧条目和原因。
- 发现跨任务有效的新事实：更新 FINDINGS；如影响路线，只标 `needs-review`，不自动改决定。
- 任务启动、阻塞、暂停、恢复或阶段变化：更新任务 PLAN/PROGRESS 和 STATUS。
- 任务完成：同步代码状态、分类任务发现、更新关联路线、记录决定、更新 STATUS，再完成任务。

每次工具调用、重复测试、临时错误、纯探索读取和未经接受的代理想法不触发长期文件更新。

## 完成权

- `completion_policy: auto`：客观验收项全部完成且已对账时可以自动 completed。
- `completion_policy: user-review`：先进入 ready_for_review，只有用户确认后 completed。
- 用户可以否决任何自动完成；用 `reopen-task` 恢复 active 并记录原因。

## Handoff

同项目恢复直接读取 STATUS 和任务文件。只有跨工具、对外或跨仓库交接时生成派生 Handoff；导出物不是 canonical truth。导入 Handoff 只登记待对账候选，用户接受也只表示允许进入对账，不能直接覆盖项目文件。

## 故障恢复与快照

- 事务写入前保存 before hash/content；中断后未应用可清除，部分应用必须显式 rollback，禁止静默覆盖。
- canonical 文件被事务外修改时标记 semantic-drift；审查后显式 acknowledge，strict 模式在确认前失败。
- 快照只用于重大决定、里程碑、风险、迁移、发布和正式交接，manifest 必须声明 `canonical:false`。
- v3 迁移顺序固定为 dry-run、备份、mapping、应用、strict validate；回滚先归档迁移后的 v4 文件，再恢复 before 文件，旧 current 始终保留。

## Scope 与 workstream

一个规划根只有一套 canonical 项目文件。workstream 只是任务分类与显式筛选；多个活动任务或多个候选工作流时保持歧义，必须 bind/filter，不能按最近修改时间猜测。只有能独立定义愿景、路线和决定的范围才应成为独立项目根。
