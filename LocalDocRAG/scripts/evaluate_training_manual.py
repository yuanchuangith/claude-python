"""Repeatable real-model checks against the explicitly imported FS-CPM manual.

This is an acceptance harness, never a runtime answer template. Results and
source text are kept in ignored data/. Read answers before judging quality.
"""
import argparse
import json
import re
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ('entry', '岗位培训申请从哪里进入？', ['FS-5.4.2.7-001']),
    ('procedure', '如何发起岗位培训申请？请说清步骤、条件和提交后的状态。', ['FS-5.4.2.7-001','FS-5.4.2.7-008','FS-5.4.2.7-009']),
    ('trainees', '初始岗位培训能同时选择多个受训人吗？', ['FS-5.4.2.7-003']),
    ('online', '岗位培训选择线上评估时试卷和合格分值怎么填？线下呢？', ['FS-5.4.2.7-005']),
    ('duration', '岗位培训自学时长最少是多少？', ['FS-5.4.2.7-005']),
    ('dedup', '岗位培训什么情况下可以去重培训资料？', ['FS-5.4.2.7-007']),
    ('signature', 'FS-5.4.2.7-008 发起岗位培训是否需要电子签名，提交后到哪个节点？', ['FS-5.4.2.7-008']),
    ('approved', '岗位培训部门审批全部同意后是什么结果？已组织代表培训完成了吗？', ['FS-5.4.2.7-009','FS-5.4.3.2-007']),
    ('qualification', '岗位资质确认从哪里进入，需要什么状态条件？', ['FS-5.4.2.8-001']),
    ('exact', 'FS-5.4.3.2-010 培训计划什么时候变成已完成？', ['FS-5.4.3.2-010']),
    ('conflict', '持续培训带出哪些培训内容？手册对持续培训是否有冲突？', ['FS-5.4.2.7-002']),
    ('unknown', '岗位培训期限默认是7天吗？手册有明确规定吗？', []),
    ('unrelated', '这份手册规定年假有多少天？', []),
]


def run(base, chat, only, refresh):
    manifest=json.loads((ROOT/'data/training-manual.json').read_text(encoding='utf-8'))
    output=ROOT/'data/training-evaluation.json'
    previous=json.loads(output.read_text(encoding='utf-8')) if output.exists() and not refresh else {}
    results=previous.get('results',{})
    with httpx.Client(base_url=base,timeout=900,trust_env=False,headers={'X-LocalDoc-Request':'1'}) as client:
        diagnostic=client.get('/api/system/status');diagnostic.raise_for_status()
        model=diagnostic.json()['settings']['chat_model']
        if previous.get('model') != model or previous.get('manifest',{}).get('version_id') != manifest['version_id']:
            results={}
        for attempt in range(300):
            preview=client.get(f"/api/documents/{manifest['document_id']}/preview");preview.raise_for_status(); document=preview.json()
            if document['document']['active_version_id']==manifest['version_id']: break
            job=client.get('/api/jobs/'+manifest['job_id']);job.raise_for_status()
            if job.json()['status'] in {'failed','canceled'}: raise RuntimeError(job.json())
            time.sleep(2)
        assert document['document']['active_version_id'] == manifest['version_id'], 'Import has not activated'
        block_ids={b['id'] for b in document['blocks']}
        for key,question,expected in CASES:
            if only and key not in only: continue
            row=results.get(key,{'question':question,'expected_fs':expected})
            if 'retrieval' not in row:
                started=time.monotonic()
                response=client.post('/api/search',json={'knowledge_base_id':manifest['knowledge_base_id'],'query':question,'top_k':8})
                response.raise_for_status(); hits=response.json()['fused']
                found=set(x for h in hits for x in h.get('fs_ids',[]))
                row['retrieval']=[{k:h.get(k) for k in ['chunk_id','type','section_path','fs_ids','table_id','row','operation_complete','content','score']} for h in hits]
                row['retrieval_expected_found']=all(fs in found for fs in expected)
                row['retrieval_seconds']=round(time.monotonic()-started,2)
                assert all(h['version_id']==manifest['version_id'] and h['block_id'] in block_ids for h in hits)
                print(key,'retrieval',row['retrieval_expected_found'],'top',hits[0]['section_path'] if hits else [],flush=True)
            if chat and 'answer' not in row:
                started=time.monotonic()
                response=client.post('/api/chat/stream',json={'knowledge_base_id':manifest['knowledge_base_id'],'question':question,'top_k':8})
                response.raise_for_status(); answer='';sources=[];done=None
                for frame in response.text.replace('\r','').split('\n\n'):
                    lines=frame.splitlines(); event=next((l[6:].strip() for l in lines if l.startswith('event:')),None)
                    raw=next((l[5:] for l in lines if l.startswith('data:')),None)
                    if not raw: continue
                    data=json.loads(raw)
                    if event=='error': raise RuntimeError(data)
                    if event=='token':answer+=data['text']
                    if event=='sources':sources=data['items']
                    if event=='done':done=data
                assert done is not None
                assert set(re.findall(r'\[(S\d+)\]',answer)) <= {s['source_id'] for s in sources}
                assert all(s['version_id']==manifest['version_id'] and s['block_id'] in block_ids for s in sources)
                row.update(answer=answer,done=done,sources=[{k:s.get(k) for k in ['source_id','chunk_id','fs_ids','table_id','row','block_id','version_id']} for s in sources],answer_seconds=round(time.monotonic()-started,2))
                print(key,'answer',answer,'insufficient',done['insufficient'],flush=True)
            results[key]=row
            temporary=output.with_suffix('.tmp')
            temporary.write_text(json.dumps({'manifest':manifest,'model':model,'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
            temporary.replace(output)
    print('saved',output,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='http://127.0.0.1:7860')
    parser.add_argument('--chat',action='store_true')
    parser.add_argument('--only',nargs='*')
    parser.add_argument('--refresh',action='store_true')
    args=parser.parse_args();run(args.base,args.chat,args.only,args.refresh)
