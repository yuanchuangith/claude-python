# 架构与双 Agent 共享知识接线

独立管理页只访问同源 `/api`，两个前端Agent通过既有网关读取同一证据源。FastAPI 保存原件，后台单 worker 持久化处理 DOCX 的有序 OOXML、合并表格和真实图片关系。RapidOCR/ONNX 在 CPU 上识别图片，本地 Ollama VL 提供独立结构化描述；失败不影响可靠正文的索引，但原图/告警/独立重试状态保留。Docling 为显式可选增强，安装 `uv sync --extra docling` 并开启 `ENABLE_DOCLING`，OOXML 始终为来源主路径。依赖已锁在 `backend/uv.lock`，默认不安装额外 Torch/Docling 大依赖。

SQLite WAL/Alembic 保存知识库、文档版本、块、表格、媒体引用、任务、会话、引用快照和设置。jieba 分词写入 FTS5，完整 FS 编号精确匹配。操作表按完整逻辑行索引（普通行上限1600字符，超长拆分重复身份前缀），并建立来源驱动的 operation 流程索引；读取该索引回填完整原文表。目录/PAGEREF 不作为业务证据。正文按自然段/章节切片，图片保留独立 OCR/VLM 和警告，Windows 可将 EMF/WMF 有界转换 PNG，原 Word 部件仍保留。Qdrant payload 只存知识库、版本、chunk ID和类型。

混合检索同时获取 BM25 与向量候选，RRF(k=60)融合与位置去重，从数据库回填整行、表头、相邻说明和原图。嵌入维度从真实响应检测；模型/digest/维度/模板决定独立 collection。后台先写 staging，逐批 upsert/回读校验，最后切换活动指针；半成品/旧版本不在检索范围。失败任务可重试，已保存向量缓存和已完成 OCR/VLM 不重复计算。重启由 worker 租约接管非终态任务。

SQLite `model_queue` 与可续期租约在 API/worker/进程间串行调度；交互优先级0、后台10，每图/每嵌入批释放队列。正在执行的模型调用不抢占；取消浏览器时等待调用完成再释放锁。模型切换前显式卸载并确认 `/api/ps`。Ollama 还设置单驻留、单并行，默认4096上下文。CPU模式显式传`num_gpu=0`，设备状态按Ollama实际`size_vram`显示。

RAG使用本地模型JSON结果；模型只见问题与本次来源证据，无shell/文件系统/外网工具。精确FS题优先命中完整行，流程题优先完整操作表并移除同表重复行；其他题移除已有单行的重复整表，保留跨表状态条件。按证据预算保留完整条目，不截断条件。模型输出逐段text/source_ids，动态schema限定本次ID，服务端验证再渲染引用，校验失败不展示原答案。RAG开启思考，输出最多4096 token且不超过上下文一半；视觉默认2048，未完成生成明确报错。SSE先生成并校验，再逐段发送token，生成期间发心跳；因此是“校验后逐段显示”，不会把未经验证的原始token立即展示。历史答案保存来源快照，不作为下一题的事实依据。引用归属通过不等于事实准确，真实模型质量另行逐题核对。

## 接入两个现有 Agent

后端工程 `claude-python/LocalDocRAG/backend` 拥有知识库、FastAPI、解析/OCR、检索和持久任务；独立React管理页位于 `LocalDocRAG/frontend`。component保留两个现有Agent的TypeScript接线，复用各自Graph与模型接入。既有 `actiondesign_gateway` 负责Provider/Profile、认证、Secret、模型转发与固定知识代理，不承担浏览器业务Graph或画布执行器。当前本机使用便携Ollama/Qdrant，Docker为另一启动方式。

`/api/agent/search` 和 `/api/agent/knowledge/{UUID}` 为两个Agent提供同一活动版本原文，不调用本地聊天模型或另起业务Agent。`/api/health` 的 `ready/evidence_ready` 只依赖Embedding与Qdrant；辅助独立问答的 `chat_ready` 和图片描述的 `image_caption_ready` 分开报告。本地模型JSON问答是独立管理页的辅助功能，不能代替实际Agent知识运用验收。

### 共同的只读知识契约

搜索按知识库隔离，保留精确FS、业务名称与跨表候选。结果快照hash绑定query、活动版本与片段；续搜可传 `result_version`，不一致返回409，检索中换活动版本或profile也拒绝返回旧快照。读取仅允许已完成活动版本，非活动与跨库返回null，读取中换版返回409。前端仍须按 `nextOffset/contentVersion` 读完正文并复验，搜索摘要不计完整读取。

搜索表行直接返回 `fullTableId/readHint`，与命中行正文及 `metadata.fullTableId` 复用同一目标解析，减少已命中后仍反复搜索整表标题的情况；原row ID、摘要和分页结构保留。目标来自真实table_id及同版本已索引operation；无FS普通表使用原表UUID直接读取现有规范表，无需重导入/重索引。不依据固定序号偏移猜测标题的相邻表。完整表保留表头、行顺序与合并单元格内容，`table_complete:true`表示完整原表，operation索引另标记 `operation_complete:true`。一张完整表不证明跨章节条件已读取，Agent应继续检索状态、例外和相关章节；各来源/分支不在API中合并。

相邻说明按原文块的直接兄弟关系读取前后各一个，仅保留段落/标题；规范block_order包含表后代，不能用±1把当前表和首单元格重复序列化到正文。此修正在hydrate阶段生效，不改数据库或索引；保留原件、片段及版本hash，完整KnowledgeDocument的contentVersion随正文变化，旧分页须重启。完整源表标记与单页是否完整分别判断。

返回 `contentPurpose:business_knowledge`、`sourceKind:business_manual`、共同consumerAgentIds、`status:evidence_only`、原件/片段hash、版本、文件名、章节、FS、表格行列、图片与原文锚点。规范表JSON、索引片段和Word原件的hash含义不同，见 [API](API.md)。OCR低置信度、视觉不确定性与冲突告警随证据保留，不猜模糊内容、缺失默认值、单位或Word页码。

知识可用于编排助手生成/检查/解释/修复的业务规则依据，也可用于操作助手理解完整流程和制定计划。手册不证明真实数据库字段、API或节点参数存在，编排助手应另核对当前设计与真实元数据。`evidence_only`不变成审核操作包，不授予保存、发布、页面输入、提交、审批或签名权限。共享只读事实不共享两个Agent的Graph、状态、历史、恢复或执行批准；业务运行时的未决门禁继续阻止输入/提交。

### 既有网关与宿主接线

开发入口默认使用既有网关固定代理 `/api/actiondesign-agent/knowledge/config`、`/search`、`/documents/{UUID}`。网关从后端配置选定唯一知识库，浏览器不指定任意URL；沿用网关认证，向LocalDocRAG重写本地Host/Origin并只传必要数据，不传Provider密钥、客户端凭证、Cookie或跨站Fetch Metadata。管理页仍同源访问7860 `/api`；返回的相对预览链接由网关改为固定本机管理页绝对地址，点击不会误落到网关或平台根路径。

component的 `createGatewayBusinessKnowledgeSource` 提供公共只读访问；两个Agent在各自Registry注册knowledge_search/read，分别按自身运行上下文使用来源。编排域保留原开发知识并区分业务手册；业务域完成门禁与读取使用同一source，资料命中不解除未审核执行阻断。默认接线、显式覆盖与组件导出详见component的[接入指南](../../../component/doc/business-operation/tools/business-knowledge-local.md)。

两个Agent还分别注册前端business-knowledge只读Skill：共享原文的读取先验证本run/Step Snapshot已加载当前方法版本，下一Context注入受保护的方法正文。此方法不复制业务答案，不增加后端路由、MCP代理或聊天模型。模型按原文回答的实际质量见component的[真实模型复核](../../../component/doc/business-operation/knowledge-live-validation-2026-10-17.md)；Skill激活不代表回答正确或知识取得执行资格。

平台壳也可通过同源 `/localdoc/api` 代理显式创建来源：

```typescript
const source = createLocalDocRagKnowledgeSource({
  apiPrefix: '/localdoc/api',
  knowledgeBaseId: 'cb65eb15-c69f-42aa-b53b-a44615b04f0b',
  processing: { retrieval: 'local', model: 'configured_gateway' },
});
```

`processing`描述真实处理方式。两个Agent使用后端已配置MiMo路由时，必要问题和检索正文会进入该Provider请求，不能称全部离线推理。旧 `localProcessingOnly:true` 仅兼容实际本地主模型、摘要、日志和遥测管线，不得用此声明掩盖外发。无需下载/调优额外本地聊天模型或重建已工作的Embedding索引。

同源代理须移除 `/localdoc` 前缀，设置后端实际Host/Origin并保留本地请求头。后端定向契约覆盖无聊天模型检索、通用完整表读取、原件/片段来源、跨库/版本隔离、检索中换版拒绝和跨章节冲突分支保留。主质量验收由两个实际Agent各自使用实际MiMo路由完成，直接API、Mock或管理页聊天不能代替；当前手册与实测见[验收记录](TRAINING-MANUAL.md)，生产Graph开发联调中的成功、失败与复验见[双Agent质量记录](../../../component/doc/business-operation/knowledge-live-validation-2026-10-17.md)和[实施报告](../../../component/doc/business-operation/knowledge-shared-implementation-2026-10-17.md)，不据源码存在宣称通过。

`scripts/verify_component_agent.cjs` 在两个真实TypeScript工具上下文中检查同源搜索与分页读取，不启动模型循环。2026-10-17[只读实测](../data/component-agent-verification-20261017T031832242Z-c157fccd-929f-4984-8cbb-baf8d3bfa990.json)确认两域各4页读完同一4882字符操作表，搜索快照、来源版本、正文hash与来源信息一致；编排侧读取未改变业务运行时读取状态，旧hash拒绝和未审核执行阻断保留。每次运行新增时间戳/UUID报告，`--output`仅允许新文件，历史报告保留。

此前[搜索提示smoke](../data/component-agent-verification-20261017T033826856Z-a7481619-694d-49c4-b2db-9d65a1a05a12.json)重复通过上述双域检查，并增加真实搜索fullTableId/readHint与知识读取目标一致的断言；当时为20次只读请求、两域各4页4882字符。4882为相邻表去重前基线，当前脚本按真实字符数动态核对连续完整分页，仍要求至少两页。原活动v2、索引与历史报告保留，未调用模型。

仅重启API后的[最新smoke](../data/component-agent-verification-20261017T040828431Z-dbe4ce56-4782-429f-9fe9-1356f76083fb.json)确认去重后2735字符，两域各2页、14次只读请求；完整条件、同源版本/hash、搜索提示、状态隔离和未审核执行阻断全部通过。未重启Ollama/Qdrant，未重导入或重建索引。

生产组件构建不包含本服务；独立Vite、Docker和Mock验收各自记录。旧IndexedDB与当前持久库保留，不自动发布；原生数据和Compose卷是不同存储，备份须对应启动模式。正式平台、真实输入、设计发布和业务签认仍分别验收。
