#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""readiness_server.py — 轻量 HTTP 接收端：iPhone「快捷指令」每天早晨把 Apple Watch
健康数据（睡眠 / HRV / 静息心率 / 主观）POST 过来，写成 readiness.json 并追加进历史基线。

纯标准库，无需 pip install。和 onelap_report.py 同目录，复用它的 append_readiness_history /
readiness_score / readiness_baseline。

用法：
  python3 readiness_server.py                         # 读 config.json 的 readiness_listen / readiness_token
  python3 readiness_server.py --port 8079 --token XXX # 命令行覆盖

安全：
  - POST 必须带 Authorization: Bearer <token>（或 ?token=<token>），与 readiness_token 一致，否则 401。
  - 字段做范围校验；输出路径固定为 readiness.json，不接受外部路径。
  - 未设 token 时只警告并继续（任何人都可写入）—— 仅建议本机调试这样用。
  - 健康数据 + token 若走明文 HTTP 有被窃听风险，强烈建议放到 nginx/caddy 反代后面走 HTTPS。

可选：readiness_trigger_auto=true 时，每次有效写入后（按日期去重）后台触发一次
  onelap_report.py --auto，让计划立刻用上当天 readiness（醒来→推送→计划刷新）。
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import onelap_report as R  # 复用 load_config / load_readiness / append_readiness_history / readiness_*

CONFIG_PATH = os.path.join(HERE, "config.json")
READINESS_PATH = os.path.join(HERE, "readiness.json")
TRIGGER_FLAG = os.path.join(HERE, ".readiness_last_trigger")
AUTO_LOG = os.path.join(HERE, "logs", "auto.log")


def _today_iso():
    return datetime.date.today().isoformat()


def validate_payload(obj):
    """校验 + 清洗 POST body。返回 (ok, dict_or_errmsg)。"""
    if not isinstance(obj, dict):
        return False, "body 必须是 JSON 对象"

    def num(key, lo, hi):
        v = obj.get(key)
        if v is None or v == "":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 不是数字: {v!r}")
        if not (lo <= f <= hi):
            raise ValueError(f"{key} 越界（应为 {lo}-{hi}）: {f}")
        return f

    try:
        s = num("sleep_h", 0, 24)
        h = num("hrv_ms", 0, 500)
        r = num("rhr_bpm", 0, 250)
        sub = num("subjective", 0, 10)
    except ValueError as e:
        return False, str(e)

    if s is None and h is None and r is None and sub is None:
        return False, "至少要有一个数据字段（sleep_h / hrv_ms / rhr_bpm / subjective）"

    out = {}
    if s is not None: out["sleep_h"] = s
    if h is not None: out["hrv_ms"] = h
    if r is not None: out["rhr_bpm"] = r
    if sub is not None: out["subjective"] = sub

    d = obj.get("date") or _today_iso()
    if not (isinstance(d, str) and len(d) == 10 and d[4] == "-" and d[7] == "-"):
        return False, f"date 应为 YYYY-MM-DD: {d!r}"
    out["date"] = d
    return True, out


def maybe_trigger_auto(enabled):
    """开启时，若今日尚未触发过，后台跑一次 onelap_report.py --auto（按日期去重，防快捷指令重试导致重复）。"""
    if not enabled:
        return False
    today = _today_iso()
    try:
        last = open(TRIGGER_FLAG, encoding="utf-8").read().strip()
    except FileNotFoundError:
        last = ""
    if last == today:
        return False
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    log_f = open(AUTO_LOG, "a", encoding="utf-8")
    subprocess.Popen([sys.executable, os.path.join(HERE, "onelap_report.py"), "--auto"],
                     cwd=HERE, stdout=log_f, stderr=subprocess.STDOUT,
                     start_new_session=True)  # detached：HTTP 响应立刻返回
    try:
        open(TRIGGER_FLAG, "w", encoding="utf-8").write(today)
    except Exception:
        pass
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "readiness/1.0"
    token = None
    trigger_auto = False

    # ---- helpers ----
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not self.token:
            return True  # 未设 token：放行（仅建议本机调试；启动时会警告）
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            auth = auth[7:].strip()
        tok_q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token", [None])[0]
        return auth == self.token or tok_q == self.token

    # ---- routes ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/readiness/health", "/health"):
            self._send(200, {"ok": True})
        elif path in ("/readiness/latest", "/readiness"):
            self._send(200, R.load_readiness() or {"readiness": None})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/readiness":
            self._send(404, {"error": "not found"})
            return
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            obj = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:
            self._send(400, {"error": f"bad json: {e}"})
            return
        ok, data = validate_payload(obj)
        if not ok:
            self._send(400, {"error": data})
            return
        # 原子写 readiness.json + 追加历史基线
        tmp = READINESS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, READINESS_PATH)
        R.append_readiness_history(data)
        triggered = maybe_trigger_auto(self.trigger_auto)
        sc = R.readiness_score(data, R.readiness_baseline())
        self._send(200, {"ok": True, "readiness": data,
                         "score": sc, "triggered_auto": triggered})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def load_settings():
    cfg = {}
    try:
        cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    except Exception:
        pass
    listen = cfg.get("readiness_listen") or "127.0.0.1:8079"
    token = cfg.get("readiness_token") or ""
    trigger = bool(cfg.get("readiness_trigger_auto"))
    return listen, token, trigger


def main():
    ap = argparse.ArgumentParser(description="readiness 接收端：iPhone 快捷指令 POST Apple Watch 健康数据")
    ap.add_argument("--listen", help="host:port（默认 config.readiness_listen 或 127.0.0.1:8079）")
    ap.add_argument("--port", type=int, help="端口（覆盖 listen 的 port）")
    ap.add_argument("--token", help="鉴权 token（默认 config.readiness_token）")
    ap.add_argument("--trigger-auto", action="store_true", help="数据到达后后台触发一次 onelap_report.py --auto")
    args = ap.parse_args()

    listen, token, trigger = load_settings()
    if args.listen: listen = args.listen
    if args.token: token = args.token
    if args.trigger_auto: trigger = True
    if "://" in listen:  # 防止误填 http://host:port
        listen = listen.split("://", 1)[1]
    host, _, port_s = listen.partition(":")
    host = host or "127.0.0.1"
    port = args.port or int(port_s or 8079)

    Handler.token = token or None
    Handler.trigger_auto = trigger

    if not token:
        sys.stderr.write("⚠️  未设 readiness_token —— 任何人都可写入！仅建议本机调试。\n"
                         "   在 config.json 设 readiness_token，或加 --token <随机长串>。\n")
    if host in ("0.0.0.0", "::") and not token:
        sys.stderr.write("⛔ 监听公网却没设 token，拒绝启动。请先设 readiness_token。\n")
        sys.exit(1)

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"readiness 接收端监听 http://{host}:{port}   token={'已设' if token else '未设'}   trigger_auto={trigger}")
    print(f"POST /readiness  (Authorization: Bearer <token>)  →  写 {READINESS_PATH}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
