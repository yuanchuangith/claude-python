import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from app.core.errors import AppError, public_error
from app.db.database import dump, now, uid
from app.ingest.images import ImageProcessor
from app.ingest.word import PARSER_VERSION, WordParser
from app.rag.chunks import make_chunks

log = logging.getLogger("localdoc.jobs")
TERMINAL = {"completed", "completed_with_warnings", "failed", "canceled"}


def enqueue(db, kind, target, payload=None):
    existing = db.one("SELECT id FROM jobs WHERE type=? AND target_id=? AND status IN ('queued','running')", (kind, target))
    if existing:
        return existing["id"]
    jid = uid()
    db.execute("INSERT INTO jobs(id,type,target_id,status,stage,created_at,payload_json) VALUES(?,?,?,'queued','queued',?,?)", (jid, kind, target, now(), dump(payload or {})))
    return jid


class Worker:
    def __init__(self, settings, db, rag):
        self.settings, self.db, self.rag = settings, db, rag
        self.images = ImageProcessor(settings, rag.models)
        self.owner = str(os.getpid()) + ":" + uid()
        self.task = None
        self.current = None

    def update(self, jid, stage, completed=0, total=0):
        self.db.execute("UPDATE jobs SET stage=?,completed=?,total=? WHERE id=? AND status='running'", (stage, completed, total, jid))

    def guard(self, version_id):
        row = self.db.one("SELECT v.*,d.kb_id,d.original_name,d.deleted_at FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?", (version_id,))
        if not row or row["deleted_at"]:
            raise AppError("TASK_CANCELED", "文档已删除，停止当前任务。", 409)
        return row

    async def retry(self, operation, jid=None):
        for attempt in range(3):
            try:
                return await operation()
            except (asyncio.CancelledError,):
                raise
            except Exception as exc:
                if jid:
                    self.db.execute("UPDATE jobs SET retries=retries+1,last_error=? WHERE id=?", (dump(public_error(exc)), jid))
                if attempt == 2 or isinstance(exc, AppError) and exc.status < 500:
                    raise
                await asyncio.sleep(.5 * 2 ** attempt)

    def save_parsed(self, version, canonical, report):
        with self.db.tx(immediate=True) as conn:
            for b in canonical["blocks"]:
                conn.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)", (b["id"], version["id"], b["parent_block_id"], b["block_order"], b["type"], dump(b["heading_path"]), b["anchor"], b["plain_text"], dump(b["structured"]), dump(b["warnings"])))
            for t in canonical["tables"]:
                conn.execute("INSERT OR REPLACE INTO tables VALUES(?,?,?,?,?,?)", (t["id"], version["id"], t["block_id"], t["caption"], dump(t["table_json"]), t["sanitized_html"]))
            for m in canonical["media"]:
                conn.execute("INSERT OR IGNORE INTO media(id,version_id,sha256,storage_path,mime,width,height,status,warning_json) VALUES(?,?,?,?,?,?,?,?,?)", (m["id"], version["id"], m["sha256"], m["storage_path"], m["mime"], m["width"], m["height"], m["status"], dump(m["warnings"])))
            for o in canonical["media_occurrences"]:
                conn.execute("INSERT OR REPLACE INTO media_occurrences VALUES(?,?,?,?,?,?,?,?)", tuple(o.values()))
            conn.execute("UPDATE document_versions SET canonical_path=?,report_path=?,parser_version=? WHERE id=?", (str(self.settings.data / "versions" / version["id"] / "canonical_document.json"), str(self.settings.data / "versions" / version["id"] / "parse_report.json"), PARSER_VERSION, version["id"]))

    async def ingest(self, version_id, jid, profile=None, activate=True):
        version = self.guard(version_id)
        output = self.settings.data / "versions" / version_id
        self.update(jid, "parsing")
        if version["canonical_path"] and Path(version["canonical_path"]).is_file():
            canonical = json.loads(Path(version["canonical_path"]).read_text(encoding="utf-8"))
            report = json.loads(Path(version["report_path"]).read_text(encoding="utf-8"))
        else:
            async def parse():
                return await asyncio.to_thread(WordParser(self.settings).parse, Path(version["file_path"]), version_id, output)
            canonical, report = await self.retry(parse, jid)
            self.guard(version_id)
            self.save_parsed(version, canonical, report)
        warnings = list(report["warnings"])
        for index, media in enumerate(canonical["media"]):
            self.guard(version_id)
            self.update(jid, "ocr", index, len(canonical["media"]))
            stored = self.db.one("SELECT * FROM media WHERE id=?", (media["id"],))
            media["ocr"] = json.loads(stored["ocr_json"]) if stored["ocr_json"] else None
            media["vision"] = json.loads(stored["vision_json"]) if stored["vision_json"] else None
            media["warnings"] = list(json.loads(stored["warning_json"]))
            if stored["status"] == "unsupported":
                media["status"] = "unsupported"
                continue
            if self.settings.ocr and media["ocr"] is None:
                try:
                    media["ocr"] = await self.retry(lambda: asyncio.to_thread(self.images.ocr, Path(media["storage_path"])), jid)
                    self.db.execute("UPDATE media SET ocr_json=? WHERE id=?", (dump(media["ocr"]), media["id"]))
                    media["warnings"] = [w for w in media["warnings"] if w != "OCR_FAILED"]
                except Exception as exc:
                    media["warnings"].append("OCR_FAILED")
                    warnings.append({**public_error(exc), "image_id": media["id"], "stage": "ocr"})
            elif not self.settings.ocr:
                media["warnings"].append("OCR_DISABLED")
            self.update(jid, "vision", index, len(canonical["media"]))
            if self.settings.caption and media["vision"] is None:
                try:
                    media["vision"] = await self.retry(lambda: self.images.describe(media), jid)
                    self.db.execute("UPDATE media SET vision_json=? WHERE id=?", (dump(media["vision"]), media["id"]))
                    media["warnings"] = [w for w in media["warnings"] if w != "VISION_FAILED"]
                except Exception as exc:
                    media["warnings"].append("VISION_FAILED")
                    warnings.append({**public_error(exc), "image_id": media["id"], "stage": "vision"})
            elif not self.settings.caption:
                media["warnings"].append("VISION_DISABLED")
            # Numeric contradictions remain explicit, never overwrite OCR with model guesses.
            if media["ocr"] and media["vision"]:
                import re
                ocr_numbers = set(re.findall(r"\d+", media["ocr"].get("text", "")))
                visual_numbers = set(re.findall(r"\d+", dump(media["vision"])))
                if visual_numbers - ocr_numbers:
                    media["warnings"].append("OCR_VISION_CONFLICT")
            media["warnings"] = list(dict.fromkeys(media["warnings"]))
            media["status"] = "completed_with_warnings" if media["warnings"] else "completed"
            self.db.execute("UPDATE media SET status=?,warning_json=? WHERE id=?", (media["status"], dump(media["warnings"]), media["id"]))
            warnings += [{"code": w, "image_id": media["id"]} for w in media["warnings"]]
        report["ocr_success"] = sum(bool(m.get("ocr")) for m in canonical["media"])
        report["warnings"] = warnings
        (output / "canonical_document.json").write_text(dump(canonical), encoding="utf-8")
        (output / "parse_report.json").write_text(dump(report), encoding="utf-8")
        self.update(jid, "indexing")
        profile = profile or await self.rag.ensure_profile()
        if await self.rag.models.digest(profile["model_name"]) != profile["model_digest"]:
            raise AppError("MODEL_DIGEST_CHANGED", "嵌入模型已变化，需要受控全量重建。", 409)
        chunks = make_chunks(canonical, version_id, version["kb_id"], version["original_name"])
        if not chunks:
            raise AppError("DOCUMENT_EMPTY", "没有可索引正文、表格或图片，请检查原件。")
        with self.db.tx() as conn:
            for c in chunks:
                conn.execute("INSERT OR IGNORE INTO chunks VALUES(?,?,?,?,?,?,?,?,?,NULL)", (c["id"], version_id, c["type"], dump(c["block_refs"]), c["content"], dump(c["source_meta"]), c["fts_text"], c["id"], "staging"))
        for start in range(0, len(chunks), self.settings.batch_size):
            self.guard(version_id)
            batch = chunks[start:start + self.settings.batch_size]
            rows = [self.db.one("SELECT * FROM chunks WHERE id=?", (c["id"],)) for c in batch]
            pending = [r for r in rows if not r["embedding_json"]]
            if pending:
                vectors = await self.retry(lambda: self.rag.models.embed([r["content"] for r in pending], profile["model_name"]), jid)
                for row, vector in zip(pending, vectors):
                    if len(vector) != profile["vector_size"]:
                        raise AppError("VECTOR_DIMENSION_MISMATCH", "索引向量维度变化，需要重新建库。", 409)
                    self.db.execute("UPDATE chunks SET embedding_json=? WHERE id=?", (dump(vector), row["id"]))
            points = [{"id": c["id"], "vector": json.loads(self.db.one("SELECT embedding_json FROM chunks WHERE id=?", (c["id"],))["embedding_json"]),
                       "payload": {"kb_id": version["kb_id"], "version_id": version_id, "chunk_id": c["id"], "type": c["type"]}} for c in batch]
            await self.retry(lambda: self.rag.vectors.upsert(profile["collection_name"], points), jid)
            if not await self.rag.vectors.verify(profile["collection_name"], [c["id"] for c in batch]):
                raise AppError("INDEX_VERIFY_FAILED", "向量索引校验失败，旧活动版本保持不变。", 503)
            with self.db.tx() as conn:
                for c in batch:
                    conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (c["id"],))
                    conn.execute("INSERT INTO chunks_fts VALUES(?,?)", (c["id"], c["fts_text"]))
                    conn.execute("UPDATE chunks SET index_status='indexed' WHERE id=?", (c["id"],))
            self.update(jid, "indexing", min(start + len(batch), len(chunks)), len(chunks))
            await asyncio.sleep(0)  # interactive tickets win before the next image/batch
        (output / "chunks.jsonl").write_text("\n".join(dump(c) for c in chunks), encoding="utf-8")
        status = "completed_with_warnings" if warnings else "completed"
        self.guard(version_id)
        with self.db.tx(immediate=True) as conn:
            conn.execute("UPDATE document_versions SET status=?,embedding_profile_id=? WHERE id=?", (status, profile["id"], version_id))
            if activate:
                conn.execute("UPDATE documents SET active_version_id=? WHERE id=? AND deleted_at IS NULL", (version_id, version["document_id"]))
        self.db.execute("UPDATE jobs SET warnings_json=? WHERE id=?", (dump(warnings), jid))
        return status

    async def rebuild(self, job):
        payload = json.loads(job["payload_json"])
        profile = await self.rag.create_profile(payload.get("model"))
        snapshot = self.db.all("SELECT * FROM documents WHERE deleted_at IS NULL AND active_version_id IS NOT NULL")
        clones = []
        for index, doc in enumerate(snapshot):
            original = self.db.one("SELECT * FROM document_versions WHERE id=?", (doc["active_version_id"],))
            clone = clone_version(self.db, self.settings, original)
            clones.append((doc["id"], clone))
            await self.ingest(clone, job["id"], profile, activate=False)
            self.update(job["id"], "rebuilding", index + 1, len(snapshot))
        # One transaction switches ALL documents and the embedding profile.
        with self.db.tx(immediate=True) as conn:
            for did, vid in clones:
                conn.execute("UPDATE documents SET active_version_id=? WHERE id=? AND deleted_at IS NULL", (vid, did))
            conn.execute("INSERT OR REPLACE INTO settings VALUES('active_embedding_profile',?)", (profile["id"],))
        return "completed"

    async def delete_document(self, document_id):
        versions = self.db.all("SELECT * FROM document_versions WHERE document_id=?", (document_id,))
        for profile in self.db.all("SELECT * FROM embedding_profiles"):
            ids = [v["id"] for v in versions if v["embedding_profile_id"] in {None, profile["id"]}]
            if ids:
                await self.rag.vectors.delete_versions(profile["collection_name"], ids)
        paths = self.db.all("SELECT storage_path FROM media WHERE version_id IN (SELECT id FROM document_versions WHERE document_id=?)", (document_id,))
        with self.db.tx(immediate=True) as conn:
            conn.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE version_id IN (SELECT id FROM document_versions WHERE document_id=?))", (document_id,))
            conn.execute("DELETE FROM document_versions WHERE document_id=?", (document_id,))
            conn.execute("UPDATE documents SET active_version_id=NULL WHERE id=?", (document_id,))
        for version in versions:
            folder = (self.settings.data / "versions" / version["id"]).resolve()
            if folder.parent == (self.settings.data / "versions").resolve():
                shutil.rmtree(folder, ignore_errors=True)
        for item in paths:
            if not self.db.one("SELECT 1 FROM media WHERE storage_path=?", (item["storage_path"],)):
                path = Path(item["storage_path"]).resolve()
                if path.parent == (self.settings.data / "media").resolve():
                    path.unlink(missing_ok=True)
        return "completed"

    async def run_job(self, job):
        self.db.execute("UPDATE jobs SET status='running',started_at=?,last_error=NULL WHERE id=?", (now(), job["id"]))
        try:
            if job["type"] == "delete":
                status = await self.delete_document(job["target_id"])
            elif job["type"] == "rebuild":
                status = await self.rebuild(job)
            else:
                status = await self.ingest(job["target_id"], job["id"])
            self.db.execute("UPDATE jobs SET status=?,stage='done',finished_at=? WHERE id=?", (status, now(), job["id"]))
        except asyncio.CancelledError:
            self.db.execute("UPDATE jobs SET status='queued' WHERE id=? AND status='running'", (job["id"],))
            raise
        except Exception as exc:
            canceled = isinstance(exc, AppError) and exc.code == "TASK_CANCELED"
            status = "canceled" if canceled else "failed"
            self.db.execute("UPDATE jobs SET status=?,last_error=?,finished_at=? WHERE id=?", (status, dump(public_error(exc)), now(), job["id"]))
            if job["type"] == "ingest":
                self.db.execute("UPDATE document_versions SET status=? WHERE id=?", (status, job["target_id"]))
            log.error("job=%s stage=%s exception=%s", job["id"], job["stage"], type(exc).__name__)

    async def loop(self):
        acquired = False
        try:
            while True:
                clock = time.time()
                with self.db.tx(immediate=True) as conn:
                    lease = conn.execute("SELECT * FROM leases WHERE name='worker'").fetchone()
                    if not lease or lease["expires"] < clock or lease["owner"] == self.owner:
                        conn.execute("INSERT OR REPLACE INTO leases VALUES('worker',?,?)", (self.owner, clock + 30))
                        if not acquired:
                            conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
                        acquired = True
                    else:
                        acquired = False
                if acquired and self.current is None:
                    job = self.db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY CASE type WHEN 'delete' THEN 0 ELSE 1 END,created_at LIMIT 1")
                    if job:
                        self.current = asyncio.create_task(self.run_job(job))
                if self.current and self.current.done():
                    await self.current
                    self.current = None
                await asyncio.sleep(.5)
        finally:
            if self.current:
                self.current.cancel()
                await asyncio.gather(self.current, return_exceptions=True)
            self.db.execute("DELETE FROM leases WHERE name='worker' AND owner=?", (self.owner,))

    async def start(self):
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def clone_version(db, settings, original):
    vid = uid()
    folder = settings.data / "versions" / vid
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("original" + Path(original["file_path"]).suffix)
    shutil.copyfile(original["file_path"], path)
    with db.tx(immediate=True) as conn:
        number = conn.execute("SELECT coalesce(max(version_no),0)+1 FROM document_versions WHERE document_id=?", (original["document_id"],)).fetchone()[0]
        conn.execute("INSERT INTO document_versions(id,document_id,version_no,sha256,file_path,status,created_at) VALUES(?,?,?,?,?,'queued',?)", (vid, original["document_id"], number, original["sha256"], str(path), now()))
    return vid
