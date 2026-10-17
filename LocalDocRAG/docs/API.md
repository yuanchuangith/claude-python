# API 1.0

同源本地 `/api`；所有 POST/PATCH/PUT/DELETE（含检索 POST）携带 `X-LocalDoc-Request: 1`。不开放CORS；Host/Origin检查防止跨站点和DNS重绑定。资源ID是UUID，不接受下载路径。错误为`{code,message,details,request_id}`，不返回原始prompt或系统文件路径。完整请求Schema见运行时`/openapi.json`。

| 方法 | 路径 | 内容 |
| --- | --- | --- |
| GET | `/health` | status活性；ready/evidence_ready表示Embedding与Qdrant就绪；chat_ready/image_caption_ready另列，本地聊天模型不阻塞Agent检索 |
| GET | `/system/status`, `/system/models` | Ollama/Qdrant/SQLite/OCR、模型、实际推理设备、队列、空间 |
| PATCH | `/system/settings` | chat_model,vision_model,context_length,ocr,caption；模型须已下载 |
| POST | `/system/rebuild` | `{model,confirm:true}`；返回持久化全量重建job |
| GET/POST | `/knowledge-bases` | 列表 / `{name,description}` |
| PATCH/DELETE | `/knowledge-bases/{id}` | 编辑 / `?confirm=true`级联软删除与清理任务 |
| GET/POST | `/knowledge-bases/{id}/documents` | 版本/阶段列表 / multipart多个`files`；返回items document/version/job/deduplicated |
| GET | `/documents/{id}/versions` | 历史版本、hash、状态 |
| GET | `/documents/{id}/preview?version_id=UUID` | 原顺序blocks、结构tables、原图/OCR/VLM、report |
| GET | `/documents/{id}/download?version_id=UUID` | 原件字节 |
| POST | `/documents/{id}/reindex` | 失败续传或建立新索引版本；不破坏旧active |
| POST | `/documents/{id}/activate` | `{version_id,confirm:true}`，恢复同profile已完成历史版本 |
| DELETE | `/documents/{id}?confirm=true` | 立刻隔离查询，后台先清向量再清元数据和媒体 |
| GET/POST | `/jobs/{id}`, `/jobs/{id}/retry` | 阶段/计数/错误/告警 / 显式失败重试 |
| POST | `/search` | `{knowledge_base_id,query,top_k}` → bm25,dense,fused（完整来源） |
| POST | `/chat/stream` | `{knowledge_base_id,question,conversation_id?,top_k}` → SSE sources/token/done/error |
| GET | `/assets/{media_id}` | 该库中的原图；不支持显示的媒体作为下载，不执行HTML/SVG |
| GET | `/conversations?knowledge_base_id=UUID` | 历史会话 |
| GET | `/conversations/{id}` | 消息与来源快照；原件删除时original_deleted=true |
| POST | `/agent/search` | 两个Agent共用；knowledge_base_id/query/offset/limit/max_snippet_chars/result_version? |
| GET | `/agent/knowledge/{chunk_id}?knowledge_base_id=UUID` | 活动证据的KnowledgeDocument；也支持完整表UUID；非活动/跨库返回null |

来源包含source_id、document_id、document_sha256（原件哈希）、sha256（索引片段哈希）、version_id、filename、section_path、block_id、anchor、table_id/row/col、image_id、excerpt、preview_url。内部row/col从0开始，UI从1开始显示。页码仅对实际成功LibreOffice渲染的回退页显示；不推断DOCX页码。

问答模型内部返回 `paragraphs[{text,source_ids}]` 和 `insufficient`；服务端逐段绑定本次来源并渲染 `[S1]`，外部 SSE 接口保持不变。动态 schema 仅允许本次来源 ID；越界引用、无有效引用的充分答案及未完成生成均不展示为成功答案。引用校验只能确认来源归属，事实与条件仍须对照原文验收。无证据的答复应以 `insufficient:true` 结束。

两个Agent从同一知识库读取相同活动版本；这两个端点只检索与读取原文，不调用本地聊天模型或另起业务Agent。`/agent/search` 的query为1–500字符、offset为0–1000、limit为1–20、max_snippet_chars为200–4000；返回 `results/totalMatches/resultVersion`。`resultVersion`绑定知识库、查询、活动版本快照和候选片段；用同一query、前端工具返回的nextOffset作为offset续读时可传 `result_version`（`sha256:`加64位小写十六进制），不一致返回409 `KNOWLEDGE_SEARCH_CHANGED`。检索过程中活动版本或嵌入profile切换，返回409 `KNOWLEDGE_VERSION_CHANGED`，要求重新检索。当前最多返回20个融合候选，继续定位其他业务应缩小或调整query。

`KnowledgeDocument.metadata` 包含 `contentPurpose:business_knowledge`、`sourceKind:business_manual`、`consumerAgentIds:[action_design,business_operation]`、`status:evidence_only` 和未审核声明。`metadata.source` 提供原件 `document_sha256`、片段 `sha256`、document/chunk/version ID、文件名、章节、锚点、预览链接、FS和表格行列/图片ID及告警。`sha256`对索引片段内容计算，完整操作表的片段hash仍是其索引原文hash；通过原表UUID读取时为规范表JSON的hash，均不是Word原件hash，也不是前端完整KnowledgeDocument的contentVersion。不推断Word页码。

表行搜索结果直接附带 `fullTableId` 与 `readHint`，可按提示调用knowledge_read读取整表，无需反复搜索“完整表”。目标由真实table_id解析，存在活动operation索引时使用其ID，否则使用原表UUID；单行正文及 `metadata.fullTableId` 与搜索返回相同目标。该兼容扩展保留原row ID、标题、摘要与搜索分页字段。无FS普通表直接读取现有规范数据，无需重导入/重建索引。完整读取保留表头、源行顺序和合并单元格内容，返回 `metadata.source.table_complete:true`，操作表还返回 `operation_complete:true`。前端工具自行按 `nextOffset/contentVersion` 读完全部分页；只读到搜索摘要或一页不表示完整读取，版本变化时不得沿用旧页。跨章节条件仍需继续检索与读取，各分支与来源保留，不在API中合并冲突或填补未规定内容。原文/图片告警必须随证据保留。

正文中的“相邻说明”仅包含当前原文块前后紧邻的同父级段落或标题；当前表及其单元格、嵌套表等后代不重复拼入，也不越过另一张表查找更远说明。此读取时修正不改变原件、活动版本、索引片段或来源hash，但会改变完整KnowledgeDocument的字符数及 `contentVersion`，旧分页必须重新从offset 0读取。`table_complete/operation_complete`说明源表已完整还原；单页 `contentComplete:false` 或 `truncated:true` 仍可正常出现，完整读取须核对同一contentVersion下连续offset直至nextOffset=null，不能只看末页标记。

证据可用于业务理解、编排建议、流程问答和计划依据；`evidence_only` 不表示已审核操作包，不授予画布写入、发布、页面输入、提交、审批或电子签名权限。两个Agent的会话、Graph、历史、恢复和执行批准由各自领域独立维护。

现有网关提供固定知识代理 `/api/actiondesign-agent/knowledge/config`、`/search`、`/documents/{UUID}`。配置由后端选择唯一知识库，沿用网关认证；浏览器不传任意后端地址、不接收Provider密钥。代理重写本地Host/Origin并只转发必要数据；将响应中的 `/documents/...` 相对path/preview_url改为配置的本机管理页绝对链接，来源hash不变。网关契约见 [claude-agent-sdk.md](../../claude-agent-sdk.md)。也可由宿主提供 `/localdoc/api` 同源反向代理；两者最终读取本服务相同API。

全量重建期间文档/知识库写操作409，避免模型profile切换竞态。API worker1；并发模型锁不依赖进程局部asyncio.Lock。任务重启从解析、图片或嵌入安全检查点续跑。

运行中的只读API可用 `node scripts/verify_component_agent.cjs` 验证；脚本调用component真实knowledge_search/read，分别使用action_design与business_operation上下文，核对相同搜索快照、完整正文、来源版本和hash、独立读取状态、旧hash拒绝与evidence_only执行阻断。2026-10-17首轮[实测报告](../data/component-agent-verification-20261017T031832242Z-c157fccd-929f-4984-8cbb-baf8d3bfa990.json)及[搜索提示smoke报告](../data/component-agent-verification-20261017T033826856Z-a7481619-694d-49c4-b2db-9d65a1a05a12.json)保留：当时两域各4页、4882字符、20次只读请求，后者确认fullTableId/readHint与读取目标一致。4882字符为去除相邻表重复前的历史基线；当前脚本按totalChars/maxChars核对连续完整分页，至少覆盖两页，不固定旧页数。未调用模型、导入或重建索引。报告默认使用时间与UUID新文件名，也可 `--output new-report.json` 指定新文件，已有文件一律拒绝覆盖。该检查不替代实际MiMo Graph与正式平台验收。

相邻说明修正并仅重启API后的[最新smoke](../data/component-agent-verification-20261017T040828431Z-dbe4ce56-4782-429f-9fe9-1356f76083fb.json)通过：两域各2页完整读取同一2735字符操作表、14次只读请求，保留全部FS条件、来源hash/活动v2、搜索整表目标、状态隔离、旧contentVersion拒绝和evidence_only门禁。旧报告保留，不以缩短后的正文长度表示内容缺失。
