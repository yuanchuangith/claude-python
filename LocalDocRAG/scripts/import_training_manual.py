"""Idempotent upload of the explicitly selected training manual, through the API."""
import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx


def run(base, source, wait, reindex=False):
    root = Path(__file__).resolve().parents[1]
    if source is None:
        source = next((root.parents[1] / "component/doc/business-operation/sources").glob("FS-CPM-001*1014.docx"))
    source = source.resolve()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with httpx.Client(base_url=base, headers={"X-LocalDoc-Request":"1"}, timeout=120, trust_env=False) as client:
        response = client.get('/api/knowledge-bases'); response.raise_for_status()
        name = "GXP培训操作手册 FS-CPM-001"
        kb = next((k for k in response.json() if k['name'] == name), None)
        if kb is None:
            response = client.post('/api/knowledge-bases',json={'name':name,'description':'FS-CPM-001 培训合并版本1014；保留原文、表格、FS编号及图像来源，供业务Agent检索读取。'})
            response.raise_for_status(); kb = response.json()
        response = client.post(f"/api/knowledge-bases/{kb['id']}/documents",files={'files':(source.name,source.read_bytes(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
        response.raise_for_status(); item = response.json()['items'][0]
        if reindex:
            response=client.post(f"/api/documents/{item['document_id']}/reindex")
            response.raise_for_status(); item.update(response.json()); item['deduplicated']=False
        result = {'knowledge_base_id':kb['id'],'knowledge_base_name':name,'source_name':source.name,'sha256':digest,**item}
        manifest = root / 'data/training-manual.json'
        manifest.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if wait:
            previous = None
            for _ in range(3600):
                response = client.get('/api/jobs/'+item['job_id']);response.raise_for_status(); job=response.json()
                progress = (job['status'],job['stage'],job['completed'],job['total'])
                if progress != previous:
                    print(json.dumps({'job':progress,'last_error':job.get('last_error')},ensure_ascii=False),flush=True); previous=progress
                if job['status'] in {'failed','completed','completed_with_warnings','canceled'}:
                    result['job'] = job
                    manifest.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                    if job['status'] not in {'completed','completed_with_warnings'}: raise RuntimeError('Import did not finish; inspect the durable job and retry after repair')
                    return
                time.sleep(2)
            raise RuntimeError('Import is still running; state is persisted in data/training-manual.json')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='http://127.0.0.1:7860')
    parser.add_argument('--source',type=Path)
    parser.add_argument('--wait',action='store_true')
    parser.add_argument('--reindex',action='store_true',help='Create a new version after model/parser changes; old active stays readable')
    args=parser.parse_args();run(args.base,args.source,args.wait,args.reindex)
