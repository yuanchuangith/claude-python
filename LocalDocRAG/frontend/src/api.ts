export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set('X-LocalDoc-Request', '1');
  if (init?.body && typeof init.body === 'string') headers.set('Content-Type', 'application/json');
  const response = await fetch('/api' + path, { ...init, headers });
  const value = await response.json();
  if (!response.ok) throw new Error(value.message || `本地服务请求失败 (${response.status})`);
  return value as T;
}
export const json = (value: unknown) => JSON.stringify(value);
export type KB = {id:string; name:string; description:string; document_count:number; chunk_count:number; updated_at:string};
export type Job = {id:string; status:string; stage:string; completed:number; total:number; last_error?:{code:string;message:string}; warnings?:unknown[]};
export type Version = {id:string; version_no:number; status:string; sha256:string};
export type Doc = {id:string;kb_id:string; original_name:string; active_version_id:string|null; versions:Version[];jobs:Job[]};
export type Cell = {row:number; col:number; rowspan:number; colspan:number; text:string;header:boolean;block_ids:string[]};
export type Table = {id:string;block_id:string;caption:string;table_json:{rows:number;cols:number;cells:Cell[]}};
export type Media = {id:string;url:string;mime:string;status:string;warnings:string[];ocr?:{text:string;low_confidence:boolean;lines:{text:string;confidence:number}[]};vision?:{summary:string;relationships:string[];uncertainty:string[];unreadable_regions:string[]}};
export type Block = {id:string;type:string;plain_text:string;anchor:string;parent_block_id:string|null;heading_path:string[];structured:{table_id?:string;media_id?:string;heading_level?:number;inline?:{type:string;text?:string;media_id?:string}[]}};
export type Preview = {document:Doc;version:Version;blocks:Block[];tables:Table[];media:Media[];report:{blocks?:number;tables?:number;image_occurrences?:number;media_parts?:number;ocr_success?:number;warnings:unknown[]}};
export type Source = {source_id:string;document_id:string;version_id:string;block_id:string;filename:string;section_path:string[];type:string;row?:number;col?:number;excerpt:string;score:number;image?:{id:string;url:string;status:string};warnings:string[];original_deleted?:boolean};
export type SearchResult = {bm25:{id:string;score:number}[];dense:{id:string;score:number}[];fused:Source[];warnings:string[]};
export type System = {ollama:string;qdrant:string;sqlite:string;ocr:string;gpu_status:string;docker:string;mode:string;free_bytes:number;models:{name:string;size:number}[];loaded_models?:{name:string;size_vram:number}[];warnings:string[];queue:{priority:number;count:number}[];settings:{chat_model:string;vision_model:string;embedding_model:string;context_length:number;ocr:boolean;caption:boolean};profile?:{model_name:string;vector_size:number;model_digest:string;collection_name:string}};

export async function streamChat(body: unknown, signal:AbortSignal, receive:(name:string,data:any)=>void) {
  const response = await fetch('/api/chat/stream', {method:'POST',headers:{'Content-Type':'application/json','X-LocalDoc-Request':'1'},body:json(body),signal});
  if (!response.ok) throw new Error((await response.json()).message || '问答服务失败');
  const reader = response.body!.getReader(), decoder = new TextDecoder();
  let pending = '';
  try {
    while (true) {
      const {done,value} = await reader.read();
      pending += decoder.decode(value,{stream:!done}).replace(/\r/g,'');
      let end;
      while ((end=pending.indexOf('\n\n'))>=0) {
        const frame=pending.slice(0,end);pending=pending.slice(end+2);
        const name=frame.split('\n').find(l=>l.startsWith('event:'))?.slice(6).trim();
        const data=frame.split('\n').filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trim()).join('\n');
        if (name && data) receive(name,JSON.parse(data));
      }
      if(done)break;
    }
  } finally { reader.releaseLock(); }
}
