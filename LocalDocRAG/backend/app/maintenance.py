"""Offline/local maintenance. Run with API and web stopped for backup/restore."""
import argparse
import asyncio
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import httpx

from app.core.config import Settings
from app.core.model_queue import ModelQueue
from app.db.database import Database, dump
from app.rag.models import Ollama, Qdrant
from app.rag.service import RAG


async def initialize(cfg):
    db = Database(cfg.database)
    db.migrate()
    models, vectors = Ollama(cfg, ModelQueue(db, cfg.timeout)), Qdrant(cfg)
    try:
        from app.ingest.images import ImageProcessor
        from PIL import Image, ImageDraw
        probe = cfg.data / "tmp/model-check.png"
        im = Image.new("RGB", (400, 160), "white")
        ImageDraw.Draw(im).rectangle((30, 30, 100, 100), fill="red")
        im.save(probe)
        ocr = await asyncio.to_thread(ImageProcessor(cfg, models).ocr, probe)
        if ocr.get("device") != "cpu":
            raise RuntimeError("CPU OCR self-test failed")
        for tag in [cfg.embed_model, cfg.vision_model, cfg.chat_model]:
            await models.digest(tag)
        profile = await RAG(cfg, db, models, vectors).ensure_profile()
        vectors2 = await models.embed(["维度稳定测试"], profile["model_name"])
        assert len(vectors2[0]) == profile["vector_size"]
        visual = await models.vision(probe)
        if not visual["summary"].strip():
            raise RuntimeError("Vision self-test returned empty description")
        reply = await models.chat([{"role": "user", "content": "请用中文回复：本地模型正常。"}])
        if not reply.strip():
            raise RuntimeError("Chat self-test returned empty text")
        loaded = await models.loaded()
        gpu = any(m.get("size_vram", 0) > 0 for m in loaded)
        if cfg.mode == "gpu" and not gpu:
            raise RuntimeError("GPU inference not confirmed by Ollama /api/ps. Choose CPU explicitly or repair Docker GPU support.")
        return {"embedding_dimension": profile["vector_size"], "vision": "passed", "chat": "passed", "ocr": "cpu", "actual_gpu": gpu, "mode": cfg.mode}
    finally:
        await models.close()
        await vectors.close()


def checksum(file):
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def backup(cfg, destination):
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("Backup destination must be empty; existing files are never overwritten")
    destination.mkdir(parents=True, exist_ok=True)
    app = destination / "app-data"
    app.mkdir()
    if cfg.database.is_file():
        with closing(sqlite3.connect(cfg.database)) as source, closing(sqlite3.connect(app / cfg.database.name)) as target:
            source.backup(target)
    for name in ["versions", "media"]:
        shutil.copytree(cfg.data / name, app / name, dirs_exist_ok=True)
    qdir = destination / "qdrant"
    qdir.mkdir()
    collections = []
    async with httpx.AsyncClient(base_url=cfg.qdrant_url, timeout=180, trust_env=False) as client:
        result = await client.get("/collections")
        result.raise_for_status()
        for item in result.json()["result"]["collections"]:
            name = item["name"]
            snapshot = await client.post(f"/collections/{name}/snapshots")
            snapshot.raise_for_status()
            filename = snapshot.json()["result"]["name"]
            raw = await client.get(f"/collections/{name}/snapshots/{filename}")
            raw.raise_for_status()
            path = qdir / (name + ".snapshot")
            path.write_bytes(raw.content)
            collections.append({"collection": name, "snapshot": path.relative_to(destination).as_posix()})
    manifest = {"app_version": "1.0.0", "schema_version": "0001", "collections": collections,
                "models": {"embedding": cfg.embed_model, "vision": cfg.vision_model, "chat": cfg.chat_model},
                "files": {p.relative_to(destination).as_posix(): checksum(p) for p in destination.rglob("*") if p.is_file()}}
    (destination / "manifest.json").write_text(dump(manifest), encoding="utf-8")
    return {"status": "backup_complete", "file_count": len(manifest["files"])}


async def restore(cfg, source, confirmed):
    if not confirmed:
        raise RuntimeError("Restore requires --confirm-overwrite; keep the API stopped and take a backup first")
    source = source.resolve()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest["app_version"] != "1.0.0" or manifest["schema_version"] != "0001":
        raise RuntimeError("Incompatible backup version")
    for filename, expected in manifest["files"].items():
        file = (source / filename).resolve()
        if not file.is_relative_to(source) or not file.is_file() or checksum(file) != expected:
            raise RuntimeError("Backup checksum validation failed")
    # Restore Qdrant first; no active API can observe partial recovery.
    async with httpx.AsyncClient(base_url=cfg.qdrant_url, timeout=300, trust_env=False) as client:
        for item in manifest["collections"]:
            path = (source / item["snapshot"]).resolve()
            if not path.is_relative_to(source):
                raise RuntimeError("Unsafe snapshot path")
            with path.open("rb") as stream:
                result = await client.post(f"/collections/{item['collection']}/snapshots/upload?priority=snapshot", files={"snapshot": (path.name, stream, "application/octet-stream")})
            result.raise_for_status()
    # Data dir may be a Docker mount point; replace only this application's known children.
    for name in ["versions", "media"]:
        target = (cfg.data / name).resolve()
        if target.parent != cfg.data or target.is_symlink():
            raise RuntimeError("Unsafe restore target")
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source / "app-data" / name, target)
    for suffix in ["", "-wal", "-shm"]:
        Path(str(cfg.database) + suffix).unlink(missing_ok=True)
    shutil.copyfile(source / "app-data" / cfg.database.name, cfg.database)
    # Backup paths are /data in Docker; remap only known relative artifacts for native restore too.
    db = Database(cfg.database)
    for version in db.all("SELECT id,file_path FROM document_versions"):
        folder = cfg.data / "versions" / version["id"]
        db.execute("UPDATE document_versions SET file_path=?,canonical_path=?,report_path=? WHERE id=?", (str(folder / Path(version["file_path"]).name), str(folder / "canonical_document.json"), str(folder / "parse_report.json"), version["id"]))
    for media in db.all("SELECT id,storage_path FROM media"):
        db.execute("UPDATE media SET storage_path=? WHERE id=?", (str(cfg.data / "media" / Path(media["storage_path"]).name), media["id"]))
    db.execute("DELETE FROM leases")
    db.execute("DELETE FROM model_queue")
    if hasattr(__import__('os'), 'geteuid') and __import__('os').geteuid() == 0:
        import os
        for target in [cfg.data, *cfg.data.rglob("*")]:
            if not target.is_symlink():
                os.chown(target, 10001, 10001)
    return {"status": "restore_complete"}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "backup", "restore"])
    parser.add_argument("path", nargs="?")
    parser.add_argument("--confirm-overwrite", action="store_true")
    args = parser.parse_args()
    cfg = Settings()
    if args.action == "init":
        result = await initialize(cfg)
    elif args.action == "backup":
        result = await backup(cfg, Path(args.path))
    else:
        result = await restore(cfg, Path(args.path), args.confirm_overwrite)
    print(dump(result))


if __name__ == "__main__":
    asyncio.run(main())
