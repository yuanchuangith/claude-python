import json
import os
from pathlib import Path

from PIL import Image


class ImageProcessor:
    def __init__(self, settings, models):
        self.settings, self.models = settings, models
        self.engine = None

    def ocr(self, path):
        # RapidOCR is CPU-only; no GPU provider is passed or silently selected.
        if self.engine is None:
            from rapidocr import RapidOCR
            params = {"Global.use_det": True, "Global.use_cls": True, "Global.use_rec": True,
                                           "EngineConfig.onnxruntime.use_cuda": False,
                                           "EngineConfig.onnxruntime.use_dml": False,
                                           "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                                           "EngineConfig.onnxruntime.inter_op_num_threads": 2}
            self.engine = RapidOCR(params=params)
        result = self.engine(str(path))
        if result is None or result.txts is None:
            return {"backend": "rapidocr", "device": "cpu", "text": "", "lines": [], "low_confidence": True}
        lines = [{"text": str(text), "confidence": float(score), "box": box.tolist() if hasattr(box, "tolist") else box}
                 for text, score, box in zip(result.txts, result.scores, result.boxes)]
        return {"backend": "rapidocr", "device": "cpu", "text": "\n".join(l["text"] for l in lines),
                "lines": lines, "low_confidence": not lines or any(l["confidence"] < .8 for l in lines)}

    async def describe(self, media):
        return await self.models.vision(Path(media["storage_path"]))

