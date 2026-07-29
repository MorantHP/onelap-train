#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Onelap OTM 训练分析报告
================================================================
抓取 https://otm.onelap.cn 的训练数据，告诉你：
  1. 最近的训练强度（PMC：体能 CTL / 疲劳 ATL / 状态 TSB；以及近 N 天实际骑行的 TSS/距离/时长/功率/心率）
  2. 未来几日的训练安排（OTM 课表里排定的训练课；若没有排课会明确说明）

接口与字段均从前端 JS 逆向 + 真实账号实测确认（见 README）。

【认证】OTM 用 localStorage 里的 token 放在 Authorization 请求头认证。
  取 token：浏览器登录 otm.onelap.cn → F12 → Application(应用) → Local Storage
            → https://otm.onelap.cn → 名为 "token" 的值。

【依赖】仅 Python 3.7+ 标准库，无需 pip install。

【用法】
  python onelap_report.py            # 正常运行
  python onelap_report.py --raw      # 额外落盘原始 JSON（调试用）
  python onelap_report.py --sample   # 假数据预览（不联网）
  python onelap_report.py --days-back 14 --days-ahead 21
"""

import argparse
import atexit
import base64
import json
import math
import os
import re
import ssl
import sys
import time
import uuid
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

# 图表（飞书卡片用）：Pillow 可选——装了才出图，没装退回文字执行率段（核心仍纯标准库）
try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

# Windows 控制台默认 GBK，打印中文会报错；强制 stdout/stderr 用 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_URL = "https://otm.onelap.cn"


def log(msg):
    """带时间戳写一行到 stderr。--auto 下用 cron 的 `>> logs/auto.log 2>&1` 把全部输出落盘。"""
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", file=sys.stderr)


_SSL_WARNED = False


def _urlopen(req, timeout):
    """带 SSL 容错的 urlopen：证书校验失败时（常见于有 TLS 拦截代理/自签根证书的服务器，
    报 'self signed certificate in certificate chain'）自动回退到不校验证书的模式，只警告一次。
    其余错误（网络断、超时等）照常抛出。"""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        _CertErr = getattr(ssl, "SSLCertVerificationError", None)  # Python 3.7.3+ 才有；3.6 取不到
        is_cert = (_CertErr is not None and isinstance(reason, _CertErr)) or "CERTIFICATE" in str(reason)
        if not is_cert:
            raise
        global _SSL_WARNED
        if not _SSL_WARNED:
            log(f"⚠️ SSL 证书校验失败（{reason}）→ 改用不校验证书模式继续。"
                f"建议：给服务器更新 ca-certificates 或导入代理根证书后可恢复校验。")
            _SSL_WARNED = True
        return urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# 已确认的接口（均已实测可达）
EP_PMC = "/api/otm/calendar/weekly_summary/pmc"   # POST {start_date,end_date}
EP_WORKOUT_LIST = "/api/otm/calendar/workout/list" # POST {start_date,end_date} → 课表规划器
EP_WORKOUT_CREATE = "/api/otm/calendar/workout"      # POST 创建训练课 → wid
EP_WORKOUT_PLAN = "/api/otm/calendar/workout/plan"   # POST 把课排到日期
EP_RIDE_LIST = "/api/otm/ride_record/list"         # POST {startTime,end_time}  → 实际骑行记录
EP_TRAINING_PLANS = "/api/otm/training/plans"      # GET
EP_USERINFO = "/api/userinfo"                       # GET → 账号 FTP/MHR/LTHR/体重（自动同步用）

# AI 教练固定使用 glm-5.2（不可切换）
COACH_MODEL = "glm-5.2"


# ---------------------------------------------------------------------------
# --auto 崩溃兜底：未捕获异常退出时推送告警 + 清 readiness 触发锁（保证无人值守可见、可重试）
#
# 契约（main ↔ atexit）：_AUTO_CRASH_CTX 是两者唯一的通信通道。
#   - main() 进入 --auto 时置 auto=True/today；正常完成或「今日已跑过」跳过时置 done=True；
#     拿到 cfg 后回填 ctx['cfg'] 供告警推送用。
#   - _auto_crash_handler 在进程退出时检查：仅当 auto 且未 done（=中途崩溃/被中断）才告警 + 清锁。
#   - 正常结束后清空 ctx['cfg']，避免引用驻留。
# 用模块级可变 holder 是因为 atexit 回调拿不到运行期才确定的 cfg——这是该约束下的惯用法。
# ---------------------------------------------------------------------------
_AUTO_CRASH_CTX = {"auto": False, "done": False, "today": None, "cfg": {}}


def _auto_crash_handler():
    """进程退出时（atexit）兜底：--auto 未正常完成则推送告警、清当日 readiness 触发锁。
    正常完成会在写 last_auto_run.txt 时把 done 置 True，本函数即跳过；故仅在「中途崩溃/被中断」时触发。"""
    ctx = _AUTO_CRASH_CTX
    if not ctx.get("auto") or ctx.get("done"):
        return
    cfg = ctx.get("cfg") or {}
    try:
        push_alert(cfg, "⚠️ OTM 自动教练未完成",
                   f"--auto 在 {ctx.get('today')} 未能正常跑完（可能抛异常或被中断），"
                   f"计划可能未生成/未导入。今日未标记完成 → 备份 cron 会重试；"
                   f"已清除 readiness 触发锁 → iPhone 捷径重传也会重试。详见 logs/auto.log。")
    except Exception:
        pass
    try:
        os.remove(os.path.join(HERE, ".readiness_last_trigger"))
    except OSError:
        pass


atexit.register(_auto_crash_handler)


# --auto 并发锁：readiness 多次更新触发 / cron 与触发同时跑时，只让一个 --auto 跑（避免日历/config 写冲突）
AUTO_LOCK = os.path.join(HERE, ".auto.lock")


def _acquire_auto_lock(stale_sec=600):
    """独占获取 --auto 锁。拿到返回 True；已有运行中（锁新鲜）返回 False；过期(>stale_sec)锁自动抢占。"""
    pid = str(os.getpid()).encode()
    try:
        fd = os.open(AUTO_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, pid); os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(AUTO_LOCK)
        except OSError:
            age = stale_sec + 1
        if age < stale_sec:
            return False
        try:  # 过期锁：抢占
            os.remove(AUTO_LOCK)
            fd = os.open(AUTO_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, pid); os.close(fd)
            return True
        except OSError:
            return False


def _release_auto_lock():
    try:
        os.remove(AUTO_LOCK)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 配置 & HTTP 层
# ---------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(
            "未找到 config.json。请复制 config.example.json 为 config.json，"
            "并填入你的 token。详见 README.md。"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    token = (cfg.get("token") or "").strip()
    cookie = (cfg.get("cookie") or "").strip()
    if not token and not cookie:
        raise SystemExit("config.json 里 token 和 cookie 都为空，请至少填一个（推荐 token）。")
    return cfg


class ApiError(RuntimeError):
    pass


def _auth_headers(cfg):
    headers = {}
    token = (cfg.get("token") or "").strip()
    cookie = (cfg.get("cookie") or "").strip()
    if token:
        headers["Authorization"] = token
    if cookie:
        headers["Cookie"] = cookie if "=" in cookie else f"token={cookie}"
    return headers


def save_config(cfg):
    """原子写回 config.json（刷新 token 后持久化新 token/refresh_token）。"""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def refresh_access_token(cfg):
    """用 refresh_token 换新的 access token（POST /api/token {token,from,to}→{token,refresh_token}），
    原子写回 config。返回 (cfg, 是否刷新)。未配置 refresh_token 则跳过（沿用静态 token，过期会失败）。"""
    rt = (cfg.get("refresh_token") or "").strip()
    if not rt:
        return cfg, False
    body = json.dumps({"token": rt, "from": "web", "to": "web"}).encode("utf-8")
    headers = {"Content-Type": "application/json;charset=UTF-8",
               "User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    tok = (cfg.get("token") or "").strip()
    if tok:
        headers["Authorization"] = tok  # 前端 directRequest 会带，这里也带上（可能已过期，服务端以 body 为准）
    req = urllib.request.Request(BASE_URL + "/api/token", data=body, method="POST", headers=headers)
    with _urlopen(req, 30) as r:
        obj = json.loads(r.read().decode("utf-8"))
    data = obj.get("data") if isinstance(obj, dict) and "code" in obj else obj
    new_tok = (data or {}).get("token")
    if not new_tok:
        raise ApiError(f"token 刷新失败: {json.dumps(obj, ensure_ascii=False)[:200]}")
    cfg["token"] = new_tok
    if (data or {}).get("refresh_token"):
        cfg["refresh_token"] = data["refresh_token"]  # refresh_token 每次轮换，必须持久化新的
    save_config(cfg)
    return cfg, True


def refresh_token_days_left(cfg):
    """估算 refresh_token 剩余有效天数（解 token 的 payload 取 exp；无法解析返回 None）。
    OTM refresh_token 约 60 天有效且不轮换——快到期需主动提醒，否则某天自动流程会静默断档。
    OTM token 实为 payload.signature（无 header 段，2 段），与标准 JWT（3 段）不同，
    故遍历各段、取第一个能解出 exp 的 JSON。"""
    tok = (cfg.get("refresh_token") or "").strip()
    if not tok or "." not in tok:
        return None
    for seg in tok.split("."):
        try:
            p = seg + "=" * (-len(seg) % 4)  # base64url 补齐 padding
            info = json.loads(base64.urlsafe_b64decode(p.encode("ascii")).decode("utf-8"))
            exp = info.get("exp") if isinstance(info, dict) else None
            if exp:
                return (int(exp) - int(time.time())) / 86400.0
        except Exception:
            continue
    return None


def api_request(cfg, path, method="POST", body=None, params=None):
    url = BASE_URL + path
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/analysisPage",
        "Accept": "application/json, text/plain, */*",
    }
    headers.update(_auth_headers(cfg))

    data = None
    if method == "POST":
        headers["Content-Type"] = "application/json;charset=UTF-8"
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    elif params:
        from urllib.parse import urlencode
        url = url + "?" + urlencode(params)

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _urlopen(req, 30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            raise ApiError(
                f"认证失败（HTTP {e.code}）。token 已过期或无效，请重新登录 otm.onelap.cn "
                f"并更新 config.json 里的 token。\n响应: {body_text}")
        raise ApiError(f"HTTP {e.code} on {path}: {body_text}")
    except urllib.error.URLError as e:
        raise ApiError(f"网络错误 on {path}: {e.reason}")

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(f"{path} 返回的不是 JSON，前 300 字符: {raw[:300]}")

    # OTM 信封：{code:200, data:...}。code 缺失时整体当作 data。
    if isinstance(obj, dict) and "code" in obj:
        code = obj.get("code")
        if code in (200, 0, "200", "0"):
            return obj.get("data")
        msg = obj.get("message") or obj.get("msg") or obj.get("error") or "(无消息)"
        raise ApiError(f"{path} 业务错误 code={code}: {msg}")
    return obj


# ---------------------------------------------------------------------------
# 业务接口
# ---------------------------------------------------------------------------
def get_pmc(cfg, start_date, end_date):
    """每日 PMC：ctl/atl/tsb。返回 list[dict]。"""
    return api_request(cfg, EP_PMC, "POST", {"start_date": start_date, "end_date": end_date}) or []


def get_workout_calendar(cfg, start_date, end_date):
    """课表规划器：{groups:[...], personal:{workouts:[...]}}。"""
    return api_request(cfg, EP_WORKOUT_LIST, "POST", {"start_date": start_date, "end_date": end_date}) or {}


def get_ride_records(cfg, days_window):
    """实际骑行记录。返回 data.list（最近若干条，新→旧）。"""
    today = date.today()
    start = (today - timedelta(days=days_window))
    body = {
        "startTime": int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000),
        "endTime": int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp() * 1000) + 86_400_000,
    }
    data = api_request(cfg, EP_RIDE_LIST, "POST", body)
    if isinstance(data, dict):
        return data.get("list") or []
    return data or []


def get_training_plans(cfg):
    try:
        return api_request(cfg, EP_TRAINING_PLANS, "GET", params={})
    except ApiError as e:
        print(f"  (训练计划接口跳过: {e})", file=sys.stderr)
        return {}


def get_userinfo(cfg):
    """GET /api/userinfo → 账号生理数据(FTP/MHR/LTHR/体重)。只读，失败返回 None。"""
    try:
        return api_request(cfg, EP_USERINFO, "GET")
    except ApiError as e:
        log(f"OTM userinfo 获取失败: {e}")
        return None


def sync_physiology_from_otm(cfg, info):
    """用 userinfo 更新 coach_profile 的 ftp/mhr/lthr/体重；有变化才原子写回 config。
    返回是否有改动。字段名容错（多候选 key）；取不到会打印可用 keys 便于排查。"""
    if not isinstance(info, dict):
        return False
    prof = cfg.setdefault("coach_profile", {})
    ftp = to_num(first(info, "ftp", "FTP", "ftp_w", "ftp_value"))
    mhr = to_num(first(info, "max_heart_rate", "maxHeartRate", "mhr", "MHR", "max_heart_rate_bpm", "max_hr"))
    lthr = to_num(first(info, "lthr", "LTHR", "lactate_threshold_hr", "lt_hr"))
    changed = False
    if ftp and ftp != to_num(prof.get("ftp")):
        prof["ftp"] = int(round(ftp)); changed = True
    if mhr and mhr != to_num(prof.get("mhr")):
        prof["mhr"] = int(round(mhr)); changed = True
    if lthr and lthr != to_num(prof.get("lthr")):
        prof["lthr"] = int(round(lthr)); changed = True
    if changed:
        save_config(cfg)
        log(f"OTM 生理数据已同步写回 config：FTP={prof.get('ftp')} MHR={prof.get('mhr')} LTHR={prof.get('lthr')}")
    elif ftp is None and mhr is None and lthr is None:
        log(f"userinfo 未识别出生理字段，可用 keys: {list(info.keys())}")
    return changed


# ---------------------------------------------------------------------------
# 字段容错提取
# ---------------------------------------------------------------------------
def first(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return default


def to_num(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def fmt_num(v, ndigits=0, suffix=""):
    n = to_num(v)
    return "--" if n is None else f"{n:.{ndigits}f}{suffix}"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_date(s):
    s = str(s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19] if " " in s else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def pmc_items(pmc_list):
    """规整 PMC：[(date, ctl, atl, tsb, tss)]，按日期升序，丢掉无日期的。"""
    out = []
    for it in pmc_list:
        d = parse_date(first(it, "date", "day", "dateStr"))
        if not d:
            continue
        out.append((d, to_num(first(it, "ctl", "CTL")),
                       to_num(first(it, "atl", "ATL")),
                       to_num(first(it, "tsb", "TSB")),
                       to_num(first(it, "tss", "TSS"))))
    out.sort(key=lambda x: x[0])
    return out


def ride_items(records):
    """规整骑行记录：[{date,name,tss,duration_s,distance_km,avg_power,avg_hr,avg_speed}]。"""
    out = []
    for r in records:
        d = parse_date(first(r, "start_riding_time", "date", "created_at"))
        if not d:
            continue
        out.append({
            "date": d,
            "name": first(r, "name", "title", default="骑行"),
            "tss": to_num(first(r, "load_tss", "tss", "TSS")),
            "duration_s": to_num(first(r, "time_seconds", "duration")),
            "distance_km": to_num(first(r, "distance_km", "distance", "totalDistance")),
            "avg_power": to_num(first(r, "avg_power_w", "avgPower")),
            "avg_hr": to_num(first(r, "avg_heart_bpm", "avgHeart", "avg_hr")),
            "avg_speed": to_num(first(r, "avg_speed_kmh", "avgSpeed")),
        })
    out.sort(key=lambda x: x["date"])
    return out


def planned_workouts(calendar):
    """从课表规划器里抽出排定的训练课：list[dict(date,name,tss,duration_s,if_score)]。"""
    raw = []
    if isinstance(calendar, dict):
        personal = calendar.get("personal") or {}
        groups = calendar.get("groups") or []
        raw = (personal.get("workouts") or []) + [w for g in groups for w in (g.get("workouts") or [])]
    elif isinstance(calendar, list):
        raw = calendar

    out = []
    for w in raw:
        d = parse_date(first(w, "date", "day", "start_time", "startTime"))
        if not d:
            continue
        out.append({
            "date": d,
            "name": first(w, "name", "title", "plan_name", "workout_name", default="训练课"),
            "tss": to_num(first(w, "tss", "TSS", "target_tss")),
            "duration_s": to_num(first(w, "duration", "time_seconds", "totalDuration")),
            "if_score": to_num(first(w, "if", "IF", "ifScore", "intensityFactor")),
        })
    out.sort(key=lambda x: x["date"])
    return out


WD = ["一", "二", "三", "四", "五", "六", "日"]


def weekday_cn(d):
    return "周" + WD[d.weekday()]


def human_duration(seconds):
    s = to_num(seconds)
    if s is None:
        return "--"
    m = int(round(s / 60.0))
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def tsb_interp(tsb):
    if tsb > 5:
        return "状态好、较清爽，适合比赛或冲强度"
    if tsb > -10:
        return "中性区间，可正常训练"
    if tsb > -30:
        return "有一定疲劳积累，注意恢复"
    return "严重疲劳 / 过载风险高，建议减量休息"


# ---------------------------------------------------------------------------
# 天气（Open-Meteo，免费无 key）：北京 16 区 + 常骑地点 → 当日天气 + AQI + 户外适宜度
#   预报 https://api.open-meteo.com/v1/forecast（daily+hourly+current）
#   空气 https://air-quality-api.open-meteo.com/v1/air-quality?current=us_aqi,pm2_5,pm10
#   16 区 + 常骑点并发抓（ThreadPoolExecutor），复用 _urlopen 的 SSL 容错。
#   天气是可降级的增强项：全链路失败→None→报告省略天气章节、教练 prompt 不带天气，绝不阻断主流程。
# ---------------------------------------------------------------------------
# 北京 16 区中心经纬度（name, lat, lon, group）
BEIJING_DISTRICTS = [
    ("东城", 39.93, 116.41, "城六区"), ("西城", 39.91, 116.36, "城六区"),
    ("朝阳", 39.92, 116.44, "城六区"), ("海淀", 39.96, 116.29, "城六区"),
    ("丰台", 39.85, 116.28, "城六区"), ("石景山", 39.90, 116.22, "城六区"),
    ("门头沟", 39.93, 116.10, "近郊"), ("房山", 39.72, 116.14, "近郊"),
    ("通州", 39.91, 116.65, "近郊"), ("顺义", 40.13, 116.65, "近郊"),
    ("昌平", 40.22, 116.23, "近郊"), ("大兴", 39.72, 116.33, "近郊"),
    ("怀柔", 40.32, 116.63, "远郊山区"), ("平谷", 40.14, 117.11, "远郊山区"),
    ("密云", 40.37, 116.83, "远郊山区"), ("延庆", 40.46, 115.97, "远郊山区"),
]
_DISTRICT_COORDS = {n: (la, lo) for n, la, lo, _ in BEIJING_DISTRICTS}

# 常骑地点精确坐标（home_district 优先从此解析，逗号分隔可多个）。
# 南海子公园实际位于大兴/亦庄一带；戒台寺在门头沟（经典爬坡）。
RIDING_SPOTS = {
    "南海子公园": (39.76, 116.50),
    "戒台寺": (39.97, 116.09),
}

# 默认只抓今天 + 明天两天（远期预报不准，按用户要求精简）
WEATHER_FORECAST_DAYS = 2

# 「周末去哪骑」候选（近/远郊热门骑行地）——仅周五 / 节假日(config.holiday_dates)触发时展示
WEEKEND_DESTINATIONS = ["门头沟", "昌平", "怀柔", "延庆", "密云", "平谷", "房山", "顺义", "大兴", "通州"]

# Open-Meteo WMO weather_code → 中文（白天语义，够用）
_WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "毛毛雨", 53: "小雨", 55: "中雨", 56: "冻毛雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "霰",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨冰雹", 99: "强雷阵雨冰雹",
}

_OM_FORECAST = "https://api.open-meteo.com/v1/forecast"
_OM_AQI = "https://air-quality-api.open-meteo.com/v1/air-quality"

# daily 列 → 规整键
_WCOLKEY = {"temperature_2m_max": "tmax", "temperature_2m_min": "tmin",
            "apparent_temperature_max": "apparent_max",
            "precipitation_probability_max": "precip_prob",
            "wind_speed_10m_max": "wind_max", "wind_direction_10m_dominant": "wind_dir",
            "uv_index_max": "uv", "weather_code": "weather_code"}
_WCOLS = list(_WCOLKEY.keys())


def wmo_desc(code):
    try:
        return _WMO.get(int(code), "—")
    except (TypeError, ValueError):
        return "—"


def aqi_level(us_aqi):
    """US AQI → 中文等级。None → '—'。"""
    if us_aqi is None:
        return "—"
    a = us_aqi
    if a <= 50: return "优"
    if a <= 100: return "良"
    if a <= 150: return "轻度污染"
    if a <= 200: return "中度污染"
    if a <= 300: return "重度污染"
    return "严重污染"


def deg_to_dir(deg):
    """风向度数 → 8 方位中文（风从哪吹来）。None → '—'。"""
    if deg is None:
        return "—"
    try:
        d = float(deg) % 360
    except (TypeError, ValueError):
        return "—"
    dirs = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return dirs[int((d + 22.5) // 45) % 8]


def uv_level(uv):
    """UV 指数 → 等级（WHO）。None → '—'。"""
    if uv is None:
        return "—"
    u = uv
    if u < 3: return "弱"
    if u < 6: return "中等"
    if u < 8: return "强"
    if u < 11: return "很强"
    return "极强"


_TAG_ORDER = {"不宜户外": 0, "注意": 1, "宜": 2}


def _worst_tag(tags):
    """多个 tag 取最保守（最差）。无有效值返回 '—'。"""
    valid = [t for t in tags if t in _TAG_ORDER]
    return min(valid, key=lambda t: _TAG_ORDER[t]) if valid else "—"


def weather_suitability(apparent_max, precip_prob, wind_max, aqi, weather_code=None):
    """规则判定【户外骑行】适宜度 → (tag, reasons[])。tag ∈ 宜/注意/不宜户外。阈值透明可解释。
    注：aqi 仅有实时值（无逐日预报），未来日评估时传 None（只看温/雨/风/恶劣天气码）。"""
    reasons, level = [], 0
    # 恶劣天气码（雷电/冰雹/大雨=不宜；中雨/雪=注意）——对骑行是硬性危险，不仅看降水概率
    try:
        wc = int(weather_code) if weather_code is not None else None
    except (TypeError, ValueError):
        wc = None
    if wc is not None:
        if wc in (65, 82, 95, 96, 99):  # 大雨/强阵雨/雷阵雨/雷阵雨冰雹
            reasons.append(wmo_desc(wc)); level = 2
        elif wc in (63, 81, 71, 73, 75, 77, 85, 86):  # 中雨/中阵雨/雪
            reasons.append(wmo_desc(wc)); level = max(level, 1)
    if aqi is not None:
        if aqi > 200:
            reasons.append(f"AQI{aqi:.0f}重度污染"); level = 2
        elif aqi > 150:
            reasons.append(f"AQI{aqi:.0f}不健康"); level = 2
        elif aqi > 100:
            reasons.append(f"AQI{aqi:.0f}轻度污染"); level = max(level, 1)
    if apparent_max is not None:
        if apparent_max >= 38:
            reasons.append(f"体感{apparent_max:.0f}℃热射病风险"); level = 2
        elif apparent_max >= 35:
            reasons.append(f"体感{apparent_max:.0f}℃高温"); level = max(level, 1)
    if precip_prob is not None:
        if precip_prob >= 70:
            reasons.append(f"降水{precip_prob:.0f}%"); level = 2
        elif precip_prob >= 50:
            reasons.append(f"降水{precip_prob:.0f}%"); level = max(level, 1)
    if wind_max is not None:
        if wind_max >= 40:
            reasons.append(f"风{wind_max:.0f}km/h"); level = 2
        elif wind_max >= 30:
            reasons.append(f"风{wind_max:.0f}km/h"); level = max(level, 1)
    return ["宜", "注意", "不宜户外"][level], reasons


def _om_get(url, timeout=20):
    """GET Open-Meteo JSON。失败返回 None（天气是可降级的增强项，绝不抛）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "onelap-train/1.0"})
    try:
        with _urlopen(req, timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _fetch_point(name, lat, lon, group, days):
    """抓一个点（区/常骑点）的当日预报(逐日+逐时) + 实时AQI → 规整 dict 或 None。"""
    days = max(1, min(int(days or 1), 16))
    daily_v = ",".join(_WCOLS)
    hourly_v = "temperature_2m,apparent_temperature,precipitation_probability,wind_speed_10m,wind_direction_10m,uv_index,weather_code"
    fc = _om_get(f"{_OM_FORECAST}?latitude={lat}&longitude={lon}&daily={daily_v}&hourly={hourly_v}"
                 f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,precipitation,weather_code"
                 f"&timezone=Asia/Shanghai&forecast_days={days}")
    if not fc or not isinstance(fc.get("daily"), dict):
        return None
    d = fc["daily"]
    times = d.get("time") or []
    daily = []
    for i, t in enumerate(times):
        rec = {"date": t}
        for c in _WCOLS:
            arr = d.get(c) or []
            rec[_WCOLKEY[c]] = arr[i] if i < len(arr) else None
        daily.append(rec)
    if not daily:
        return None
    today = daily[0]

    a = _om_get(f"{_OM_AQI}?latitude={lat}&longitude={lon}&current=us_aqi,pm2_5,pm10&timezone=Asia/Shanghai")
    aqi = None
    if a and isinstance(a.get("current"), dict):
        aqi = {k: a["current"].get(k) for k in ("us_aqi", "pm2_5", "pm10")}

    h = fc.get("hourly") or {}
    ht = h.get("time") or []
    hkeys = ["temperature_2m", "apparent_temperature", "precipitation_probability",
             "wind_speed_10m", "wind_direction_10m", "uv_index", "weather_code"]
    hcols = {k: (h.get(k) or []) for k in hkeys}
    hourly = []
    for i, t in enumerate(ht):
        hourly.append({"time": t, **{k: (hcols[k][i] if i < len(hcols[k]) else None) for k in hkeys}})

    tag, reasons = weather_suitability(today.get("apparent_max"), today.get("precip_prob"),
                                      today.get("wind_max"), (aqi or {}).get("us_aqi"),
                                      today.get("weather_code"))
    return {"name": name, "group": group, "lat": lat, "lon": lon,
            "today": today, "daily": daily, "hourly": hourly, "aqi": aqi,
            "current": fc.get("current") or {}, "suit_tag": tag, "suit_reasons": reasons}


def get_beijing_weather(today, days_ahead, home_district=None):
    """并发抓 16 区 + 常骑点。返回 {home, home_points[], districts[], fetched_at} 或 None。
    home_district 支持逗号/顿号/空格分隔多个常骑点（南海子公园、戒台寺…）。"""
    home_input = (home_district or "").strip()
    home_names = [h.strip() for h in re.split(r"[,，、\s]+", home_input) if h.strip()] or ["朝阳"]
    days = days_ahead or 1

    pts = [(n, la, lo, g) for (n, la, lo, g) in BEIJING_DISTRICTS]
    for nm in home_names:
        c = RIDING_SPOTS.get(nm)
        if c and nm not in _DISTRICT_COORDS:  # 纯常骑点（非16区）额外抓
            pts.append((nm, c[0], c[1], "常骑点"))
        elif nm not in _DISTRICT_COORDS and nm not in RIDING_SPOTS:
            log(f"⚠️ 未识别的常骑点「{nm}」（不在 16 区/RIDING_SPOTS 内），已忽略。")

    def work(p):
        n, la, lo, g = p
        return n, _fetch_point(n, la, lo, g, days)

    results = {}
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for n, r in ex.map(work, pts):
                if r:
                    results[n] = r
    except Exception:
        pass
    if not results:
        return None

    districts = [results[n] for n, *_ in BEIJING_DISTRICTS if n in results]
    home_points = [results[nm] for nm in home_names if nm in results]
    if not home_points and "朝阳" in results:  # 常骑点全失败 → 回退朝阳
        home_points = [dict(results["朝阳"], name="朝阳(回退)")]
    if not home_points:
        return None
    return {"home": "、".join(h["name"] for h in home_points),
            "home_points": home_points, "districts": districts,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M")}


# ---- 天气 → markdown 渲染（报告章节 / 教练 prompt / 全量明细） ----
def _pt_day(pt, date_iso):
    for day in (pt.get("daily") or []):
        if str(day.get("date", ""))[:10] == date_iso:
            return day
    return None


def _weather_window_rows(pt, date_iso, h_lo, h_hi):
    rows = []
    for hr in (pt.get("hourly") or []):
        t = str(hr.get("time", ""))
        if t[:10] == date_iso and len(t) >= 13 and h_lo <= int(t[11:13]) <= h_hi:
            rows.append(hr)
    return rows


def _win_row(hr, label):
    hh = str(hr.get("time", ""))[11:16] or "?"
    return (f"| {label}{hh} | {fmt_num(hr.get('temperature_2m'),0)} | "
            f"{fmt_num(hr.get('apparent_temperature'),0)} | {fmt_num(hr.get('precipitation_probability'),0)} | "
            f"{fmt_num(hr.get('wind_speed_10m'),0)} | {deg_to_dir(hr.get('wind_direction_10m'))} | "
            f"{fmt_num(hr.get('uv_index'),1)} |")


def _weather_majority_tag(pts):
    tags = [p.get("suit_tag") for p in pts if p.get("suit_tag") in _TAG_ORDER]
    if not tags:
        return "—"
    counts = {}
    for t in tags:
        counts[t] = counts.get(t, 0) + 1
    maxn = max(counts.values())
    tied = [t for t, n in counts.items() if n == maxn]
    return min(tied, key=lambda t: _TAG_ORDER[t])


def _weather_group_overview(districts, group):
    pts = [d for d in districts if d.get("group") == group]
    if not pts:
        return None
    tmins = [d["today"].get("tmin") for d in pts if d["today"].get("tmin") is not None]
    tmaxs = [d["today"].get("tmax") for d in pts if d["today"].get("tmax") is not None]
    aqis = [(d.get("aqi") or {}).get("us_aqi") for d in pts if (d.get("aqi") or {}).get("us_aqi") is not None]
    return {
        "trange": (f"{min(tmins):.0f}~{max(tmaxs):.0f}℃") if tmins and tmaxs else "—",
        "aqi_range": (f"{min(aqis):.0f}~{max(aqis):.0f}") if aqis else "—",
        "tag": _weather_majority_tag(pts),
    }


def _next_weekend_dates(today, n=2):
    """接下来 n 个周末日（周六/周日，含今天若是周末）。"""
    out, d = [], today
    while len(out) < n and (d - today).days <= 14:
        if d.weekday() in (5, 6):
            out.append(d)
        d += timedelta(days=1)
    return out


def weather_show_weekend(today, cfg):
    """是否展示「周末去哪骑」：周五，或今日在 config.holiday_dates 里。"""
    if today.weekday() == 4:  # 周五
        return True
    holidays = set(str(h) for h in (cfg.get("holiday_dates") or []))
    return today.isoformat() in holidays


def weather_fetch_days(today, show_weekend):
    """平时抓今明两天；触发周末表时抓到本周日，确保周末两天有数据。"""
    if not show_weekend:
        return WEATHER_FORECAST_DAYS
    days_to_sun = (6 - today.weekday()) % 7  # 0=周一…6=周日
    return max(WEATHER_FORECAST_DAYS, days_to_sun + 1)


def render_weather_section(weather, today, show_weekend=False):
    """天气章节 markdown 行列表（聚焦呈现）。weather 为 None → []。"""
    if not weather:
        return []
    hps = weather.get("home_points") or []
    today_iso = today.isoformat()
    L = ["## 二、今日北京天气与户外训练适宜度\n"]
    L.append(f"> 数据来源 Open-Meteo（免费无 key）；实时 AQI 抓取于 {weather.get('fetched_at', '?')}。\n")

    # 常骑点（工作日锚点）逐个：一行摘要 + 骑行窗口逐时
    for hp in hps:
        t = hp.get("today") or {}
        aqi = (hp.get("aqi") or {}).get("us_aqi")
        reasons = "、".join(hp.get("suit_reasons") or []) or "无明显不利因素"
        L.append(f"**常骑点 · {hp.get('name')}**　{wmo_desc(t.get('weather_code'))}，"
                 f"{fmt_num(t.get('tmin'),0)}~{fmt_num(t.get('tmax'),0)}℃，体感最高 {fmt_num(t.get('apparent_max'),0)}℃，"
                 f"降水 {fmt_num(t.get('precip_prob'),0)}%，{deg_to_dir(t.get('wind_dir'))}风 {fmt_num(t.get('wind_max'),0)}km/h，"
                 f"UV {fmt_num(t.get('uv'),1)}({uv_level(t.get('uv'))})，实时 AQI {fmt_num(aqi,0)}({aqi_level(aqi)})　→　"
                 f"**适宜度：{hp.get('suit_tag','—')}**（{reasons}）")
        am = _weather_window_rows(hp, today_iso, 5, 8)
        pm = _weather_window_rows(hp, today_iso, 17, 20)
        if am or pm:
            L.append("")
            L.append("| 时段 | 温 | 体感 | 降水% | 风 km/h | 风向 | UV |")
            L.append("|---|---|---|---|---|---|---|")
            for hr in am:
                L.append(_win_row(hr, "早"))
            for hr in pm:
                L.append(_win_row(hr, "晚"))
        L.append("")

    # 各区概览（今日，文本行——省一张表，把表格额度留给教练计划表）
    bits = []
    for g in ("城六区", "近郊"):
        ov = _weather_group_overview(weather.get("districts") or [], g)
        if ov:
            bits.append(f"{g} 气温 {ov['trange']}、AQI {ov['aqi_range']}（{ov['tag']}）")
    if bits:
        L.append("**各区概览（今日）**　" + "；".join(bits))
        L.append("")

    # 明日天气（只抓今明两天；明日无 AQI 预报，适宜度仅看温/雨/风/恶劣天气码）
    tmr = today + timedelta(days=1)
    tmr_iso = tmr.isoformat()
    tmr_lines = []
    for hp in hps:
        day = _pt_day(hp, tmr_iso)
        if not day:
            continue
        tg, _ = weather_suitability(day.get("apparent_max"), day.get("precip_prob"),
                                    day.get("wind_max"), None, day.get("weather_code"))
        tmr_lines.append(f"- **{hp.get('name')}**　{wmo_desc(day.get('weather_code'))}，"
                         f"{fmt_num(day.get('tmin'),0)}~{fmt_num(day.get('tmax'),0)}℃，体感最高 {fmt_num(day.get('apparent_max'),0)}℃，"
                         f"降水 {fmt_num(day.get('precip_prob'),0)}%，{deg_to_dir(day.get('wind_dir'))}风 {fmt_num(day.get('wind_max'),0)}km/h，"
                         f"UV {fmt_num(day.get('uv'),1)}({uv_level(day.get('uv'))})　→　**{tg}**")
    if tmr_lines:
        L.append(f"**明日天气（{tmr_iso} {weekday_cn(tmr)}）**\n")
        L.extend(tmr_lines)
        L.append("")

    # 周末去哪骑（仅周五 / 节假日触发；未来日无 AQI，适宜度仅看温/雨/风/恶劣天气）
    if show_weekend:
        wdates = _next_weekend_dates(today, 2)
        dmap = {d.get("name"): d for d in (weather.get("districts") or [])}
        if wdates:
            L.append("**周末去哪骑（近/远郊热点）**\n")
            L.append("| 地点 |" + "".join(f" {weekday_cn(dd)}{dd.isoformat()[5:]} |" for dd in wdates) + " 综合适宜度 |")
            L.append("|---|" + "---|" * (len(wdates) + 1))
            for nm in WEEKEND_DESTINATIONS:
                pt = dmap.get(nm)
                if not pt:
                    continue
                cells, dtags = [], []
                for dd in wdates:
                    day = _pt_day(pt, dd.isoformat())
                    if day:
                        cells.append(f"{wmo_desc(day.get('weather_code'))} {fmt_num(day.get('tmax'),0)}℃ 降水{fmt_num(day.get('precip_prob'),0)}%")
                        tg, _ = weather_suitability(day.get("apparent_max"), day.get("precip_prob"), day.get("wind_max"), None, day.get("weather_code"))
                        dtags.append(tg)
                    else:
                        cells.append("—"); dtags.append("—")
                L.append(f"| {nm} | " + " | ".join(cells) + f" | {_worst_tag(dtags)} |")
            L.append("")

    # 一句建议（取所有常骑点最差适宜度）
    valid = [hp for hp in hps if hp.get("suit_tag") in _TAG_ORDER]
    worst_hp = min(valid, key=lambda h: _TAG_ORDER[h.get("suit_tag")]) if valid else None
    worst = worst_hp.get("suit_tag") if worst_hp else "—"
    if worst == "宜":
        adv = "常骑点天气适宜，可按计划户外骑行。"
    elif worst == "注意":
        adv = (f"注意：{'、'.join(worst_hp.get('suit_reasons') or [])}；建议避开午间高温/高 AQI 时段，"
               f"强度酌降或改清晨/晚窗。")
    elif worst == "不宜户外":
        adv = f"今日不宜户外骑行（{'、'.join(worst_hp.get('suit_reasons') or [])}）；建议改室内骑行台或休息。"
    else:
        adv = "按计划与体感执行。"
    L.append(f"**今日户外骑行建议**：{adv}\n")
    return L


def weather_full_table_lines(weather):
    """全 16 区明细表（--weather-only 调试用）。"""
    L = ["| 区 | 天气 | 气温 | 体感最高 | 降水% | 风 | UV | AQI | 等级 | 适宜度 |"]
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for d in (weather.get("districts") or []):
        t = d.get("today", {})
        aqi = (d.get("aqi") or {}).get("us_aqi")
        L.append(f"| {d['name']} | {wmo_desc(t.get('weather_code'))} | "
                 f"{fmt_num(t.get('tmin'),0)}~{fmt_num(t.get('tmax'),0)} | {fmt_num(t.get('apparent_max'),0)} | "
                 f"{fmt_num(t.get('precip_prob'),0)} | {deg_to_dir(t.get('wind_dir'))}风{fmt_num(t.get('wind_max'),0)} | "
                 f"{fmt_num(t.get('uv'),1)} | {fmt_num(aqi,0)} | {aqi_level(aqi)} | {d.get('suit_tag','—')} |")
    return L


def _sample_point(name, group, lat, lon, today, off):
    tmax, tmin, app = 33 + off, 24 + off, 37 + off
    wcodes = [2, 1, 63, 0, 3, 2, 95]
    pprobs = [20, 15, 60, 10, 40, 25, 80]
    daily = []
    for i in range(7):  # 样例生成一周，供「周末去哪骑」演示
        d = today + timedelta(days=i)
        daily.append({"date": d.isoformat(), "tmax": tmax + i % 3, "tmin": tmin, "apparent_max": app + i % 3,
                      "precip_prob": pprobs[i % 7], "wind_max": 18, "wind_dir": 90, "uv": 8,
                      "weather_code": wcodes[i % 7]})
    hourly = []
    for hh in (5, 6, 7, 8, 17, 18, 19, 20):
        hourly.append({"time": f"{today.isoformat()}T{hh:02d}:00", "temperature_2m": 24 + hh % 5,
                       "apparent_temperature": 26 + hh % 5, "precipitation_probability": 20,
                       "wind_speed_10m": 15, "wind_direction_10m": 90,
                       "uv_index": 2.0 if hh < 10 else 0.5, "weather_code": 2})
    aqi = {"us_aqi": 132, "pm2_5": 55, "pm10": 78}
    tag, reasons = weather_suitability(app, 20, 18, 132)
    return {"name": name, "group": group, "lat": lat, "lon": lon, "today": daily[0],
            "daily": daily, "hourly": hourly, "aqi": aqi, "current": {},
            "suit_tag": tag, "suit_reasons": reasons}


def sample_weather(today):
    home_pts = [_sample_point("南海子公园", "常骑点", 39.76, 116.50, today, 0),
                _sample_point("戒台寺", "常骑点", 39.97, 116.09, today, -1)]
    districts = [_sample_point(n, g, la, lo, today, (i % 4) - 1)
                 for i, (n, la, lo, g) in enumerate(BEIJING_DISTRICTS)]
    return {"home": "南海子公园、戒台寺", "home_points": home_pts,
            "districts": districts, "fetched_at": "(样例)"}


# ---------------------------------------------------------------------------
# glm-5.2 教练：把历史训练数据交给 LLM（角色=自行车教练）生成未来计划
# ---------------------------------------------------------------------------
SYSTEM_COACH = (
    "你是一位资深自行车教练兼运动营养师，为【这一位具体车手】量身排课——紧扣其档案、目标与限制，不要泛泛而谈。"
    "精通功率训练、PMC 与周期化：CTL≈长期体能(约42天加权)，ATL≈短期疲劳(约7天加权)，"
    "TSB=CTL−ATL（>5清爽、−10~5中性、<−10疲劳、<−30严重疲劳）。\n"
    "执教原则：①周期化——围绕目标赛事递进(base/build/peak/taper)，由当前阶段决定侧重；"
    "②两极化——强度日真强(Z4/Z5/甜区)、恢复日真轻松(Z1/Z2)，避免整天堆在 Z3 灰色地带累积疲劳；"
    "③渐进负荷——周总 TSS 与周末长骑时长增幅 ≤10-15%，绝不堆量过载(别让 TSB 长期 <−20)；"
    "④伤病优先——「伤病/限制」是硬约束，宁可保守也不安排会加重它的课；"
    "⑤可执行——所有课必须落在真实时间窗内(工作日早 ≤8 点前到家)；"
    "⑥恢复优先——疲劳高或 readiness 差时先恢复与睡眠。"
    "综合【个人档案/目标、当日 readiness、可训练时间窗、季节与阶段、近期负荷与伤病】排课，并给配套饮食/补给建议。"
    "用中文，专业、简洁、可直接照做。"
)


# 北京户外骑行季单一事实源：月份 → (季节提示文本, 默认训练阶段)
# 户外季 3-11 月（含 3、11 月）；12-2 月无骑行台 → 跑步/力量交叉训练（off-season）。
# season_hint 与 phase_for_season 都从此派生，改季节只动这一张表
# （config 的 winter_note 是给教练看的自由文本，另算）。
_BEIJING_SEASON = {
    1:  ("冬休·无骑行台，跑步/力量交叉训练（12-2月）", "transition"),
    2:  ("冬休·无骑行台，跑步/力量交叉训练（12-2月）", "transition"),
    3:  ("季前·转户外打基础", "base"),
    4:  ("季前·转户外打基础", "base"),
    5:  ("旺季·户外", "build"),
    6:  ("旺季·户外", "build"),
    7:  ("旺季·户外", "build"),
    8:  ("旺季·户外", "build"),
    9:  ("旺季·户外", "build"),
    10: ("季末·户外（10-11月，仍可户外，逐步转室内）", "build"),
    11: ("季末·户外（10-11月，仍可户外，逐步转室内）", "build"),
    12: ("冬休·无骑行台，跑步/力量交叉训练（12-2月）", "transition"),
}
_DEFAULT_SEASON = ("旺季·户外", "build")  # 兜底（12 个月全覆盖，理论上不命中）


def season_hint(today):
    """北京户外骑行季 = 3月~11月底；12月~2月无骑行台，转跑步/力量交叉训练。按月给季节阶段提示（派生自 _BEIJING_SEASON）。"""
    return _BEIJING_SEASON.get(today.month, _DEFAULT_SEASON)[0]


def load_readiness():
    """读 readiness.json（由 Apple Watch 捷径每日写入：sleep_h/hrv_ms/rhr_bpm/subjective/date）。无则 None。"""
    p = os.path.join(HERE, "readiness.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


READINESS_HISTORY = os.path.join(HERE, "readiness_history.jsonl")
REST_FLAGS = os.path.join(HERE, "rest_flags.json")          # 用户标记的休息日 {"rest_dates":[...]}
PLAN_GEN_FLAG = os.path.join(HERE, "last_plan_gen.txt")     # 上次 AI 生成计划的日期（降频复用用）


def _read_readiness_history():
    """读 readiness_history.jsonl，按 date 去重（保留最新一条），按日期升序返回。"""
    if not os.path.exists(READINESS_HISTORY):
        return []
    by_date = {}
    try:
        with open(READINESS_HISTORY, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                d = rec.get("date")
                if d:
                    by_date[d] = rec
    except Exception:
        return []
    return [by_date[k] for k in sorted(by_date)]


def append_readiness_history(rd):
    """把当日 readiness 追加进历史（按 date 去重保留最新），供算个人基线。无 date 则跳过。幂等。"""
    if not rd or not rd.get("date"):
        return
    keep = [r for r in _read_readiness_history() if r.get("date") != rd["date"]]
    keep.append({k: rd.get(k) for k in ("date", "sleep_h", "hrv_ms", "rhr_bpm", "subjective")})
    keep.sort(key=lambda r: r.get("date", ""))
    try:
        with open(READINESS_HISTORY, "w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        pass


DISCOMFORT_PATH = os.path.join(HERE, "discomfort.json")
DISCOMFORT_HISTORY = os.path.join(HERE, "discomfort_history.jsonl")


def load_discomfort():
    """读 discomfort.json（iPhone 捷径赛后上报：date/pain/note）。无则 None。"""
    if not os.path.exists(DISCOMFORT_PATH):
        return None
    try:
        return json.load(open(DISCOMFORT_PATH, encoding="utf-8"))
    except Exception:
        return None


def _read_discomfort_history():
    """读 discomfort_history.jsonl，按 date 去重保留最新，升序返回。"""
    if not os.path.exists(DISCOMFORT_HISTORY):
        return []
    by_date = {}
    try:
        with open(DISCOMFORT_HISTORY, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                d = rec.get("date")
                if d:
                    by_date[d] = rec
    except Exception:
        return []
    return [by_date[k] for k in sorted(by_date)]


def append_discomfort_history(rec):
    """把赛后不适记录追加进历史（按 date 去重保留最新）。幂等。"""
    if not rec or not rec.get("date"):
        return
    keep = [r for r in _read_discomfort_history() if r.get("date") != rec["date"]]
    keep.append({k: rec.get(k) for k in ("date", "pain", "note")})
    keep.sort(key=lambda r: r.get("date", ""))
    try:
        with open(DISCOMFORT_HISTORY, "w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_discomfort(today, days=7):
    """近 days 天有不适（pain∈{腰,颈}）的记录，升序。供喂教练/报告。"""
    cutoff = (today - timedelta(days=days - 1)).isoformat()
    return [r for r in _read_discomfort_history()
            if r.get("date", "") >= cutoff and r.get("pain") in ("腰", "颈")]


RUN_HISTORY = os.path.join(HERE, "run_history.jsonl")


def _read_run_history():
    """读 run_history.jsonl（import_apple_health 解析 Apple Watch 跑步写入），按 date 去重保留最新，升序。"""
    if not os.path.exists(RUN_HISTORY):
        return []
    by_date = {}
    try:
        with open(RUN_HISTORY, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                d = rec.get("date")
                if d:
                    by_date[d] = rec
    except Exception:
        return []
    return [by_date[k] for k in sorted(by_date)]


def readiness_baseline(today=None, window=14, min_n=6):
    """返回近期（默认最近 window 天，今天之前）HRV/静息心率/睡眠 的【中位数】作为个人基线。
    每项独立门槛：该项样本 < min_n 则返回 None（评分该项降级为绝对阈值）。
    用中位数而非均值：抗偶发坏数据/测试污染；min_n：数据不够就不强行算基线。"""
    recs = _read_readiness_history()
    if today is not None:
        iso = today.isoformat() if hasattr(today, "isoformat") else str(today)
        recs = [r for r in recs if r.get("date") and r["date"] < iso]  # 基线只取【今天之前】的常态
    recs = recs[-window:]

    def median(key):
        vals = sorted(r.get(key) for r in recs if isinstance(r.get(key), (int, float)))
        if len(vals) < min_n:
            return None
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    hrv, rhr, slp = median("hrv_ms"), median("rhr_bpm"), median("sleep_h")
    if hrv is None and rhr is None:
        return None
    return {"hrv_ms": hrv, "rhr_bpm": rhr, "sleep_h": slp, "n": len(recs)}


def readiness_score(rd, baseline=None):
    """综合 readiness 打分 0-100（绿≥80 / 黄 65-79 / 橙 50-64 / 红<50）。
    睡眠、主观用绝对值；HRV、静息心率相对【个人滚动基线】评判（无基线则不计入、权重重归一）。
    这样没基线时也不会被"未知"拉低分数。无任何可用数据返回 None。"""
    if not rd:
        return None
    parts = []  # [(score 0-100, weight)]

    slp = rd.get("sleep_h")
    if isinstance(slp, (int, float)):
        s = 95 if slp >= 8 else 82 if slp >= 7 else 66 if slp >= 6 else 48 if slp >= 5 else 28
        parts.append((s, 0.30))

    sub = rd.get("subjective")
    if isinstance(sub, (int, float)):
        parts.append((max(0, min(100, sub * 10)), 0.25))

    hrv, b_hrv = rd.get("hrv_ms"), (baseline.get("hrv_ms") if baseline else None)
    if isinstance(hrv, (int, float)) and isinstance(b_hrv, (int, float)) and b_hrv > 0:
        s = 60 + (hrv - b_hrv) / b_hrv * 200  # 持平基线=60；高 10%→80；低 10%→40
        parts.append((max(0, min(100, s)), 0.25))

    rhr, b_rhr = rd.get("rhr_bpm"), (baseline.get("rhr_bpm") if baseline else None)
    if isinstance(rhr, (int, float)) and isinstance(b_rhr, (int, float)) and b_rhr > 0:
        s = 60 - (rhr - b_rhr) * 7  # 持平基线=60；高 3bpm→39；低 3bpm→81
        parts.append((max(0, min(100, s)), 0.20))

    if not parts:
        return None
    wsum = sum(s * w for s, w in parts)
    tw = sum(w for _, w in parts)
    score = round(wsum / tw) if tw else 50
    if score >= 80:
        advice = "绿灯·可按计划上强度"
    elif score >= 65:
        advice = "黄灯·强度略降 10-15%，保时长"
    elif score >= 50:
        advice = "橙灯·以 Z2/恢复为主，跳过 VO2/阈值"
    else:
        advice = "红灯·优先恢复/休息，勿上强度"
    return {"score": score, "advice": advice}


def readiness_flag(rd, baseline=None):
    """把 readiness 数值 + 综合分翻译成一句人话提示（喂给教练）。"""
    if not rd:
        return "（今日无 readiness 数据，按计划与体感执行）"
    bits = []
    if rd.get("sleep_h") is not None: bits.append(f"睡眠 {rd['sleep_h']}h")
    if rd.get("hrv_ms") is not None: bits.append(f"HRV {rd['hrv_ms']}ms")
    if rd.get("rhr_bpm") is not None: bits.append(f"静息心率 {rd['rhr_bpm']}")
    if rd.get("subjective") is not None: bits.append(f"主观 {rd['subjective']}/10")
    sc = readiness_score(rd, baseline)
    score_txt = f"；readiness {sc['score']}/100（{sc['advice']}）" if sc else ""
    return f"（{'、'.join(bits)}{score_txt}）"


def build_coach_prompt(cfg, pmc, rides, today, days_ahead, start_date=None, exec_rows=None, zone_rows=None):
    if start_date is None:
        start_date = today + timedelta(days=1)  # 默认从明天起；--auto 从今天起
    prof = cfg.get("coach_profile") or {}
    sched = prof.get("schedule") or {}
    past = [x for x in pmc if x[0] <= today]
    latest = past[-1] if past else None
    _d, ctl, atl, tsb, _tss = latest if latest else (None, None, None, None, None)
    recent = [r for r in rides if (today - timedelta(days=13)) <= r["date"] <= today]
    w = prof.get("weight_kg") or 79

    u = [f"今天是 {today.isoformat()}（{weekday_cn(today)}）。以下是这位车手的真实档案与数据：", ""]

    u.append("**【个人档案 / 目标 / 时间表】**")
    # 生理基线：年龄由 birth_year 推（或直接 age），性别/身高可选
    _by, _age_cfg = prof.get("birth_year"), prof.get("age")
    _age = f"{today.year - int(_by)}岁、 " if isinstance(_by, int) else (f"{_age_cfg}岁、 " if _age_cfg else "")
    _sex = f"{prof.get('gender')}、" if prof.get("gender") else ""
    _hgt = f"{prof.get('height_cm')}cm/" if prof.get("height_cm") else ""
    _hr = (f"，MHR {prof.get('mhr')}" if prof.get("mhr") else "") + (f"/LTHR {prof.get('lthr')}" if prof.get("lthr") else "")
    if _hr:
        _hr += "bpm"
    u.append(f"- {_age}{_sex}{_hgt}{prof.get('weight_kg','?')}kg，FTP {prof.get('ftp','?')}W{_hr}")
    u.append(f"- 所在地 {prof.get('location','?')}；季节阶段：{season_hint(today)}；训练阶段：{prof.get('phase','base')}")
    _te = prof.get("target_event")
    u.append(f"- **目标方向**：{_te or prof.get('goal','提高FTP')}"
             + (f"（请据此做周期化，向目标递进；当前阶段={prof.get('phase','base')}）" if _te else ""))
    _cs = prof.get("constraints")
    if _cs:
        u.append(f"- ⚠️ **伤病/限制（排课须规避或缓解）**：{_cs if isinstance(_cs, str) else '；'.join(_cs)}")
    _pf = prof.get("preferences")
    if _pf:
        u.append(f"- 偏好（提高执行率，但勿牺牲科学性）：{_pf if isinstance(_pf, str) else '；'.join(_pf)}")
    if sched:
        u.append(f"- 工作日早窗：{sched.get('weekday_am','?')}")
        u.append(f"- 工作日晚窗：{sched.get('weekday_pm','?')}")
        u.append(f"- 周末：{sched.get('weekend','?')}")
        if sched.get("winter_note"):
            u.append(f"- 冬季说明：{sched.get('winter_note')}")
    u.append("")

    _rd = load_readiness()
    u.append(f"**【今日 readiness】** {readiness_flag(_rd, readiness_baseline(today))}")
    u.append("")

    u.append("**【当前体能 PMC】**")
    u.append(f"- CTL={fmt_num(ctl,1)}，ATL={fmt_num(atl,1)}，TSB={fmt_num(tsb,1)}" +
             (f"（{tsb_interp(tsb)}）" if tsb is not None else ""))
    u.append("")
    u.append(f"**【最近 {len(recent) or 14} 天骑行】** 日期|TSS|时长|距离|均功|均心")
    u.append("---|---|---|---|---|---")
    for r in recent:
        u.append(f"{r['date'].isoformat()}|{fmt_num(r['tss'],0)}|{human_duration(r['duration_s'])}|"
                 f"{fmt_num(r['distance_km'],0)}km|{fmt_num(r['avg_power'],0)}|{fmt_num(r['avg_hr'],0)}")
    u.append("")

    # 跑步（交叉训练叠加层，不并入 OTM PMC，但计入总负荷避免叠加过载）
    _runs_recent = [r for r in _read_run_history()
                    if (today - timedelta(days=13)).isoformat() <= r.get("date", "") <= today.isoformat()]
    if _runs_recent:
        _rtss = sum((r.get("tss") or 0) for r in _runs_recent)
        u.append(f"**【最近跑步（交叉训练）】** 近 14 天 {len(_runs_recent)} 次"
                 f"（最近 {_runs_recent[-1].get('date')}），合计 {_rtss:.0f} TSS（估算）。"
                 f"排课时把跑步计入总负荷，避免骑行+跑步叠加过载。")
        u.append("")

    u.append("**【营养目标：减脂增肌】**")
    u.append(f"- 蛋白 {int(w*1.8)}-{int(w*2.0)}g/天；强度日碳水 5-7g/kg、休息日 3-4g/kg；小幅热量缺口 −300~−400kcal；"
             f"长骑/强度课途中补碳水 30-60g/h，课后补碳水+蛋白。")
    u.append("")

    if exec_rows:
        _missed = [r for r in exec_rows if r["done"] is False]
        u.append("**【近期计划执行情况】**（实际/计划 TSS；完成 = 实际 ≥ 计划的 70%）")
        u.append("日期|计划|实际|状态")
        u.append("---|---|---|---")
        for r in exec_rows:
            _st = "休息/无计划" if r["done"] is None else ("✅完成" if r["done"] else "❌未完成")
            u.append(f"{r['date']}|{r['planned']:.0f}|{r['actual']:.0f}|{_st}")
        if _missed:
            u.append(f"⚠️ 近期有 {len(_missed)} 天未完成计划（{', '.join(m['date'] for m in _missed)}）。"
                     f"请把未完成的关键课（尤其昨日）顺延到今日或近期，或据此调整今日强度——勿简单跳过。")
        else:
            u.append("近期计划全部完成，执行良好。")
        u.append("")

    # 强度命中率（计划 zone vs 实际 avg_power 落区，只标明显错配）
    if zone_rows:
        _pd = [r for r in zone_rows if r["planned_zone"]]
        _mis = [r for r in _pd if r["mismatch"]]
        if _pd:
            _hit = (len(_pd) - len(_mis)) * 100 // len(_pd)
            _line = (f"**【强度命中率】** 近 {len(_pd)} 个训练日，强度命中率约 {_hit}%"
                     f"（按实际 avg_power 落区 vs 计划 zone；间歇课 avg 含热身/放松会偏低，仅供参考）")
            if _mis:
                _line += "；明显错配：" + "；".join(
                    f"{r['date']} 计划{r['planned_zone']}/实际{r['actual_zone']}" for r in _mis) + "——核实是否练成了目标强度课。"
            u.append(_line)
            u.append("")

    u.append(f"请综合以上，为**从 {start_date.isoformat()}（{weekday_cn(start_date)}）起未来 {days_ahead} 天**制定计划。要求：")
    u.append("1) 先 2-3 句整体判断（当前状态/本周侧重/过载风险）。")
    u.append("2) **今日重点**：把今天的训练安排进真实时段（早窗/晚窗/休息），并给今天 1 句饮食提示；若 readiness 偏差则降级为恢复。")
    u.append("3) 未来逐日计划表：日期|星期|训练/休息|课目|时段|时长|目标TSS|强度IF|主要目的。休息日也列。时段必须符合上面的时间窗（工作日早 ≤8点前到家）。")
    u.append("4) 营养与恢复各 2 条注意事项。")
    u.append("5) 最后输出 ```json 代码块（供程序导入，严格如下，无多余字段）：")
    u.append('```json')
    u.append('{"summary":"整体判断","notes":["注意1","注意2"],')
    u.append(' "days":[{"date":"YYYY-MM-DD","action":"train或rest","name":"课目",')
    u.append('   "duration_min":120,"tss":70,"if":0.70,"zone":"Z2","purpose":"目的"}]}')
    u.append('```')
    u.append(f"要求：days 覆盖 {start_date.isoformat()} 起 {days_ahead} 天、日期连续；action=rest 时 duration_min=0/tss=0/if=0/zone=\"\"；"
             f"zone 取 Z1/Z2/Z3/Z4/Z5/Z6 或\"甜区\"；if 为 0~1.2 的小数。强度(TSS)循序渐进，勿让 TSB 长期 <−20。")
    if prof.get("constraints"):
        u.append("【硬约束·伤病】严格遵守上面「伤病/限制」，不得安排会加重它的课——"
                 "爬坡致腰疼→控制单次连续爬坡时长、穿插平路/伸展/核心训练；"
                 "平路或下坡致颈疼→分段骑行、变换握姿、加颈部放松与核心；"
                 "长课务必安排中途起身/变换姿势。")
    # 赛后不适反馈（动态硬约束）：近期腰/颈不适 → 教练必须规避
    _disc = recent_discomfort(today)
    if _disc:
        _disc_txt = "；".join(f"{d['date']} {d['pain']}" + (f"({d.get('note')})" if d.get('note') else "")
                             for d in _disc)
        u.append(f"【硬约束·近期不适】车手近期上报不适：{_disc_txt}。"
                 "排课时须主动规避/缓解对应部位（腰→少连续爬坡/加核心；颈→分段平路/变换握姿），"
                 "宁可降强度或换交叉训练，不得安排会加重它的课。")
    # 过载兜底：若教练仍被调用且 TSB 已严重偏低 → 强制今日 rest/极轻
    if tsb is not None and tsb < OVERREACH_TSB:
        u.append(f"【硬约束·过度疲劳】当前 TSB={tsb:.0f} 已严重偏低，今日必须 action=rest 或仅 Z1 恢复，"
                 "不得安排任何中高强度课。")
    return SYSTEM_COACH, "\n".join(u)


def call_llm(cfg, system, user):
    """调用 glm-5.2（智谱 v4 接口）。返回生成的文本；无 key 返回 None。"""
    key = (cfg.get("glm_api_key") or "").strip()
    if not key:
        return None
    endpoint = cfg.get("glm_endpoint") or "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    model = COACH_MODEL  # 固定 glm-5.2
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.6,
        "stream": False,
        "thinking": {"type": "enabled", "budget_tokens": 4096},  # 开启思考：深度整合 readiness+PMC+历史骑行做分析；封顶 4096 token
    }).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    last = None
    for attempt in range(2):  # 网络/SSL 抖动时重试 1 次
        try:
            with _urlopen(req, 240) as r:
                obj = json.loads(r.read().decode("utf-8"))
            last = None
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")[:300]
            raise ApiError(f"GLM 接口 HTTP {e.code}: {msg}")
        except Exception as e:  # URLError / socket.timeout / SSLError / RemoteDisconnected
            last = e
            time.sleep(2)
    if last is not None:
        raise ApiError(f"GLM 接口网络错误({type(last).__name__}): {last}")
    try:
        return obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ApiError(f"GLM 返回结构异常: {json.dumps(obj, ensure_ascii=False)[:300]}")


# ---------------------------------------------------------------------------
# 教练输出解析：拆出 (给人看的 markdown, 给程序用的结构化 plan)
# ---------------------------------------------------------------------------
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _to_float(s):
    m = re.search(r"\d+(?:\.\d+)?", str(s))
    return float(m.group(0)) if m else 0.0


def _parse_min(s):
    """'1.5h' / '90m' / '1h30m' / '90' → 分钟数。"""
    s = str(s); tot = 0.0
    h = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    m = re.search(r"(\d+)\s*m", s)
    if h: tot += float(h.group(1)) * 60
    if m: tot += int(m.group(1))
    if not h and not m:
        n = re.search(r"\d+(?:\.\d+)?", s)
        if n: tot = float(n.group(0))
    return int(tot)


def _guess_zone(name):
    n = str(name)
    for z in ["Z6", "Z5", "Z4", "Z3", "Z2", "Z1"]:
        if z.lower() in n.lower():
            return z
    if "甜区" in n or "sweet" in n.lower():
        return "甜区"
    if "vo2" in n.lower() or "无氧" in n:
        return "Z5"
    if "阈值" in n or "threshold" in n.lower():
        return "Z4"
    if "耐力" in n or "有氧" in n:
        return "Z2"
    if "恢复" in n:
        return "Z1"
    return ""


def parse_coach_response(text):
    """拆出 (report_md, plan)。plan 含 days；解析全失败返回 (text, None)。"""
    if not text:
        return "", None
    m = _JSON_BLOCK.search(text)
    if m:
        try:
            plan = json.loads(m.group(1))
        except json.JSONDecodeError:
            plan = None
        report_md = (text[:m.start()] + "\n" + text[m.end():]).strip()
        if plan and plan.get("days"):
            return report_md, plan
        return (report_md if plan else text), parse_plan_table(text)
    return text, parse_plan_table(text)


def parse_plan_table(text):
    """兜底：从 Markdown 表格行解析 days。"""
    days = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or not _DATE_RE.search(s):
            continue
        if set(s.replace("|", "").strip()) <= set("-: "):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4:
            continue
        ds = _DATE_RE.search(cells[0])
        if not ds:
            continue
        day = {"date": ds.group(0), "action": "rest" if "休" in cells[2] else "train",
               "name": cells[3] if len(cells) > 3 else "", "duration_min": _parse_min(cells[4]) if len(cells) > 4 else 0,
               "tss": _to_float(cells[5]) if len(cells) > 5 else 0,
               "if": _to_float(cells[6]) if len(cells) > 6 else 0.0,
               "zone": "", "purpose": cells[7] if len(cells) > 7 else ""}
        day["zone"] = _guess_zone(day["name"])
        days.append(day)
    return {"summary": "", "notes": [], "days": days} if days else None


def normalize_plan_days(plan, today, days_ahead, start_date=None):
    """校验/清洗 LLM 的 days，只保留窗口内的，返回规整列表。
    start_date 默认 today+1；窗口 = [start_date, start_date + days_ahead - 1]。"""
    if not plan or not plan.get("days"):
        return []
    fut_start = start_date if start_date else (today + timedelta(days=1))
    fut_end = fut_start + timedelta(days=days_ahead - 1)
    out = []
    for d in plan["days"]:
        dt = parse_date(str(d.get("date", ""))[:10])
        if not dt or not (fut_start <= dt <= fut_end):
            continue
        act_raw = str(d.get("action", ""))
        action = "rest" if (act_raw.lower().startswith("rest") or "休" in act_raw) else "train"
        name = str(d.get("name") or "").strip() or ("休息" if action == "rest" else "训练")
        out.append({
            "date": dt,
            "action": action,
            "name": name,
            "duration_min": int(_to_float(d.get("duration_min"))),
            "tss": int(_to_float(d.get("tss"))),
            "if": _to_float(d.get("if")),
            "zone": str(d.get("zone") or _guess_zone(name)).strip(),
            "purpose": str(d.get("purpose") or "").strip(),
        })
    out.sort(key=lambda x: x["date"])
    return out


# ---------------------------------------------------------------------------
# 计划自洽校验：按生成的 TSS 用 PMC 递推公式前向推演 CTL/ATL/TSB
# ---------------------------------------------------------------------------
def project_tsb(plan_days, ctl0, atl0, ctl_tc=42.0, atl_tc=7.0):
    """CTL_t = CTL_{t-1} + (TSS_{t-1} - CTL_{t-1}) * (1 - e^{-1/ctl_tc})；ATL 同理（atl_tc=7）。
    plan_days 取自 normalize_plan_days（含 'tss'，休息日为 0）。ctl0/atl0 为今日最新 PMC 值。
    返回 [{"date","tss","ctl","atl","tsb"}] 按日期升序；ctl0/atl0 缺失或无计划返回 []。"""
    if ctl0 is None or atl0 is None or not plan_days:
        return []
    ctl, atl = float(ctl0), float(atl0)
    kc = 1 - math.exp(-1.0 / ctl_tc)
    ka = 1 - math.exp(-1.0 / atl_tc)
    out = []
    for d in sorted(plan_days, key=lambda x: x["date"]):
        tss = float(d.get("tss") or 0)
        ctl += (tss - ctl) * kc
        atl += (tss - atl) * ka
        out.append({"date": d["date"].isoformat() if hasattr(d["date"], "isoformat") else str(d["date"]),
                    "tss": tss, "ctl": ctl, "atl": atl, "tsb": ctl - atl})
    return out


# ---------------------------------------------------------------------------
# 目标日期驱动的训练阶段（phase）建议
# ---------------------------------------------------------------------------
def suggest_phase(target_date, today):
    """按距目标日期的剩余周数建议阶段（经验映射）：
    已过→transition；≤1周→peak(减量)；1-3周→build；>3周→base。无目标返回 None。
    target_date 为 ISO 字符串/date；today 为 date 对象。"""
    td = parse_date(str(target_date)[:10]) if target_date else None
    if not td or not today:
        return None
    weeks = (td - today).days / 7.0
    if weeks < 0:
        return "transition"
    if weeks <= 1:
        return "peak"
    if weeks <= 3:
        return "build"
    return "base"


def _extract_event_date(cfg):
    """从 coach_profile 抽目标日期：优先 target_date(YYYY-MM-DD)，否则从 target_event 文本抓 20YY-MM(-DD)。
    文本里有多个日期时取【最后一个】（通常是真正的目标，避免抓到总结里的历史日期）。"""
    prof = cfg.get("coach_profile") or {}
    d = parse_date(str(prof.get("target_date") or "")[:10])
    if d:
        return d
    te = str(prof.get("target_event") or "")
    matches = re.findall(r"(20\d{2})-(\d{1,2})(?:-(\d{1,2}))?", te)
    if matches:
        y, mo, d_ = matches[-1]
        try:
            return date(int(y), int(mo), int(d_ or "1"))
        except ValueError:
            return None
    return None


def phase_for_season(month):
    """无明确赛事时，按北京户外季给默认阶段（派生自 _BEIJING_SEASON）：
    户外旺季(5-11月)→build；季前(3-4月)→base；冬休(12-2月，无骑行台/交叉训练)→transition。
    month 为 None 返回 None。"""
    if month is None:
        return None
    return _BEIJING_SEASON.get(month, _DEFAULT_SEASON)[1]


def phase_advisory(cfg, today):
    """训练阶段建议：优先按目标赛事日期（suggest_phase）；无赛事则按北京户外季（phase_for_season）。
    phase_autosync=true 时写回 config，否则仅文字提示。返回 markdown 或 None。"""
    if not cfg:
        return None
    prof = cfg.setdefault("coach_profile", {})  # setdefault：确保 phase 写回能落盘（避免 or {} 拿到游离 dict）
    cur = str(prof.get("phase") or "base").strip()
    td = _extract_event_date(cfg)
    if td:
        sug = suggest_phase(td, today)
        days_to = (td - today).days
        reason = (f"目标（{td.isoformat()}）已过" if days_to < 0
                  else f"距目标（{td.isoformat()}，约 {days_to / 7.0:.0f} 周）")
    else:
        sug = phase_for_season(today.month)
        reason = "按北京户外季（3-11月户外、12-2月室内）"
    if not sug or sug == cur:
        return None
    head = f"🗓️ **阶段建议**：{reason}建议进入 **{sug}** 阶段（当前 {cur}）。"
    if cfg.get("phase_autosync"):
        prof["phase"] = sug
        try:
            save_config(cfg)
            return head + f" 已按 phase_autosync 自动写回 config → phase={sug}。"
        except Exception as e:
            return head + f"（自动写回失败：{e}，请手动改 phase）"
    return head + " 未开启 phase_autosync；如需自动切换在 config 设 \"phase_autosync\": true。"


# ---------------------------------------------------------------------------
# 微信推送（Server酱）
# ---------------------------------------------------------------------------
def push_serverchan(cfg, title, desp):
    """通过 Server酱 推送到微信。成功返回 True。"""
    key = (cfg.get("serverchan_key") or "").strip()
    if not key:
        print("未配置 serverchan_key，跳过微信推送。", file=sys.stderr)
        return False
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = urllib.parse.urlencode({"title": title[:32], "desp": desp}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _urlopen(req, 30) as r:
            obj = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"微信推送 HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}", file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(f"微信推送网络错误: {e.reason}", file=sys.stderr)
        return False
    if obj.get("code") == 0:
        print("✅ 报告已推送到微信。", file=sys.stderr)
        return True
    print(f"微信推送失败: {obj.get('message') or obj}", file=sys.stderr)
    return False


def push_alert(cfg, title, msg, images=None):
    """告警推送（--auto 无人值守时的关键失败用）：serverchan + 飞书都试，任一配了 key 就推。
    images（PNG bytes 列表）只发给飞书（Server酱不支持图）。至少一个成功返回 True。"""
    ok = False
    if (cfg.get("serverchan_key") or "").strip():
        ok = push_serverchan(cfg, title, msg) or ok
    if (cfg.get("feishu_app_id") or "").strip():
        try:
            ok = push_feishu(cfg, title, msg, images=images) or ok
        except Exception as e:
            print(f"飞书告警推送失败: {e}", file=sys.stderr)
    if not ok:
        print(f"⚠️ 推送失败或未配置任何通道：{title}", file=sys.stderr)
    return ok


def _feishu_table_to_text(lines):
    """连续的 markdown 表格行 → 飞书卡片可读的列表形式（兜底用）。
    飞书 markdown 不支持表格，正常路径走 _feishu_parse_table 转 table 元素。
    """
    rows = []
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("|"):
            continue
        if re.match(r"^\|[\s:|\-]+\|?$", ln):  # 分隔行 |---|---|
            continue
        rows.append([c.strip() for c in ln.strip("|").split("|")])
    if not rows:
        return []
    out = ["**" + " ｜ ".join(rows[0]) + "**"]  # 表头加粗
    for r in rows[1:]:
        out.append("• " + " ｜ ".join(r))
    return out


def _feishu_parse_table(lines):
    """连续 markdown 表格行 → 飞书原生 table 元素的 {columns, rows}。
    返回 None 表示不是合法表格。飞书限制：最多 10 列、每卡最多 5 个表。
    """
    rows_raw = []
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            continue
        if re.match(r"^\|[\s:|\-]+\|?$", s):  # 分隔行 |---|---|
            continue
        rows_raw.append([c.strip() for c in s.strip("|").split("|")])
    if len(rows_raw) < 2:  # 至少表头 + 1 行数据
        return None
    header = rows_raw[0]
    ncols = min(len(header), 10)  # 飞书硬限 10 列
    columns = [{
        "name": f"c{i}",
        "display_name": header[i] if i < len(header) else "",
        "data_type": "text",
        "horizontal_align": "left",
    } for i in range(ncols)]
    rows = []
    for r in rows_raw[1:]:
        rows.append({f"c{i}": (r[i] if i < len(r) else "") for i in range(ncols)})
    return {"columns": columns, "rows": rows}


def _feishu_split_md_paragraphs(text, limit=3500):
    """按空行把 markdown 切成多段，每段不超过 limit 字符（飞书 markdown
    元素过长会降级为纯文本渲染，造成「乱码」感）。"""
    text = text.strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, buf = [], ""
    for p in parts:
        if len(p) > limit:
            p = p[:limit - 1] + "…"
        if not buf:
            buf = p
        elif len(buf) + 2 + len(p) <= limit:
            buf = buf + "\n\n" + p
        else:
            out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out


FEISHU_MAX_TABLES = 5  # 飞书单卡片表格数量上限，超出整张卡片会被拒（HTTP 400）


def _feishu_section_to_elements(sec_lines, table_counter, total_tables):
    """处理一个 ## 段落 → element 列表。表格用原生 table 元素，
    其余按空行切成多个小 markdown 元素，避免飞书 markdown 过长降级。
    table_counter[0] 是当前表的全局序号；total_tables 是全卡总表数——
    超飞书上限(5)时优先保留前几张 + 末张(末张=教练计划表)，中间多余的转文本。
    """
    out, md_buf, table_buf = [], [], []

    def flush_md():
        if not md_buf:
            return
        text = "\n".join(md_buf).strip()
        md_buf.clear()
        if not text:
            return
        # ## / ### 标题 → 行内粗体
        cleaned = []
        for ln in text.splitlines():
            m = re.match(r"^(#{2,6})\s+(.*)$", ln)
            cleaned.append(f"**{m.group(2).strip()}**" if m else ln)
        for chunk in _feishu_split_md_paragraphs("\n".join(cleaned)):
            out.append({"tag": "markdown", "content": chunk})

    def flush_table():
        if not table_buf:
            return
        tbl = _feishu_parse_table(table_buf)
        idx = table_counter[0]
        # 优先保留「前几张 + 末张」(末张即教练计划表)；超限时把中间多余的表转文本
        keep = tbl is not None and (idx < FEISHU_MAX_TABLES - 1 or idx == total_tables - 1)
        if keep:
            flush_md()  # 先把前面攒的 markdown 冲掉
            out.append({
                "tag": "table",
                "page_size": min(20, max(1, len(tbl["rows"]))),
                "row_height": "low",
                "header_style": {
                    "text_align": "left",
                    "text_size": "normal",
                    "bold": True,
                },
                "columns": tbl["columns"],
                "rows": tbl["rows"],
            })
        else:
            # 超飞书上限(5)的中间表、或非合法表 → 回退为列表文本
            md_buf.extend(_feishu_table_to_text(table_buf))
        table_counter[0] += 1  # 无论是否保留，全局序号都要前进
        table_buf.clear()

    for ln in sec_lines:
        if ln.lstrip().startswith("|"):
            table_buf.append(ln)
        else:
            flush_table()
            md_buf.append(ln)
    flush_table()
    flush_md()
    return out


def _count_table_blocks(lines):
    """数一段 lines 里有几个 markdown 表格块（连续 | 开头的行算一块，分隔行不影响计数）。"""
    n, in_tbl = 0, False
    for ln in lines:
        if ln.lstrip().startswith("|"):
            if not in_tbl:
                n += 1
                in_tbl = True
        else:
            in_tbl = False
    return n


def _feishu_card_elements(content):
    """把完整报告 markdown 切成飞书卡片 elements 数组：
    - 首行 # H1 标题丢给 card.header，不进正文
    - ## 段各自一块，段间插 hr 分隔线
    - markdown 表格 → 飞书原生 table 元素（飞书 markdown 不渲染表格）
    - 长 markdown 段落按空行切成多个小 markdown 元素，避免过长被降级
    - 末尾 --- 之后的短说明（无二级标题、≤500 字）放进 note 灰色脚注
    """
    lines = content.splitlines()
    # 丢掉首个 H1
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            lines = lines[:i] + lines[i + 1:]
            break
    # 从末尾找最近一个 --- 作脚注边界：其后内容需短、无 ## 标题（避免吞掉 coach_md 中段的 ---）
    footer = []
    cut_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "---":
            tail = [x for x in lines[i + 1:] if x.strip()]
            tail_text = "\n".join(tail)
            if len(tail_text) <= 500 and not any(l.lstrip().startswith("#") for l in tail):
                cut_idx = i
                footer = tail
                break
    if cut_idx is not None:
        lines = lines[:cut_idx]
    # 按 ## 切段
    sections = [[]]
    for ln in lines:
        if ln.startswith("## "):
            sections.append([ln])
        else:
            sections[-1].append(ln)
    total_tables = sum(_count_table_blocks(sec) for sec in sections)
    elements = []
    table_counter = [0]  # 飞书单卡最多 5 个表；超限时优先保留首表+末表(教练计划表)
    for sec in sections:
        sec_elements = _feishu_section_to_elements(sec, table_counter, total_tables)
        if not sec_elements:
            continue
        if elements:  # 段间分隔线（跳过首个）
            elements.append({"tag": "hr"})
        elements.extend(sec_elements)
    if footer:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "  ".join(footer)[:800]}],
        })
    return elements


# ---------------------------------------------------------------------------
# 训练图表（Pillow 可选）：计划 vs 实际 TSS、PMC 体能/疲劳趋势。CVD 安全配色（Okabe-Ito）。
# 单轴原则：TSS 一图、CTL/ATL 一图（TSB<0 的疲劳区用背景红带表示，不另开轴）。
# ---------------------------------------------------------------------------
_CH_BLUE = (0, 114, 178)      # CTL / 计划柱
_CH_SKY = (86, 180, 233)      # 计划柱（浅）
_CH_VERM = (213, 94, 0)       # ATL / 实际柱
_CH_GREEN = (0, 158, 115)     # 睡眠/恢复（Okabe-Ito green）
_CH_INK = (40, 44, 52)
_CH_MUTED = (150, 156, 166)
_CH_GRID = (232, 234, 237)
_CH_FATIGUE = (245, 220, 220)  # TSB<0 疲劳区背景

_CJK_FONT_PATHS = [
    r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\Deng.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
]
_PIL_FONT_CACHE = {}


def _pil_font(size, bold=False):
    """加载 CJK 字体（msyh 等），找不到回退默认字体。缓存。"""
    if not HAVE_PIL:
        return None
    key = (size, bold)
    if key in _PIL_FONT_CACHE:
        return _PIL_FONT_CACHE[key]
    paths = ([r"C:\Windows\Fonts\msyhbd.ttc"] if bold else []) + _CJK_FONT_PATHS
    f = None
    for p in paths:
        try:
            f = ImageFont.truetype(p, size); break
        except Exception:
            continue
    if f is None:
        f = ImageFont.load_default()
    _PIL_FONT_CACHE[key] = f
    return f


def _img_to_png(img):
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _planned_actual_rows(planned_by_date, actual_by_date, today, days_back=21):
    """近 days_back 天（升序）每日 (iso, planned_tss, actual_tss)，只含有训练/实际的日子。"""
    rows = []
    for i in range(days_back):
        d = today - timedelta(days=days_back - 1 - i)
        iso = d.isoformat()
        p = float(planned_by_date.get(iso) or 0)
        a = float(actual_by_date.get(iso) or 0)
        if p > 0 or a > 0:
            rows.append((iso, p, a))
    return rows


def chart_planned_vs_actual(rows, title="计划 vs 实际 TSS（近 N 天）"):
    """分组柱状图：每日 计划(浅蓝) vs 实际(橙)。等高=完成、矮=欠、高=超。返回 PNG bytes 或 None。"""
    if not HAVE_PIL or not rows:
        return None
    rows = [(d, max(0.0, float(p or 0)), max(0.0, float(a or 0))) for d, p, a in rows]
    n = len(rows)
    W = max(640, 64 * n + 120); H = 360
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_title = _pil_font(20, bold=True); f_lbl = _pil_font(13); f_tick = _pil_font(12)
    d.text((20, 12), title, font=f_title, fill=_CH_INK)
    top, bottom, left, right = 56, H - 40, 56, W - 24
    plot_w = right - left; plot_h = bottom - top
    vmax = max([max(p, a) for _, p, a in rows] + [50]) * 1.15
    for i in range(5):
        y = bottom - plot_h * i / 4
        d.line([(left, y), (right, y)], fill=_CH_GRID, width=1)
        d.text((4, y - 8), f"{vmax * i / 4:.0f}", font=f_tick, fill=_CH_MUTED)
    group_w = plot_w / n
    bar_w = min(16.0, group_w / 3.4)
    gap = 2
    for i, (iso, p, a) in enumerate(rows):
        cx = left + group_w * (i + 0.5)
        x1 = cx - bar_w - gap / 2; x2 = cx + gap / 2
        d.rectangle([x1, bottom - plot_h * (p / vmax), x1 + bar_w, bottom], fill=_CH_SKY)
        d.rectangle([x2, bottom - plot_h * (a / vmax), x2 + bar_w, bottom], fill=_CH_VERM)
        d.text((cx - 15, bottom + 6), iso[5:].replace("-", "/"), font=f_tick, fill=_CH_MUTED)
    lx, ly = W - 184, 20
    d.rectangle([lx, ly, lx + 14, ly + 14], fill=_CH_SKY); d.text((lx + 20, ly - 2), "计划", font=f_lbl, fill=_CH_INK)
    d.rectangle([lx + 82, ly, lx + 96, ly + 14], fill=_CH_VERM); d.text((lx + 102, ly - 2), "实际", font=f_lbl, fill=_CH_INK)
    return _img_to_png(img)


def chart_pmc_load(pmc_pts, title="体能 CTL / 疲劳 ATL（红带=TSB<0 过载）"):
    """CTL(蓝)+ATL(橙) 折线，ATL>CTL 区段背景标红表示疲劳/过载。返回 PNG bytes 或 None。"""
    if not HAVE_PIL or not pmc_pts:
        return None
    pts = [(x[0], to_num(x[1]), to_num(x[2])) for x in pmc_pts
           if to_num(x[1]) is not None and to_num(x[2]) is not None]
    if len(pts) < 2:
        return None
    W, H = 760, 320
    img = Image.new("RGB", (W, H), (255, 255, 255))
    dr = ImageDraw.Draw(img)
    f_title = _pil_font(18, bold=True); f_tick = _pil_font(12); f_lbl = _pil_font(13)
    dr.text((20, 10), title, font=f_title, fill=_CH_INK)
    top, bottom, left, right = 50, H - 36, 56, W - 20
    plot_w = right - left; plot_h = bottom - top
    vals = [v for _, c, a in pts for v in (c, a)]
    vmin = min(min(vals) * 0.9, 0); vmax = max(vals) * 1.1
    span = (vmax - vmin) or 1.0
    n = len(pts)
    xy = lambda i, v: (left + plot_w * (i / (n - 1)),
                       bottom - plot_h * ((v - vmin) / span))
    # 疲劳背景带（ATL>CTL 的连续区段）
    i = 0
    while i < n - 1:
        if pts[i][2] > pts[i][1]:
            j = i
            while j < n - 1 and pts[j][2] > pts[j][1]:
                j += 1
            x0 = left + plot_w * (i / (n - 1)); x1 = left + plot_w * (j / (n - 1))
            dr.rectangle([x0, top, x1, bottom], fill=_CH_FATIGUE)
            i = j
        else:
            i += 1
    for s in range(5):
        y = bottom - plot_h * s / 4
        dr.line([(left, y), (right, y)], fill=_CH_GRID, width=1)
        dr.text((4, y - 8), f"{vmin + span * s / 4:.0f}", font=f_tick, fill=_CH_MUTED)
    dr.line([xy(i, a) for i, (_, c, a) in enumerate(pts)], fill=_CH_VERM, width=2)
    dr.line([xy(i, c) for i, (_, c, a) in enumerate(pts)], fill=_CH_BLUE, width=2)
    lx, ly = W - 210, 14
    dr.line([(lx, ly + 7), (lx + 18, ly + 7)], fill=_CH_BLUE, width=2); dr.text((lx + 24, ly - 2), "CTL 体能", font=f_lbl, fill=_CH_INK)
    dr.line([(lx + 104, ly + 7), (lx + 122, ly + 7)], fill=_CH_VERM, width=2); dr.text((lx + 128, ly - 2), "ATL 疲劳", font=f_lbl, fill=_CH_INK)
    # 首尾日期
    dr.text((left, bottom + 8), str(pts[0][0])[:10], font=f_tick, fill=_CH_MUTED)
    dr.text((right - 40, bottom + 8), str(pts[-1][0])[:10], font=f_tick, fill=_CH_MUTED)
    return _img_to_png(img)


def chart_readiness_trend(history, baseline, today, days=21,
                          title="readiness 趋势（HRV / 静息心率 / 睡眠）"):
    """3 面板小图（HRV/静息心率/睡眠），各面板独立纵轴 + 基线虚线。最近 days 天（今天之前）。
    返回 PNG bytes 或 None（无 PIL / 数据不足）。"""
    if not HAVE_PIL or not history:
        return None
    iso_today = today.isoformat() if hasattr(today, "isoformat") else str(today)[:10]
    pts = [r for r in history if r.get("date") and r["date"] < iso_today][-days:]
    pts = [r for r in pts if any(isinstance(r.get(k), (int, float))
                                 for k in ("hrv_ms", "rhr_bpm", "sleep_h"))]
    if len(pts) < 2:
        return None
    W, H = 760, 470
    img = Image.new("RGB", (W, H), (255, 255, 255))
    dr = ImageDraw.Draw(img)
    f_title = _pil_font(18, bold=True); f_lbl = _pil_font(13); f_tick = _pil_font(11)
    dr.text((20, 8), title, font=f_title, fill=_CH_INK)
    bl = baseline or {}
    panels = [
        ("HRV (ms)", [p.get("hrv_ms") for p in pts], bl.get("hrv_ms"), _CH_BLUE, 60),
        ("静息心率 (bpm)", [p.get("rhr_bpm") for p in pts], bl.get("rhr_bpm"), _CH_VERM, 196),
        ("睡眠 (h)", [p.get("sleep_h") for p in pts], bl.get("sleep_h"), _CH_GREEN, 332),
    ]
    left, right = 56, W - 16
    n = len(pts)
    for name, series, baseval, color, ytop in panels:
        ybot = ytop + 92
        dr.text((left, ytop - 18), name, font=f_lbl, fill=_CH_INK)
        vals = [v for v in series if isinstance(v, (int, float))]
        if len(vals) < 2:
            dr.text((left + 4, ytop + 30), "数据不足", font=f_tick, fill=_CH_MUTED)
            continue
        vmin = min(vals) * 0.95
        vmax = max(vals) * 1.05
        if vmax <= vmin:
            vmax = vmin + 1
        span = vmax - vmin
        if isinstance(baseval, (int, float)) and vmin <= baseval <= vmax:
            by = ybot - (baseval - vmin) / span * (ybot - ytop)
            for x in range(left, right, 8):  # 基线虚线
                dr.line([(x, by), (x + 4, by)], fill=_CH_MUTED, width=1)
            dr.text((right - 64, by - 6), f"基线 {baseval:.0f}", font=f_tick, fill=_CH_MUTED)
        xy = []
        for i, v in enumerate(series):
            if isinstance(v, (int, float)):
                x = left + (right - left) * (i / (n - 1))
                y = ybot - (v - vmin) / span * (ybot - ytop)
                xy.append((x, y))
        if len(xy) >= 2:
            dr.line(xy, fill=color, width=2)
        if xy:  # 最新点标个圆点
            lx, ly = xy[-1]
            dr.ellipse([lx - 3, ly - 3, lx + 3, ly + 3], fill=color)
        dr.text((4, ytop), f"{vmax:.0f}", font=f_tick, fill=_CH_MUTED)
        dr.text((4, ybot - 12), f"{vmin:.0f}", font=f_tick, fill=_CH_MUTED)
    dr.text((left, H - 16), pts[0]["date"], font=f_tick, fill=_CH_MUTED)
    dr.text((right - 40, H - 16), pts[-1]["date"], font=f_tick, fill=_CH_MUTED)
    return _img_to_png(img)


def _feishu_get_token(app_id, app_secret):
    """用 app_id/app_secret 换 tenant_access_token。失败抛 ApiError。"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with _urlopen(req, 30) as r:
        obj = json.loads(r.read().decode("utf-8"))
    if obj.get("code") != 0:
        raise ApiError(f"飞书 token 失败: {obj.get('msg') or obj}")
    return obj["tenant_access_token"]


def _feishu_resolve_chat(token, cfg):
    """解析目标群 chat_id：优先 config.feishu_chat_id；否则列出机器人所在群自动取。返回 None 表示失败。"""
    chat_id = (cfg.get("feishu_chat_id") or "").strip()
    if chat_id:
        return chat_id
    req = urllib.request.Request("https://open.feishu.cn/open-apis/im/v1/chats?page_size=50",
        method="GET", headers={"Authorization": f"Bearer {token}"})
    with _urlopen(req, 30) as r:
        obj = json.loads(r.read().decode("utf-8"))
    if obj.get("code") != 0:
        print(f"飞书列出群失败（需开权限 im:chat:readonly）: {obj.get('msg') or obj}", file=sys.stderr)
        return None
    items = ((obj.get("data") or {}).get("items")) or []
    if not items:
        print("飞书机器人未加入任何群：请把应用机器人拉进一个飞书群，或在 config 设 feishu_chat_id。", file=sys.stderr)
        return None
    if len(items) == 1:
        return items[0].get("chat_id")
    names = "\n".join(f"  - {it.get('name','?')} → chat_id={it.get('chat_id')}" for it in items)
    print(f"飞书机器人在多个群，请在 config.json 的 feishu_chat_id 指定一个：\n{names}", file=sys.stderr)
    return None


def _feishu_upload_image(token, png_bytes):
    """上传 PNG 到飞书图床 → image_key（multipart，标准库手写）。失败返回 None。"""
    boundary = "----onelap" + uuid.uuid4().hex
    bb = boundary.encode()
    head = (b"--" + bb + b"\r\n"
            + b'Content-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n'
            + b"--" + bb + b"\r\n"
            + b'Content-Disposition: form-data; name="image"; filename="chart.png"\r\n'
            + b"Content-Type: image/png\r\n\r\n")
    body = head + png_bytes + b"\r\n--" + bb + b"--\r\n"
    req = urllib.request.Request("https://open.feishu.cn/open-apis/im/v1/images",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with _urlopen(req, 30) as r:
            obj = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        hint = ""
        if "im:resource" in body_txt:
            hint = "（飞书应用缺权限 im:resource:upload：去开放平台开通该权限并发布版本后生效；未开通时图表会跳过、卡片照发）"
        print(f"飞书图片上传失败 HTTP {e.code}{hint}: {body_txt}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"飞书图片上传异常: {e}", file=sys.stderr)
        return None
    if obj.get("code") != 0:
        print(f"飞书图片上传失败: {obj.get('msg') or obj}", file=sys.stderr)
        return None
    return (obj.get("data") or {}).get("image_key")


def push_feishu(cfg, title, content, images=None):
    """通过飞书自建应用（App ID/Secret）把互动卡片发到群。images=[png_bytes,...] 时先上传为
    图片元素插到卡片顶部（训练图表）。成功返回 True。"""
    app_id = (cfg.get("feishu_app_id") or "").strip()
    app_secret = (cfg.get("feishu_app_secret") or "").strip()
    if not app_id or not app_secret:
        print("未配置 feishu_app_id/feishu_app_secret，跳过飞书推送。", file=sys.stderr)
        return False
    try:
        token = _feishu_get_token(app_id, app_secret)
        chat_id = _feishu_resolve_chat(token, cfg)
    except ApiError as e:
        print(f"飞书推送: {e}", file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(f"飞书推送网络错误: {e.reason}", file=sys.stderr)
        return False
    if not chat_id:
        return False
    elements = _feishu_card_elements(content)
    if not elements:
        elements = [{"tag": "markdown", "content": content[:28000]}]
    # 训练图表（计划vs实际 TSS、PMC 体能/疲劳）上传为图片元素，插到卡片最前
    if images:
        img_elements = []
        for png in images:
            if not png:
                continue
            key = _feishu_upload_image(token, png)
            if key:
                img_elements.append({"tag": "img", "img_key": key,
                                     "alt": {"tag": "plain_text", "content": "训练图表"}})
        if img_elements:
            elements = ([{"tag": "markdown", "content": "**📊 训练执行与负荷（数据图表）**"}]
                        + img_elements + [{"tag": "hr"}] + elements)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title[:100]},
            "template": "indigo",
            "subtitle": {"tag": "plain_text", "content": "AI 教练 · 每日训练分析"},
        },
        "elements": elements,
    }
    body = json.dumps({
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),  # content 必须是 JSON 字符串
    }).encode("utf-8")
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with _urlopen(req, 30) as r:
            obj = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"飞书推送 HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}", file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(f"飞书推送网络错误: {e.reason}", file=sys.stderr)
        return False
    if obj.get("code") == 0:
        print("✅ 报告已推送到飞书。", file=sys.stderr)
        return True
    print(f"飞书推送失败（需开权限 im:message）: {obj.get('msg') or obj}", file=sys.stderr)
    return False


def planned_tss_by_date(planned_items):
    """汇总每日【计划】TSS → {ISO日期: tss}。planned_items 可为 planned_workouts() 输出
    （date 为 date 对象、键 tss）或 list_planned_workouts 原始项（date 为字符串、键 TSS）。"""
    out = {}
    for p in planned_items or []:
        d = p.get("date")
        iso = d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]
        if len(iso) != 10:
            continue
        tss = to_num(first(p, "tss", "TSS", "tss_score", "load_tss")) or 0
        out[iso] = out.get(iso, 0) + tss
    return out


def actual_tss_by_date(pmc_pts):
    """汇总每日【实际】TSS → {ISO日期: tss}。pmc_items 元组 (date,ctl,atl,tsb,tss)。"""
    out = {}
    for d, _c, _a, _t, tss in (pmc_pts or []):
        if tss is None:
            continue
        out[d.isoformat()] = out.get(d.isoformat(), 0) + float(tss)
    return out


EXEC_DONE_RATIO = 0.7   # 实际/计划 TSS ≥ 0.7 视为完成；低于视为未完成（漏练）
EXEC_MISS_MIN_TSS = 30  # 漏练触发重排的门槛：计划 TSS < 30（轻量/恢复日）漏练只告知教练、不强制重排
HEARTBEAT_STALE_DAYS = 2  # --heartbeat：last_auto_run 距今超过这么多天则告警（自动化可能停摆）
OVERREACH_TSB = -20         # 过载硬保护：TSB 低于此值 → 今日强制休息
OVERREACH_RHR_DELTA = 5     # 过载硬保护：今日静息心率高于个人基线这么多 bpm → 今日强制休息


def execution_status_rows(planned_by_date, actual_by_date, today, days_back=7):
    """近 days_back 天（含今日）逐日 计划/实际 TSS + 完成状态，供教练 prompt 与"昨天漏练"判定共用。
    返回 [{date, planned, actual, done}]（升序，最旧→今日）：
    done=True 完成 / False 未完成 / None 当日无计划（休息/空档）。"""
    rows = []
    for i in range(days_back, -1, -1):
        iso = (today - timedelta(days=i)).isoformat()
        p = float(planned_by_date.get(iso, 0) or 0)
        a = float(actual_by_date.get(iso, 0) or 0)
        rows.append({"date": iso, "planned": p, "actual": a,
                     "done": (a >= p * EXEC_DONE_RATIO) if p > 0 else None})
    return rows


def yesterday_missed(planned_by_date, actual_by_date, today, threshold=EXEC_DONE_RATIO, min_tss=EXEC_MISS_MIN_TSS):
    """昨天有【实质性】训练课（计划 TSS ≥ min_tss）但未完成（实际 < threshold*计划）→ True，触发今日重排。
    轻量/恢复日（计划 TSS < min_tss）漏练不触发重排（只在教练执行情况里告知）；昨天无计划 → False。"""
    iso = (today - timedelta(days=1)).isoformat()
    p = float(planned_by_date.get(iso, 0) or 0)
    if p < min_tss:
        return False
    return float(actual_by_date.get(iso, 0) or 0) < p * threshold


def execution_rate(planned_by_date, actual_by_date, today, days_back=14):
    """近 days_back 天（含今日）训练执行率。只对 planned>0 的训练日计算（休息/空档日不计分母，
    避免休息日把分母撑大）。返回 {rate, planned_tss, actual_tss, train_days, rows[{date,planned,actual,rate}]}。"""
    rows = []
    tot_p = tot_a = 0.0
    train_days = 0
    for i in range(days_back):
        iso = (today - timedelta(days=i)).isoformat()
        p = float(planned_by_date.get(iso) or 0)
        a = float(actual_by_date.get(iso) or 0)
        if p <= 0:
            continue  # 非训练日（休息/空档）：不进分母
        tot_p += p
        tot_a += a
        train_days += 1
        rows.append({"date": iso, "planned": p, "actual": a, "rate": a / p})
    rate = (tot_a / tot_p) if tot_p > 0 else None
    return {"rate": rate, "planned_tss": tot_p, "actual_tss": tot_a,
            "train_days": train_days, "rows": rows}


def build_weekly_report(pmc, rides, today):
    """近 7 天（含今日）周报：骑行+跑步合计 TSS、CTL/ATL/TSB 起末、关键课、下周建议。
    跑步为交叉训练叠加层（来自 Apple Watch，不并入 OTM 的 PMC）。"""
    start = today - timedelta(days=6)
    L = [f"# 📅 周报 {start.isoformat()} ~ {today.isoformat()}", ""]
    past = [x for x in pmc if start <= x[0] <= today]
    if past:
        s, e = past[0], past[-1]
        L.append(f"**体能（PMC）**：CTL {fmt_num(s[1],0)}→{fmt_num(e[1],0)}，"
                 f"ATL {fmt_num(s[2],0)}→{fmt_num(e[2],0)}，TSB {fmt_num(s[3],0)}→{fmt_num(e[3],0)}"
                 + (f"（{tsb_interp(e[3])}）" if e[3] is not None else ""))
    wk = [r for r in rides if start <= r["date"] <= today]
    bike_tss = sum((r["tss"] or 0) for r in wk)
    bike_km = sum((r["distance_km"] or 0) for r in wk)
    bike_min = sum((r["duration_s"] or 0) for r in wk) / 60
    L.append(f"\n**骑行**：{len(wk)} 次 / {bike_tss:.0f} TSS / {bike_km:.0f} km / {bike_min:.0f} min")
    for r in wk[-5:]:
        L.append(f"  - {r['date'].isoformat()} {first(r,'name',default='')}：{(r['tss'] or 0):.0f} TSS，"
                 f"{(r['distance_km'] or 0):.0f}km，均功 {fmt_num(r['avg_power'],0)}W")
    runs = [r for r in _read_run_history() if start.isoformat() <= r.get("date", "") <= today.isoformat()]
    run_tss = sum((r.get("tss") or 0) for r in runs)
    if runs:
        L.append(f"\n**跑步（交叉训练）**：{len(runs)} 次 / {run_tss:.0f} TSS")
    L.append(f"\n**本周合计训练负荷**：骑行 {bike_tss:.0f} + 跑步 {run_tss:.0f} = **{bike_tss + run_tss:.0f} TSS**")
    if past:
        e = past[-1]
        L.append(f"\n**下周提示**：当前 TSB {fmt_num(e[3],0)}（{tsb_interp(e[3])}）。"
                 + ("疲劳偏高，下周优先恢复 + 1 次强度课。" if (e[3] or 0) < -10
                    else "状态良好，可安排 1 次长骑 + 1 次强度课，循序渐进。"))
    L.append("\n_数据：骑行/PMC 来自 OTM，跑步来自 Apple Watch（交叉训练叠加层）。_")
    return "\n".join(L)


def build_report(pmc, rides, planned, training_plans, today, days_back, days_ahead, coach_md=None, coach_model="", weather=None, show_weekend=False, exec_rate=None):
    L = []
    L.append(f"# Onelap 训练分析报告  ·  {today.isoformat()}\n")

    # ---- 一、最近训练强度 ----
    L.append("## 一、最近的训练强度\n")

    # PMC：取 ≤ 今天 的最新一条作为“当前状态”
    pmc_past = [x for x in pmc if x[0] <= today]
    latest = pmc_past[-1] if pmc_past else (pmc[-1] if pmc else None)
    if latest:
        d, ctl, atl, tsb, _tss = latest
        L.append(f"- 当前体能 **CTL**：{fmt_num(ctl, 1)}")
        L.append(f"- 当前疲劳 **ATL**：{fmt_num(atl, 1)}")
        L.append(f"- 当前状态 **TSB**：{fmt_num(tsb, 1)}  （数据日期 {d}）")
        if tsb is not None:
            L.append(f"  - 解读：{tsb_interp(tsb)}")
        # 近 7 天 CTL 变化
        if len(pmc_past) >= 8:
            wk = pmc_past[-8]
            if ctl is not None and wk[1] is not None:
                delta = ctl - wk[1]
                arrow = "↑ 体能建设中" if delta > 0.5 else ("↓ 可能减量/休息期" if delta < -0.5 else "→ 基本持平")
                L.append(f"- 近 7 天 CTL：{wk[1]:.1f} → {ctl:.1f}（{arrow}）")
        L.append("")
    else:
        L.append("- （未取到 PMC 数据，可用 `--raw` 排查。）\n")

    # 近 N 天实际骑行
    window_start = today - timedelta(days=days_back - 1)
    recent = [r for r in rides if window_start <= r["date"] <= today]
    L.append(f"### 近 {days_back} 天实际骑行（已完成）\n")
    if recent:
        tot_tss = sum((r["tss"] or 0) for r in recent)
        tot_dist = sum((r["distance_km"] or 0) for r in recent)
        tot_dur = sum((r["duration_s"] or 0) for r in recent)
        powers = [r["avg_power"] for r in recent if r["avg_power"]]
        hrs = [r["avg_hr"] for r in recent if r["avg_hr"]]
        L.append(f"- 骑行次数：**{len(recent)}** 次")
        L.append(f"- 累计 TSS：**{tot_tss:.0f}**（日均 {tot_tss/days_back:.1f}）")
        L.append(f"- 累计距离：{tot_dist:.0f} km；累计时长：{human_duration(tot_dur)}")
        if powers:
            L.append(f"- 平均功率：{sum(powers)/len(powers):.0f} W（区间 {min(powers):.0f}–{max(powers):.0f}）")
        if hrs:
            L.append(f"- 平均心率：{sum(hrs)/len(hrs):.0f} bpm\n")
        L.append("| 日期 | 星期 | TSS | 距离 | 时长 | 均功 | 均心 |")
        L.append("|---|---|---|---|---|---|---|")
        for r in recent:
            L.append(f"| {r['date'].isoformat()} | {weekday_cn(r['date'])} | "
                     f"{fmt_num(r['tss'],0)} | {fmt_num(r['distance_km'],0)} km | "
                     f"{human_duration(r['duration_s'])} | {fmt_num(r['avg_power'],0)} W | "
                     f"{fmt_num(r['avg_hr'],0)} |")
        L.append("")
    else:
        L.append(f"- 近 {days_back} 天没有骑行记录（可能是这几天确实没骑，或记录未同步）。\n")

    # ---- 训练执行率（计划 vs 实际）----
    if exec_rate and exec_rate.get("train_days"):
        L.append(f"### 训练执行率（计划 vs 实际，近 {days_back} 天）\n")
        r = exec_rate["rate"]
        rate_txt = f"{r*100:.0f}%" if r is not None else "--"
        flag = ("（⚠️ 偏低：计划没骑够——要么计划定高了，要么需要补骑）" if r is not None and r < 0.70
                else "（⚠️ 偏高：实际远超计划，留意过载）" if r is not None and r > 1.15
                else "（✅ 执行良好）" if r is not None else "")
        L.append(f"- 训练日 {exec_rate['train_days']} 天：计划 TSS **{exec_rate['planned_tss']:.0f}** / "
                 f"实际 **{exec_rate['actual_tss']:.0f}** → 执行率 **{rate_txt}**{flag}\n")
        L.append("| 日期 | 计划TSS | 实际TSS | 完成 |")
        L.append("|---|---|---|---|")
        for row in sorted(exec_rate["rows"], key=lambda x: x["date"], reverse=True):
            rr = row["rate"]
            mark = "✅" if 0.70 <= rr <= 1.15 else ("⬇️" if rr < 0.70 else "⬆️")
            L.append(f"| {row['date']} | {row['planned']:.0f} | {row['actual']:.0f} | {mark} {rr*100:.0f}% |")
        L.append("")

    # ---- 二、今日北京天气与户外训练适宜度（Open-Meteo；失败则省略） ----
    if weather:
        L.extend(render_weather_section(weather, today, show_weekend=show_weekend))

    # ---- 三、未来训练安排（glm-5.2 教练生成；OTM 课表为空时用此） ----
    L.append(f"## 三、未来 {days_ahead} 天的训练安排\n")
    if coach_md:
        L.append(f"> 以下计划由 {coach_model or 'GLM'}（角色：自行车教练）基于你上面的历史训练数据生成：\n")
        L.append(coach_md.strip())
        L.append("")
    else:
        fut_start = today + timedelta(days=1)
        fut_end = today + timedelta(days=days_ahead)
        future = [w for w in planned if fut_start <= w["date"] <= fut_end]

        if future:
            fut_tss = sum((w["tss"] or 0) for w in future)
            L.append(f"- 已排 **{len(future)}** 次课，计划累计 TSS 约 **{fut_tss:.0f}**\n")
            L.append("| 日期 | 星期 | 课目 | 目标 TSS | 时长 | 强度 IF |")
            L.append("|---|---|---|---|---|---|")
            for w in future:
                ifs = w["if_score"]
                L.append(f"| {w['date'].isoformat()} | {weekday_cn(w['date'])} | {w['name']} | "
                         f"{fmt_num(w['tss'],0)} | {human_duration(w['duration_s'])} | "
                         f"{fmt_num(ifs,2) if ifs is not None else '--'} |")
            L.append("")
        else:
            total_planned = len(planned)
            tp_count = 0
            if isinstance(training_plans, dict):
                personal = (training_plans.get("personal") or {}).get("plans") or []
                groups = training_plans.get("groups") or []
                tp_count = len(personal) + sum(len((g.get("plans") or [])) for g in groups)
            if total_planned == 0 and tp_count == 0:
                L.append(f"- 你的 OTM 账号里**没有预先排定的未来训练课**，也没有训练计划。")
                L.append("  （看起来你是「骑完即记」型，没用 OTM 的课表/计划功能。）\n")
                L.append("**建议**：在 config.json 填入 `glm_api_key`，即可用 AI 教练基于历史数据自动生成未来计划；")
                L.append("或在 OTM 的「训练计划」里创建/套用官方计划。\n")
            elif total_planned > 0:
                L.append(f"- 未来 {days_ahead} 天没有排课（课表里共 {total_planned} 项，但都不在窗口内）。\n")

    L.append("---")
    L.append("说明：CTL≈长期体能负荷，ATL≈短期疲劳，TSB=CTL−ATL（正值偏清爽、负值偏疲劳）。")
    L.append("TSS=单次训练压力分数，IF=强度系数。数据来源 otm.onelap.cn。\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# OTM 导入：计划日 → 间歇训练课 → 写入账号
# 字段结构 2026-07-21 由前端压缩 JS 逆向确认（workout_editer/calendar chunk 里的
#   Dt()/Ca()/Ke()）：节点用 {duration:{...}, target:[...], tips, remark, intensity}；
#   创建时整个 workout 对象被 JSON.stringify 后包进 {data: "..."} 再 POST。
# ---------------------------------------------------------------------------
_ZONE_POWER = {"Z1": (50, 60), "Z2": (60, 72), "Z3": (75, 85), "甜区": (88, 94),
               "Z4": (95, 105), "Z5": (105, 120), "Z6": (120, 135)}


def zone_to_power(zone, if_score):
    """zone/IF → (min%, max%) of FTP。"""
    z = str(zone or "").strip()
    if z in _ZONE_POWER:
        return _ZONE_POWER[z]
    if if_score and if_score > 0:
        c = if_score * 100
        return (max(40, int(c - 6)), min(150, int(c + 6)))
    return (60, 72)


# %FTP 下界 → zone（_ZONE_POWER 的反向映射，把实际功率落到区间）
_ZONE_BY_PCT_LO = [(120, "Z6"), (105, "Z5"), (95, "Z4"), (88, "甜区"), (75, "Z3"), (60, "Z2")]
_ZONE_RANK = {"Z1": 1, "Z2": 2, "Z3": 3, "甜区": 4, "Z4": 5, "Z5": 6, "Z6": 7}


def power_to_zone(avg_power_w, ftp):
    """avg_power(W) → %FTP → zone 标签（Z1..Z6/甜区）。无功率/FTP → ""。
    ⚠️ 单次骑行 avg_power 含热身/放松，会把 Z4 间歇摊低——仅适合判【明显错配】，非间歇精度。"""
    if not avg_power_w or not ftp or ftp <= 0:
        return ""
    pct = avg_power_w / ftp * 100
    for lo, z in _ZONE_BY_PCT_LO:
        if pct >= lo:
            return z
    return "Z1"


def planned_zone_by_date(scheduled_items, ftp):
    """逐日计划 zone（IF→%FTP→power_to_zone，_guess_zone(name) 交叉）；同日多课取强度最高。
    scheduled_items 用 list_planned_workouts() 输出（date 字符串、键 IF/name）。返回 {iso: zone}。"""
    out = {}
    for it in scheduled_items or []:
        d = it.get("date")
        iso = d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]
        if len(iso) != 10:
            continue
        name = first(it, "name", "title", default="")
        gz = _guess_zone(name)
        ifr = to_num(first(it, "if", "IF", "ifScore", "intensityFactor"))
        z = gz or (power_to_zone((ifr or 0) * ftp, ftp) if ifr else "")
        if z and (z not in out or _ZONE_RANK.get(z, 0) > _ZONE_RANK.get(out[iso], 0)):
            out[iso] = z
    return out


def zone_alignment_rows(planned_zone_by_d, rides, ftp, today, days_back=7):
    """近 days_back 天（不含今日）逐日：计划 zone vs 实际 zone（当日最高 avg_power 的骑行→zone）。
    返回 [{date, planned_zone, actual_zone, mismatch}]。mismatch=True 仅标【明显错配】
    （计划≥甜区/Z4 即 rank≥4 但实际≤Z2 即 rank≤2）。avg_power 对间歇不准，故只标明显错配。"""
    by_date = {}
    for r in rides or []:
        iso = r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"])[:10]
        by_date.setdefault(iso, []).append(r)
    rows = []
    for i in range(days_back, 0, -1):
        iso = (today - timedelta(days=i)).isoformat()
        day = by_date.get(iso, [])
        if not day:
            continue
        top = max(day, key=lambda r: r.get("avg_power") or 0)
        az = power_to_zone(top.get("avg_power"), ftp)
        pz = planned_zone_by_d.get(iso, "")
        mismatch = bool(pz) and _ZONE_RANK.get(pz, 0) >= 4 and _ZONE_RANK.get(az, 0) <= 2 and az != ""
        rows.append({"date": iso, "planned_zone": pz, "actual_zone": az, "mismatch": mismatch})
    return rows


RUN_IF_ASSUME = 0.8  # 跑步无心率时的假定强度因子（估算）


def run_tss(duration_s, avg_hr=None, lthr=180):
    """跑步 TSS。HR-based 优先：IF=clamp(avg_hr/lthr,0.5,1.2)，TSS=duration_h*IF²*100。
    无 avg_hr 时退化为时长×RUN_IF_ASSUME（结果为估算）。返回 (tss, estimated_bool)。"""
    if not duration_s or duration_s <= 0:
        return 0.0, False
    dur_h = duration_s / 3600.0
    if avg_hr and lthr and lthr > 0:
        ifr = max(0.5, min(1.2, float(avg_hr) / float(lthr)))
        return round(dur_h * ifr * ifr * 100, 1), False
    ifr = RUN_IF_ASSUME
    return round(dur_h * ifr * ifr * 100, 1), True


def _main_segments(zone, main_s, lo, hi, name):
    """主体时段 → 段列表。Z5/Z6（VO2/无氧）拆成 on/off 微间歇并【封顶高强度总量】
    （Z5≤18min、Z6≤8min 停留在目标区），其余主体时间用 Z2 耐力填充——
    避免一次堆太多 VO2（户外也骑不下来），且总时长≈main_s。Z1/Z2/Z3/Z4/甜区 为连续块。"""
    z = str(zone or "").strip()
    if z in ("Z5", "Z6"):
        on = 180 if z == "Z5" else 60            # Z5: 3min 冲；Z6: 1min 冲
        off = 120 if z == "Z5" else 180           # Z5: 2min 恢复；Z6: 3min 恢复
        cap = (18 * 60) if z == "Z5" else (8 * 60)  # 高强度累计上限
        segs, on_total, i = [], 0, 1
        while on_total + on <= cap and on_total + on + off <= main_s:
            segs.append({"name": f"{name}·冲{i}", "duration_s": on, "lo": lo, "hi": hi})
            segs.append({"name": f"恢复{i}", "duration_s": off, "lo": 50, "hi": 60})
            on_total += on
            i += 1
        used = sum(s["duration_s"] for s in segs)
        if main_s - used > 60:                    # 剩余时间用 Z2 耐力填充（保证总时长≈main_s）
            segs.append({"name": "耐力填充", "duration_s": main_s - used, "lo": 60, "hi": 72})
        return segs or [{"name": name, "duration_s": main_s, "lo": lo, "hi": hi}]
    return [{"name": name, "duration_s": main_s, "lo": lo, "hi": hi}]


def build_intervals(day):
    """计划日 → 间歇段列表 [{name, duration_s, lo, hi}]。休息日返回空。
    Z5/Z6（VO2/无氧）主体拆成 on/off 微间歇（路骑可执行、且封顶高强度量）；其余强度为连续块。"""
    if day["action"] == "rest" or day["duration_min"] <= 0:
        return []
    total = day["duration_min"] * 60
    lo, hi = zone_to_power(day["zone"], day["if"])
    warmup = min(600, int(total * 0.12))
    cooldown = min(600, int(total * 0.12))
    main = max(60, total - warmup - cooldown)
    segs = []
    if warmup > 0:
        segs.append({"name": "热身", "duration_s": warmup, "lo": 50, "hi": min(60, lo)})
    segs.extend(_main_segments(day["zone"], main, lo, hi, day["name"] or "主体"))
    if cooldown > 0:
        segs.append({"name": "放松", "duration_s": cooldown, "lo": 45, "hi": 60})
    return segs


def _step_node(duration_s, lo, hi):
    """构造一个间歇节点（OTM 服务端格式，由前端 Dt() + 真实课表 GET 逆向确认）。
    target.value 是单个 %FTP（取区间中点，与官方课表一致）；踏频(type=3)不设 value。"""
    mid = int(round((lo + hi) / 2))
    return {
        "duration": {"type": 0, "value": int(duration_s), "unit": "s", "units": "min"},
        "target": [
            {"type": 4, "unit": "%", "value": mid},
            {"type": 3, "unit": "rpm"},
        ],
        "intensity": 0,
        "tips": [],
        "remark": "",
    }


def build_workout_payload(day):
    """构造创建训练课的内层对象（OTM 服务端格式）。
    外层会被 create_workout 再包成 {data: JSON.stringify(...)}。休息日返回 None。
    关键：必须带 IF/TSS（后端校验，缺则静默返回 null 不落库）；取自教练给的 day.if/day.tss。"""
    segs = build_intervals(day)
    if not segs:
        return None
    total_s = int(sum(s["duration_s"] for s in segs))
    return {
        "name": f"{day['name']}（计划）",
        "workoutType": 1,      # 1 = 骑行
        "group_id": -1,        # -1 = 个人（无教练分组）
        "distance": int(total_s * 8),
        "duration": total_s,
        "favorite": 0,
        "public": False,
        "intro": "",
        "IF": round(float(day.get("if") or 0), 2),
        "TSS": int(day.get("tss") or 0),
        "steps": [_step_node(s["duration_s"], s["lo"], s["hi"]) for s in segs],
    }


def create_workout(cfg, payload):
    """创建训练课。OTM 要求 HTTP body = {data: JSON.stringify(内层 workout 对象)}（双重编码）。"""
    wrapped = {"data": json.dumps(payload, ensure_ascii=False)}
    return api_request(cfg, EP_WORKOUT_CREATE, "POST", wrapped)


def assign_plan(cfg, wid, date_s):
    """把训练课排到某日期：URL 带 wid，body={date,wid}（不套 data，与前端 mt() 一致）。"""
    return api_request(cfg, f"{EP_WORKOUT_PLAN}/{wid}", "POST", {"wid": wid, "date": date_s})


PLAN_MARKER = "（计划）"  # 脚本导入的课名后缀，用于识别「自己造的课」并清理


def list_planned_workouts(cfg):
    """读已排课日历：POST /api/otm/calendar/workout/plan body={} → [{date,name,wid,did,duration,IF,TSS,steps}]。
    注意：不是 workout/list（那是个人训练库，已排期的课不在那里）。"""
    return api_request(cfg, "/api/otm/calendar/workout/plan", "POST", {}) or []


def delete_workout(cfg, wid):
    return api_request(cfg, f"{EP_WORKOUT_CREATE}/{wid}", "DELETE", {})


def cleanup_future_plan(cfg, start_date):
    """删除日期 >= start_date 且名字含 PLAN_MARKER 的已排课（只动脚本自己导入的课）。
    保留用户手动建的课（不含 marker）和历史课（< start_date）。返回 (删除数, 跳过数)。"""
    deleted, skipped = 0, 0
    for p in list_planned_workouts(cfg):
        if PLAN_MARKER not in str(p.get("name", "")):
            continue
        d = parse_date(str(p.get("date", ""))[:10])
        if d is None or d < start_date:
            skipped += 1
            continue
        wid = p.get("wid")
        try:
            delete_workout(cfg, wid)
            deleted += 1
            log(f"  删除旧计划课 wid={wid} ({p.get('name')}, {d})")
        except ApiError as e:
            log(f"  删除 wid={wid} 失败: {e}")
    return deleted, skipped


def import_plan(cfg, days, dry_run=True, test_date=None):
    """按 days 创建/预览训练课。返回结果列表（含 wid/error）。"""
    results = []
    for day in days:
        if test_date and day["date"] != test_date:
            continue
        segs = build_intervals(day)
        if not segs:
            print(f"  {day['date']} {weekday_cn(day['date'])} 休息日 → 跳过", file=sys.stderr)
            continue
        seg_str = " + ".join(f"{s['name']}{s['duration_s']//60}min@{s['lo']}-{s['hi']}%" for s in segs)
        if dry_run:
            print(f"  [DRY] {day['date']} {weekday_cn(day['date'])} {day['name']} | "
                  f"{seg_str} | 目标TSS {day['tss']}", file=sys.stderr)
            results.append({"date": day["date"].isoformat(), "name": day["name"],
                            "segments": segs, "target_tss": day["tss"]})
            continue
        # 拆成「创建」与「排期」两步：排期失败时回滚已建的课，避免 OTM 训练库
        # 累积未排期的「（计划）」孤儿课（cleanup_future_plan 只扫已排期课，看不到孤儿）。
        try:
            res = create_workout(cfg, build_workout_payload(day))
        except ApiError as e:
            print(f"  [FAIL] {day['date']} {day['name']} 创建课失败: {e}", file=sys.stderr)
            results.append({"date": day["date"].isoformat(), "name": day["name"],
                            "error": f"创建失败: {e}"})
            continue
        wid = (res or {}).get("wid") or (res or {}).get("id") or (res or {}).get("_id")
        if not wid:
            print(f"  [FAIL] {day['date']} {day['name']} 创建课未返回 wid: {res}", file=sys.stderr)
            results.append({"date": day["date"].isoformat(), "name": day["name"],
                            "error": f"无 wid: {res}"})
            continue
        try:
            assign_plan(cfg, wid, day["date"].isoformat())
        except ApiError as e:
            try:
                delete_workout(cfg, wid)
                log(f"  排期失败，已回滚删除 wid={wid}：{e}")
                rolled = True
            except ApiError as de:
                log(f"  ⚠️ 排期失败且回滚删除也失败 wid={wid}：{de}（训练库可能残留孤儿课，需手动清）")
                rolled = False
            print(f"  [FAIL] {day['date']} {day['name']} 排期失败"
                  f"{'（已回滚）' if rolled else '（回滚失败，请手动清理训练库）'}: {e}", file=sys.stderr)
            results.append({"date": day["date"].isoformat(), "name": day["name"], "wid": wid,
                            "rolled_back": rolled, "error": f"排期失败: {e}"})
            continue
        print(f"  [OK]  {day['date']} {day['name']} → wid={wid}", file=sys.stderr)
        results.append({"date": day["date"].isoformat(), "name": day["name"],
                        "wid": wid, "response": res})
    return results


def _import_ok_count(results):
    """数导入成功的天数：有 wid 且无 error（创建失败/无 wid/排期失败/回滚失败都带 error，不算成功）。
    休息日不进 results，故成功率的分母应取 len(results) 而非计划总天数。"""
    return len([r for r in results if r.get("wid") and not r.get("error")])


# ---------------------------------------------------------------------------
# 休息日 flag（用户标记）/ 计划降频复用 / 按日清理与读取
# ---------------------------------------------------------------------------
def load_rest_flags():
    """读 rest_flags.json → set of 'YYYY-MM-DD'。"""
    try:
        d = json.load(open(REST_FLAGS, encoding="utf-8"))
        return set(str(x) for x in (d.get("rest_dates") or []))
    except Exception:
        return set()


def save_rest_flags(dates):
    """原子写回休息日集合（升序）。"""
    dates = sorted(set(str(x) for x in dates))
    tmp = REST_FLAGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"rest_dates": dates}, f, ensure_ascii=False)
    os.replace(tmp, REST_FLAGS)


def mark_rest(date_iso, clear=False):
    """标记/取消某日为休息。返回当前是否仍为休息。"""
    flags = load_rest_flags()
    if clear:
        flags.discard(date_iso)
    else:
        flags.add(date_iso)
    save_rest_flags(flags)
    return date_iso in flags


def read_date_flag(path):
    """读一个写有 'YYYY-MM-DD' 的标志文件 → date 或 None。"""
    try:
        return parse_date(open(path, encoding="utf-8").read().strip())
    except Exception:
        return None


def cleanup_plan_on_date(cfg, date_iso):
    """删除某日日历上【脚本导入的】（计划）课。返回删除数。用户手建的课不动。"""
    n = 0
    for p in list_planned_workouts(cfg):
        if PLAN_MARKER not in str(p.get("name", "")):
            continue
        if str(p.get("date", ""))[:10] == date_iso:
            try:
                delete_workout(cfg, p.get("wid"))
                n += 1
                log(f"  删除休息日计划课 wid={p.get('wid')} ({p.get('name')}, {date_iso})")
            except ApiError as e:
                log(f"  删除 wid={p.get('wid')} 失败: {e}")
    return n


def get_plan_on_date(cfg, date_iso):
    """读某日日历上排定的训练课（list[dict]，含用户手建+脚本导入）。"""
    out = []
    for p in list_planned_workouts(cfg):
        if str(p.get("date", ""))[:10] == date_iso:
            out.append(p)
    return out



# ---------------------------------------------------------------------------
# 样例数据
# ---------------------------------------------------------------------------
def sample_data(today):
    pmc = []
    for i in range(45, -1, -1):
        d = today - timedelta(days=i)
        ctl = 42 + (45 - i) * 0.25 + (i % 7) * 0.5
        atl = ctl - 6 + (i % 5) * 2.5
        pmc.append({"date": d.isoformat(), "ctl": round(ctl, 1),
                    "atl": round(atl, 1), "tsb": round(ctl - atl, 1)})
    rides = []
    for i in range(14):
        d = today - timedelta(days=14 - 1 - i)
        if d.weekday() in (2, 5):
            rides.append({"start_riding_time": d.isoformat() + " 18:00:00",
                          "name": "阈值课", "load_tss": 88, "time_seconds": 3600,
                          "distance_km": 28, "avg_power_w": 210, "avg_heart_bpm": 158})
        elif d.weekday() == 0:
            rides.append({"start_riding_time": d.isoformat() + " 07:00:00",
                          "name": "恢复骑", "load_tss": 30, "time_seconds": 2700,
                          "distance_km": 20, "avg_power_w": 130, "avg_heart_bpm": 128})
    return pmc, rides, []


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Onelap OTM 训练分析报告")
    ap.add_argument("--days-back", type=int, default=14, help="回看几天（默认 14）")
    ap.add_argument("--days-ahead", type=int, default=None, help="展望几天（没传则读 config.json 的 days_ahead，默认 14）")
    ap.add_argument("--raw", action="store_true", help="原始 JSON 落盘（调试用）")
    ap.add_argument("--sample", action="store_true", help="用假数据预览，不联网")
    ap.add_argument("--no-coach", action="store_true", help="不调用 glm-5.2 教练生成计划")
    ap.add_argument("--no-save", action="store_true", help="不写报告文件")
    ap.add_argument("--push", action="store_true", help="把报告推送到微信（Server酱，需配 serverchan_key）")
    ap.add_argument("--dry-run-import", action="store_true", help="预览把计划导入 OTM 的训练课（不写入）")
    ap.add_argument("--import-test-date", metavar="YYYY-MM-DD", help="只在该日期创建 1 条训练课（实测验证）")
    ap.add_argument("--import", dest="do_import", action="store_true", help="把计划批量写入 OTM 日历")
    ap.add_argument("--auto", action="store_true",
                    help="每日自动模式（readiness 上传触发 / 可选 cron 兜底）：刷新token→抓数据→生成计划→删旧→导入→推送→写日志")
    ap.add_argument("--start-today", action="store_true", help="计划从今天起（默认从明天起）；--auto 默认开启")
    ap.add_argument("--no-cleanup", action="store_true", help="--auto 时导入前不删除旧的「（计划）」课")
    ap.add_argument("--retries", type=int, default=12, help="--auto 时教练 LLM 调用失败的重试次数（默认 12）")
    ap.add_argument("--force", action="store_true", help="强制再跑一次 --auto（忽略「今日已运行」防重复，调试用）")
    ap.add_argument("--no-weather", action="store_true", help="跳过北京各区天气抓取")
    ap.add_argument("--weather-only", action="store_true",
                    help="只抓天气并打印适宜度表后退出（不联网 OTM、不调 LLM，验证用）")
    ap.add_argument("--show-weekend", action="store_true",
                    help="强制展示「周末去哪骑」（平时仅周五/节假日自动触发；调试/预览用）")
    ap.add_argument("--regen", action="store_true",
                    help="强制重新生成 AI 计划（忽略降频复用，单次跑仍调教练）")
    ap.add_argument("--rest", metavar="DATE", nargs="?", const="tomorrow",
                    help="标记某日为休息日（默认明天；可给 YYYY-MM-DD 或 today），写入 rest_flags 后退出。"
                         "标记今日会立即删除当日计划课。取消用 --rest-clear")
    ap.add_argument("--rest-clear", metavar="DATE", nargs="?", const="all",
                    help="取消休息日标记（给日期或 all 全清）后退出")
    ap.add_argument("--heartbeat", action="store_true",
                    help="心跳检查：读 last_auto_run.txt，若距上次成功 --auto 超过 %d 天则推送告警。供 systemd timer 每日调用。" % HEARTBEAT_STALE_DAYS)
    ap.add_argument("--weekly", action="store_true",
                    help="输出近 7 天周报（骑行+跑步合计 TSS、体能变化、下周建议）后退出，可配合 --push")
    args = ap.parse_args()
    if getattr(args, "weekly", False):
        args.no_coach = True  # 周报不需要教练计划，跳过 glm 调用（省 token）

    # --days-ahead：命令行优先；没传就读 config.json 的 days_ahead，再默认 14
    if args.days_ahead is None:
        try:
            _c = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
        except Exception:
            _c = {}
        args.days_ahead = int(_c.get("days_ahead", 14) or 14)

    today = date.today()

    if getattr(args, "heartbeat", False):
        # 心跳：last_auto_run.txt 距今 > HEARTBEAT_STALE_DAYS → 推送告警（自动化可能停摆）
        cfg = load_config()
        _guard = os.path.join(HERE, "last_auto_run.txt")
        try:
            _last = datetime.strptime(open(_guard, encoding="utf-8").read().strip()[:10], "%Y-%m-%d").date()
        except Exception:
            _last = None
        if _last is None:
            push_alert(cfg, "⚠️ 训练自动化停摆",
                       "未找到 last_auto_run.txt（--auto 从未成功跑过）。检查服务器 onelap-readiness 服务 + iPhone 触发链路。")
        else:
            _age = (today - _last).days
            if _age >= HEARTBEAT_STALE_DAYS:
                push_alert(cfg, "⚠️ 训练自动化停摆",
                           f"上次成功 --auto 在 {_age} 天前（{_last.isoformat()}）。"
                           f"可能：服务挂了 / iPhone 没推 readiness / 端点不通。请排查。")
            else:
                log(f"心跳正常：上次 --auto 在 {_age} 天前（{_last.isoformat()}）。")
        return

    cfg = {}
    coach_md = None
    coach_plan = None
    weather = None
    show_weekend = False
    exec_rate = None
    charts = []

    # --weather-only：只抓天气并打印，不联网 OTM、不调 LLM（最快验证天气链路）
    if args.weather_only:
        _c = {}
        _home = None
        try:
            _c = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
            _home = _c.get("home_district")
        except Exception:
            pass
        _wk = args.show_weekend or weather_show_weekend(today, _c)
        print("正在抓取北京各区实时天气（Open-Meteo，免费无 key）……", file=sys.stderr)
        weather = get_beijing_weather(today, weather_fetch_days(today, _wk), _home)
        if not weather:
            print("天气获取失败（网络/接口），可加 --no-weather 跳过。", file=sys.stderr)
            sys.exit(1)
        print("\n" + "\n".join(render_weather_section(weather, today, show_weekend=_wk)))
        print("\n**全 16 区明细**\n")
        print("\n".join(weather_full_table_lines(weather)))
        return

    # --rest / --rest-clear：标记/取消休息日（标记今日会立即删当日计划课），完成后退出
    if args.rest is not None or args.rest_clear is not None:
        _cfg = load_config() if os.path.exists(CONFIG_PATH) else {}
        if args.rest is not None:
            _d = args.rest
            _rd = (today.isoformat() if _d == "today"
                   else (today + timedelta(days=1)).isoformat() if _d == "tomorrow" else _d)
            if not parse_date(_rd):
                print(f"日期格式不对：{_rd}（应为 YYYY-MM-DD / today / tomorrow）", file=sys.stderr)
                sys.exit(1)
            mark_rest(_rd)
            msg = f"✅ 已标记 {_rd} 为休息日。该日 --auto 会跳过 AI、移除当日计划课。"
            if _rd == today.isoformat():
                try:
                    msg += f" 已立即删除当日 {cleanup_plan_on_date(_cfg, _rd)} 节计划课。"
                except Exception as e:
                    msg += f" 删除当日课失败：{e}"
            print(msg, file=sys.stderr)
        else:
            _d = args.rest_clear
            if _d == "all":
                save_rest_flags([])
                print("✅ 已清空所有休息日标记。", file=sys.stderr)
            else:
                mark_rest(_d, clear=True)
                print(f"✅ 已取消 {_d} 的休息日标记。", file=sys.stderr)
        return

    # --auto：一站式每日流程（刷新 token / 推送 / 导入 / 从今天起 / 写日志）
    if args.auto:
        _AUTO_CRASH_CTX.update(auto=True, done=False, today=today.isoformat(), cfg={})  # 崩溃兜底用
        # 每日只自动跑一次（避免「早晨触发 + 备份 cron」同日重复）；--force 可强制重跑
        _guard = os.path.join(HERE, "last_auto_run.txt")
        if not getattr(args, "force", False):
            try:
                if open(_guard, encoding="utf-8").read().strip() == today.isoformat():
                    log(f"今日已自动运行过（{today.isoformat()}），跳过。强制重跑请加 --force。")
                    _AUTO_CRASH_CTX["done"] = True  # 正常跳过，不算崩溃
                    return
            except FileNotFoundError:
                pass
        # 并发锁：readiness 多次更新触发 / cron 与触发同时跑时，只让一个 --auto 跑
        if not _acquire_auto_lock():
            log("已有 --auto 在运行中，跳过本次（数据变化会在那次跑里体现）。")
            _AUTO_CRASH_CTX["done"] = True
            return
        os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)  # 供 cron 重定向 logs/auto.log
        args.push = True
        args.do_import = True
        args.start_today = True
        log("==== 自动运行开始 ====")

    start_date = today if getattr(args, "start_today", False) else (today + timedelta(days=1))

    if args.sample:
        print("（--sample 模式：使用假数据预览，不调用教练）\n")
        pmc_raw, rides_raw, planned_raw = sample_data(today)
        rides = ride_items(rides_raw)
        planned = planned_workouts(planned_raw)
        training_plans = {}
        weather = sample_weather(today)
        show_weekend = True  # 样例预览展示「周末去哪骑」
        _pmc = pmc_items(pmc_raw)
    else:
        cfg = load_config()
        append_readiness_history(load_readiness())  # 记今日 readiness 进历史，供算个人基线
        _AUTO_CRASH_CTX["cfg"] = cfg  # 让崩溃兜底告警能拿到配置（含推送 key）
        if args.auto:
            try:
                cfg, refreshed = refresh_access_token(cfg)
                log(f"token {'已刷新并写回 config' if refreshed else '未配 refresh_token，沿用静态 token'}")
                # refresh_token 自身约 60 天有效且不轮换；快到期主动告警，避免某天静默断档
                _left = refresh_token_days_left(cfg)
                if _left is not None and _left < 7:
                    log(f"⚠️ refresh_token 剩余 {_left:.1f} 天，已推送告警")
                    push_alert(cfg, "⏰ OTM refresh_token 即将过期",
                               f"refresh_token 剩余约 {_left:.1f} 天（约 60 天有效、不轮换）。\n"
                               f"过期后自动刷新会失败、每日计划停止推送。\n"
                               f"请在过期前重新登录 otm.onelap.cn 取新 refresh_token，更新 config.json。")
            except ApiError as e:
                log(f"token 刷新失败：{e}（沿用旧 token，若已过期后续会 401）")
                push_alert(cfg, "⚠️ OTM token 刷新失败",
                           f"refresh_token 可能已失效（旧 access token 约 48h 后过期）。\n"
                           f"错误：{e}\n请尽快重新登录 otm.onelap.cn 并更新 config.json 的 "
                           f"token / refresh_token，否则自动流程会断档。")
        # 同步 OTM 账号 FTP/MHR/LTHR/体重（只读；失败不阻断，沿用 config 值）
        try:
            _info = get_userinfo(cfg)
            if _info:
                sync_physiology_from_otm(cfg, _info)
        except Exception as e:
            log(f"OTM 生理数据同步失败，沿用 config 值：{e}")
        pmc_start = (today - timedelta(days=45)).isoformat()
        pmc_end = (today + timedelta(days=2)).isoformat()
        cal_start = (today - timedelta(days=args.days_back)).isoformat()
        cal_end = (today + timedelta(days=args.days_ahead + 1)).isoformat()

        print("正在抓取数据……", file=sys.stderr)
        try:
            pmc_raw = get_pmc(cfg, pmc_start, pmc_end)
            rides_raw = get_ride_records(cfg, max(args.days_back, 30))
            cal_raw = get_workout_calendar(cfg, cal_start, cal_end)
        except ApiError as e:
            print(f"\n抓取失败：{e}", file=sys.stderr)
            sys.exit(1)
        training_plans = get_training_plans(cfg) if args.raw else {}

        print(f"  PMC {len(pmc_raw)} 点；骑行记录 {len(rides_raw)} 条；"
              f"课表 workout/list 已取。", file=sys.stderr)

        # 北京各区天气（Open-Meteo，免费无 key；失败不阻断主流程）
        if not args.no_weather:
            try:
                show_weekend = args.show_weekend or weather_show_weekend(today, cfg)
                print("正在抓取北京各区天气……"
                      f"{'（含周末预报）' if show_weekend else ''}", file=sys.stderr)
                weather = get_beijing_weather(today, weather_fetch_days(today, show_weekend),
                                             cfg.get("home_district"))
                if weather:
                    _ht = "、".join(f"{h['name']}={h.get('suit_tag', '?')}"
                                    for h in (weather.get("home_points") or []))
                    print(f"  天气已取：常骑点 {_ht}", file=sys.stderr)
            except Exception as e:
                log(f"天气获取失败，跳过：{e}")
                weather = None

        if args.raw:
            with open(os.path.join(HERE, f"data_{today.isoformat()}.json"), "w", encoding="utf-8") as f:
                json.dump({"pmc": pmc_raw, "rides": rides_raw,
                           "calendar": cal_raw, "training_plans": training_plans,
                           "weather": weather},
                          f, ensure_ascii=False, indent=2, default=str)
            print(f"  原始数据已写入：data_{today.isoformat()}.json", file=sys.stderr)

        rides = ride_items(rides_raw)
        planned = planned_workouts(cal_raw)
        _pmc = pmc_items(pmc_raw)  # 规整一次，供教练 prompt / TSB 投影 / 报告共用（避免重复解析、避免三处不一致）
        # ⚠️ 计划 TSS 必须走 /workout/plan（已排课 list_planned_workouts）；get_workout_calendar 走的
        # /workout/list 是 OTM 课表规划器，对本账号为空 → 计划恒 0（执行率/图表/昨天漏练都会失真）。
        # planned（list）仍用 planned_workouts(cal_raw) 给 build_report 渲染（其 w["date"] 须为 date 对象）。
        try:
            _scheduled = list_planned_workouts(cfg)
            _planned_by_d = planned_tss_by_date(_scheduled)
            # 强度命中率：计划 zone（按 IF/课名推断）vs 实际 avg_power 落区，只标明显错配
            _ftp = (cfg.get("coach_profile") or {}).get("ftp")
            _planned_zone_by_d = planned_zone_by_date(_scheduled, _ftp)
            _zone_rows = zone_alignment_rows(_planned_zone_by_d, rides, _ftp, today)
        except Exception as e:
            log(f"已排课读取失败，执行率/图表/漏练/强度判定将缺计划数据：{e}")
            _planned_by_d = {}
            _zone_rows = []
        try:  # 训练执行率：计划 TSS（已排课）vs 实际 TSS（PMC），近 days_back 天
            exec_rate = execution_rate(_planned_by_d, actual_tss_by_date(_pmc),
                                       today, days_back=args.days_back)
        except Exception as e:
            log(f"执行率计算失败，跳过：{e}")
            exec_rate = None
        # 训练图表（飞书卡片用，需 Pillow；无 Pillow/失败则跳过，不影响主流程）
        if HAVE_PIL:
            try:
                _rows = _planned_actual_rows(_planned_by_d, actual_tss_by_date(_pmc),
                                             today, days_back=min(args.days_back, 21))
                _p1 = chart_planned_vs_actual(_rows, f"计划 vs 实际 TSS（近 {len(_rows)} 天）")
                _p2 = chart_pmc_load(_pmc[-45:])
                _p3 = chart_readiness_trend(_read_readiness_history(), readiness_baseline(today), today)
                charts = [p for p in (_p1, _p2, _p3) if p]
            except Exception as e:
                log(f"图表生成失败，跳过：{e}")
                charts = []

        # —— 模式判定：休息 / 再生 / 复用 ——
        # 休息日(用户标记)→跳过AI、删当日课；降频复用→未到刷新间隔则跳过AI、保留日历既有计划。
        rest_dates = load_rest_flags()
        is_rest = today.isoformat() in rest_dates
        _last_gen = read_date_flag(PLAN_GEN_FLAG)
        _refresh = int(cfg.get("plan_refresh_days", 3) or 3)
        # 计划执行情况：昨天有计划未完成 → 今日强制重排，并把近期执行情况同步给教练
        _actual_by_d = actual_tss_by_date(_pmc)  # _planned_by_d 已用 list_planned_workouts 算好（见上）
        _exec_rows = execution_status_rows(_planned_by_d, _actual_by_d, today)
        _missed_yest = yesterday_missed(_planned_by_d, _actual_by_d, today)
        if _missed_yest:
            log("⚠️ 昨日计划未完成，今日强制重排并同步给教练。")
        # 过载硬保护：TSB 过低 或 静息心率异常偏高 → 今日强制休息（避免堆量致过训练/伤病）
        _past_pmc = [x for x in _pmc if x[0] <= today]
        _tsb = _past_pmc[-1][3] if _past_pmc else None
        _base = readiness_baseline(today)
        _rd_today = load_readiness() or {}
        _rhr_today, _rhr_base = _rd_today.get("rhr_bpm"), (_base or {}).get("rhr_bpm")
        _overreached = (_tsb is not None and _tsb < OVERREACH_TSB) or (
            isinstance(_rhr_today, (int, float)) and isinstance(_rhr_base, (int, float))
            and (_rhr_today - _rhr_base) >= OVERREACH_RHR_DELTA)
        if _overreached:
            log(f"🛑 过度疲劳（TSB={_tsb}, RHR 今日{_rhr_today}/基线{_rhr_base}）→ 今日强制休息。")
        do_regen = (not is_rest) and (not _overreached) and (args.regen or not args.auto or not _last_gen
                                      or (_last_gen and (today - _last_gen).days >= _refresh)
                                      or _missed_yest)
        if is_rest:
            log("🛌 今日为休息日（你标记），跳过 AI 计划。")
            try:
                cleanup_plan_on_date(cfg, today.isoformat())
            except Exception as e:
                log(f"删除今日计划课失败: {e}")
            coach_md = ("🛌 **今日休息**（你标记的休息日）。\n\n"
                        "已从 OTM 日历移除今日训练课。优先睡眠与恢复，明日按计划继续。")
            coach_plan = None
            args.do_import = False
            args.dry_run_import = False
        elif _overreached:
            log("🛑 过度疲劳，今日强制休息（跳过强度课）。")
            try:
                cleanup_plan_on_date(cfg, today.isoformat())
            except Exception as e:
                log(f"删除今日计划课失败: {e}")
            coach_md = ("🛑 **今日强制休息·过度疲劳**"
                        + (f"（TSB {_tsb:.0f}，偏低）" if _tsb is not None and _tsb < OVERREACH_TSB else "")
                        + (f"（静息心率 {_rhr_today} 高于基线 {_rhr_base}）"
                           if isinstance(_rhr_today, (int, float)) and isinstance(_rhr_base, (int, float))
                           and _rhr_today - _rhr_base >= OVERREACH_RHR_DELTA else "")
                        + "。\n\n已从 OTM 日历移除今日强度课。今日只做恢复（Z1/散步/拉伸）或全休，"
                          "优先睡眠，明日视状态恢复。")
            coach_plan = None
            args.do_import = False
            args.dry_run_import = False
        elif do_regen and not args.no_coach and (cfg.get("glm_api_key") or "").strip():
            # 给教练的执行情况排除今日（今日未结束，actual 多为 0，会误显"未完成"）
            _exec_rows_past = [r for r in _exec_rows if r["date"] != today.isoformat()]
            system, user = build_coach_prompt(cfg, _pmc, rides, today, args.days_ahead,
                                              start_date=start_date, exec_rows=_exec_rows_past,
                                              zone_rows=_zone_rows)
            attempts = (args.retries if args.retries and args.retries > 0 else 12) if args.auto else 1
            raw = None
            for attempt in range(attempts):
                tag = f" (尝试 {attempt+1}/{attempts})" if args.auto else ""
                print(f"正在请 {COACH_MODEL} 教练生成计划……{tag}", file=sys.stderr)
                try:
                    raw = call_llm(cfg, system, user)
                    break
                except ApiError as e:
                    raw = None
                    if attempt < attempts - 1:
                        msg = f"  教练调用失败({attempt+1}/{attempts})，3s 后重试：{e}"
                        print(msg, file=sys.stderr); log(msg); time.sleep(3)
                    else:
                        print(f"  教练调用失败，改用兜底说明：{e}", file=sys.stderr)
                        log(f"教练调用最终失败：{e}")
            if raw:
                coach_md, coach_plan = parse_coach_response(raw)
                try:
                    open(PLAN_GEN_FLAG, "w", encoding="utf-8").write(today.isoformat())
                except Exception:
                    pass
                # 计划自洽校验：按生成的 TSS 用 PMC 公式前向推演 TSB，过载则告警（不调 LLM，0 额外 token）
                try:
                    _past = [x for x in _pmc if x[0] <= today]
                    if _past and coach_plan:
                        _, _ctl, _atl, _, _ = _past[-1]
                        _proj = project_tsb(normalize_plan_days(coach_plan, today, args.days_ahead, start_date=start_date),
                                            _ctl, _atl)
                        if _proj:
                            _fin, _min_tsb = _proj[-1], min(p["tsb"] for p in _proj)
                            _line = (f"\n\n📈 **计划自洽校验**（按上方 TSS 用 PMC 公式前向推演）："
                                     f"预计 {len(_proj)} 日后 CTL {_fin['ctl']:.0f} / ATL {_fin['atl']:.0f} "
                                     f"/ TSB {_fin['tsb']:.0f}；窗口内最低 TSB {_min_tsb:.0f}。")
                            if _min_tsb < -20 or _fin["tsb"] < -25:
                                _line += "\n⚠️ **过载风险**：推演 TSB 偏低，建议下调强度日 TSS 或加恢复日。"
                            coach_md = (coach_md or "") + _line
                except Exception as e:
                    log(f"TSB 前向投影失败，跳过：{e}")
            else:
                coach_md, coach_plan = None, None
        elif do_regen:
            # 到了刷新日但没配 glm key / --no-coach：无计划
            coach_md, coach_plan = None, None
        else:
            # 复用模式：跳过 AI，保留 OTM 日历既有计划，只读今日课展示
            log(f"📋 计划复用（上次生成 {_last_gen}，未满 {_refresh} 天），跳过 AI 省 token。")
            try:
                _tp = get_plan_on_date(cfg, today.isoformat())
            except Exception as e:
                log(f"读取今日日历课失败：{e}")
                _tp = []
            if _tp:
                _det = "\n".join(f"- {first(p, 'name', 'Name', default='?')}："
                                 f"TSS {first(p, 'TSS', 'tss', default='?')}，"
                                 f"时长 {human_duration(first(p, 'duration', 'duration_s'))}" for p in _tp)
                coach_md = (f"📋 **今日训练（沿用 {_last_gen} 生成的计划，未调用 AI 省 token）**\n\n"
                            f"OTM 日历今日排定：\n{_det}")
            else:
                coach_md = f"📋 今日无训练课（沿用 {_last_gen} 起的计划周期，本日为休息/空档）。"
            coach_plan = None
            args.do_import = False
            args.dry_run_import = False

    # 目标日期驱动的阶段建议（基于 target_event/target_date；phase_autosync 控制是否写回 config）
    try:
        _phase_adv = phase_advisory(cfg, today)
    except Exception as e:
        _phase_adv = None
        if args.auto:
            log(f"阶段建议计算失败，跳过：{e}")
    if _phase_adv:
        coach_md = (coach_md + "\n\n" + _phase_adv) if coach_md else _phase_adv

    if getattr(args, "weekly", False):
        wr = build_weekly_report(_pmc, rides, today)
        print("\n" + wr)
        if not args.no_save:
            out = os.path.join(HERE, f"weekly_{today.isoformat()}.md")
            with open(out, "w", encoding="utf-8") as f:
                f.write(wr)
            print(f"\n（周报已保存：{out}）", file=sys.stderr)
        if args.push:
            push_alert(cfg, f"Onelap 周报 {today.isoformat()}", wr)
        return

    report = build_report(_pmc, rides, planned, training_plans,
                          today, args.days_back, args.days_ahead,
                          coach_md=coach_md, coach_model=COACH_MODEL,
                          weather=weather, show_weekend=show_weekend, exec_rate=exec_rate)
    print("\n" + report)

    if not args.no_save:
        out = os.path.join(HERE, f"report_{today.isoformat()}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n（报告已保存：{out}）", file=sys.stderr)

    if args.push:
        _title = f"Onelap训练报告 {today.isoformat()}"
        push_alert(cfg, _title, report, images=charts)  # 复用告警通道（带 key 校验 + 异常兜底，图表只发飞书）

    # 导入 OTM（dry-run / 测1条 / 批量）
    if args.dry_run_import or args.import_test_date or args.do_import:
        if args.sample:
            print("导入需真实登录，--sample 模式不支持。", file=sys.stderr)
        elif not coach_plan:
            print("没有可导入的结构化计划（教练未输出 JSON，或用了 --no-coach）。", file=sys.stderr)
            if args.auto:
                log("无计划可导入，结束（旧计划保留）")
        else:
            days = normalize_plan_days(coach_plan, today, args.days_ahead, start_date=start_date)
            if not days:
                print("结构化计划解析为空，无法导入。", file=sys.stderr)
            else:
                td = parse_date(args.import_test_date) if args.import_test_date else None
                dry = args.dry_run_import and not (args.do_import or args.import_test_date)
                # --auto 批量写入前：删掉旧的「（计划）」未来课（用户手建/历史课不动）→ 避免重复
                if args.auto and not dry and not td and not args.no_cleanup:
                    log(f"清理 ≥ {start_date} 且含「{PLAN_MARKER}」的旧计划课……")
                    deleted, skipped = cleanup_future_plan(cfg, start_date)
                    log(f"清理完成：删除 {deleted}，保留 {skipped}（历史/非脚本导入）")
                label = "预览(dry-run)" if dry else ("测试写入1条" if td else "批量写入")
                print(f"\n准备{label} {len(days)} 天计划中的训练日……", file=sys.stderr)
                results = import_plan(cfg, days, dry_run=dry, test_date=td)
                ok_n = _import_ok_count(results)  # 有 wid 且无 error 才算成功（回滚失败也排除）
                if args.auto:
                    log(f"导入完成：成功 {ok_n}/{len(results)} 天")  # 分母=实际尝试的训练日（休息日不计）
                if not dry:
                    p = os.path.join(HERE, f"imported_{today.isoformat()}.json")
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
                    print(f"导入结果已记录：{p}", file=sys.stderr)

    if args.auto:
        try:
            open(os.path.join(HERE, "last_auto_run.txt"), "w", encoding="utf-8").write(today.isoformat())
        except Exception:
            pass
        _AUTO_CRASH_CTX["done"] = True  # 正常完成，抑制崩溃兜底告警
        _AUTO_CRASH_CTX["cfg"] = {}      # 清掉 cfg 引用（不再需要）
        _release_auto_lock()
        log("==== 自动运行结束 ====\n")


if __name__ == "__main__":
    main()
