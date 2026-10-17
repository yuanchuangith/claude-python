"""Browser acceptance harness only: synthetic model responses, isolated data.

Not referenced by Docker, launchers or production API. Never a runtime fallback.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["LOCALDOC_NO_GLOBAL_APP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from app.main import create_app
from app.core.config import Settings
from test_localdoc import FakeModels, FakeVectors
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

scratch = tempfile.TemporaryDirectory(prefix="localdoc-browser-test-")
app = create_app(Settings(data=Path(scratch.name), worker=True, ocr=False, caption=False, mode="cpu",
                          ollama_url="http://127.0.0.1:11434", qdrant_url="http://127.0.0.1:6333"), FakeModels(), FakeVectors())
rag_root = Path(__file__).resolve().parents[2]
frontend = Path(os.environ.get("LOCALDOC_FRONTEND_DIR", str(rag_root / "frontend")))
dist = frontend / "dist"
app.mount("/assets", StaticFiles(directory=dist / "assets"), name="static")

@app.get("/")
@app.get("/documents/{path:path}")
def index(path=""):
    return FileResponse(dist / "index.html")
