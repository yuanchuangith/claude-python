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
import tarfile
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SERVER = "43.135.137.212"
USER = "ubuntu"
KEY = r"F:\Desktop\code_new.pem"

REMOTE_CONVERSATION_LOG_DIR = "/home/ubuntu/actiondesign-agent-gateway/logs/actiondesign-agent"
REMOTE_TOOL_LOG_DIR = "/home/ubuntu/actiondesign-agent-gateway/debug_logs"

LOCAL_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_log")
LOCAL_TOOL_LOG_DIR = os.path.join(LOCAL_LOG_DIR, "tool-logs")
LOCAL_CONVERSATION_LOG_DIR = os.path.join(LOCAL_LOG_DIR, "conversation-logs")
LOCAL_TEMP_ARCHIVE = os.path.join(LOCAL_LOG_DIR, "_remote_logs.tar.gz")


def ssh(cmd: str) -> str:
    result = subprocess.run(
        ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", f"{USER}@{SERVER}", cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 and result.stderr.strip():
        print(f"SSH 错误: {result.stderr.strip()}", file=sys.stderr)
    return result.stdout


def scp(remote: str, local: str) -> bool:
    result = subprocess.run(
        ["scp", "-i", KEY, "-o", "StrictHostKeyChecking=no", f"{USER}@{SERVER}:{remote}", local],
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.returncode == 0


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def list_remote_jsonl(remote_dir: str) -> list[dict]:
    """列出远端 .jsonl 文件，按时间倒序。使用 stat 获取精确到秒的时间。"""
    raw = ssh(
        f"for f in {remote_dir}/*.jsonl; do "
        f"[ -f \"$f\" ] && stat -c '%Y %s %n' \"$f\"; "
        f"done 2>/dev/null | sort -rn"
    )
    files = []
    for line in raw.strip().splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 3:
            mtime = int(parts[0])
            size = int(parts[1])
            name = parts[2].split("/")[-1]
            time_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            files.append({"name": name, "time_str": time_str, "size": size})
    return files


def list_remote_subdirs(remote_dir: str) -> list[dict]:
    """列出远端子目录，按时间倒序。使用 stat 获取精确到秒的时间。"""
    raw = ssh(
        f"for d in {remote_dir}/*/; do "
        f"[ -d \"$d\" ] && stat -c '%Y %n' \"$d\"; "
        f"done 2>/dev/null | sort -rn"
    )
    dirs = []
    for line in raw.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) >= 2:
            mtime = int(parts[0])
            name = parts[1].rstrip("/").split("/")[-1]
            time_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            dirs.append({"name": name, "time_str": time_str})
    return dirs


def clear_local_logs() -> None:
    if os.path.exists(LOCAL_LOG_DIR):
        shutil.rmtree(LOCAL_LOG_DIR)
    os.makedirs(LOCAL_TOOL_LOG_DIR, exist_ok=True)
    os.makedirs(LOCAL_CONVERSATION_LOG_DIR, exist_ok=True)


def pack_and_download(conv_names: list[str], tool_names: list[str]) -> bool:
    """在服务器上打包日志并下载。"""
    tmp_archive = "/tmp/actiondesign_logs.tar.gz"

    # 构建打包命令
    parts = []
    for name in conv_names:
        parts.append(f"{REMOTE_CONVERSATION_LOG_DIR}/{name}")
    for name in tool_names:
        parts.append(f"{REMOTE_TOOL_LOG_DIR}/{name}")

    if not parts:
        return False

    files_str = " ".join(parts)
    print("  服务器打包中...", end="", flush=True)
    result = ssh(f"tar -czf {tmp_archive} {files_str} 2>&1 && echo 'OK'")
    if "OK" not in result:
        print(f" 失败: {result.strip()}")
        return False

    # 获取压缩包大小
    size_raw = ssh(f"stat -c %s {tmp_archive} 2>/dev/null || echo 0")
    try:
        size = int(size_raw.strip())
    except ValueError:
        size = 0
    print(f" {format_size(size)}")

    print("  下载中...", end="", flush=True)
    if not scp(tmp_archive, LOCAL_TEMP_ARCHIVE):
        print(" 失败")
        return False
    print(" 完成")

    # 清理服务器临时文件
    ssh(f"rm -f {tmp_archive}")
    return True


def extract_archive(conv_names: list[str], tool_names: list[str]) -> None:
    """解压日志到对应目录。"""
    if not os.path.exists(LOCAL_TEMP_ARCHIVE):
        return

    with tarfile.open(LOCAL_TEMP_ARCHIVE, "r:gz") as tar:
        tar.extractall(path=LOCAL_LOG_DIR)

    # 移动文件到正确目录
    # 对话日志: debug_log/home/ubuntu/actiondesign-agent-gateway/logs/actiondesign-agent/xxx.jsonl
    # 工具日志: debug_log/home/ubuntu/actiondesign-agent-gateway/debug_logs/xxx/
    extracted_base = os.path.join(LOCAL_LOG_DIR, "home", "ubuntu", "actiondesign-agent-gateway")

    # 移动对话日志
    remote_conv_dir = os.path.join(extracted_base, "logs", "actiondesign-agent")
    if os.path.exists(remote_conv_dir):
        for name in conv_names:
            src = os.path.join(remote_conv_dir, name)
            dst = os.path.join(LOCAL_CONVERSATION_LOG_DIR, name)
            if os.path.exists(src):
                shutil.move(src, dst)

    # 移动工具日志
    remote_tool_dir = os.path.join(extracted_base, "debug_logs")
    if os.path.exists(remote_tool_dir):
        for name in tool_names:
            src = os.path.join(remote_tool_dir, name)
            dst = os.path.join(LOCAL_TOOL_LOG_DIR, name)
            if os.path.exists(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.move(src, dst)

    # 清理临时目录
    extracted_home = os.path.join(LOCAL_LOG_DIR, "home")
    if os.path.exists(extracted_home):
        shutil.rmtree(extracted_home)

    # 删除压缩包
    os.remove(LOCAL_TEMP_ARCHIVE)


def format_time(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


def show_summary() -> None:
    """显示下载结果摘要。"""
    print("\n--- 对话日志 ---")
    conv_dir = LOCAL_CONVERSATION_LOG_DIR
    if os.path.exists(conv_dir):
        files = sorted(os.listdir(conv_dir), key=lambda f: os.path.getmtime(os.path.join(conv_dir, f)), reverse=True)
        for f in files:
            path = os.path.join(conv_dir, f)
            size = os.path.getsize(path)
            mtime = format_time(os.path.getmtime(path))
            try:
                with open(path, encoding="utf-8") as fh:
                    lines = len(fh.readlines())
                print(f"  {f}  [{mtime}]  {format_size(size)}  {lines} 条记录")
            except Exception:
                print(f"  {f}  [{mtime}]  {format_size(size)}")
    else:
        print("  (无)")

    print("\n--- 工具日志 ---")
    tool_dir = LOCAL_TOOL_LOG_DIR
    if os.path.exists(tool_dir):
        dirs = sorted(os.listdir(tool_dir), key=lambda d: os.path.getmtime(os.path.join(tool_dir, d)), reverse=True)
        for d in dirs:
            dpath = os.path.join(tool_dir, d)
            if os.path.isdir(dpath):
                files = os.listdir(dpath)
                mtime = format_time(os.path.getmtime(dpath))
                print(f"  {d}/  [{mtime}]  {len(files)} 个文件")
    else:
        print("  (无)")


def main():
    parser = argparse.ArgumentParser(description="拉取服务器对话日志")
    parser.add_argument("--all", action="store_true", help="拉取所有日志")
    parser.add_argument("--last", type=int, default=1, help="拉取最近 N 条 (默认 1)")
    parser.add_argument("--keep", action="store_true", help="保留本地已有日志")
    args = parser.parse_args()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] 开始拉取日志...\n")

    if not args.keep:
        clear_local_logs()
        print(f"已清空本地目录: {LOCAL_LOG_DIR}")
    else:
        os.makedirs(LOCAL_TOOL_LOG_DIR, exist_ok=True)
        os.makedirs(LOCAL_CONVERSATION_LOG_DIR, exist_ok=True)

    # 收集要拉取的文件
    conv_files = list_remote_jsonl(REMOTE_CONVERSATION_LOG_DIR)
    tool_dirs = list_remote_subdirs(REMOTE_TOOL_LOG_DIR)

    conv_targets = conv_files if args.all else conv_files[: args.last]
    tool_targets = tool_dirs if args.all else tool_dirs[: args.last]

    print(f"\n[对话日志] 共 {len(conv_files)} 条，拉取 {len(conv_targets)} 条")
    for f in conv_targets:
        print(f"  {f['name']}  [{f['time_str']}]  {format_size(f['size'])}")

    print(f"\n[工具日志] 共 {len(tool_dirs)} 条，拉取 {len(tool_targets)} 条")
    for d in tool_targets:
        print(f"  {d['name']}  [{d['time_str']}]")

    if not conv_targets and not tool_targets:
        print("\n没有可拉取的日志")
        return

    # 打包下载
    conv_names = [f["name"] for f in conv_targets]
    tool_names = [d["name"] for d in tool_targets]

    print("\n打包下载:")
    if pack_and_download(conv_names, tool_names):
        print("  解压中...", end="", flush=True)
        extract_archive(conv_names, tool_names)
        print(" 完成")

    show_summary()
    print(f"\n完成，日志保存在: {LOCAL_LOG_DIR}")


if __name__ == "__main__":
    main()
