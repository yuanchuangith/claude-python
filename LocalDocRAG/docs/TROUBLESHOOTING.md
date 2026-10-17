# 排错

| 情况/错误码 | 处理 |
| --- | --- |
| Docker不存在/daemon500或npipe不存在 | 本机开发可直接按README使用项目内便携原生服务，无需修复Docker；容器环境另查Desktop/WSL2和doctor，客户端存在不等于Engine就绪。 |
| Compose缺失/版本旧 | 安装Compose v2以上；启动入口提前检查。 |
| GPU Docker测试失败 | Windows核对WSL2/GPU驱动，Linux安装NVIDIA Container Toolkit；或明确使用CPU模式。程序不会安装驱动/修改系统策略。 |
| GPU_OUT_OF_MEMORY | 保留任务，降低4096上下文/图片尺寸，等当前调用结束后重试，或停止服务后明确选择CPU模式。 |
| MODEL_NOT_READY / OLLAMA_UNAVAILABLE | 重跑安装入口继续下载3个明确标签；检查本地Ollama及磁盘/镜像网络。已有数据不清空。 |
| QDRANT_UNAVAILABLE / INDEX_VERIFY_FAILED | 检查Qdrant，重试任务；活动指针保持旧版本，向量幂等重写和回读校验。 |
| VECTOR_DIMENSION_MISMATCH / MODEL_DIGEST_CHANGED | 禁止写入原集合，使用全量重建生成新profile。 |
| ZIP_LIMIT / ZIP_RATIO_LIMIT / UNSAFE_ARCHIVE | 压缩包展开/条目/比率/路径异常，拒绝解析；确认文件合法后重新生成。 |
| MACRO_NOT_ALLOWED / CORRUPT_OOXML | 不执行宏；检查加密、损坏或错误文件格式。 |
| DOC_CONVERSION_FAILED / LIBREOFFICE_UNAVAILABLE | 容器包含LibreOffice；原生模式需安装。保留原件，可另存标准DOCX后重试。 |
| unsupported_visual / RENDER_FALLBACK_FAILED | 图表/SmartArt/OLE记录未支持节点；尝试LibreOffice/Poppler页级回退。Windows EMF/WMF可由Pillow/GDI转PNG（VECTOR_IMAGE_RASTERIZED），原件保留。仍失败时下载原件核对，不把告警当识别成功。 |
| OCR_FAILED / VISION_FAILED | 原图可查看，文档可带警告完成。模型恢复后重新索引；未完成图像不能靠模型猜测补数字。 |
| OCR_VISION_CONFLICT / OCR_LOW_CONFIDENCE | 查看两种输出和原图；数值优先原生表格/清晰OCR，方向不清时承认不确定。 |
| Anaconda下ONNX DLL load failed | 使用项目独立CPython3.12.10（uv sync --python 3.12.10）；本机已验证能解决此冲突，避免改用户全局环境。 |
| 7860被占用 | 修改.env APP_PORT，不会终止占用它的进程。若运行了原生诊断服务器，先Ctrl+C再用Docker入口。 |
| LOCAL_ONLY / ORIGIN_REJECTED | 固定用127.0.0.1及正确端口，跨站/LAN访问不属于V1。不通过关闭门禁来暴露文件。 |
| REBUILD_IN_PROGRESS | 全部活动版本重建期间等待完成；失败可重试，旧版本仍有效。 |
| 引用校验失败 | 答案不展示，重新提问或打开检索调试查看原文。来源ID必须属于本次提交证据。 |

默认日志只记录请求ID、阶段、状态、耗时和异常类型，不写全文和图片。备份/恢复失败不要删除持久卷。恢复前自动生成安全备份；失败保持服务停止，按manifest修复后重试。

本地单用户服务不支持公网或LAN认证。不要仅修改端口绑定扩大访问范围。所有知识内容都是不可信来源，模型没有命令执行能力。
