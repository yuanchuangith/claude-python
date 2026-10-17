# LocalDocRAG 本地知识库

> 最新需求：两个前端 Agent 共用同一后端业务知识库——编排助手理解业务逻辑，用于生成、检查、解释和修复；操作助手理解完整操作流程。两者复用既有后端 MiMo 2.6 Pro，见[最新需求](../../component/doc/business-operation/knowledge-requirements-2026-10-17.md)。2026-10-17 用户已恢复实施，两个 Agent 通过既有8888网关只读代理访问同一活动知识源。实际验证与边界见[共享知识验收报告](../../component/doc/business-operation/knowledge-shared-implementation-2026-10-17.md)。独立本地聊天模型非Agent接入前置条件。

Word 导入、图文表解析、混合检索、带原文引用的本地问答，早期实现依据 [PRD](PRD.md)，后续范围以上方最新需求为准。Python 服务和独立 React 管理页都在本后端工程；组件工程保留两个现有 Agent 的必要 TypeScript 接线。

当前共享接线、真实完整读取与权限隔离已有证据；两个现有Agent还各自接入business-knowledge只读Skill，实际MiMo主请求已确认加载及受保护方法注入。20个新增/受影响定向回归文件通过，严格类型检查无新增诊断；多批回答仍有许可加强、状态逆推与补证扩张，核心规则运用质量未全部通过。详情及逐场结果见上方共享知识验收报告，独立服务测试或Skill加载成功不能替代两个Agent的质量验收。

默认地址 **http://127.0.0.1:7860**。现有独立本地问答实现将文档、图片和问题在本机处理，历史验证未经过既有8888网关。当前两个Agent使用既有MiMo路由，必要检索证据可能进入上游Provider请求，不能称为全部离线推理。真实培训手册的版本、知识库 ID、质量核对和限制见 [手册验收记录](docs/TRAINING-MANUAL.md)。

## Windows 本地开发启动

本机 Docker Engine 返回 500，因此本次使用项目内便携 Ollama 0.40.5 / Qdrant 1.14.1，已验证 RTX 2080 Ti 的实际推理。无需修复 Docker 才能继续本地开发，也不修改系统驱动或安装全局服务。

在本目录执行。已有环境从第 4 步启动即可：

```powershell
# 1. 独立 CPython 环境，避免 Anaconda 的 ONNX DLL 冲突
python -m uv sync --project backend --python 3.12.10 --locked
# 2. 官方便携程序，只放在本目录 runtime/
./backend/.venv/Scripts/python.exe scripts/setup_native.py
# 3. 独立管理页面，构建范围仅 frontend/
npm ci --prefix frontend
npm run build --prefix frontend
# 4. 后台启动本机依赖及 API；培训手册使用 8192 上下文
./scripts/start-native.ps1 -Mode gpu -ContextLength 8192
# 5. 首次使用时下载检索模型；已有模型会复用
$env:OLLAMA_HOST='127.0.0.1:11434'
./runtime/ollama-v0.40.5/ollama.exe pull qwen3-embedding:0.6b
# 新导入图片需要本地视觉描述时再下载；读取已有索引不需要
./runtime/ollama-v0.40.5/ollama.exe pull qwen3-vl:2b-instruct
# 仅独立本地问答页需要；两个 Agent 不依赖此模型
./runtime/ollama-v0.40.5/ollama.exe pull qwen3:4b
```

启动脚本遇到已占用端口不会终止其进程。`/api/health` 的 `ready/evidence_ready` 表示检索依赖就绪，`chat_ready/image_caption_ready` 分别表示可选聊天/图片描述能力；启动进程本身不等于模型已下载。显式 CPU 模式用 `-Mode cpu`，需先停止现有 API 和 Ollama；模式/上下文修改需重启 API 才生效。单模型驻留、单并行、CPU OCR；首个请求冷加载可能需要一分钟左右。

本地数据位置：`data/` 保存 SQLite、原件、解析结果与媒体；`runtime/qdrant-data/` 保存向量；`runtime/models/` 保存模型；`runtime/logs/` 保存运行日志。它们均被 Git 忽略，**不要为了重启而删除**。`runtime/native-processes.json` 记录启动器拥有的进程，停止前核对命令行，避免误停其它项目。

## 指定培训手册

```powershell
./backend/.venv/Scripts/python.exe scripts/import_training_manual.py --wait
```

脚本默认读取同级 component 中用户指定的 FS-CPM-001 培训 DOCX，也支持 `--source <完整路径>`。通过真实 API 创建/复用知识库，同名同 SHA256 上传去重；结果保存在 `data/training-manual.json`。已导入手册直接使用，无需每次重传。更新文件后重新上传，完整索引校验成功后才原子切换活动版本，旧版本仍可核对。

```powershell
# 只检索实际手册，不调用本地聊天模型
./backend/.venv/Scripts/python.exe scripts/evaluate_training_manual.py
# 复用component真实工具，检查两个Agent上下文同源同版本读取
node scripts/verify_component_agent.cjs
# 可选：单独评估本地问答页；不是MiMo Agent验收
./backend/.venv/Scripts/python.exe scripts/evaluate_training_manual.py --chat
```

手册检索/本地问答结果在 `data/training-evaluation.json`，不提交原文和答案全文。`--refresh` 重跑全部，`--only entry signature` 缩小范围。该脚本是验收工具，业务回答不使用预置答案。component工具验证默认新增带时间戳与UUID的 `data/component-agent-verification-*.json`；`--output new-report.json` 可指定新文件，已有报告拒绝覆盖。它不调用MiMo，真实Graph质量见上方验收报告。

## 管理页面与业务 Agent

两个前端Agent共用只读业务事实，编排侧保留原有开发知识并可明确选择业务/开发来源。共享知识不合并两者的Registry、Graph、状态、历史和恢复，也不改变画布与页面各自的执行权限。完成标准以两个实际Agent分别使用知识的验收为准，管理页聊天仅为辅助。

页面提供知识库、上传、持久任务、版本、预览、检索调试、问答、来源跳转和诊断。7860 问答页仍为可选本地聊天功能；两个 Agent 使用既有 MiMo 路由，无证据时承认缺少信息。引用保留源哈希、版本、章节、FS 条款、表格行和原文锚点，不猜 DOCX 页码。

默认接线使用既有模型网关。在后端根目录被忽略的 `.env.local.json` 中配置两个非密钥项，随后运行 `./start-local.ps1 -Background`：

```json
{
  "ACTIONDESIGN_LOCALDOC_BASE_URL": "http://127.0.0.1:7860",
  "ACTIONDESIGN_LOCALDOC_KNOWLEDGE_BASE_ID": "cb65eb15-c69f-42aa-b53b-a44615b04f0b"
}
```

网关复用现有认证提供 `/api/actiondesign-agent/knowledge/config`、`/knowledge/search`、`/knowledge/documents/{id}`。前端编排服务和业务挂载入口使用同一网关配置自动注入源；显式 `knowledgeSource` 仍可替换。代理只访问固定本机服务和配置的知识库，不转发浏览器凭据或提供导入/发布接口。源工厂从 component 的 web/webPreview 导出；已有 `/localdoc/api` 同源接线可保留，但必须如实声明模型处理方式。具体示例见[接线说明](docs/ARCHITECTURE.md)。证据标记 evidence_only，可用于业务理解、编排建议和流程问答；执行与签名另有门禁。

## Docker 和维护

Docker 正常的环境可执行 [start.bat](start.bat) 或 Linux `bash install.sh`；CPU 模式 `start.bat -Mode cpu`。Compose 默认从本目录 frontend/ 构建管理页，可用 LOCALDOC_FRONTEND_DIR 覆盖。容器、GPU 和完整合成烟测的验收与本次原生验收分别记录，不混称已通过。

scripts/doctor.ps1、backup.ps1、restore.ps1 等原有脚本面向 **Compose 命名卷**，不要用它们假定已经备份原生目录。原生完整迁移须先停止 API/Qdrant，再复制 data/ 与 runtime/qdrant-data/，保留 runtime/models/ 或模型 digest 清单；恢复到同一模型和解析版本。不得删除持久卷。

## 定向开发检查

```powershell
./backend/.venv/Scripts/python.exe -m pytest backend/tests -q
npm run check --prefix frontend
docker compose -f compose.yaml -f compose.cpu.yaml config --quiet
docker compose -f compose.yaml -f compose.gpu.yaml config --quiet
```

未运行 component 工程全局构建。更多内容：[API](docs/API.md)、[架构](docs/ARCHITECTURE.md)、[排错](docs/TROUBLESHOOTING.md)、[开发验收](docs/ACCEPTANCE.md)、[页面开发](frontend/README.md)。

