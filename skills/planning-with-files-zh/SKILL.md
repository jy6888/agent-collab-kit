---
name: planning-with-files-zh
description: 为复杂代码任务维护可恢复的文件计划，并可按用户确认升级为长期项目规划，持续维护目标、路线、决定、发现、阶段和任务状态。新代码项目可在复杂任务中自动使用独立 task-only 模式，不依赖长期规划；已有项目层则读取并更新长期上下文。学习资料、行政、购物、普通研究等非代码工作区不要自动使用，项目可通过 `.codex/planning-config.json` 关闭。用户明确要求文件规划时可在任意项目使用。
---

# Planning with Files v4

> 两个 Agent 分工协作（一个规划、一个执行）时，写入边界由项目的角色策略文件决定，见同包的 `agent-collab` skill；单 Agent 使用时忽略这一条。

把本 skill 当成代码项目的磁盘工作记忆，不要把它与 Codex Plan 模式混为一谈。Plan 模式管理当前对话的步骤；本 skill 保存跨压缩、跨对话和跨任务的真实状态。

## 先解析模式

运行：

```powershell
# <skill> 是本 skill 所在目录；只用 Python 3.10+ 标准库，无第三方依赖
$planningPython = 'python'
$planningState = '<skill>\scripts\planning_state.py'
& $planningPython $planningState probe
```

- `off`：自动流程全部静默；只有用户显式要求时才可带 `--force` 启动。
- `explicit-only`：非代码项目不自动启动。
- `task-only`：复杂代码任务自动运行，不要求 PROJECT/ROADMAP 存在。
- `project`：同时加载项目长期层和当前任务。

仅仅打开目录、读取文件或处理简单问题时不要创建计划。复杂代码任务通常至少有 3 个阶段或预计超过 5 次工具调用。

## 开始或恢复任务

先运行 `resume-context`（或较底层的 `status`）。它返回项目愿景/目标、当前路线、已放弃方向、活动任务、阻塞和下一步，不依赖旧对话。已有 session 绑定任务时，读取其 `PLAN.md`、`PROGRESS.md`、`FINDINGS.md` 后继续；多个活动任务时必须显式绑定，不按最近修改时间猜测。任务可用 `--workstream <name>` 分类；工作流只缩小显式查询范围，不成为第二套项目层。

启动独立任务：

```powershell
& $planningPython $planningState start-task --title '任务名称' --completion-policy auto
```

主观体验、产品效果或用户要求亲自验收时使用 `--completion-policy user-review`。旧命令 `init --title ...` 保留为 `start-task` 的兼容别名。

task-only 会创建：

```text
docs/project/STATUS.md
docs/project/tasks/<task-id>/
  PLAN.md
  PROGRESS.md
  FINDINGS.md
```

它必须在没有 `PROJECT.md`、`ROADMAP.md`、项目级发现或决定时完整运行。

## 建立长期项目层

只有用户已经明确给出或确认长期愿景时才运行：

```powershell
& $planningPython $planningState init-project --title '项目名称' --vision '用户确认的长期愿景'
```

不得从 README、代码差异或代理推测生成 accepted 目标。进入 project 模式后，在修改长期状态前读取 [project-model.md](references/project-model.md)，按权威顺序和语义检查点更新：

```text
docs/project/
  PROJECT.md
  ROADMAP.md
  STATUS.md
  FINDINGS.md
  DECISIONS.md
```

项目 Markdown 是唯一语义事实源；`.codex/planning/index.json` 只是可重建缓存。

## 执行任务

1. 把 `PLAN.md` 的目标和验收条件改成可观察结果，再开始写代码。
2. 每次阶段变化更新 PLAN；PROGRESS 覆盖式维护“当前状态 / 未决问题 / 最近验证 / 自修记录”四节，整份文件不超过 40 行；旧格式仍可读取。
3. 先做最小冒烟；确认有成功样例和正确产物后再扩大。0 成功、无产物或连续失败时立即停止并记录。
4. 把临时发现留在任务 FINDINGS。只有跨任务仍有效且有证据的事实才晋升项目 FINDINGS。
5. 接管、代码同步、状态变化、项目对账的机械记录只追加至任务 `events.jsonl`（时间、事件类型、摘要、fingerprint 前 12 位），进 Git，不再追加 PROGRESS；hook 不得接受、放弃或替代长期路线。
6. 同一失败最多尝试三种有实质差异的方法；仍失败时记录证据和阻塞条件。

手动同步：

```powershell
& $planningPython $planningState sync --note '本次修改原因和验证结论'
```

## 长期语义变化

用户明确改变目标、范围、决定、持续约束或放弃项后，在同一轮、下一次实质代码修改前更新 canonical 文件。hook 只提醒，不做自然语言猜测。

长期语义命令默认只 dry-run；读取目标文件 front matter 的 `revision`（也可从 `status` 的 `document_states` 查看），作为 `--expected-revision`，确认预览后才加 `--apply`。陈旧 revision 必须失败，禁止 last-write-wins。

可用确定性命令维护状态机：

```powershell
& $planningPython $planningState add-goal --title '目标' --acceptance '可验证结果' --status proposed --authority agent --expected-revision 3
& $planningPython $planningState add-milestone --title '阶段' --acceptance '阶段验收' --status accepted --authority user --expected-revision 2 --apply
& $planningPython $planningState update-roadmap --item-id M-001 --status abandoned --authority user --reason '用户明确放弃' --evidence '当前对话' --expected-revision 3 --apply
& $planningPython $planningState record-finding --title '事实' --evidence '命令或文件证据' --impact '影响' --status verified --expected-revision 1 --apply
& $planningPython $planningState record-decision --title '决定' --status accepted --authority user --reason '用户明确确认' --expected-revision 1 --apply
```

还可用 `update-finding`、`update-decision` 推进已有条目。代理权威只能写 `proposed`；目标/路线/决定的 `accepted`、`rejected`、`abandoned` 或 `superseded` 必须来自用户。里程碑 `completed` 必须带证据。放弃、失效和替代保留原条目、原因及关联 ID，不删除历史；inactive 条目不得成为默认下一步。

## 判断完成

先确保验收条件至少有一项且全部勾选，再运行：

```powershell
& $planningPython $planningState complete
```

- `auto`：客观验收、最终机械同步和项目对账完成后自动标记 `completed`。
- `user-review`：先进入 `ready_for_review`；用户确认后用 `complete --review-approved`。
- 用户否决后用 `reopen-task --task-id <id> --reason '未通过原因'` 恢复 active。

完整 project 模式下，完成时必须显式提供 `--project-impact none|updated --reconciliation-note '对账结果' --next-action '下一项可执行动作'`；task-only 不需要。若任务 `FINDINGS.md` 的“待晋升项目发现”存在真实候选，还必须提供 `--findings-reviewed --findings-note '逐项说明 promoted/local/discarded'`。完成前更新受影响的 ROADMAP、DECISIONS、FINDINGS 和 STATUS。即使长期语义没有变化，也要保留 `project reconciliation: no semantic change` 记录。

## 恢复、验证和迁移

```powershell
& $planningPython $planningState check-staleness
& $planningPython $planningState resume-context
& $planningPython $planningState validate
& $planningPython $planningState validate --strict
& $planningPython $planningState recover-transactions
& $planningPython $planningState migrate-v3 --dry-run
```

默认 validation 只要求可恢复；strict 额外检查 schema、状态、引用、外部 revision 漂移、完成对账和未完成事务。若进程在语义事务中断，先运行 `recover-transactions`；部分写入默认只报告，审查事务保存的 before 内容后才可用 `recover-transactions --rollback` 恢复。人工直接修改 canonical 文件会成为 `semantic-drift`；审查无误后用 `acknowledge-document-drift --note '审查结论' --apply` 确认新 revision。Git 项目使用 HEAD/diff fingerprint；非 Git 项目使用受限 event-path fingerprint，并明确报告验证较弱。

迁移 v3 前先 dry-run。真实迁移先在 `.codex/planning/migration-backups/<migration-id>/` 复制旧 current 和迁移前文件，写逐文件 mapping；迁移保留 active/blocked/completed 状态、时间、旧 session 和 fingerprint。旧任务声称完成但验收项不完整时降为 `ready_for_review`，不得伪完成。旧目录永不删除。需要撤回时运行 `rollback-v3-migration --migration-id <id>`；若 v4 产物后来已改变则默认拒绝，审查后可用 `--force`，它会先把当前 v4 产物归档到 migration-rollbacks。

只在重大决定、里程碑、风险、迁移或发布检查点显式运行 `snapshot --kind <kind> --reason '原因'`。快照带 manifest 且 `canonical:false`，正常任务和 hook 不自动创建。

## Hook 与 Handoff

项目可在仓库级 `.codex/hooks.json` 里调用 `planning_state.py hook <event>`（事件：`session_start`、`user_prompt_submit`、`pre_tool`、`post_tool`、`stop`）。仓库 hook 与用户级 hook 会并行加载：用户级 hook 发现项目自带 hook 或角色策略文件时自动让位；项目 hook 调用的是共享安装的本 skill 时，在命令里设 `PLANNING_HOOK_SCOPE=project`，由它负责执行。写入边界由 `PreToolUse` 路径守卫按角色策略文件执行。

项目级可调项写在 `.codex/planning-config.json`：`lint_since`（此日期之前的记录不查）、`lint_limits`（覆盖各条上限）、`handoff_readmes`（参与数字重复检查的交接入口文件）、`policy_path`（角色策略文件位置，默认 `tools/codex/project-policy.json`）。

记录检查 `lint-records`（并入 `validate --strict` 的 warnings，只提醒不阻断）：L1 PROGRESS 超长或含会话日志；L2 决定标题超长；L3 发现正文超长；L4 演进条目超长；L5 同一数字在多个长期文件重复；L6 手写验证过多；L7 关联任务都已结项的决定仍留在 DECISIONS（归档，或在条目里加 `- Keep: 原因`）；L8 DECISIONS、FINDINGS 条目数超限。`complete` 的输出 `archive_reminders` 列出本任务该归档的决定。

同项目恢复直接读取 canonical 文件。只有跨工具、对外或跨仓库交接时使用 `export-handoff --output <relative-path>`；导出物是只读派生产物。旧 Handoff 只能先用 `import-handoff --input <path>` dry-run，再加 `--apply` 登记为 `pending-reconciliation` 候选；即使用户用 `resolve-handoff-import --disposition accept-for-reconciliation --authority user` 接受进入对账，它也不会直接改 canonical 文件。Handoff 永远不得成为第二份长期真相。
