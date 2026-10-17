import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.core.errors import AppError
from app.core.model_queue import ModelQueue
from app.db.database import Database, dump, uid
from app.maintenance import backup, restore
from app.rag.models import Ollama
from test_localdoc import settings


@pytest.mark.asyncio
async def test_sqlite_backup_restore_checksum_and_confirmation(settings,tmp_path,monkeypatch):
    db=Database(settings.database);db.migrate()
    db.execute("INSERT INTO settings VALUES('marker','before')")
    media=settings.data/'media'/'test.png';media.write_bytes(b'original-media')
    transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'result':{'collections':[]}}))
    real=httpx.AsyncClient
    monkeypatch.setattr('app.maintenance.httpx.AsyncClient',lambda **kw:real(transport=transport,**kw))
    destination=tmp_path/'backup'
    result=await backup(settings,destination)
    assert result['status']=='backup_complete'
    with sqlite3.connect(destination/'app-data/localdoc.sqlite3') as copied:
        assert copied.execute("SELECT value FROM settings WHERE key='marker'").fetchone()[0]=='before'
    db.execute("UPDATE settings SET value='after' WHERE key='marker'")
    with pytest.raises(RuntimeError,match='requires --confirm'):
        await restore(settings,destination,False)
    assert (await restore(settings,destination,True))['status']=='restore_complete'
    assert db.one("SELECT value FROM settings WHERE key='marker'")['value']=='before'
    (destination/'app-data/media/test.png').write_bytes(b'tampered')
    with pytest.raises(RuntimeError,match='checksum'):
        await restore(settings,destination,True)


@pytest.mark.asyncio
async def test_ollama_calls_are_serial_even_when_browser_cancels(settings):
    db=Database(settings.database);db.migrate()
    models=Ollama(settings,ModelQueue(db,10))
    entered=asyncio.Event();finish=asyncio.Event();order=[]
    async def prepare(model):pass
    async def request(method,path,**kwargs):
        label=kwargs['json']['messages'][0]['content']
        order.append('start:'+label)
        if label=='first':entered.set();await finish.wait()
        order.append('end:'+label)
        return {'message':{'content':'ok'}}
    models.prepare=prepare;models.request=request
    first=asyncio.create_task(models.chat([{'role':'user','content':'first'}]))
    await entered.wait();first.cancel()
    second=asyncio.create_task(models.chat([{'role':'user','content':'second'}]))
    await asyncio.sleep(.2)
    assert order==['start:first']
    finish.set();await asyncio.gather(first,second,return_exceptions=True)
    assert order==['start:first','end:first','start:second','end:second']
    await models.close()


def test_default_service_urls_reject_remote_models(tmp_path):
    from app.core.config import Settings
    with pytest.raises(ValueError,match='local/internal'):
        Settings(data=tmp_path,ollama_url='https://api.example.com')

