#!/usr/bin/env bash
# pull_debug_logs.sh — 实时拉取服务器 AI 调试日志
# 用法:
#   ./scripts/pull_debug_logs.sh                     # 默认参数
#   ./scripts/pull_debug_logs.sh -d ./my_logs -i 5   # 自定义目录和间隔
#   ./scripts/pull_debug_logs.sh --once              # 只拉一次，不轮询
#   ./scripts/pull_debug_logs.sh --list              # 只列出远端文件

set -euo pipefail

# ── 默认配置 ──────────────────────────────────────────
SSH_KEY="/f/Desktop/code_new.pem"
SSH_HOST="ubuntu@43.135.137.212"
REMOTE_DIR="/tmp/ai_debug_logs"
LOCAL_DIR="./debug_logs"
INTERVAL=10        # 轮询间隔（秒）
MODE="watch"       # watch | once | list
MAX_FILES=50       # 单次最多拉取文件数

# ── 参数解析 ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--dir)       LOCAL_DIR="$2";  shift 2 ;;
        -k|--key)       SSH_KEY="$2";    shift 2 ;;
        -H|--host)      SSH_HOST="$2";   shift 2 ;;
        -r|--remote)    REMOTE_DIR="$2"; shift 2 ;;
        -i|--interval)  INTERVAL="$2";   shift 2 ;;
        -m|--max)       MAX_FILES="$2";  shift 2 ;;
        --once)         MODE="once";     shift   ;;
        --list)         MODE="list";     shift   ;;
        -h|--help)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  -d, --dir DIR        本地保存目录 (默认: ./debug_logs)"
            echo "  -k, --key PATH       SSH 私钥路径"
            echo "  -H, --host USER@HOST SSH 连接地址"
            echo "  -r, --remote DIR     远端日志目录"
            echo "  -i, --interval SEC   轮询间隔秒数 (默认: 10)"
            echo "  -m, --max N          单次最多拉取文件数 (默认: 50)"
            echo "  --once               只拉取一次，不轮询"
            echo "  --list               只列出远端文件，不下载"
            echo "  -h, --help           显示帮助"
            exit 0
            ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

SSH_CMD="ssh -i $SSH_KEY $SSH_HOST"
SCP_CMD="scp -i $SSH_KEY"

# ── 颜色 ──────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

# ── 函数 ──────────────────────────────────────────────

remote_ls() {
    $SSH_CMD "ls -t $REMOTE_DIR/ 2>/dev/null" 2>/dev/null || echo ""
}

remote_file_count() {
    $SSH_CMD "ls $REMOTE_DIR/ 2>/dev/null | wc -l" 2>/dev/null || echo "0"
}

pull_files() {
    local files="$1"
    local count=0
    while IFS= read -r fname; do
        [[ -z "$fname" ]] && continue
        local local_path="$LOCAL_DIR/$fname"
        if [[ -f "$local_path" ]]; then
            continue  # 本地已有，跳过
        fi
        $SCP_CMD "$SSH_HOST:$REMOTE_DIR/$fname" "$local_path" 2>/dev/null
        if [[ $? -eq 0 ]]; then
            echo -e "${GREEN}[新增]${NC} $fname"
            ((count++))
        fi
    done <<< "$files"
    echo "$count"
}

list_remote() {
    echo -e "${CYAN}远端日志文件:${NC}"
    echo "─────────────────────────────────────────────"
    local files
    files=$(remote_ls)
    if [[ -z "$files" ]]; then
        echo "  (空)"
        return
    fi
    local n=0
    while IFS= read -r fname; do
        [[ -z "$fname" ]] && continue
        ((n++))
        if [[ $n -le 20 ]]; then
            echo "  $fname"
        fi
    done <<< "$files"
    if [[ $n -gt 20 ]]; then
        echo "  ... 还有 $((n - 20)) 个文件"
    fi
    echo "─────────────────────────────────────────────"
    echo "  共 $n 个文件"
}

# ── 主逻辑 ────────────────────────────────────────────

# list 模式
if [[ "$MODE" == "list" ]]; then
    list_remote
    exit 0
fi

# 创建本地目录
mkdir -p "$LOCAL_DIR"
echo -e "${CYAN}本地目录:${NC} $LOCAL_DIR"
echo -e "${CYAN}远端目录:${NC} $SSH_HOST:$REMOTE_DIR"
echo ""

# 首次拉取
echo -e "${YELLOW}首次拉取...${NC}"
remote_count=$(remote_file_count)
echo -e "远端文件数: ${CYAN}$remote_count${NC}"

files=$(remote_ls)
if [[ -z "$files" ]]; then
    echo "远端暂无日志文件"
else
    # 取最近 MAX_FILES 个
    files=$(echo "$files" | head -n "$MAX_FILES")
    new_count=$(pull_files "$files")
    echo -e "本次新增: ${GREEN}$new_count${NC} 个文件"
fi

# once 模式直接退出
if [[ "$MODE" == "once" ]]; then
    echo ""
    echo -e "${GREEN}完成。${NC}"
    exit 0
fi

# watch 模式
echo ""
echo -e "${YELLOW}进入轮询模式 (间隔 ${INTERVAL}s, Ctrl+C 退出)${NC}"
echo ""

seen_files=$(ls "$LOCAL_DIR" 2>/dev/null | sort)

while true; do
    sleep "$INTERVAL"

    current_files=$(remote_ls)
    [[ -z "$current_files" ]] && continue

    new_files=""
    while IFS= read -r fname; do
        [[ -z "$fname" ]] && continue
        if [[ ! -f "$LOCAL_DIR/$fname" ]]; then
            new_files+="$fname"$'\n'
        fi
    done <<< "$current_files"

    if [[ -n "$new_files" ]]; then
        timestamp=$(date '+%H:%M:%S')
        echo -e "${CYAN}[$timestamp]${NC} 发现新文件:"
        count=$(pull_files "$new_files")
        echo -e "  拉取: ${GREEN}$count${NC} 个"
        echo ""
    fi
done
