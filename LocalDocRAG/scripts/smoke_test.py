import argparse
import json
import tempfile
import time
from pathlib import Path

import httpx

from generate_fixtures import generate


def run(base, minimal=False):
    with tempfile.TemporaryDirectory() as temp, httpx.Client(base_url=base, timeout=900, headers={"X-LocalDoc-Request": "1"}, trust_env=False) as client:
        health = client.get("/api/health")
        health.raise_for_status()
        capabilities = health.json()
        if not capabilities.get("ready") or not capabilities.get("chat_ready", capabilities["ready"]) or not capabilities.get("image_caption_ready", capabilities["ready"]):
            raise RuntimeError("NOT TESTED: Ollama/Qdrant/models are not ready. No mock success is substituted.")
        generated = generate(temp)
        kb = client.post("/api/knowledge-bases", json={"name": "__smoke_" + str(time.time_ns()), "description": "隔离合成冒烟测试，结束自动删除"})
        kb.raise_for_status()
        kid = kb.json()["id"]
        try:
            for filename in (["training_manual.docx"] if minimal else ["training_manual.docx", "table_image.docx", "scan_text.docx"]):
                with (generated / filename).open("rb") as stream:
                    result = client.post(f"/api/knowledge-bases/{kid}/documents", files={"files": (filename, stream)})
                result.raise_for_status()
                item = result.json()["items"][0]
                started = time.monotonic()
                while True:
                    job = client.get("/api/jobs/" + item["job_id"]).json()
                    if job["status"] in {"completed", "completed_with_warnings", "failed", "canceled"}:
                        break
                    if time.monotonic() - started > 1800:
                        raise RuntimeError("Smoke job timeout; inspect persistent job status")
                    time.sleep(1)
                if job["status"] not in {"completed", "completed_with_warnings"}:
                    raise RuntimeError("Smoke ingest failed: " + json.dumps(job.get("last_error"), ensure_ascii=False))
                preview = client.get(f"/api/documents/{item['document_id']}/preview").json()
                assert preview["blocks"] and preview["media"]
                if filename == "table_image.docx":
                    assert preview["report"]["image_occurrences"] == 3
                for media in preview["media"]:
                    assert client.get(media["url"]).is_success
                for warning in preview["report"]["warnings"]:
                    if warning.get("code") in {"OCR_FAILED", "VISION_FAILED"} or warning.get("stage") in {"ocr", "vision"}:
                        raise RuntimeError("Image model/OCR stage did not pass: " + json.dumps(warning, ensure_ascii=False))
            questions = json.loads((generated / "questions.json").read_text(encoding="utf-8"))
            if minimal:
                questions = questions[:2]
            passed = 0
            results = []
            for case in questions:
                result = client.post("/api/search", json={"knowledge_base_id": kid, "query": case["question"]})
                result.raise_for_status()
                response = client.post("/api/chat/stream", json={"knowledge_base_id": kid, "question": case["question"]})
                response.raise_for_status()
                token_text = ""
                citations = []
                done = False
                for frame in response.text.replace("\r", "").split("\n\n"):
                    lines = frame.splitlines()
                    event = next((l[6:].strip() for l in lines if l.startswith("event:")), None)
                    data = next((json.loads(l[5:]) for l in lines if l.startswith("data:")), None)
                    if event == "error": raise RuntimeError(data)
                    if event == "token": token_text += data["text"]
                    if event == "sources": citations = data["items"]
                    if event == "done": done = True
                assert done
                import re
                assert set(re.findall(r"\[(S\d+)\]", token_text)) <= {s["source_id"] for s in citations}
                for source in citations:
                    original = client.get(f"/api/documents/{source['document_id']}/preview?version_id={source['version_id']}")
                    assert original.is_success and any(b["id"] == source["block_id"] for b in original.json()["blocks"])
                correct = case["expected"].replace(" ", "") in token_text.replace(" ", "")
                passed += correct
                results.append({"question": case["question"], "correct": correct, "answer": token_text, "citations": len(citations)})
            threshold = len(questions) if minimal else 10
            print(json.dumps({"tested": len(questions), "passed": passed, "required": threshold, "citation_ids_valid": True, "results": results}, ensure_ascii=False))
            if passed < threshold:
                raise RuntimeError("Answer accuracy below acceptance threshold")
        finally:
            cleanup = client.delete(f"/api/knowledge-bases/{kid}?confirm=true")
            if not cleanup.is_success:
                print("WARNING: smoke KB cleanup must be retried: " + kid)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7860")
    parser.add_argument("--minimal", action="store_true")
    args = parser.parse_args()
    run(args.base, args.minimal)
