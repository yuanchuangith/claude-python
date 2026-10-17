# FS-CPM-001 手册导入与质量验收

日期：2026-10-17，Asia/Shanghai。指定培训Word保留在本机同一持久知识库，现在由两个现有前端Agent共读：编排助手理解规则，操作助手理解流程，复用既有 MiMo 2.6 Pro。启动见 [README](../README.md)，接线见 [架构](ARCHITECTURE.md)，当前真实模型结果见[双Agent质量记录](../../../component/doc/business-operation/knowledge-live-validation-2026-10-17.md)及[实施报告](../../../component/doc/business-operation/knowledge-shared-implementation-2026-10-17.md)。下文独立本地聊天结果为历史辅助证据。

## 实际持久数据

| 项目 | 结果 |
| --- | --- |
| 源文件 | component/doc/business-operation/sources/FS-CPM-001培训合并版本1014.docx |
| 源SHA256 | 11fa6b38fb08c83b0cef8b70adf787675018120284e62ea36c9ef416a3dec0ba |
| 知识库 | GXP培训操作手册 FS-CPM-001 |
| knowledge_base_id | cb65eb15-c69f-42aa-b53b-a44615b04f0b |
| document_id | 047b6b6b-14ac-4bf4-8000-8c90b6072568 |
| 当前活动v2 | 9705a1e4-3a58-41d0-bbc7-8a2d0c0ac76c |
| job_id | 739f79d3-ed7c-4718-8cd6-507158b72068 |
| 任务结果 | completed_with_warnings；1528/1528，retries=0，last_error=null |
| 解析 | 7504块，187标题，170表，4媒体部件/5次引用；37导航块排除索引 |
| 切片 | 401正文、1037表格行、85操作索引、5图片，共1528 |
| 向量 | qwen3-embedding:0.6b，真实1024维，Qdrant localdoc_129662a9faed4e4ec75dfa13 |
| 图片 | 4个媒体部件均完成真实CPU RapidOCR和本地qwen3-vl:2b-instruct |

v1 `003b7c3d-ad80-49a9-9de1-78626b9e83c3` 保留历史核对，含旧视觉失败告警，不是当前活动版本。原件与完整报告在被Git忽略的 `data/versions/<version_id>/`，导入清单在 `data/training-manual.json`。模型与向量分别在 `runtime/models/`、`runtime/qdrant-data/`；重启不重新下载或导入。

## 问答质量

历史13题检索预期匹配通过，**这不等于13题答案正确**。初版qwen3:4b出现必填审批人遗漏、单位编造、持续培训分支遗漏及状态误判，不能作为通过结果。本轮不继续调优或下载本地聊天模型；两个Agent改用已配置MiMo并分别核对实际回答，结果由上方质量记录维护。

逐题原始结果写入被忽略的 `data/training-evaluation.json`。评估脚本只存问题、原文引用与真实模型输出，不向运行时提供预写培训答案。最终核对需检查事实、条件、子问和引用，未知默认7天与无年假条款必须承认证据不足。

## 两个Agent的真实工具调用

历史业务域验证真正加载component的TypeScript工厂、knowledge_search、knowledge_read及BusinessRuntime，通过真实API检索精确FS行，再读取完整操作表；当时4页共4843字符，结果保留在 `data/component-agent-verification.json`，不覆盖或当作双域验证。

当前运行 `node scripts/verify_component_agent.cjs` 分别验证action_design与business_operation上下文。2026-10-17[首轮双域报告](../data/component-agent-verification-20261017T031832242Z-c157fccd-929f-4984-8cbb-baf8d3bfa990.json)通过：同一v2搜索快照与证据ID，两域各4页读完保留表头的4882字符操作表，来源版本、正文hash和来源信息一致；编排侧读取不改变业务运行时状态，业务侧未读完时判定false、末页才true；两域旧contentVersion被拒绝，未审核evidence_only仍阻止业务执行。共20次只读请求，未调用本地聊天模型或MiMo，未重导入或重建索引。

此前[搜索提示smoke报告](../data/component-agent-verification-20261017T033826856Z-a7481619-694d-49c4-b2db-9d65a1a05a12.json)再次通过同一组检查，并确认表行搜索直接提供fullTableId/readHint、两域可发现相同整表且与读取接口目标一致；当时为20次只读请求、两域各4页4882字符，活动v2和索引保持原样。031832报告继续保留。

4882为相邻表去重前的历史正文长度。此前“相邻说明”曾按block_order±1包含当前整表与首单元格，造成JSON转义形式的重复证据；读取时已改用紧邻同父段落/标题，保留真正前后说明且不越过另一表。原件、活动v2与索引不变，完整KnowledgeDocument字符数/hash随正文修正变化。smoke改按totalChars/maxChars核对连续完整分页并要求至少两页，后续报告追加保留。

仅重启API后的[最新smoke报告](../data/component-agent-verification-20261017T040828431Z-dbe4ce56-4782-429f-9fe9-1356f76083fb.json)通过：同一操作表去冗余后2735字符，两域各2页、14次只读请求；全部FS条件保留，版本、来源hash、搜索目标、状态隔离、旧hash拒绝与evidence_only执行阻断均通过。未调用模型，未动Ollama/Qdrant、源文档、活动版本或索引，旧报告保留。

每次运行默认写入新的时间戳/UUID报告；可用 `--output new-report.json` 指定新文件，已有文件拒绝覆盖。该脚本验证实际工具与后端API，不替代两个生产Graph使用真实MiMo的质量验收。

Python与独立React管理页均在本后端LocalDocRAG。component仅保留知识源、业务运行时、两个工具、助手参数及生产入口导出。旧工作台/脚本/重复演示移到被忽略的 `runtime/obsolete-frontend/`，源文档、原IndexedDB和无关修改保留。

当前两个生产入口默认使用既有8888网关知识代理，无需平台另外新增 `/localdoc` 代理；原同源工厂和显式source覆盖保留。网关配置本机服务及此知识库ID，复用既有认证与MiMo，必要检索证据可进入外部Provider，不再宣称全离线。开发Graph与真实工具验收不等于正式平台部署、实际画布/页面执行或业务审批签认；资料不授权保存发布、输入、提交或电子签名。

本轮后端28项通过（19.00s），网关代理与相关认证/日志32项通过；检索期间换版、续页版本冲突、普通非FS整表读取、搜索表行直接给出整表ID、来源hash、跨章节冲突保留及相邻说明排除自身/后代均有定向覆盖。新增回归含嵌套表、操作/规范表及表行、前后说明与不跨独立表取远处正文。实际持久库版本未为测试切换，未重导入、重建或删除。

## 保留告警与验收范围

- EMF经Windows GDI/Pillow转换有界PNG，保留转换告警和Word原件。
- 两个媒体部件没有正文引用位置，明确保留UNREFERENCED_MEDIA_PART。
- `body/p[93]` 存在无法直接解析的绘图/嵌入对象；本机LibreOffice页面回退不可用，保留unsupported_visual/RENDER_FALLBACK_FAILED。不能宣称全部Word对象保真。
- 本机Docker Engine返回500；真实运行采用Ollama0.40.5/Qdrant1.14.1便携程序。容器端到端、完整备份恢复、正式平台页面办理分别验收。

## 复现

在后端LocalDocRAG目录：

```powershell
./scripts/start-native.ps1 -Mode gpu -ContextLength 8192
Invoke-RestMethod http://127.0.0.1:7860/api/health
# 两个Agent真实工具/API验证，不调用聊天模型
node scripts/verify_component_agent.cjs
# 只检索手册；--chat另行调用可选本地问答模型，并非MiMo验收
./backend/.venv/Scripts/python.exe scripts/evaluate_training_manual.py
./backend/.venv/Scripts/python.exe -m pytest backend/tests -q
npm run check --prefix frontend
```

不执行component全局构建；不删除持久数据。原生目录备份需先停止API/Qdrant，再一致复制数据库与向量目录；现有Compose备份脚本不代表原生备份完成。
