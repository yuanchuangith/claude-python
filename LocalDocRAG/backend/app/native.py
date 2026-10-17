"""Optional native developer host; production start.bat uses Docker Compose.

Needs real local Ollama/Qdrant. Missing services are reported, never mocked.
"""
import os
from pathlib import Path

root = Path(__file__).resolve().parents[2]
os.environ.setdefault("DATA_DIR", str(root / "data"))
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:11434")
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:6333")

from app.main import app
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

frontend = Path(os.environ.get("LOCALDOC_FRONTEND_DIR", str(root / "frontend")))
dist = frontend / "dist"
if (dist / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="static")

@app.get("/")
@app.get("/documents/{path:path}")
def index(path=""):
    if not (dist / "index.html").is_file():
        return {"message": "请先在后端 LocalDocRAG/frontend 构建管理页面，或设置 LOCALDOC_FRONTEND_DIR。"}
    return FileResponse(dist / "index.html")
