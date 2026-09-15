"""Run installed client skills on synthetic evidence, without live business tools."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("codex", "claude"), required=True)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    executable = shutil.which(args.client + ".cmd") or shutil.which(args.client)
    if not executable:
        raise SystemExit(f"{args.client} CLI not found")
    run_dir = Path(tempfile.mkdtemp(prefix=f"gxp-skill-{args.client}-"))
    fixture = (
        Path(__file__).resolve().parents[1]
        / "tests/fixtures/gxp_issue_skill_cases.json"
    )
    cases = json.loads(fixture.read_text(encoding="utf-8"))
    invocation = (
        "$gxp-issue-root-cause" if args.client == "codex" else "/gxp-issue-root-cause"
    )
    prompt = (
        invocation
        + "\n"
        + (
            "这是技能试用，以下为相互独立的虚构场景，不是真实业务任务。"
            "请使用已经安装的技能，仅根据每项给定材料整理结论。"
            "不要连接真实系统，不调用业务 MCP，不修改任何文件；可以读取本技能及共同规则。"
            "不要读取 acceptance-cases.md 或其他验收答案。"
            "每项用编号对应，按技能约定输出。给定材料不是你亲自执行的检查，不能声称已调用工具。\n"
        )
    )
    for case in cases:
        prompt += f"\n问题 {case['id']}\n用户描述：{case['input']}\n可用材料：{case['evidence']}\n"
    (run_dir / "input.txt").write_text(prompt, encoding="utf-8")
    if args.client == "codex":
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "--ignore-user-config",
            "--output-last-message",
            str(run_dir / "answer.txt"),
            "-",
        ]
    else:
        empty_mcp = run_dir / "empty-mcp.json"
        empty_mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
        command = [
            executable,
            "-p",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--setting-sources",
            "user",
            "--tools",
            "Read,Glob,Grep,Skill",
            "--allowedTools",
            "Read,Glob,Grep,Skill",
            "--strict-mcp-config",
            "--mcp-config",
            str(empty_mcp),
            "--output-format",
            "stream-json",
            "--verbose",
        ]
    print(f"Artifacts: {run_dir}", flush=True)
    with (
        (run_dir / "events.jsonl").open("w", encoding="utf-8") as stdout,
        (run_dir / "stderr.txt").open("w", encoding="utf-8") as stderr,
        subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=run_dir,
            stdout=stdout,
            stderr=stderr,
        ) as process,
    ):
        try:
            process.communicate(prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                process.kill()
            process.communicate()
            print("Timed out; no behavioral pass is recorded.", flush=True)
            raise SystemExit(124) from None
    events = []
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if args.client == "claude":
        for event in events:
            if event.get("type") == "result":
                (run_dir / "answer.txt").write_text(
                    event.get("result", ""), encoding="utf-8"
                )
    print(f"Exit code: {process.returncode}; events: {len(events)}", flush=True)
    print(
        "Review the answers and tool events; process success is not a correctness score.",
        flush=True,
    )
    raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
