import importlib.util
import os
import sys
from pathlib import Path

os.environ["LOCALDOC_NO_GLOBAL_APP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

