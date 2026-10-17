import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.db.database import dump, uid
from app.jobs.worker import clone_version, enqueue
from app.main import create_app
from test_localdoc import FakeModels, FakeVectors, fixtures, settings

H = {"x-localdoc-request":"1"}


def create_upload(client, fixtures, name="training_manual.docx", data=None):
    kid = client.post("/api/knowledge-bases", json={"name": "恢复测试"}).json()["id"]
    body = data or (fixtures/name).read_bytes()
    item = client.post(f"/api/knowledge-bases/{kid}/documents", files={"files":(name,body)}).json()["items"][0]
    return kid, item


async def execute(app, jid):
    await app.state.worker.run_job(app.state.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    return app.state.db.one("SELECT * FROM jobs WHERE id=?", (jid,))


def test_index_interruption_preserves_old_version_and_resumes(settings, fixtures):
    vectors = FakeVectors()
    app = create_app(settings, FakeModels(), vectors)
    with TestClient(app,headers=H) as client:
        kid,item = create_upload(client,fixtures)
        assert asyncio.run(execute(app,item["job_id"]))["status"] == "completed_with_warnings"
        changed = Document(fixtures / "training_manual.docx")
        changed.add_paragraph("新增版本内容：测试索引中断恢复。")
        buf = io.BytesIO();changed.save(buf)
        newer = client.post(f"/api/knowledge-bases/{kid}/documents", files={"files":("training_manual.docx",buf.getvalue())}).json()["items"][0]
        real = vectors.upsert
        async def failed(*a): raise AppError("QDRANT_UNAVAILABLE","interrupted",503)
        vectors.upsert = failed
        failed_job = asyncio.run(execute(app,newer["job_id"]))
        assert failed_job["status"] == "failed"
        assert app.state.db.one("SELECT active_version_id FROM documents WHERE id=?",(item["document_id"],))["active_version_id"] == item["version_id"]
        result = client.post("/api/search",json={"knowledge_base_id":kid,"query":"管理人员"}).json()
        assert all(s["version_id"]==item["version_id"] for s in result["fused"])
        vectors.upsert = real
        assert client.post('/api/jobs/'+newer["job_id"]+'/retry').is_success
        assert asyncio.run(execute(app,newer["job_id"]))["status"] == "completed_with_warnings"
        assert app.state.db.one("SELECT active_version_id FROM documents WHERE id=?",(item["document_id"],))["active_version_id"] == newer["version_id"]
        assert len(client.get(f"/api/documents/{item['document_id']}/versions").json()) == 2
        assert client.post(f"/api/documents/{item['document_id']}/activate",json={"version_id":item["version_id"],"confirm":True}).is_success


def test_image_503_keeps_original_and_warning(settings, fixtures):
    settings.caption=True
    model=FakeModels();model.fail_vision=True
    app=create_app(settings,model,FakeVectors())
    with TestClient(app,headers=H) as client:
        kid,item=create_upload(client,fixtures)
        row=asyncio.run(execute(app,item["job_id"]))
        assert row["status"]=='completed_with_warnings'
        preview=client.get(f"/api/documents/{item['document_id']}/preview").json()
        assert preview['media'][0]['status']=='completed_with_warnings'
        assert 'VISION_FAILED' in preview['media'][0]['warnings']
        assert client.get(preview['media'][0]['url']).is_success


def test_agent_source_active_and_kb_fenced(settings, fixtures):
    app=create_app(settings,FakeModels(),FakeVectors())
    with TestClient(app,headers=H) as client:
        kid,item=create_upload(client,fixtures)
        asyncio.run(execute(app,item['job_id']))
        matches=client.post('/api/agent/search',json={'knowledge_base_id':kid,'query':'管理人员'}).json()
        assert matches['resultVersion'].startswith('sha256:')
        hit=matches['results'][0]
        cid=hit['id'].removeprefix('localdoc:')
        read=client.get(f'/api/agent/knowledge/{cid}?knowledge_base_id={kid}').json()
        assert read['id']==hit['id'] and read['metadata']['status']=='evidence_only'
        assert read['metadata']['unresolved']
        other=client.post('/api/knowledge-bases',json={'name':'其他库'}).json()['id']
        assert client.get(f'/api/agent/knowledge/{cid}?knowledge_base_id={other}').json() is None
        client.delete(f"/api/documents/{item['document_id']}?confirm=true")
        assert client.get(f'/api/agent/knowledge/{cid}?knowledge_base_id={kid}').json() is None


def test_rebuild_failure_does_not_switch_collection(settings,fixtures):
    models,vectors=FakeModels(),FakeVectors()
    app=create_app(settings,models,vectors)
    with TestClient(app,headers=H) as client:
        kid,item=create_upload(client,fixtures)
        asyncio.run(execute(app,item['job_id']))
        old=app.state.rag.profile()
        old_vid=app.state.db.one('SELECT active_version_id FROM documents WHERE id=?',(item['document_id'],))['active_version_id']
        original=models.embed
        async def changed(texts,model=None,priority=10):
            if model=='changed': return [[.1,.2,.3,.4,.5] for _ in texts]
            return await original(texts,model,priority)
        models.embed=changed
        real=vectors.upsert
        async def failed(*a):raise AppError('QDRANT_UNAVAILABLE','fail',503)
        vectors.upsert=failed
        jid=client.post('/api/system/rebuild',json={'model':'changed','confirm':True}).json()['job_id']
        assert asyncio.run(execute(app,jid))['status']=='failed'
        assert app.state.rag.profile()['id']==old['id']
        assert app.state.db.one('SELECT active_version_id FROM documents WHERE id=?',(item['document_id'],))['active_version_id']==old_vid
        vectors.upsert=real
        client.post('/api/jobs/'+jid+'/retry')
        assert asyncio.run(execute(app,jid))['status']=='completed'
        assert app.state.rag.profile()['vector_size']==5
        assert app.state.rag.profile()['id']!=old['id']


def test_worker_restart_resumes_nonterminal(settings):
    app=create_app(settings,FakeModels(),FakeVectors())
    with TestClient(app,headers=H):
        jid=enqueue(app.state.db,'ingest',uid())
        app.state.db.execute("UPDATE jobs SET status='running',stage='indexing' WHERE id=?",(jid,))
        async def recover():
            await app.state.worker.start()
            await asyncio.sleep(.1)
            await app.state.worker.stop()
        asyncio.run(recover())
        row=app.state.db.one('SELECT * FROM jobs WHERE id=?',(jid,))
        assert row['status']=='canceled'  # recovered and attempted; nonexistent document safely fenced


def test_real_cpu_ocr(settings,fixtures):
    from app.ingest.images import ImageProcessor
    result=ImageProcessor(settings,None).ocr(fixtures/'scan.png')
    assert result['device']=='cpu' and 'ABC-2048' in result['text'] and '30' in result['text']
    assert result['lines'][0]['confidence'] > .8

