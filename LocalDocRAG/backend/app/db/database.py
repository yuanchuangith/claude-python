import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid4())


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE knowledge_bases(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
CREATE TABLE embedding_profiles(id TEXT PRIMARY KEY,model_name TEXT NOT NULL,model_digest TEXT NOT NULL,vector_size INTEGER NOT NULL,query_template TEXT NOT NULL,preprocess_version TEXT NOT NULL,collection_name TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE documents(id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),original_name TEXT NOT NULL,created_at TEXT NOT NULL,active_version_id TEXT,deleted_at TEXT);
CREATE UNIQUE INDEX document_name ON documents(kb_id,original_name) WHERE deleted_at IS NULL;
CREATE TABLE document_versions(id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents(id),version_no INTEGER NOT NULL,sha256 TEXT NOT NULL,file_path TEXT NOT NULL,parser_version TEXT,status TEXT NOT NULL,report_path TEXT,embedding_profile_id TEXT REFERENCES embedding_profiles(id),canonical_path TEXT,created_at TEXT NOT NULL);
CREATE TABLE blocks(id TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,parent_block_id TEXT,block_order INTEGER NOT NULL,type TEXT NOT NULL,heading_path_json TEXT NOT NULL,anchor TEXT NOT NULL,plain_text TEXT NOT NULL,structured_json TEXT NOT NULL,warning_json TEXT NOT NULL);
CREATE TABLE tables(id TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,block_id TEXT NOT NULL,caption TEXT NOT NULL,table_json TEXT NOT NULL,sanitized_html TEXT NOT NULL);
CREATE TABLE media(id TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,sha256 TEXT NOT NULL,storage_path TEXT NOT NULL,mime TEXT NOT NULL,width INTEGER,height INTEGER,ocr_json TEXT,vision_json TEXT,status TEXT NOT NULL,warning_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE media_occurrences(id TEXT PRIMARY KEY,media_id TEXT NOT NULL REFERENCES media(id) ON DELETE CASCADE,block_id TEXT NOT NULL,table_id TEXT,row_index INTEGER,col_index INTEGER,occurrence_order INTEGER NOT NULL,anchor TEXT NOT NULL);
CREATE TABLE chunks(id TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,type TEXT NOT NULL,block_refs_json TEXT NOT NULL,content TEXT NOT NULL,source_meta_json TEXT NOT NULL,fts_text TEXT NOT NULL,qdrant_point_id TEXT NOT NULL,index_status TEXT NOT NULL,embedding_json TEXT);
CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED,fts_text,tokenize='unicode61');
CREATE TABLE jobs(id TEXT PRIMARY KEY,type TEXT NOT NULL,target_id TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,completed INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL DEFAULT 0,retries INTEGER NOT NULL DEFAULT 0,last_error TEXT,warnings_json TEXT NOT NULL DEFAULT '[]',started_at TEXT,finished_at TEXT,created_at TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX jobs_queue ON jobs(status,created_at);
CREATE TABLE leases(name TEXT PRIMARY KEY,owner TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE model_queue(id TEXT PRIMARY KEY,owner TEXT NOT NULL,priority INTEGER NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
CREATE TABLE conversations(id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id),role TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE message_citations(id TEXT PRIMARY KEY,message_id TEXT NOT NULL REFERENCES messages(id),source_id TEXT NOT NULL,snapshot_json TEXT NOT NULL);
"""


class Database:
    def __init__(self, path):
        self.path = str(path)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def tx(self, immediate=False):
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield conn

    def all(self, sql, args=()):
        with closing(self.connect()) as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.tx() as conn:
            return conn.execute(sql, args).rowcount

    def migrate(self):
        from alembic import command
        from alembic.config import Config
        from pathlib import Path
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
        config.set_main_option("sqlalchemy.url", "sqlite:///" + self.path.replace("\\", "/"))
        command.upgrade(config, "head")

