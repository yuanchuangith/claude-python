import asyncio
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import AppError
from app.core.model_queue import ModelQueue
from app.db.database import Database, dump, uid
from app.ingest.word import WordParser, validate_upload
from app.jobs.worker import enqueue
from app.main import create_app
from app.rag.chunks import make_chunks
from app.rag.models import VisionResult
from generate_fixtures import generate


@pytest.fixture
def fixtures(tmp_path):
    return generate(tmp_path / "fixtures")


@pytest.fixture
def settings(tmp_path):
    return Settings(data=tmp_path / "data", ollama_url="http://127.0.0.1:11434", qdrant_url="http://127.0.0.1:6333", worker=False, mode="cpu", caption=False, ocr=False)


def test_parser_order_tables_images_and_sha(settings, fixtures):
    source = fixtures / "table_image.docx"
    canonical, report = WordParser(settings).parse(source, uid(), settings.data / "parsed")
    assert report["media_parts"] == 2
    assert report["image_occurrences"] == 3
    assert report["processed_media_parts"] + report["unsupported_media_parts"] == 2
    first = canonical["media_occurrences"][0]
    assert first["table_id"] and first["row_index"] == 1 and first["col_index"] == 1
    assert canonical["media_occurrences"][1]["media_id"] == canonical["media_occurrences"][2]["media_id"]
    table = next(t for t in canonical["tables"] if t["table_json"]["rows"] == 4)
    assert any(c["colspan"] == 2 and c["text"] == "考核标准" for c in table["table_json"]["cells"])
    assert any(c["rowspan"] == 2 and c["text"] == "纵向合并类别" for c in table["table_json"]["cells"])
    assert [b["block_order"] for b in canonical["blocks"]] == list(range(len(canonical["blocks"])))
    assert "<script" not in table["sanitized_html"]
    assert (settings.data / "parsed/canonical_document.json").is_file()


def test_table_rows_are_not_mixed(settings, fixtures):
    vid = uid()
    canonical, _ = WordParser(settings).parse(fixtures / "training_manual.docx", vid, settings.data / "parsed")
    chunks = make_chunks(canonical, vid, uid(), "training_manual.docx")
    managers = [c for c in chunks if c["type"] == "table_row" and "岗位=管理人员" in c["content"]]
    assert len(managers) == 1 and "培训周期=6个月" in managers[0]["content"] and "合格分数=90分" in managers[0]["content"]
    assert "80分" not in managers[0]["content"]
    assert managers[0]["source_meta"]["row"] == 2


def test_bad_archives_rejected(settings):
    for name, body in [("../escape", b"bad"), ("word/vbaProject.bin", b"macro")]:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("word/document.xml", b"<a/>")
            z.writestr(name, body)
        with pytest.raises(AppError):
            validate_upload(buffer.getvalue(), ".docx", settings)
    with pytest.raises(AppError):
        validate_upload(b"not zip", ".docx", settings)


class FakeModels:
    bad_citation = False
    fail_vision = False

    async def close(self): pass
    async def tags(self): return [{"name": n, "digest": "test-digest"} for n in ["qwen3:4b", "qwen3-vl:2b", "qwen3-embedding:0.6b"]]
    async def loaded(self): return []
    async def digest(self, model): return "test-digest"
    async def embed(self, texts, model=None, priority=10): return [[float("管理" in text), .5, .3] for text in texts]
    async def vision(self, path):
        if self.fail_vision: raise AppError("OLLAMA_REQUEST_FAILED", "visual unavailable", 503)
        return VisionResult(summary="提交申请到部门审批，失败退回", entities=["提交申请", "部门审批", "退回"], relationships=["审批失败→退回"]).model_dump()
    async def chat(self, messages, schema=None, **kwargs):
        payload = json.loads(messages[-1]["content"])
        question, evidence = payload["question"], payload["evidence"]
        if self.bad_citation:
            return dump({"paragraphs": [{"text": "这是错误来源", "source_ids": ["S999"]}], "insufficient": False})
        chosen = next((e for e in evidence if "岗位=管理人员" in e["content"]), evidence[0])
        sid = chosen["source_id"]
        if "年假" in question:
            return dump({"paragraphs": [{"text": "当前知识库没有足够信息。", "source_ids": []}], "insufficient": True})
        return dump({"paragraphs": [{"text": "管理人员培训周期6个月，合格分数90分。", "source_ids": [sid]}], "insufficient": False})


class FakeVectors:
    def __init__(self): self.points = {}; self.fail_next = False
    async def close(self): pass
    async def request(self, *a, **kw): return {"result": {"collections": []}}
    async def ensure(self, *a): pass
    async def upsert(self, collection, points):
        if self.fail_next:
            self.fail_next = False
            raise AppError("QDRANT_UNAVAILABLE", "unavailable", 503)
        self.points.setdefault(collection, {}).update({p["id"]:p for p in points})
    async def verify(self, collection, ids): return all(i in self.points.get(collection, {}) for i in ids)
    async def search(self, collection, vector, kb, versions, limit):
        return [{"id":p["id"], "score":.8} for p in self.points.get(collection, {}).values() if p["payload"]["kb_id"]==kb and p["payload"]["version_id"] in versions][:limit]
    async def delete_versions(self, collection, versions):
        for i in list(self.points.get(collection, {})):
            if self.points[collection][i]["payload"]["version_id"] in versions: del self.points[collection][i]


def setup_client(settings):
    return create_app(settings, FakeModels(), FakeVectors())


def test_api_ingest_dedup_search_sse_version_delete(settings, fixtures):
    app = setup_client(settings)
    headers = {"x-localdoc-request":"1"}
    with TestClient(app, headers=headers) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        kid = client.post("/api/knowledge-bases", json={"name":"测试知识库"}).json()["id"]
        data = (fixtures / "training_manual.docx").read_bytes()
        uploaded = client.post(f"/api/knowledge-bases/{kid}/documents", files={"files":("培训.docx",data)}).json()["items"][0]
        async def ingest():
            await app.state.worker.run_job(app.state.db.one("SELECT * FROM jobs WHERE id=?", (uploaded["job_id"],)))
        asyncio.run(ingest())
        job = client.get("/api/jobs/" + uploaded["job_id"]).json()
        assert job["status"] == "completed_with_warnings", job
        doc = client.get(f"/api/knowledge-bases/{kid}/documents").json()[0]
        assert doc["active_version_id"] == uploaded["version_id"]
        preview = client.get(f"/api/documents/{doc['id']}/preview").json()
        assert preview["tables"] and preview["media"] and "storage_path" not in preview["media"][0]
        assert client.get(f"/api/documents/{doc['id']}/download").content == data
        assert client.get(preview["media"][0]["url"]).status_code == 200
        repeat = client.post(f"/api/knowledge-bases/{kid}/documents", files={"files":("培训.docx",data)}).json()["items"][0]
        assert repeat["deduplicated"] and repeat["version_id"] == uploaded["version_id"]
        result = client.post("/api/search", json={"knowledge_base_id":kid,"query":"管理人员 合格分数"}).json()
        assert any("90分" in s["content"] for s in result["fused"])
        stream = client.post("/api/chat/stream", json={"knowledge_base_id":kid,"question":"管理人员的周期与分数？"}).text
        assert "90分" in stream and "event: sources" in stream and "event: done" in stream
        app.state.rag.models.bad_citation = True
        rejected = client.post("/api/chat/stream", json={"knowledge_base_id":kid,"question":"管理人员"}).text
        assert "S999" not in rejected and "校验未通过" in rejected
        assert client.delete(f"/api/documents/{doc['id']}").status_code == 409
        assert client.delete(f"/api/documents/{doc['id']}?confirm=true").status_code == 200
        assert client.get(preview["media"][0]["url"]).status_code == 404
        assert client.post("/api/search", json={"knowledge_base_id":kid,"query":"管理人员"}).json()["fused"] == []


def test_security_origin_host_and_cross_knowledge(settings):
    with TestClient(setup_client(settings)) as client:
        assert client.post("/api/knowledge-bases", json={"name":"x"}).status_code == 403
        assert client.get("/api/health", headers={"host":"evil.example"}).status_code == 403
        assert client.get("/api/health", headers={"origin":"http://evil.example"}).status_code == 403
        assert client.get("/api/assets/../../etc/passwd").status_code != 200


@pytest.mark.asyncio
async def test_model_queue_cross_instance_priority(settings):
    db = Database(settings.database)
    db.migrate()
    first, second = ModelQueue(db, 5), ModelQueue(db, 5)
    order = []
    gate = asyncio.Event()
    async def run(queue, priority, label, block=False):
        async with queue.acquire(priority):
            order.append(label)
            if block: await gate.wait()
            await asyncio.sleep(.01)
    current = asyncio.create_task(run(first,10,"current",True))
    await asyncio.sleep(.05)
    background = asyncio.create_task(run(second,10,"background"))
    interactive = asyncio.create_task(run(first,0,"interactive"))
    await asyncio.sleep(.05)
    gate.set()
    await asyncio.gather(current, background, interactive)
    assert order == ["current", "interactive", "background"]


def test_vision_json_schema():
    with pytest.raises(Exception): VisionResult.model_validate({"summary":"x", "invented":1})
