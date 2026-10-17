# 本地开发验收

日期：2026-10-17，Asia/Shanghai。独立服务早期需求见 [PRD](../PRD.md)，双Agent最新范围见[需求](../../../component/doc/business-operation/knowledge-requirements-2026-10-17.md)及[实施报告](../../../component/doc/business-operation/knowledge-shared-implementation-2026-10-17.md)。指定真实手册的版本、来源、逐题质量和限制由 [手册验收](TRAINING-MANUAL.md) 维护。当前服务已在本机真实运行；正式平台部署和真实业务签认分别验收。

**双Agent共享接线已实现，最终MiMo复验已结束，核心质量未全部通过。** 后端28项及真实双域证据读取通过只证明本服务范围；解释/流程的Review漏检、生成建议未收敛和修复补证超上下文等结果以[真实模型记录](../../../component/doc/business-operation/knowledge-live-validation-2026-10-17.md)为准，不登记整项需求验收完成。

## 工程归属与运行环境

Python服务、React管理页、数据库、模型与脚本均在后端 claude-python/LocalDocRAG，分别为 backend/、frontend/、data/ 和 runtime/。component只保留TypeScript业务知识源接线；旧重复工作台和脚本已移出，源Word和无关已有修改保留。未运行component全局构建，也未提交、推送或部署。

本机Docker Engine持续500，因此真实验收使用项目内便携Ollama0.40.5和Qdrant1.14.1；这不等于容器端到端已验证。RTX2080Ti 11GB已完成真实嵌入、视觉和聊天推理，Ollama报告实际size_vram大于0。单模型驻留、单并行、CPU OCR，上下文8192。Python为独立CPython3.12.10，避免Anaconda的ONNX DLL冲突。

## 已执行检查

| 范围 | 结果与证据 |
| --- | --- |
| 历史独立服务套件 | 22 passed，16.86s；包括解析安全、真实CPU OCR、版本原子切换、索引失败、取消串行、跨库、引用抑制、完整操作表、死进程队列恢复及证据去重 |
| 本轮后端定向套件 | 28 passed，19.00s；无本地聊天模型读证据、普通非FS整表与表头、搜索fullTableId/readHint、来源hash、活动版本及跨库隔离、跨章节冲突保留；新增相邻说明排除整表自身/嵌套后代、保留前后说明且不越过独立表 |
| 管理页 | npm run check --prefix frontend及本服务独立Vite构建通过 |
| component回归 | LocalDocRagKnowledgeSource、BusinessOperationSafety、BusinessRecoveryIsolation三个脚本通过 |
| 历史业务域工具 | component真实TS工厂和工具访问7860，4页4843字符完整读取，版本变化拒绝，未审核执行阻断；data/component-agent-verification.json，仅覆盖业务域 |
| 本轮双域真实工具 | [搜索提示smoke](../data/component-agent-verification-20261017T033826856Z-a7481619-694d-49c4-b2db-9d65a1a05a12.json)：搜索fullTableId/readHint真实生效；去除相邻表重复前，两域同源同版本各4页4882字符、20次只读请求；状态隔离、旧hash拒绝与evidence_only执行阻断通过，不替代MiMo Graph质量验收 |
| 去冗余后双域工具 | [最新smoke](../data/component-agent-verification-20261017T040828431Z-dbe4ce56-4782-429f-9fe9-1356f76083fb.json)：两域各2页2735字符、14次只读请求；全部条件、同源版本/hash、搜索整表目标、状态隔离、旧hash拒绝与evidence_only门禁通过，旧报告保留；仅重启API，无导入/重建 |
| 真实导入 | 指定Word活动v2，1528/1528索引、4媒体OCR/VLM完成、170表；详见手册验收，明确保留绘图回退等告警 |
| 检索与问答 | 13题检索预期匹配；答案另行逐题核对，检索通过数不可当准确率，最终判断见手册验收 |
| 类型检查范围 | 上轮component四个核心改动TS文件范围语义诊断0，依赖既有诊断567；七文件语法0，不声称全项目类型检查通过 |
| Compose | CPU/GPU overlay配置检查通过；未启动容器、未验证Docker备份恢复 |

第三方Starlette/AnyIO有弃用警告，前端旧Browserslist数据有提示；本轮不改无关依赖。早期Mock浏览器截图和native-diagnostic-result等只代表当时的合成验证，不代表当前真实模型状态。

## 浏览器与复现

真实服务为 **http://127.0.0.1:7860**。文档预览已验证170表、5次图片引用可加载、页面无横向溢出；问答与点击来源的最终复测结果记录在手册验收。旧7863页面是Mock，不计真实验收。

在LocalDocRAG目录：

```powershell
./scripts/start-native.ps1 -Mode gpu -ContextLength 8192
Invoke-RestMethod http://127.0.0.1:7860/api/health
./backend/.venv/Scripts/python.exe -m pytest backend/tests -q
npm run check --prefix frontend
./backend/.venv/Scripts/python.exe scripts/evaluate_training_manual.py --chat --refresh
node scripts/verify_component_agent.cjs
```

## 保留边界

- 真实手册不包含平台运行截图；无法直接解析的绘图/OLE保留告警、原件和锚点，不推断Word页码。
- 问答是模型基于来源的解释，引用校验只校验来源归属；模型可能遗漏条件，复杂任务仍需读取完整原文核对。
- 2026-10-17追加两个前端Agent各自注册business-knowledge只读Skill并验证实际MiMo主请求中的激活版本和受保护方法正文。同一事实对照读取的表25/67 ID、活动v2及正文哈希一致，核心审批状态回答一致；两答仍有将充分条件加强为必要条件的问题，复杂场景也有过度检索，不能登记全部质量通过。逐场证据见[共享知识复核](../../../component/doc/business-operation/knowledge-live-validation-2026-10-17.md#追加共用业务知识skill与效果复核)。20个受影响定向回归及零新增类型诊断只证明各自开发范围。
- 证据为evidence_only；两个现有Agent已默认通过8888只读网关注入同一知识源，正式平台挂载、受控操作包审核发布、真实页面输入、电子签名与业务签认仍分别验收。
- 当前两个Agent复用既有MiMo路由，问题及必要原文可进入实际Provider；processing声明须反映真实管线。旧localProcessingOnly仅适用于确实本地的主模型/摘要/日志管线，该声明不能改变云Provider。
- 原生数据在data/和runtime/；Compose备份/恢复脚本操作命名卷，不代表原生数据备份完成。干净容器一键安装、完整快照恢复、旧DOC转换及Docling可选增强尚未做本机端到端验收。
