#!/usr/bin/env bash
# ============================================================================
# onelap-train 一键部署脚本（Alibaba Cloud Linux 3 / 任意 RHEL 系）
# 在全新的 ECS 上以 root 运行：  sudo bash deploy.sh
#
# 做的事：装 Python≥3.7 → 取代码 → 配 config → systemd 常驻 receiver → cron 每天4点 → 防火墙 → 自测
# 幂等：可重复运行，不会破坏已有 config.json 的密钥。
# ============================================================================
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/onelap-train}"
REPO="${REPO:-https://github.com/MorantHP/onelap-train.git}"
PORT="${PORT:-8079}"
export PORT

c_blue() { printf '\033[1;34m%s\033[0m\n' "$*"; }
c_green(){ printf '\033[1;32m%s\033[0m\n' "$*"; }
c_red()  { printf '\033[1;31m%s\033[0m\n' "$*"; }

# ----------------------------------------------------------------------------
c_blue "==== 1/8  选 Python（≥3.7，readiness_server 需要 ThreadingHTTPServer）===="
pick_python() {
  for cand in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
    p=$(command -v "$cand" 2>/dev/null) || continue
    if "$p" -c 'import sys;sys.exit(0 if sys.version_info>=(3,7) else 1)' 2>/dev/null; then
      command -v "$p"; return 0
    fi
  done
  return 1
}
PY=$(pick_python || true)
if [ -z "$PY" ]; then
  c_blue "默认 Python 不到 3.7，装 python39 …"
  dnf install -y python39 2>/dev/null || dnf install -y python3 2>/dev/null || true
  PY=$(command -v python3.9 || command -v python3)
fi
if [ -z "$PY" ] || ! "$PY" -c 'import sys;sys.exit(0 if sys.version_info>=(3,7) else 1)' 2>/dev/null; then
  c_red "!! 找不到 ≥3.7 的 Python。手动装：dnf install -y python39，然后重跑本脚本。"; exit 1
fi
PY_ABS=$(command -v "$PY")
c_green "用 Python：$PY_ABS  ($("$PY" --version 2>&1))"

# ----------------------------------------------------------------------------
c_blue "==== 2/8  装系统依赖（cronie / curl / git）===="
dnf install -y cronie curl git 2>/dev/null || true
systemctl enable --now crond 2>/dev/null || true

# ----------------------------------------------------------------------------
c_blue "==== 3/8  取代码到 $APP_DIR ===="
mkdir -p "$APP_DIR"; cd "$APP_DIR"
if [ -f onelap_report.py ]; then
  c_green "onelap_report.py 已存在，跳过下载（沿用你 scp 上来的文件）。"
elif git clone --depth 1 "$REPO" /tmp/_onelap_clone 2>/dev/null; then
  cp /tmp/_onelap_clone/*.py /tmp/_onelap_clone/config.example.json "$APP_DIR"/ 2>/dev/null || true
  rm -rf /tmp/_onelap_clone
  c_green "已从 GitHub 拉取代码。"
else
  c_red "!! GitHub 拉取失败（国内 ECS 访问 GitHub 常不稳）。请在本地 Windows 用 scp 上传："
  echo "    scp onelap_report.py readiness_server.py config.example.json deploy.sh root@<ECS公网IP>:$APP_DIR/"
  echo "  传完再重跑本脚本。"; exit 1
fi
[ -f readiness_server.py ] || { c_red "缺 readiness_server.py，请 scp 上来再重跑。"; exit 1; }

# 编译自检
"$PY" -m py_compile onelap_report.py readiness_server.py && c_green "代码语法 OK" || { c_red "代码语法错"; exit 1; }

# ----------------------------------------------------------------------------
c_blue "==== 4/8  config.json（沿用已有；没有则从模板复制）===="
if [ ! -f config.json ]; then
  cp config.example.json config.json
  c_green "从 config.example.json 复制为 config.json。"
  c_red "记得填真实密钥：token / refresh_token / glm_api_key / serverchan_key / coach_profile"
fi

# 补 readiness_* 三键（已有不覆盖；token 没设就生成一个并打印）
c_blue "--- 确认/补 readiness_* 配置 ---"
"$PY" - <<'PYEOF'
import json, os, secrets
p = 'config.json'
c = json.load(open(p, encoding='utf-8'))
port = os.environ.get('PORT', '8079')
chg = False
if not c.get('readiness_listen'):
    c['readiness_listen'] = f'0.0.0.0:{port}'; chg = True
if not c.get('readiness_token'):
    c['readiness_token'] = secrets.token_hex(24); chg = True
    print('!! 新生成 readiness_token（记下来，快捷指令要用）:\n    ', c['readiness_token'])
if 'readiness_trigger_auto' not in c:
    c['readiness_trigger_auto'] = True; chg = True
if chg:
    json.dump(c, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print('config.json 已写入 readiness_* 三键')
else:
    print('config.json 已有 readiness_* 三键，未改动。token =', c.get('readiness_token'))
PYEOF

# ----------------------------------------------------------------------------
c_blue "==== 5/8  systemd 常驻 readiness_server（开机自启 + 崩溃重启）===="
cat > /etc/systemd/system/onelap-readiness.service <<EOF
[Unit]
Description=Onelap readiness receiver (Apple Watch 健康数据接收端)
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$PY_ABS $APP_DIR/readiness_server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now onelap-readiness
systemctl --no-pager --lines=3 status onelap-readiness 2>/dev/null || true

# ----------------------------------------------------------------------------
c_blue "==== 6/8  cron：每天 04:00 跑 onelap_report.py --auto（北京时间）===="
# 时区先设成上海（否则 cron 不是北京时间）
timedatectl set-timezone Asia/Shanghai 2>/dev/null || true
( crontab -l 2>/dev/null | grep -v 'onelap_report.py --auto' \
; echo "0 4 * * * cd $APP_DIR && mkdir -p logs && $PY_ABS onelap_report.py --auto >> logs/auto.log 2>&1" ) | crontab -
c_green "已安装 cron 任务："; crontab -l | grep onelap_report || true

# ----------------------------------------------------------------------------
c_blue "==== 7/8  放行端口 $PORT ===="
if systemctl is-active --quiet firewalld 2>/dev/null; then
  firewall-cmd --permanent --add-port=$PORT/tcp 2>/dev/null && firewall-cmd --reload 2>/dev/null
  c_green "firewalld 已放行 TCP $PORT"
else
  c_red "firewalld 未启用（阿里云默认靠安全组）。必须去【阿里云控制台 → 安全组】放行 TCP $PORT，否则手机连不上！"
fi

# ----------------------------------------------------------------------------
c_blue "==== 8/8  本机自测 ===="
sleep 1
if curl -sf "http://127.0.0.1:$PORT/readiness/health" >/dev/null 2>&1; then
  c_green "✅ receiver 起来了：http://127.0.0.1:$PORT/readiness/health -> {\"ok\":true}"
else
  c_red "❌ 健康检查失败。看日志：journalctl -u onelap-readiness --no-pager -n 30"
fi

echo
c_green "================ 部署完成 ================"
echo "接下来 3 件事："
echo " 1) 填 config.json 真实密钥（若还没填）：token / refresh_token / glm_api_key / serverchan_key / coach_profile"
echo " 2) 【阿里云控制台 → 安全组】放行 TCP $PORT（必做，否则手机连不上）"
TOKEN=$("$PY" -c 'import json;print(json.load(open("config.json",encoding="utf-8")).get("readiness_token",""))' 2>/dev/null)
echo " 3) 快捷指令 URL: http://<ECS公网IP>:$PORT/readiness"
echo "    请求头 Authorization: Bearer $TOKEN"
echo " 4) 验证端到端：$PY_ABS $APP_DIR/onelap_report.py --auto   （会刷新 token+生成计划+导入+推微信）"
echo "=========================================="
