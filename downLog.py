#!/usr/bin/env python3
"""从服务器拉取最新的 ActionDesign Agent 对话日志。

日志来源:
  - logs/actiondesign-agent/{conversationId}.jsonl  (完整对话日志)
  - debug_logs/{conversationId}/tool-results.jsonl   (工具调用日志)

用法:
    python downLog.py            # 拉取最新的一条对话日志
    python downLog.py --all      # 拉取所有对话日志
    python downLog.py --last 3   # 拉取最近 3 条对话日志
    python downLog.py --keep     # 保留本地已有日志，不清空
"""

import argparse
import os
import shutil
import subprocess
import sys

# Windows 终端 UTF-8 支持
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SERVER = "43.135.137.212"
USER = "ubuntu"
KEY = r"F:\Desktop\code_new.pem"

# 服务器上的两个日志目录
REMOTE_CONVERSATION_LOG_DIR = "/home/ubuntu/actiondesign-agent-gateway/logs/actiondesign-agent"
REMOTE_TOOL_LOG_DIR = "/home/ubuntu/actiondesign-agent-gateway/debug_logs"

LOCAL_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_log")


def ssh(cmd: str) -> str:
    result = subprocess.run(
        ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", f"{USER}@{SERVER}", cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"SSH 错误: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def scp(remote: str, local: str) -> bool:
    result = subprocess.run(
        ["scp", "-i", KEY, "-o", "StrictHostKeyChecking=no", f"{USER}@{SERVER}:{remote}", local],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0


def list_remote_files(remote_dir: str) -> list[dict]:
    """列出远端目录下的文件，按修改时间倒序。"""
    raw = ssh(
        f"find {remote_dir} -maxdepth 1 -type f -name '*.jsonl' -printf '%T@ %f\\n' 2>/dev/null "
        f"| sort -rn"
    )
    files = []
    for line in raw.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            files.append({"name": parts[1], "mtime": float(parts[0])})
    return files


def list_remote_dirs(remote_dir: str) -> list[dict]:
    """列出远端目录下的子目录，按修改时间倒序。"""
    raw = ssh(
        f"find {remote_dir} -maxdepth 1 -type d ! -path {remote_dir} -printf '%T@ %f\\n' 2>/dev/null "
        f"| sort -rn"
    )
    dirs = []
    for line in raw.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            dirs.append({"name": parts[1], "mtime": float(parts[0])})
    return dirs


LOCAL_TOOL_LOG_DIR = os.path.join(LOCAL_LOG_DIR, "tool-logs")
LOCAL_CONVERSATION_LOG_DIR = os.path.join(LOCAL_LOG_DIR, "conversation-logs")


def clear_local_logs() -> None:
    if os.path.exists(LOCAL_LOG_DIR):
        shutil.rmtree(LOCAL_LOG_DIR)
    os.makedirs(LOCAL_TOOL_LOG_DIR, exist_ok=True)
    os.makedirs(LOCAL_CONVERSATION_LOG_DIR, exist_ok=True)


def pull_conversation_logs(names: list[str]) -> int:
    """拉取完整对话日志 (logs/actiondesign-agent/)，返回拉取文件数。"""
    count = 0
    for name in names:
        remote_path = f"{REMOTE_CONVERSATION_LOG_DIR}/{name}"
        local_path = os.path.join(LOCAL_CONVERSATION_LOG_DIR, name)
        if scp(remote_path, local_path):
            count += 1
            try:
                with open(local_path, encoding="utf-8") as f:
                    lines = len(f.readlines())
            except Exception:
                lines = "?"
            print(f"    {name}  ({lines} 条记录)")
    return count


def pull_tool_logs(names: list[str]) -> int:
    """拉取工具调用日志 (debug_logs/)，返回拉取文件数。"""
    count = 0
    for name in names:
        remote_dir = f"{REMOTE_TOOL_LOG_DIR}/{name}"
        local_dir = os.path.join(LOCAL_TOOL_LOG_DIR, name)
        os.makedirs(local_dir, exist_ok=True)
        files_raw = ssh(f"ls {remote_dir}/ 2>/dev/null")
        files = [f.strip() for f in files_raw.strip().splitlines() if f.strip()]
        for f in files:
            if scp(f"{remote_dir}/{f}", os.path.join(local_dir, f)):
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="拉取服务器对话日志")
    parser.add_argument("--all", action="store_true", help="拉取所有日志")
    parser.add_argument("--last", type=int, default=1, help="拉取最近 N 条 (默认 1)")
    parser.add_argument("--keep", action="store_true", help="保留本地已有日志")
    args = parser.parse_args()

    if not args.keep:
        clear_local_logs()
        print(f"已清空本地目录: {LOCAL_LOG_DIR}")
    else:
        os.makedirs(LOCAL_TOOL_LOG_DIR, exist_ok=True)
        os.makedirs(LOCAL_CONVERSATION_LOG_DIR, exist_ok=True)

    # 1. 完整对话日志 (logs/actiondesign-agent/{conversationId}.jsonl)
    conv_files = list_remote_files(REMOTE_CONVERSATION_LOG_DIR)
    if conv_files:
        targets = conv_files if args.all else conv_files[: args.last]
        print(f"\n[对话日志] 找到 {len(conv_files)} 条，拉取 {len(targets)} 条:")
        pull_conversation_logs([f["name"] for f in targets])
    else:
        print("\n[对话日志] 暂无 (需开启 full_conversation_log_enabled)")

    # 2. 工具调用日志 (debug_logs/{conversationId}/tool-results.jsonl)
    tool_dirs = list_remote_dirs(REMOTE_TOOL_LOG_DIR)
    if tool_dirs:
        targets = tool_dirs if args.all else tool_dirs[: args.last]
        print(f"\n[工具日志] 找到 {len(tool_dirs)} 条，拉取 {len(targets)} 条:")
        pull_tool_logs([d["name"] for d in targets])
    else:
        print("\n[工具日志] 暂无")

    print(f"\n完成，日志保存在: {LOCAL_LOG_DIR}")


if __name__ == "__main__":
    main()
