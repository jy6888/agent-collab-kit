# agent-collab-kit：多 Agent 协作 skill

让两个编码 Agent 在同一个项目里分工：一个规划（默认 Claude Code），一个执行（默认 Codex）。项目状态存在文件里，两端经 bridge 互发短通知，对话可以随时更换，连续性只靠文件。

本包把规划、协作规则和通信三样东西放在一起，新项目装一次即可复用。

## 它解决什么问题

- **长任务换对话后丢上下文**：目标、决定、发现、任务进度都落在 `docs/project/` 下的文件里，新对话运行一次 `resume-context` 就能接上。
- **两个 Agent 互相覆盖对方的文件**：规划者和执行者各有写入范围，执行者一侧由 hook 强制。
- **靠用户在两个窗口之间转述**：两端经 bridge 直接发消息；消息只放文件指针，内容以文件为准。
- **规划文档越写越长**：每个事实只在一个位置写全，记录检查（L1–L8）自动提醒超长、重复和该归档的条目。
- **Agent 遇到问题就停下问用户**：三级升级——执行者先自修一次，不行交规划者裁定，只有换方法、动预算、改目标这类事才找用户。

## 包含的内容

| 部分 | 位置 | 作用 |
|---|---|---|
| 规划 skill | `skills/planning-with-files-zh/` | 文件规划 CLI：任务、长期目标、决定、发现、状态，带 revision 与 dry-run |
| 规划 MCP | `mcp/planning_mcp.py` | 把规划 CLI 暴露成 4 个工具，只用 Python 标准库 |
| 协作 skill | `skills/agent-collab/` | 分工、消息格式、换对话、三级升级、记录与提交纪律 |
| 桥接 skill 与 MCP | `skills/claude-bridge/`、`vendor/codex-claude-desktop-bridge/` | Codex 与 Claude Code 对话间互发消息 |
| 项目模板 | `templates/` | AGENTS / CLAUDE 规则段落、角色策略、Codex hook、端点登记、规划配置 |
| 收件监视 | `tools/bridge_inbox_watch.py` | 只读列出发给规划会话的投递记录 |

本包不含任何具体项目的规则、路径或数据。项目自己的约束写在项目的 AGENTS.md 里。

## 协作方式一览

```
用户 ──需求、确认──▶ 规划者（Claude Code）──BRIEF / VERDICT──▶ 执行者（Codex）
                        ▲                                        │
                        └────── QUESTION / READY / BLOCKED ──────┘
              两端共同读写：docs/project/（规划文件）与任务目录
```

- 规划者：维护长期规划文件，建任务并写规格，审阅交付，结项。
- 执行者：写代码与测试，维护任务的进度、发现和证据，完成后提交审阅。
- 消息共六种：`HELLO`、`BRIEF`、`QUESTION`、`VERDICT`、`READY`、`BLOCKED`，每条不超过 10 行，必须带文件指针。

完整规则见 `skills/agent-collab/SKILL.md`。

## 环境

- Python 3.10+（`python` 在 PATH 上；规划 CLI 与 MCP 无第三方依赖）
- Node 20+（桥接插件）
- 桥接插件的原生传输目前只在 Windows 上验证过，详见 `vendor/codex-claude-desktop-bridge/README.md`

## 安装

先把仓库克隆到本机任意位置，下文用 `<kit 绝对路径>` 指这个目录。本包既是 Claude Code 插件（`.claude-plugin/`），也是 Codex 插件（`.codex-plugin/`）。

**如果已单独装过 `codex-claude-desktop-bridge`**：两处都提供同名 MCP 服务，同时启用会出现两套重复的桥接工具。启用本包前先停用单独安装的那一个。切换后桥接工具的名字前缀会变（例如 `mcp__plugin_agent-collab-kit_codex-claude-desktop-bridge__send_to_codex`），已有的权限放行条目和项目文档里的工具名要跟着改。

### Claude Code

把本目录加为本地 marketplace 并启用插件，在 `~/.claude/settings.json` 中：

```json
{
  "extraKnownMarketplaces": {
    "agent-collab-kit-local": { "source": { "source": "directory", "path": "<kit 绝对路径>" } }
  },
  "enabledPlugins": { "agent-collab-kit@agent-collab-kit-local": true },
  "crossSessionInbound": "accept"
}
```

`crossSessionInbound: accept` 必须写在用户级设置里，否则 bridge 发来的消息会被扣下。

### Codex

在个人 marketplace（`~/.agents/plugins/marketplace.json`）的 `plugins` 里加一项 `{ "name": "agent-collab-kit", "source": "<kit 绝对路径>" }`，再在 `~/.codex/config.toml` 加：

```toml
[plugins."agent-collab-kit@personal"]
enabled = true
```

### 只用规划部分

不装插件也行，直接运行：

```
python skills/planning-with-files-zh/scripts/planning_state.py <命令> --root <项目>
```

## 规划 MCP 的工具

| 工具 | 说明 |
|---|---|
| `planning_resume_context` | 只读。项目愿景、路线、活动任务、阻塞、下一步 |
| `planning_validate` | 只读。可恢复性检查；strict 另查 schema、引用、文档漂移与记录 lint |
| `planning_lint_records` | 只读。记录 lint（L1–L8） |
| `planning_run` | 其余所有 CLI 命令。长期语义命令默认只预览，带 `--apply` 与正确的 `--expected-revision` 才写入 |

每个工具都要传 `root`（项目绝对路径）。`hook` 命令不经 MCP，由宿主的 hook 机制调用。

## 给新项目接入

1. **建长期项目层**（需要用户确认过的愿景）：`planning_run`，`command: init-project`，`args: ["--title", "…", "--vision", "…"]`。
2. **并入规则**：`templates/AGENTS.collab.md` → 项目 `AGENTS.md`；`templates/CLAUDE.collab.md` → 项目 `CLAUDE.md`。按注释替换占位符。带【需用户授权】的条款（执行者新开对话）须用户明确同意，并记一条 accepted 决定。
3. **角色策略**：`templates/project-policy.json` → `tools/codex/project-policy.json`，按项目目录结构填两边拥有的路径。
4. **执行者 hook**：`templates/codex-hooks.json` → `.codex/hooks.json`，把 `<kit>` 换成本包绝对路径。命令里的 `PLANNING_HOOK_SCOPE=project` 不能删：它让共享安装的 skill 负责执行，而不是把自己当成用户级 hook 让位。
5. **端点登记**：`templates/bridge-endpoints.json` → `.runtime/bridge-endpoints.json`，并在 `.gitignore` 加 `.runtime/`。
6. **可选配置**：参考 `templates/planning-config.json`，在 `.codex/planning-config.json` 里设 `lint_since`（接入日期，之前的记录不查）、`lint_limits`、`handoff_readmes`、`policy_path`。
7. 两端各开一个对话，规划者 `resume-context` 后登记自己并发 `HELLO`。

## 测试

```powershell
cd skills\planning-with-files-zh\tests
python -s -m unittest test_workflow_v2 test_planning_v4
cd ..\..\..
python -s -m unittest discover -s tests
```

规划测试会创建临时 git 仓库。临时目录如果位于另一个 git 仓库内部（例如主目录本身是仓库），先设 `GIT_CEILING_DIRECTORIES` 或把 `TMP`/`TEMP` 指到仓库外，否则会很慢或失败。

## 当前状态

- 规划 CLI、规划 MCP 与模板有自动测试覆盖。
- 桥接插件在单独安装时长期使用过；作为本包的一部分加载后的真实收发、以及 Codex 端按本包清单加载，尚未实测。遇到问题欢迎提 issue。

## 许可与来源

- 本包自有内容：MIT，见 `LICENSE`。
- `vendor/codex-claude-desktop-bridge/`：[KeeVeeG/codex-claude-desktop-bridge](https://github.com/KeeVeeG/codex-claude-desktop-bridge) v0.4.2，MIT，许可证见该目录的 `LICENSE`。相对上游安装产物只有一处改动：三份清单里去掉了安装器写入的本机状态目录（`CODEX_CLAUDE_BRIDGE_STATE_DIR`），运行时改用插件自己的默认位置。升级时整目录替换，并把其 `skills/claude-bridge/` 同步到本包 `skills/claude-bridge/`。
