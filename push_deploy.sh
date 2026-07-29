#!/usr/bin/env bash
# ============================================================================
# push_deploy.sh — 本地直推部署（绕开 GitHub HTTPS 抖动）
# ----------------------------------------------------------------------------
# 链路：scp 源文件到服务器 → 远程单测门禁（失败中止）→ 重启 readiness 服务。
# 走已验证稳定的 SSH（本机 ~/.ssh/config 别名 lfy）。
#
# 用法：
#   ./push_deploy.sh                 # 推全部源文件
#   ./push_deploy.sh onelap_report.py # 只推指定文件
#
# 完整工作流：本地 commit → git push（GitHub 存档）→ ./push_deploy.sh（真正上线）
# ============================================================================
set -euo pipefail

HOST="${LFY_HOST:-lfy}"
REMOTE_DIR="${LFY_DIR:-/opt/onelap-train}"
PY="${LFY_PY:-/usr/bin/python3.11}"

# 默认推送的源文件（可用参数覆盖）
DEFAULT_FILES="onelap_report.py readiness_server.py import_apple_health.py test_onelap.py"
FILES=("$@")
[ ${#FILES[@]} -eq 0 ] && FILES=($DEFAULT_FILES)

cd "$(dirname "$0")"

echo "=== 1/3) scp 源文件 → ${HOST}:${REMOTE_DIR}/ ==="
for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "❌ 本地不存在: $f"; exit 1; }
done
scp -q "${FILES[@]}" "${HOST}:${REMOTE_DIR}/"
echo "    已推送: ${FILES[*]}"

echo "=== 2/3) 远程单测门禁（失败则中止部署）==="
if ! ssh "${HOST}" "cd ${REMOTE_DIR} && ${PY} -m unittest test_onelap >/tmp/_deploy_test.log 2>&1"; then
  echo "❌ 远程单测失败，部署中止："
  ssh "${HOST}" "tail -25 /tmp/_deploy_test.log"
  exit 1
fi
echo "    远程单测通过。"

echo "=== 3/3) 重启 onelap-readiness 服务 ==="
ssh "${HOST}" "systemctl restart onelap-readiness && sleep 2 && systemctl is-active onelap-readiness"

echo "=== ✅ 部署完成 ==="
