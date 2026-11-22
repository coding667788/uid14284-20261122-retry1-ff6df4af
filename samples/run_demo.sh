#!/usr/bin/env bash
# 一键演示：在项目根下的临时目录跑全部虚构样例，仅依赖标准库。
# 用法（项目根目录）： bash samples/run_demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TMP=".demo-tmp"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

# runner 是实体文件，配合 -I 保持隔离（忽略用户站点包、环境注入的 json.py 等）。
# 全部使用相对路径，避开 Git Bash 与 Windows Python 之间的路径转换问题。
run() {
  python -I solution/runner.py "$@"
}

echo "== 1) 初始化账号/受托人/证明/配方 =="
run ingest samples/01_bootstrap.json --db "$TMP/state.sqlite" >/dev/null
echo "OK"

echo "== 2) 冻结窗口内取消（预留证明永久锁定，回执可回放） =="
run ingest samples/02_reserve_challenge_cancel.json --db "$TMP/state.sqlite" >/dev/null
echo "OK"

echo "== 3) 全新数据库上的完整 reserve -> activate 身份世代推进 =="
run ingest samples/01_bootstrap.json --db "$TMP/happy.sqlite" >/dev/null
run ingest samples/03_reserve_activate.json --db "$TMP/happy.sqlite" >/dev/null
echo "OK"

echo "== 4) 同截止点旧认证器异议优先（即使 seq 排在撤销事件之后） =="
run ingest samples/01_bootstrap.json --db "$TMP/cutoff.sqlite" >/dev/null
run ingest samples/04_cutoff_challenge_priority.json --db "$TMP/cutoff.sqlite" >/dev/null
echo "OK"

echo "== 5) simulate_loss 离线演练：最小准备清单（无 --db，纯计算） =="
run simulate_loss samples/05_simulate_loss.json
echo

echo "== 6) simulate_loss 无解诊断（退出码 1，输出第一个失败情形与缺失要求） =="
set +e
run simulate_loss samples/06_simulate_no_plan.json
code=$?
set -e
echo "exit code = $code （预期 1）"

echo
echo "全部样例执行完毕。"
