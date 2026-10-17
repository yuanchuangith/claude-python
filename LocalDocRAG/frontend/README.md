# 本地知识库前端

本目录归属后端工程 LocalDocRAG，维护 React / TypeScript / Vite / Tailwind 管理页面。Python API、Word 解析、OCR、索引、数据库和任务位于 [backend](../backend)，本地启动见 [README](../README.md)。

日常打开 `http://127.0.0.1:7860`，原生 API 提供本目录的 `dist/`。组件工程不再保留独立知识库工作台或 Node 启动代理。

单独开发页面：本目录执行 `npm ci`、`npm run dev`，Vite 使用 7861，将 `/api` 同源代理到 7862；先停止现有 API，再在父目录执行 `./scripts/start-native.ps1 -Mode gpu -Port 7862 -ContextLength 8192`。需要真实本机 Ollama 11434 和 Qdrant 6333，不同时启动多个 API 操作同一数据库。

定向检查使用 `npm run check`。本目录的独立 Vite 构建使用 `npm run build`，不构建 component 组件工程。
