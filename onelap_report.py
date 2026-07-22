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
import json
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
        is_cert = isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE" in str(reason)
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

# AI 教练固定使用 glm-5.2（不可切换）
COACH_MODEL = "glm-5.2"


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
# glm-5.2 教练：把历史训练数据交给 LLM（角色=自行车教练）生成未来计划
# ---------------------------------------------------------------------------
SYSTEM_COACH = (
    "你是一位资深自行车教练兼运动营养师，精通功率训练、PMC 模型与周期化："
    "CTL≈长期体能(约42天加权)，ATL≈短期疲劳(约7天加权)，TSB=CTL−ATL（>5清爽、−10~5中性、<−10疲劳、<−30严重疲劳）。"
    "你会综合车手的【个人档案与目标、当日 readiness(睡眠/HRV/静息心率)、可训练时间窗、季节与阶段、近期训练负荷】，"
    "制定科学且【能在真实时间窗内执行】的计划，并给配套饮食建议。"
    "铁律：疲劳高或 readiness 差时优先恢复与睡眠；强度日与恢复日交替；循序渐进绝不堆量过载（别让 TSB 长期 <−20）。"
    "用中文回答，语气专业、简洁、可直接照做。"
)


def season_hint(today):
    """按北京气候给季节阶段提示（冬训室内 / 转户外 / 赛季 / 过渡）。"""
    m = today.month
    if m in (11, 12, 1, 2):
        return "冬训·室内骑行台（北京户外不宜）"
    if m in (3, 4):
        return "转户外 + 巅峰期"
    if m in (5, 6, 7, 8, 9):
        return "赛季·户外"
    return "赛季末 + 过渡"


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


def readiness_baseline(today=None, window=7):
    """返回近期（默认最近 window 天，含今日）HRV/静息心率/睡眠 的滚动均值作为个人基线。样本不足返回 None。"""
    recs = _read_readiness_history()
    if today is not None:
        iso = today.isoformat() if hasattr(today, "isoformat") else str(today)
        recs = [r for r in recs if r.get("date") and r["date"] < iso]  # 基线只取【今天之前】的常态
    recs = recs[-window:]

    def avg(key):
        vals = [r.get(key) for r in recs if isinstance(r.get(key), (int, float))]
        return sum(vals) / len(vals) if vals else None

    hrv, rhr, slp = avg("hrv_ms"), avg("rhr_bpm"), avg("sleep_h")
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


def build_coach_prompt(cfg, pmc, rides, today, days_ahead, start_date=None):
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
    u.append(f"- 体重 {prof.get('weight_kg','?')}kg，FTP {prof.get('ftp','?')}W，目标：{prof.get('goal','提高FTP')}")
    u.append(f"- 所在地 {prof.get('location','?')}；季节阶段：{season_hint(today)}；训练阶段：{prof.get('phase','base')}")
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

    u.append("**【营养目标：减脂增肌】**")
    u.append(f"- 蛋白 {int(w*1.8)}-{int(w*2.0)}g/天；强度日碳水 5-7g/kg、休息日 3-4g/kg；小幅热量缺口 −300~−400kcal；"
             f"长骑/强度课途中补碳水 30-60g/h，课后补碳水+蛋白。")
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
    }).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    last = None
    for attempt in range(2):  # 网络/SSL 抖动时重试 1 次
        try:
            with _urlopen(req, 90) as r:
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


def build_report(pmc, rides, planned, training_plans, today, days_back, days_ahead, coach_md=None, coach_model=""):
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

    # ---- 二、未来训练安排（glm-5.2 教练生成；OTM 课表为空时用此） ----
    L.append(f"## 二、未来 {days_ahead} 天的训练安排\n")
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


def build_intervals(day):
    """计划日 → 间歇段列表 [{name, duration_s, lo, hi}]。休息日返回空。"""
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
    segs.append({"name": day["name"] or "主体", "duration_s": main, "lo": lo, "hi": hi})
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
        try:
            res = create_workout(cfg, build_workout_payload(day))
            wid = (res or {}).get("wid") or (res or {}).get("id") or (res or {}).get("_id")
            if wid:
                assign_plan(cfg, wid, day["date"].isoformat())
            print(f"  [OK]  {day['date']} {day['name']} → wid={wid}", file=sys.stderr)
            results.append({"date": day["date"].isoformat(), "name": day["name"],
                            "wid": wid, "response": res})
        except ApiError as e:
            print(f"  [FAIL] {day['date']} {day['name']}: {e}", file=sys.stderr)
            results.append({"date": day["date"].isoformat(), "name": day["name"], "error": str(e)})
    return results


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
    ap.add_argument("--days-ahead", type=int, default=14, help="展望几天（默认 14）")
    ap.add_argument("--raw", action="store_true", help="原始 JSON 落盘（调试用）")
    ap.add_argument("--sample", action="store_true", help="用假数据预览，不联网")
    ap.add_argument("--no-coach", action="store_true", help="不调用 glm-5.2 教练生成计划")
    ap.add_argument("--no-save", action="store_true", help="不写报告文件")
    ap.add_argument("--push", action="store_true", help="把报告推送到微信（Server酱，需配 serverchan_key）")
    ap.add_argument("--dry-run-import", action="store_true", help="预览把计划导入 OTM 的训练课（不写入）")
    ap.add_argument("--import-test-date", metavar="YYYY-MM-DD", help="只在该日期创建 1 条训练课（实测验证）")
    ap.add_argument("--import", dest="do_import", action="store_true", help="把计划批量写入 OTM 日历")
    ap.add_argument("--auto", action="store_true",
                    help="每日自动模式（cron 用）：刷新token→抓数据→生成计划→删旧计划→导入→推送→写日志")
    ap.add_argument("--start-today", action="store_true", help="计划从今天起（默认从明天起）；--auto 默认开启")
    ap.add_argument("--no-cleanup", action="store_true", help="--auto 时导入前不删除旧的「（计划）」课")
    ap.add_argument("--retries", type=int, default=12, help="--auto 时教练 LLM 调用失败的重试次数（默认 12）")
    args = ap.parse_args()

    today = date.today()
    cfg = {}
    coach_md = None
    coach_plan = None

    # --auto：一站式每日流程（刷新 token / 推送 / 导入 / 从今天起 / 写日志）
    if args.auto:
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
    else:
        cfg = load_config()
        append_readiness_history(load_readiness())  # 记今日 readiness 进历史，供算个人基线
        if args.auto:
            try:
                cfg, refreshed = refresh_access_token(cfg)
                log(f"token {'已刷新并写回 config' if refreshed else '未配 refresh_token，沿用静态 token'}")
            except ApiError as e:
                log(f"token 刷新失败：{e}（沿用旧 token，若已过期后续会 401）")
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

        if args.raw:
            with open(os.path.join(HERE, f"data_{today.isoformat()}.json"), "w", encoding="utf-8") as f:
                json.dump({"pmc": pmc_raw, "rides": rides_raw,
                           "calendar": cal_raw, "training_plans": training_plans},
                          f, ensure_ascii=False, indent=2)
            print(f"  原始数据已写入：data_{today.isoformat()}.json", file=sys.stderr)

        rides = ride_items(rides_raw)
        planned = planned_workouts(cal_raw)

        # glm-5.2 教练生成计划（config.json 里填了 glm_api_key 就启用）
        if not args.no_coach and (cfg.get("glm_api_key") or "").strip():
            system, user = build_coach_prompt(cfg, pmc_items(pmc_raw), rides, today, args.days_ahead,
                                              start_date=start_date)
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
            else:
                coach_md, coach_plan = None, None

    report = build_report(pmc_items(pmc_raw), rides, planned, training_plans,
                          today, args.days_back, args.days_ahead,
                          coach_md=coach_md, coach_model=COACH_MODEL)
    print("\n" + report)

    if not args.no_save:
        out = os.path.join(HERE, f"report_{today.isoformat()}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n（报告已保存：{out}）", file=sys.stderr)

    if args.push:
        push_serverchan(cfg, f"Onelap训练报告 {today.isoformat()}", report)

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
                ok_n = len([r for r in results if r.get("wid")])
                if args.auto:
                    log(f"导入完成：成功 {ok_n}/{len(days)} 天")
                if not dry:
                    p = os.path.join(HERE, f"imported_{today.isoformat()}.json")
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
                    print(f"导入结果已记录：{p}", file=sys.stderr)

    if args.auto:
        log("==== 自动运行结束 ====\n")


if __name__ == "__main__":
    main()
