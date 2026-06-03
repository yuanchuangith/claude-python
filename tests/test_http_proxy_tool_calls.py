from examples.http_proxy_server import (
    _REPAIR_PROMPT_TEMPLATE,
    _clean_extracted_tool_calls,
    _normalize_image_data,
    _normalize_tool_call_markers,
    _strip_json_actions_content,
    _transform_tool_args,
    extract_tool_calls,
    extract_tool_calls_with_diagnostics,
)


def test_extracts_typo_tool_call_marker():
    content = '[TOL_CALL] create_node({"elementKey":"NullCondition","targetAction":"validateForm"})'
    calls = extract_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "create_node"
    assert calls[0]["arguments"]["elementKey"] == "NullCondition"


def test_repairs_unquoted_keys_without_corrupting_url_strings():
    content = '[TOOL_CALL] create_node({elementKey:"OpenMessageDialog",params:{"url":"http://host:8888/a:b"}})'
    calls = extract_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["arguments"]["elementKey"] == "OpenMessageDialog"
    assert calls[0]["arguments"]["params"]["url"] == "http://host:8888/a:b"


def test_extract_keeps_repeated_mutating_tool_calls():
    content = '''
[TOOL_CALL] create_node({"elementKey":"ExitAction","title":"退出验证","targetAction":"validateBeforeSubmit","inputParams":{"returnValue":{"value":false}}})
[TOOL_CALL] create_node({"elementKey":"ExitAction","title":"退出验证","targetAction":"validateBeforeSubmit","inputParams":{"returnValue":{"value":false}}})
'''
    calls = extract_tool_calls(content)
    assert len(calls) == 2
    assert calls[0]["name"] == "create_node"
    assert calls[1]["name"] == "create_node"


def test_extract_reports_failed_tool_call_parse():
    content = '''
[TOOL_CALL] create_node({"elementKey":"ExitAction"})
[TOOL_CALL] create_node({"elementKey":)
'''
    calls, diagnostics = extract_tool_calls_with_diagnostics(content)
    assert len(calls) == 1
    assert diagnostics["tool_call_marker_count"] == 2
    assert diagnostics["parsed_tool_calls_count"] == 1
    assert diagnostics["dropped_duplicate_count"] == 0
    assert diagnostics["parse_failed_count"] == 1
    assert diagnostics["parse_failed_spans"]


def test_clean_removes_normalized_tool_call():
    content = 'before [TOL_CALL] preview_code({"targetAction":"validateForm"}) after'
    cleaned = _clean_extracted_tool_calls(content)
    assert cleaned == 'before'


def test_normalize_reports_typo_markers():
    normalized, typo_matches = _normalize_tool_call_markers('[TOL_CALL] preview_code({})')
    assert normalized == '[TOOL_CALL] preview_code({})'
    assert typo_matches == ['[TOL_CALL]']


def test_strip_json_actions_code_block():
    content = '''
说明文本
```json
{"actions":{"main":[{"elementKey":"NullCondition","paramsValue":{}}]}}
```
后续文本
'''
    cleaned, removed = _strip_json_actions_content(content)
    assert "actions" not in cleaned
    assert cleaned == "说明文本\n\n后续文本"
    assert removed > 0


def test_strip_json_actions_raw_block():
    content = 'before {"message":"ok","actions":{"main":[{"elementKey":"ExitAction"}]}} after'
    cleaned, removed = _strip_json_actions_content(content)
    assert cleaned == "before  after"
    assert removed > 0


def test_strip_bare_json_actions_raw_block():
    content = 'before {"actions":{"main":[{"elementKey":"ExitAction","paramsValue":{"message":"ok"}}]}} after'
    cleaned, removed = _strip_json_actions_content(content)
    assert cleaned == "before  after"
    assert removed > 0


def test_repair_prompt_is_format_only():
    prompt = _REPAIR_PROMPT_TEMPLATE.format(content='{"actions": {"main": []')
    assert "Repair only the JSON formatting" in prompt
    assert "Do not add, remove, reorder, rename, translate, or infer" in prompt
    assert "Return only the JSON code block" in prompt


def test_question_alias_args_transform_to_ask_user_shape():
    args = {
        "questions": [
            {
                "question": "Which fields?",
                "header": "Form",
                "options": [
                    {"label": "Required", "description": "Only required fields"},
                    "All fields",
                ],
            }
        ]
    }
    transformed = _transform_tool_args("Question", args)
    assert transformed == {
        "question": "Which fields?",
        "context": "Form",
        "suggestedOptions": ["Required（Only required fields）", "All fields"],
    }


def test_normalize_image_data_strips_data_url_prefix():
    assert _normalize_image_data("data:image/png;base64,abc123") == "abc123"
    assert _normalize_image_data("abc123") == "abc123"

