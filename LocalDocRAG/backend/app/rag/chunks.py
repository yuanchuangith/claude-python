import hashlib
import re
from uuid import UUID, uuid5

import jieba

PREPROCESS_VERSION = "chunks-v2-operation-rows"
FS_ID = re.compile(r"\bFS-\d+(?:\.\d+)*-\d+\b", re.I)


def tokenize(text):
    # Keep the exact technical identifier alongside Chinese segmentation.
    tokens = [w.strip().lower() for w in jieba.cut(text) if w.strip() and any(c.isalnum() for c in w)]
    tokens += re.findall(r"[A-Za-z]+(?:[-_][A-Za-z0-9]+)+", text.lower())
    return " ".join(tokens)


def make_chunks(canonical, version_id, kb_id, filename):
    chunks = []
    blocks = {b["id"]: b for b in canonical["blocks"]}

    def add(kind, content, refs, meta, key):
        if not content.strip():
            return
        cid = str(uuid5(UUID(version_id), "chunk:" + key))
        source = {"kb_id": kb_id, "version_id": version_id, "filename": filename, "block_id": refs[0],
                  "block_order": blocks[refs[0]]["block_order"], "section_path": blocks[refs[0]]["heading_path"],
                  "anchor": blocks[refs[0]]["anchor"], "type": kind, "sha256": hashlib.sha256(content.encode()).hexdigest(), **meta}
        source["fs_ids"] = list(dict.fromkeys(FS_ID.findall(content)))
        chunks.append({"id": cid, "type": kind, "block_refs": refs, "content": content, "source_meta": source, "fts_text": tokenize(content)})

    for block in canonical["blocks"]:
        if block["type"] not in {"paragraph", "heading"} or block["parent_block_id"] or not block["plain_text"].strip() or block["structured"].get("navigation"):
            continue
        text = block["plain_text"]
        for index, start in enumerate(range(0, len(text), 520)):
            part = text[start:start + 600]
            add("text", " > ".join(block["heading_path"]) + "\n" + part, [block["id"]], {}, block["id"] + f":{index}")

    for table in canonical["tables"]:
        data, tid = table["table_json"], table["id"]
        headers = [c for c in data["cells"] if c["header"]]
        section = " > ".join(blocks[table["block_id"]]["heading_path"])
        prefix = section + "；" + table["caption"]
        operation_rows = []
        for row in range(data["rows"]):
            cells = [c for c in data["cells"] if c["row"] <= row < c["row"] + c["rowspan"]]
            if not cells or all(c["header"] for c in cells):
                continue
            values = []
            for cell in sorted(cells, key=lambda c: c["col"]):
                labels = [h["text"] for h in headers if h["row"] < cell["row"] and h["col"] <= cell["col"] < h["col"] + h["colspan"] and h["text"]]
                values.append(("/".join(labels) or f"列{cell['col']+1}") + "=" + (cell["text"] or "（空单元格）"))
            body = "；".join(values)
            row_meta = {"table_id": tid, "row": row, "col": None}
            # Keep a complete ordinary operation/condition together. Only very
            # large rows split, repeating the section/row identity in every part.
            starts = [0] if len(body) <= 1600 else range(0, len(body), 1400)
            for index, start in enumerate(starts):
                text = prefix + f"；行{row+1}；" + body[start:start + 1600]
                add("table_row", text, [table["block_id"]], row_meta, tid + f":row{row}:{index}")
            if any(FS_ID.search(c["text"]) for c in cells):
                operation_rows.append((row, cells))
        if operation_rows:
            # An overview is searchable by process name. Reading it hydrates the
            # complete original operation table instead of a generated summary.
            lines = [f"{c[0]['text']}；" + "；".join(x["text"][:110] for x in c[1:]) for _, c in operation_rows]
            overview = prefix + "\n完整操作流程索引（步骤、条件、发起、审批与完成标准）：\n" + "\n".join(lines)
            add("operation", overview, [table["block_id"]], {"table_id": tid, "row": None, "col": None}, tid + ":operation")

    media_by_id = {m["id"]: m for m in canonical["media"]}
    for occurrence in canonical["media_occurrences"]:
        media = media_by_id[occurrence["media_id"]]
        block = blocks[occurrence["block_id"]]
        ocr, vision = media.get("ocr") or {}, media.get("vision") or {}
        nearby = [b["plain_text"] for b in canonical["blocks"] if abs(b["block_order"] - block["block_order"]) <= 1 and b["type"] != "table"]
        text = " > ".join(block["heading_path"]) + "\n" + "\n".join(nearby) + "\n图片OCR：" + ocr.get("text", "") + "\n图片语义：" + vision.get("summary", "") + "\n" + "；".join(vision.get("relationships", []))
        warnings = list(media.get("warnings", []))
        if ocr.get("low_confidence"):
            warnings.append("OCR_LOW_CONFIDENCE")
        if vision.get("uncertainty") or vision.get("unreadable_regions"):
            warnings.append("VISION_UNCERTAINTY")
        add("image", text[:2400], [block["id"]], {"image_id": media["id"], "table_id": occurrence["table_id"],
            "row": occurrence["row_index"], "col": occurrence["col_index"], "warnings": warnings, "ocr": ocr, "vision": vision, "image_status": media["status"]}, occurrence["id"])
    return chunks
