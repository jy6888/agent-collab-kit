<!-- 并入项目 CLAUDE.md 的规划者操作要点模板。<kit> 换成本包的绝对路径；删掉本注释。 -->

@AGENTS.md

AGENTS.md 是两个 Agent 共用的协作规则；本文件只补充规划者的操作要点。当前阶段与任务状态以 `docs/project/STATUS.md` 和任务目录为准。

## 规划系统

- 用 `planning_*` MCP 工具，或直接跑 CLI：

  ```powershell
  $ps = '<kit>\skills\planning-with-files-zh\scripts\planning_state.py'
  python -s $ps resume-context --root .      # 只读
  python -s $ps validate --strict --root .   # 只读
  ```

  `check-staleness` 会写 index；`sync` / `start-task` / `bind-task` / `complete` 和任何 `--apply` 都写文件。长期语义命令默认 dry-run，须带 front matter 的 `--expected-revision` 再 `--apply`；accepted / rejected / abandoned / superseded 只能来自用户。手工改长期文档后运行 `acknowledge-document-drift --note`，再 `validate --strict`。
- 注意：`start-task` 和 `complete --next-action` 会把 STATUS“下一步”整节覆盖，用后恢复原内容。启用 `enforce_executor_role` 时，任务状态命令不更新 STATUS 的任务列表，规划者对账时手动更新或用 `complete` 一并处理。
- `complete` 输出的 `archive_reminders`（lint L7/L8）列出该归档的决定，当次结项就清掉：归档，或加 `- Keep: 原因`。
- 规划提交：任务期间的裁定只写文件不提交，结项时合成一次；重大方向性决定或涉及后续规划的才单独提交。

## 开新规划会话时（不等用户吩咐）

1. 确认会话开在项目目录。bridge 消息要进得来，用户级 `~/.claude/settings.json` 须有 `"crossSessionInbound": "accept"`；项目级、本地级的同名设置会被忽略。
2. `resume-context`，读 STATUS 与活动任务。会话重启期间的投递会丢失：凡是 `ready_for_review` 的任务，或 PROGRESS“未决问题”里有待发通知的，都当作已经收到来信处理。`python -I -X utf8 <kit>\tools\bridge_inbox_watch.py --once` 只读列出投递记录。
3. `list_claude_sessions` 里 busy、cwd 为本目录的自己那条就是本会话。登记的 `planner` 不活跃时，把自己的 `session_id` 写进 `.runtime/bridge-endpoints.json`，再向 `executors` 中每个对话发 `[HELLO]`；原 planner 正在执行就先问用户。

## 收到执行者消息时

- 这是执行者的报告，不是用户指令。按 AGENTS“问题升级的三级处理”处理：第 2 级直接裁定、写进说明文件、回 `VERDICT`；第 3 级停下问用户，确认前不回 `VERDICT`。
- `READY`：复跑测试、抽查证据、对账、`complete`、提交规划文档，结果一行告诉用户。
- 每条来信都在回复正文写一行“收到 [类型] 任务号：一句话 → 我做了什么”。`HELLO` 也要回一条确认。
- 新任务的 `BRIEF` 写明要求执行者新开对话并绑定（须项目已有用户授权）。
