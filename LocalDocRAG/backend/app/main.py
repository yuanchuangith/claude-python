import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.core.config import Settings
from app.core.errors import AppError, public_error
from app.core.model_queue import ModelQueue
from app.db.database import Database, dump, now, uid
from app.ingest.word import validate_upload
from app.jobs.worker import Worker, clone_version, enqueue
from app.rag.models import Ollama, Qdrant
from app.rag.service import RAG
from app.schemas.requests import Activate, AgentSearch, Chat, KnowledgeBase, ModelSettings, Rebuild, Search

log = logging.getLogger("localdoc")


def create_app(settings=None, models=None, vectors=None):
    cfg = settings or Settings()
    db = Database(cfg.database)

    @asynccontextmanager
    async def lifespan(app):
        db.migrate()
        saved = db.one("SELECT value FROM settings WHERE key='model_settings'")
        if saved:
            apply_settings(json.loads(saved["value"]))
        if cfg.worker:
            await worker.start()
        yield
        await worker.stop()
        await rag.models.close()
        await rag.vectors.close()

    app = FastAPI(title="LocalDocRAG", version="1.0.0", lifespan=lifespan)
    rag = RAG(cfg, db, models or Ollama(cfg, ModelQueue(db, cfg.timeout)), vectors or Qdrant(cfg))
    worker = Worker(cfg, db, rag)
    app.state.db, app.state.rag, app.state.worker, app.state.settings = db, rag, worker, cfg

    def apply_settings(value):
        cfg.chat_model, cfg.vision_model = value["chat_model"], value["vision_model"]
        cfg.num_ctx, cfg.ocr, cfg.caption = value["context_length"], value["ocr"], value["caption"]

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        request.state.request_id = uid()
        # Browser same-origin + DNS rebinding protection, including LAN port rebinding.
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "testserver", "api"}:
            return JSONResponse({"code": "LOCAL_ONLY", "message": "仅允许本机访问。", "request_id": request.state.request_id}, 403)
        origin = request.headers.get("origin")
        expected = "http://" + request.headers.get("host", "")
        if origin and origin != expected:
            return JSONResponse({"code": "ORIGIN_REJECTED", "message": "不允许跨站点请求。", "request_id": request.state.request_id}, 403)
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            return JSONResponse({"code": "ORIGIN_REJECTED", "message": "请从本地工作台访问。", "request_id": request.state.request_id}, 403)
        if request.method in {"POST", "PATCH", "DELETE", "PUT"} and request.headers.get("x-localdoc-request") != "1":
            return JSONResponse({"code": "LOCAL_HEADER_REQUIRED", "message": "缺少本地请求标记。", "request_id": request.state.request_id}, 403)
        # A rebuild snapshots every active document. Serialize mutations against
        # that snapshot; never swap a new global profile with old-model uploads.
        if request.method in {"POST", "PATCH", "DELETE"} and (
            request.url.path.startswith("/api/documents/") or request.url.path.startswith("/api/knowledge-bases/")
        ) and db.one("SELECT 1 FROM jobs WHERE type='rebuild' AND status IN ('queued','running')"):
            return JSONResponse({"code": "REBUILD_IN_PROGRESS", "message": "全量重建期间请等待任务完成后再修改文档。", "request_id": request.state.request_id}, 409)
        started = time.monotonic()
        response = await call_next(request)
        response.headers.update({"X-Request-ID": request.state.request_id, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
        log.info("request=%s method=%s status=%s elapsed_ms=%d", request.state.request_id, request.method, response.status_code, int((time.monotonic() - started) * 1000))
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse({**public_error(exc), "request_id": request.state.request_id}, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        return JSONResponse({"code": "INVALID_REQUEST", "message": "请求参数不正确。", "details": [{"loc": e["loc"], "type": e["type"]} for e in exc.errors()], "request_id": request.state.request_id}, 422)

    @app.exception_handler(Exception)
    async def unknown_error(request, exc):
        log.error("request=%s exception=%s", request.state.request_id, type(exc).__name__)
        return JSONResponse({**public_error(exc), "request_id": request.state.request_id}, 500)

    def kb(kid):
        row = db.one("SELECT * FROM knowledge_bases WHERE id=? AND deleted_at IS NULL", (str(kid),))
        if not row:
            raise AppError("KNOWLEDGE_BASE_NOT_FOUND", "知识库不存在。", 404)
        return row

    def document(did):
        row = db.one("SELECT d.* FROM documents d JOIN knowledge_bases k ON k.id=d.kb_id WHERE d.id=? AND d.deleted_at IS NULL AND k.deleted_at IS NULL", (str(did),))
        if not row:
            raise AppError("DOCUMENT_NOT_FOUND", "文档不存在或已删除。", 404)
        return row

    def version(did, vid=None):
        doc = document(did)
        vid = str(vid) if vid else doc["active_version_id"]
        row = db.one("SELECT * FROM document_versions WHERE document_id=? AND id=?", (doc["id"], vid))
        if not row and not vid:
            row = db.one("SELECT * FROM document_versions WHERE document_id=? ORDER BY version_no DESC LIMIT 1", (doc["id"],))
        if not row:
            raise AppError("VERSION_NOT_FOUND", "文档版本不存在。", 404)
        return doc, row

    @app.get("/api/health")
    async def health():
        db.one("SELECT 1")
        evidence_ready = chat_ready = image_caption_ready = False
        try:
            await rag.vectors.request("GET", "/collections")
            tags = await rag.models.tags()
            available = {m.get("name") for m in tags}
            # Agents synthesize evidence through their existing model gateway.
            # Local chat/vision models are optional to the retrieval/read path.
            evidence_ready = (rag.profile() or {}).get("model_name", cfg.embed_model) in available
            chat_ready = evidence_ready and cfg.chat_model in available
            image_caption_ready = cfg.vision_model in available
        except AppError:
            pass
        return {"status": "ok", "ready": evidence_ready, "evidence_ready": evidence_ready,
                "chat_ready": chat_ready, "image_caption_ready": image_caption_ready, "version": "1.0.0"}

    @app.get("/api/system/status")
    @app.get("/api/system/models")
    async def system():
        result = {"sqlite": "ready", "mode": cfg.mode, "gpu_status": "检测失败", "models": [], "ollama": "unavailable", "qdrant": "unavailable",
                  "free_bytes": shutil.disk_usage(cfg.data).free, "queue": db.all("SELECT priority,count(*) AS count FROM model_queue GROUP BY priority"),
                  "profile": rag.profile(), "settings": {"chat_model": cfg.chat_model, "vision_model": cfg.vision_model, "embedding_model": cfg.embed_model, "context_length": cfg.num_ctx, "ocr": cfg.ocr, "caption": cfg.caption},
                  "warnings": [], "jobs": db.all("SELECT status,count(*) AS count FROM jobs GROUP BY status")}
        try:
            result["models"] = await rag.models.tags()
            loaded = await rag.models.loaded()
            result["loaded_models"], result["ollama"] = loaded, "ready"
            # Never claim GPU based on host nvidia-smi. size_vram is Ollama's actual loaded allocation.
            result["gpu_status"] = "CPU 模式" if cfg.mode == "cpu" else ("GPU 运行" if any(m.get("size_vram", 0) > 0 for m in loaded) else "尚未确认 GPU 推理")
        except AppError as exc:
            result["warnings"].append(exc.message)
        try:
            await rag.vectors.request("GET", "/collections")
            result["qdrant"] = "ready"
        except AppError as exc:
            result["warnings"].append(exc.message)
        import importlib.util
        result["ocr"] = "CPU / rapidocr" if importlib.util.find_spec("rapidocr") else "unavailable"
        result["docker"] = "容器内运行" if Path("/.dockerenv").exists() else "原生开发模式，Docker状态由启动脚本检查"
        return result

    @app.patch("/api/system/settings")
    async def change_settings(value: ModelSettings):
        if worker.current:
            raise AppError("WORKER_BUSY", "请等待当前文档阶段结束后修改模型配置。", 409)
        available = {m["name"] for m in await rag.models.tags()}
        if value.chat_model not in available or value.vision_model not in available:
            raise AppError("MODEL_NOT_READY", "所选模型尚未下载。", 409)
        apply_settings(value.model_dump())
        db.execute("INSERT OR REPLACE INTO settings VALUES('model_settings',?)", (dump(value.model_dump()),))
        return {"status": "saved"}

    @app.post("/api/system/rebuild")
    async def rebuild(value: Rebuild):
        return {"job_id": enqueue(db, "rebuild", "all", {"model": value.model})}

    @app.get("/api/knowledge-bases")
    async def list_kbs():
        return db.all("SELECT k.*,(SELECT count(*) FROM documents d WHERE d.kb_id=k.id AND d.deleted_at IS NULL) AS document_count,(SELECT count(*) FROM chunks c JOIN documents d ON d.active_version_id=c.version_id WHERE d.kb_id=k.id AND d.deleted_at IS NULL) AS chunk_count FROM knowledge_bases k WHERE k.deleted_at IS NULL ORDER BY k.created_at")

    @app.post("/api/knowledge-bases", status_code=201)
    async def create_kb(value: KnowledgeBase):
        kid = uid()
        db.execute("INSERT INTO knowledge_bases(id,name,description,created_at,updated_at) VALUES(?,?,?,?,?)", (kid, value.name, value.description, now(), now()))
        return kb(kid)

    @app.patch("/api/knowledge-bases/{kid}")
    async def update_kb(kid: UUID, value: KnowledgeBase):
        kb(kid)
        db.execute("UPDATE knowledge_bases SET name=?,description=?,updated_at=? WHERE id=?", (value.name, value.description, now(), str(kid)))
        return kb(kid)

    @app.delete("/api/knowledge-bases/{kid}")
    async def delete_kb(kid: UUID, confirm: bool = False):
        kb(kid)
        if not confirm:
            raise AppError("CONFIRM_REQUIRED", "请确认删除知识库及其中的文档。", 409)
        docs = db.all("SELECT id FROM documents WHERE kb_id=? AND deleted_at IS NULL", (str(kid),))
        with db.tx(immediate=True) as conn:
            conn.execute("UPDATE knowledge_bases SET deleted_at=? WHERE id=?", (now(), str(kid)))
            conn.execute("UPDATE documents SET deleted_at=? WHERE kb_id=?", (now(), str(kid)))
        return {"jobs": [enqueue(db, "delete", d["id"]) for d in docs]}

    @app.get("/api/knowledge-bases/{kid}/documents")
    async def documents(kid: UUID):
        kb(kid)
        rows = db.all("SELECT d.* FROM documents d WHERE kb_id=? AND deleted_at IS NULL ORDER BY created_at DESC", (str(kid),))
        for d in rows:
            d["versions"] = db.all("SELECT id,version_no,sha256,status,created_at FROM document_versions WHERE document_id=? ORDER BY version_no DESC", (d["id"],))
            d["jobs"] = db.all("SELECT j.* FROM jobs j JOIN document_versions v ON v.id=j.target_id WHERE v.document_id=? ORDER BY j.created_at DESC", (d["id"],))
            for j in d["jobs"]:
                j["last_error"] = json.loads(j["last_error"]) if j["last_error"] else None
        return rows

    @app.post("/api/knowledge-bases/{kid}/documents", status_code=202)
    async def upload(kid: UUID, files: list[UploadFile] = File(...)):
        kb(kid)
        results = []
        if len(files) > 20:
            raise AppError("TOO_MANY_FILES", "每批最多上传20个Word文件。")
        for upload in files:
            name = (upload.filename or "document.docx").replace("\\", "/").split("/")[-1][:240]
            suffix = Path(name).suffix.lower()
            data = bytearray()
            while part := await upload.read(1024 * 1024):
                data.extend(part)
                if len(data) > cfg.max_upload:
                    raise AppError("UPLOAD_TOO_LARGE", "文件超过大小限制。", 413)
            await asyncio.to_thread(validate_upload, data, suffix, cfg)
            digest = hashlib.sha256(data).hexdigest()
            with db.tx(immediate=True) as conn:
                doc = conn.execute("SELECT * FROM documents WHERE kb_id=? AND original_name=? AND deleted_at IS NULL", (str(kid), name)).fetchone()
                did = doc["id"] if doc else uid()
                if not doc:
                    conn.execute("INSERT INTO documents(id,kb_id,original_name,created_at) VALUES(?,?,?,?)", (did, str(kid), name, now()))
                prior = conn.execute("SELECT * FROM document_versions WHERE document_id=? AND sha256=? ORDER BY version_no DESC LIMIT 1", (did, digest)).fetchone()
                if prior:
                    existing = conn.execute("SELECT id FROM jobs WHERE target_id=? ORDER BY created_at DESC LIMIT 1", (prior["id"],)).fetchone()
                    results.append({"document_id": did, "version_id": prior["id"], "job_id": existing[0] if existing else None, "deduplicated": True})
                    continue
                vid = uid()
                folder = cfg.data / "versions" / vid
                folder.mkdir(parents=True)
                filepath = folder / ("original" + suffix)
                filepath.write_bytes(data)
                number = conn.execute("SELECT coalesce(max(version_no),0)+1 FROM document_versions WHERE document_id=?", (did,)).fetchone()[0]
                conn.execute("INSERT INTO document_versions(id,document_id,version_no,sha256,file_path,status,created_at) VALUES(?,?,?,?,?,'queued',?)", (vid, did, number, digest, str(filepath), now()))
            results.append({"document_id": did, "version_id": vid, "job_id": enqueue(db, "ingest", vid), "deduplicated": False})
        return {"items": results}

    @app.get("/api/documents/{did}/versions")
    async def versions(did: UUID):
        document(did)
        return db.all("SELECT id,version_no,sha256,status,created_at,embedding_profile_id FROM document_versions WHERE document_id=? ORDER BY version_no DESC", (str(did),))

    @app.get("/api/documents/{did}/preview")
    async def preview(did: UUID, version_id: UUID | None = None):
        doc, ver = version(did, version_id)
        if not ver["canonical_path"] or not Path(ver["canonical_path"]).is_file():
            return {"document": doc, "version": {"id": ver["id"], "status": ver["status"]}, "blocks": [], "tables": [], "media": [], "report": {"warnings": []}}
        canonical = json.loads(Path(ver["canonical_path"]).read_text(encoding="utf-8"))
        for media in canonical["media"]:
            media.pop("storage_path", None)
            media["url"] = "/api/assets/" + media["id"]
        report = json.loads(Path(ver["report_path"]).read_text(encoding="utf-8"))
        return {**canonical, "document": doc, "version": {"id": ver["id"], "status": ver["status"], "sha256": ver["sha256"], "version_no": ver["version_no"]}, "report": report}

    @app.get("/api/documents/{did}/download")
    async def download(did: UUID, version_id: UUID | None = None):
        doc, ver = version(did, version_id)
        return FileResponse(ver["file_path"], filename=doc["original_name"], media_type="application/octet-stream")

    @app.post("/api/documents/{did}/reindex")
    async def reindex(did: UUID):
        doc = document(did)
        original = db.one("SELECT * FROM document_versions WHERE document_id=? ORDER BY version_no DESC LIMIT 1", (doc["id"],))
        if not original:
            raise AppError("VERSION_NOT_FOUND", "没有可重新索引的版本。", 404)
        if original["status"] in {"failed", "queued"}:
            vid = original["id"]
        else:
            vid = clone_version(db, cfg, original)
        return {"job_id": enqueue(db, "ingest", vid), "version_id": vid}

    @app.post("/api/documents/{did}/activate")
    async def activate(did: UUID, value: Activate):
        _, ver = version(did, value.version_id)
        profile = rag.profile()
        if ver["status"] not in {"completed", "completed_with_warnings"} or not profile or ver["embedding_profile_id"] != profile["id"]:
            raise AppError("VERSION_NOT_READY", "该版本未完成索引或模型不同，请重新索引后恢复。", 409)
        db.execute("UPDATE documents SET active_version_id=? WHERE id=?", (str(value.version_id), str(did)))
        return {"status": "activated"}

    @app.delete("/api/documents/{did}")
    async def delete_document(did: UUID, confirm: bool = False):
        document(did)
        if not confirm:
            raise AppError("CONFIRM_REQUIRED", "请确认删除文档及历史版本。", 409)
        db.execute("UPDATE documents SET deleted_at=? WHERE id=?", (now(), str(did)))
        return {"job_id": enqueue(db, "delete", str(did))}

    @app.get("/api/jobs/{jid}")
    async def job(jid: UUID):
        row = db.one("SELECT * FROM jobs WHERE id=?", (str(jid),))
        if not row:
            raise AppError("JOB_NOT_FOUND", "任务不存在。", 404)
        row["last_error"] = json.loads(row["last_error"]) if row["last_error"] else None
        row["warnings"] = json.loads(row.pop("warnings_json"))
        return row

    @app.post("/api/jobs/{jid}/retry")
    async def retry_job(jid: UUID):
        row = await job(jid)
        if row["status"] not in {"failed", "canceled"}:
            raise AppError("JOB_NOT_RETRYABLE", "任务尚在运行或已完成。", 409)
        db.execute("UPDATE jobs SET status='queued',finished_at=NULL WHERE id=?", (str(jid),))
        return {"job_id": str(jid)}

    @app.post("/api/search")
    async def search(value: Search):
        kb(value.knowledge_base_id)
        return await rag.search(str(value.knowledge_base_id), value.query, value.top_k)

    @app.post("/api/agent/search")
    async def agent_search(value: AgentSearch):
        result = await rag.search(str(value.knowledge_base_id), value.query, 20)
        sources = result["fused"]
        version = "sha256:" + hashlib.sha256(dump([str(value.knowledge_base_id), value.query, result.get("active_versions", []),
            [(s["chunk_id"], s["version_id"], s["sha256"]) for s in sources]]).encode()).hexdigest()
        if value.result_version and value.result_version != version:
            raise AppError("KNOWLEDGE_SEARCH_CHANGED", "知识检索结果已变化，请从首页重新检索。", 409)
        hits = []
        for source in sources[value.offset:value.offset + value.limit]:
            hit = {"id": "localdoc:" + source["chunk_id"], "title": source["filename"] + " / " + " > ".join(source["section_path"]), "path": source["preview_url"],
                   "score": source["score"], "snippet": source["excerpt"][:value.max_snippet_chars], "tags": ["localdoc", source["type"]], "updatedAt": 0}
            full_table_id = rag.full_table_id(source)
            if full_table_id:
                hit.update({"fullTableId": full_table_id,
                            "readHint": "Read fullTableId with knowledge_read; follow all nextOffset pages until null."})
            hits.append(hit)
        return {"totalMatches": len(sources), "resultVersion": version, "results": hits}

    @app.get("/api/agent/knowledge/{chunk_id}")
    async def agent_knowledge(chunk_id: UUID, knowledge_base_id: UUID):
        kb(knowledge_base_id)
        snapshot = rag.active_versions(str(knowledge_base_id))
        chunk = db.one("SELECT c.* FROM chunks c JOIN documents d ON d.active_version_id=c.version_id JOIN document_versions v ON v.id=c.version_id WHERE c.id=? AND d.kb_id=? AND d.deleted_at IS NULL AND c.index_status='indexed' AND v.status IN ('completed','completed_with_warnings')", (str(chunk_id), str(knowledge_base_id)))
        chunk = chunk or rag.table_chunk(str(chunk_id), str(knowledge_base_id))
        if not chunk:
            return None
        source = rag.hydrate(chunk)
        # A retrieval chunk is evidence for Q&A, not an approved operation definition.
        content = source["content"] + "\n\n来源：" + source["filename"] + "\n章节：" + " > ".join(source["section_path"]) + "\n位置：" + source["anchor"]
        if source.get("row_cells"):
            content += "\n完整相关表格行及表头：" + dump({"cells": source["row_cells"], "headers": source["headers"]})
        content += "\n相邻说明：" + dump(source["adjacent_blocks"])
        if source.get("image"):
            content += "\n图片OCR及视觉证据（待核对告警）：" + dump(source["image"])
        full_table_id = rag.full_table_id(source)
        if full_table_id:
            # Let an agent move from a specific row to its full operation chain
            # using the same existing knowledge_read tool and active-version fence.
            content += "\n完整原文表（须继续读取全部分页；跨章节规则需另行检索）：" + full_table_id
        if snapshot != rag.active_versions(str(knowledge_base_id)):
            raise AppError("KNOWLEDGE_VERSION_CHANGED", "读取期间活动知识版本已变化，请重新检索。", 409)
        return {"id": "localdoc:" + str(chunk_id), "title": source["filename"] + " / " + " > ".join(source["section_path"]),
                "content": content, "path": source["preview_url"], "tags": ["localdoc"], "updatedAt": 0,
                "metadata": {"sourceVersion": chunk["version_id"], "contentPurpose": "business_knowledge", "sourceKind": "business_manual",
                             "consumerAgentIds": ["action_design", "business_operation"], "status": "evidence_only", "fullTableId": full_table_id,
                             "dependencies": [], "unresolved": ["原文证据尚未审核为可执行操作；可用于业务理解、编排建议和流程问答，不授予画布写入、发布、页面输入、提交、审批或签名权限。"],
                             "source": {k:source.get(k) for k in ["document_id", "document_sha256", "chunk_id", "sha256", "version_id", "filename", "section_path", "block_id", "anchor", "preview_url", "table_id", "row", "col", "image_id", "warnings", "fs_ids", "table_complete", "operation_complete"]}}}

    @app.post("/api/chat/stream")
    async def chat(value: Chat, request: Request):
        kb(value.knowledge_base_id)
        async def events():
            def event(name, data):
                return f"event: {name}\ndata: {dump(data)}\n\n"
            # Heartbeats keep proxy/browser alive during a local 4B model call.
            task = asyncio.create_task(rag.answer(str(value.knowledge_base_id), value.question, str(value.conversation_id) if value.conversation_id else None, value.top_k))
            try:
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=10)
                    if not done:
                        yield ": 正在本地检索和生成\n\n"
                result = await task
                yield event("sources", {"items": result["sources"]})
                # Validate citations BEFORE showing model text; then progressively render.
                for start in range(0, len(result["answer"]), 24):
                    yield event("token", {"text": result["answer"][start:start+24]})
                    await asyncio.sleep(0)
                yield event("done", {k: v for k, v in result.items() if k not in {"sources", "answer"}} | {"status": "completed"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield event("error", {**public_error(exc), "request_id": request.state.request_id})
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/assets/{mid}")
    async def asset(mid: UUID):
        row = db.one("SELECT m.* FROM media m JOIN document_versions v ON v.id=m.version_id JOIN documents d ON d.id=v.document_id JOIN knowledge_bases k ON k.id=d.kb_id WHERE m.id=? AND d.deleted_at IS NULL AND k.deleted_at IS NULL", (str(mid),))
        if not row:
            raise AppError("ASSET_NOT_FOUND", "图片不存在或原件已删除。", 404)
        supported = row["mime"] in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
        return FileResponse(row["storage_path"], media_type=row["mime"] if supported else "application/octet-stream", filename=None if supported else "original-media" + Path(row["storage_path"]).suffix,
                            headers={"Content-Security-Policy": "default-src 'none'; sandbox"})

    @app.get("/api/conversations")
    async def conversations(knowledge_base_id: UUID):
        kb(knowledge_base_id)
        return db.all("SELECT c.*,(SELECT content FROM messages m WHERE m.conversation_id=c.id AND m.role='user' ORDER BY created_at LIMIT 1) AS title FROM conversations c WHERE kb_id=? ORDER BY updated_at DESC", (str(knowledge_base_id),))

    @app.get("/api/conversations/{cid}")
    async def conversation(cid: UUID):
        row = db.one("SELECT * FROM conversations WHERE id=?", (str(cid),))
        if not row:
            raise AppError("CONVERSATION_NOT_FOUND", "会话不存在。", 404)
        kb(row["kb_id"])
        messages = db.all("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (str(cid),))
        for message in messages:
            message["sources"] = [json.loads(c["snapshot_json"]) for c in db.all("SELECT snapshot_json FROM message_citations WHERE message_id=?", (message["id"],))]
            for source in message["sources"]:
                source["original_deleted"] = not bool(db.one("SELECT 1 FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=? AND d.deleted_at IS NULL", (source["version_id"],)))
        return {**row, "messages": messages}

    return app


if os.getenv("LOCALDOC_NO_GLOBAL_APP") != "1":
    app = create_app()
