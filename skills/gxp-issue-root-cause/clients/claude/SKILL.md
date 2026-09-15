---
name: gxp-issue-root-cause
description: 根据 GXP 实际业务和检查证据，用非技术人员能懂的话输出根本原因、解决方法、影响范围。支持只有问题描述、没有处理记录的情况；复用 gxp-lowcode-debug 和只读 MCP，不猜原因、不带姓名日期。
argument-hint: "[问题描述、业务单号或处理记录]"
allowed-tools: Read, Glob, Grep
disallowed-tools: Edit, Write, NotebookEdit
metadata:
  client: claude
  version: "1.1.0"
---

# GXP 业务根因分析 - Claude Code

这是分析当前业务问题的技能，不是修改技能或业务代码的命令。问题来自当前用户消息及随调用传入的内容；没有参数时沿用当前对话，不把空参数当作空任务。

## 先加载共同规则

用 Read 完整读取 `${CLAUDE_SKILL_DIR}/references/business-rules.md`，再处理问题。最终默认只输出每个问题的“根本原因、解决方法、影响范围”。共同规则中的证据标准在后续追问中继续适用。

## Claude Code 适配

- 用户通过 `/gxp-issue-root-cause` 调用；仅整理材料时，不为排版连接业务系统。
- 需要查证时，优先用 Read 读取当前已安装的 `gxp-lowcode-debug` 技能和相关引用文件；根据实际技能位置查找，个人目录通常是 `~/.claude/skills/gxp-lowcode-debug/SKILL.md`。不要套用 Codex 的插件缓存路径。
- 如果通过 Skill 工具加载原排查技能，保留它的查证规则；三项回填仍遵循本技能的业务表达约定，不因加载了详细排查技能而改成技术报告。
- 使用本会话已连接的 `gxp-lowcode-readonly` MCP。按实际工具目录和接口调用，不将文档里的工具名当成已成功执行的检查；找不到工具时明确待确认。
- 优先使用 Read、Glob、Grep 与结构化只读 MCP，不用 Bash 扫描整台电脑、连接数据库或执行写操作；不调用编辑类 MCP，也不保存或发布草稿。
- frontmatter 的工具设置只是客户端层的部分限制，不代表所有 MCP 都只读；仍须检查目标工具的用途。不能因工具被允许便实施修改。
- 遇到权限拒绝时，说明缺少哪项证据，不改用别的执行方式绕过限制。纯材料整理不受 MCP 不可用影响。
- 长对话中保留当前问题、已确认业务规则和证据位置。无法从摘要确认的原因重新查证；不能把自己以前的回答当事实。新读取的证据如果推翻旧判断，应明确更正。
- 按共同规则逐项判断原因、方案和影响范围。无需展示内部思考过程，用户要求证据时提供可核查事实和来源即可。

仅在技能验收时读取 `${CLAUDE_SKILL_DIR}/references/acceptance-cases.md`。不要把其中的虚构案例当作本次故障事实。
