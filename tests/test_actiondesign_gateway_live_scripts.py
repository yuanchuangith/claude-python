import runpy
from pathlib import Path

from claude_agent_sdk.actiondesign_gateway.models import TraceEnvelope


def test_deploy_archive_includes_live_verifiers():
    deploy_script = (
        Path(__file__).resolve().parents[1] / "deploy.sh"
    ).read_text(encoding="utf-8")

    assert "src tests scripts pyproject.toml README.md claude-agent-sdk.md" in deploy_script


def test_langsmith_live_verifier_uses_canonical_runtime_identity():
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "verify_actiondesign_langsmith_live.py"
        )
    )

    event = script["_event"](
        event_id="event.live:1",
        trace_id="trace.live:1",
        run_id="trace.run:1",
        parent_id="",
        name="actiondesign_graph",
        run_type="graph",
        event="start",
        offset_ms=0,
    )

    validated = TraceEnvelope.model_validate(event)

    assert validated.metadata.conversation_id == script["CONVERSATION_ID"]
    assert validated.agent_run_id == script["AGENT_RUN_ID"]
