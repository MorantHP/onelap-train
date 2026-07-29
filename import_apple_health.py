#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_apple_health.py — 从 Apple Health 导出（export.xml / 导出.xml）回填真实历史到
readiness_history.jsonl，让 readiness 基线立刻建立在真实数据上（替代测试/假数据）。

逐行扫（1GB 也能跑、低内存），只取 HRV(SDNN)/静息心率/睡眠(Asleep) 三类，按天聚合：
  - HRV、静息心率：当天多条取中位数；
  - 睡眠：当天所有 Asleep 段时长求和，归到【醒来日】(endDate 的日期)=那天早晨的"昨晚睡眠"。

用法（项目目录下）：
  python3 import_apple_health.py "E:\\path\\导出.xml"            # 覆盖写 readiness_history.jsonl
  python3 import_apple_health.py "E:\\path\\导出.xml" --days 90  # 只取最近 90 天（默认）
  python3 import_apple_health.py "E:\\path\\导出.xml" --merge    # 与现有历史合并（同日以导出为准）
纯标准库。
"""
import argparse
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import onelap_report as R  # 复用 run_tss（跑步 TSS）
HISTORY = os.path.join(HERE, "readiness_history.jsonl")
RUN_HISTORY_OUT = os.path.join(HERE, "run_history.jsonl")

RX_TYPE = re.compile(r'type="(HK[^"]*)"')
RX_START = re.compile(r'startDate="(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?)')
RX_END = re.compile(r'endDate="(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?)')
RX_VAL = re.compile(r'value="([^"]*)"')
RX_DUR = re.compile(r'duration="([^"]*)"')
RX_DURUNIT = re.compile(r'durationUnit="([^"]*)"')
RX_DIST = re.compile(r'totalDistance="([^"]*)"')


def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if not vals:
        return None
    return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 1)


def _collect_run(line, cutoff, runs):
    """从一条 <Workout HKWorkoutActivityTypeRunning ...> 行提取时长/距离/日期，聚合进 runs（按天累加）。"""
    s = RX_START.search(line)
    if not s:
        return
    day = s.group(1)[:10]
    if day < cutoff:
        return
    dur = RX_DUR.search(line)
    unit = RX_DURUNIT.search(line)
    duration_s = None
    if dur:
        try:
            d = float(dur.group(1))
            u = (unit.group(1) if unit else "min").lower()
            duration_s = d * 3600 if u.startswith("h") else d * 60  # 默认 min
        except ValueError:
            pass
    dist = RX_DIST.search(line)
    distance_km = None
    if dist:
        try:
            distance_km = float(dist.group(1))
        except ValueError:
            pass
    tss, _est = R.run_tss(duration_s)  # 暂无心率 → 估算
    runs[day]["duration_s"] += duration_s or 0
    if distance_km:
        runs[day]["distance_km"] += distance_km
    runs[day]["tss"] += tss


def parse(path, days):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    today = date.today().isoformat()
    hrv = defaultdict(list)        # date(ISO) -> [hrv...]
    rhr = defaultdict(list)
    sleep = defaultdict(float)     # wake date -> hours
    runs = defaultdict(lambda: {"duration_s": 0.0, "distance_km": 0.0, "tss": 0.0})
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "<Workout " in line and "HKWorkoutActivityTypeRunning" in line:
                _collect_run(line, cutoff, runs)
                n += 1
                if n % 200000 == 0:
                    print(f"  已扫 {n} 行…", file=sys.stderr)
                continue
            if "Record" not in line:
                continue
            typ = RX_TYPE.search(line)
            if not typ:
                continue
            t = typ.group(1)
            if t.endswith("HeartRateVariabilitySDNN"):
                d = RX_START.search(line)
                v = RX_VAL.search(line)
                day = d.group(1)[:10] if d else None
                if day and day >= cutoff and v:
                    try:
                        hrv[day].append(float(v.group(1)))
                    except ValueError:
                        pass
            elif t.endswith("RestingHeartRate"):
                d = RX_START.search(line)
                v = RX_VAL.search(line)
                day = d.group(1)[:10] if d else None
                if day and day >= cutoff and v:
                    try:
                        rhr[day].append(float(v.group(1)))
                    except ValueError:
                        pass
            elif t.endswith("SleepAnalysis"):
                v = RX_VAL.search(line)
                val = v.group(1) if v else ""
                if "Asleep" in val and "Awake" not in val:  # AsleepCore/Deep/REM/Unspecified
                    s = RX_START.search(line)
                    e = RX_END.search(line)
                    if s and e:
                        try:
                            sf = "%Y-%m-%d %H:%M:%S" if len(s.group(1)) > 10 else "%Y-%m-%d"
                            ef = "%Y-%m-%d %H:%M:%S" if len(e.group(1)) > 10 else "%Y-%m-%d"
                            sd = datetime.strptime(s.group(1), sf)
                            ed = datetime.strptime(e.group(1), ef)
                            wake = e.group(1)[:10]
                            if wake >= cutoff:
                                sleep[wake] += (ed - sd).total_seconds() / 3600.0
                        except ValueError:
                            pass
            n += 1
            if n % 200000 == 0:
                print(f"  已扫 {n} 行…", file=sys.stderr)
    days_set = set(hrv) | set(rhr) | set(sleep)
    out = []
    for d in sorted(days_set):
        if d >= today:
            continue  # 只回填今天之前
        rec = {"date": d}
        if hrv[d]:
            rec["hrv_ms"] = _median(hrv[d])
        if rhr[d]:
            rec["rhr_bpm"] = _median(rhr[d])
        if sleep[d]:
            rec["sleep_h"] = round(sleep[d], 2)
        out.append(rec)
    run_list = []
    for d in sorted(runs):
        if d >= today:
            continue
        v = runs[d]
        run_list.append({"date": d, "duration_s": round(v["duration_s"]),
                         "distance_km": round(v["distance_km"], 2) if v["distance_km"] else None,
                         "tss": round(v["tss"], 1), "estimated": True})
    return out, {"hrv": len(hrv), "rhr": len(rhr), "sleep": len(sleep), "runs": len(run_list), "lines": n}, run_list


def main():
    ap = argparse.ArgumentParser(description="Apple Health 导出 → readiness_history.jsonl 回填真实基线")
    ap.add_argument("xml", help="Apple Health export.xml / 导出.xml 路径")
    ap.add_argument("--days", type=int, default=90, help="只取最近 N 天（默认 90）")
    ap.add_argument("--merge", action="store_true", help="与现有历史合并（同日以导出为准）；默认整体覆盖")
    ap.add_argument("--runs-only", action="store_true", help="只解析跑步写 run_history.jsonl，不动 readiness_history")
    args = ap.parse_args()

    if not os.path.exists(args.xml):
        sys.exit(f"找不到文件：{args.xml}")
    print(f"解析 {args.xml}（最近 {args.days} 天，1GB 约需 1-2 分钟）…", file=sys.stderr)
    recs, stat, run_list = parse(args.xml, args.days)
    print(f"完成：HRV {stat['hrv']} / 静息心率 {stat['rhr']} / 睡眠 {stat['sleep']} / 跑步 {stat['runs']} 天（扫 {stat['lines']} 行）",
          file=sys.stderr)

    # 跑步历史（交叉训练叠加层，写 run_history.jsonl）
    if run_list:
        with open(RUN_HISTORY_OUT, "w", encoding="utf-8") as f:
            for r in run_list:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"✅ 已写入 {len(run_list)} 条跑步历史 → {RUN_HISTORY_OUT}", file=sys.stderr)
        print("最近 5 条跑步：", file=sys.stderr)
        for r in run_list[-5:]:
            print("  " + json.dumps(r, ensure_ascii=False))

    if not recs:
        if not run_list:
            sys.exit("该窗口内没解析到任何 readiness/跑步数据，未写入。试试加大 --days 或确认文件。")
        sys.exit(0)  # 仅有跑步、无 readiness，正常退出

    if args.runs_only:
        sys.exit(0)  # 只写跑步，不动 readiness_history

    if os.path.exists(HISTORY) and not args.merge:
        bak = HISTORY + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(HISTORY, bak)
            print(f"已备份旧历史 → {bak}（全是测试数据，可留可删）", file=sys.stderr)
    if args.merge:
        existing = {}
        try:
            with open(HISTORY, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            r = json.loads(line)
                            existing[r["date"]] = r
                        except Exception:
                            pass
        except FileNotFoundError:
            pass
        for r in recs:
            existing[r["date"]] = r  # 导出覆盖同日
        recs = sorted(existing.values(), key=lambda x: x.get("date", ""))

    with open(HISTORY, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"✅ 已写入 {len(recs)} 条真实历史 → {HISTORY}", file=sys.stderr)
    print("最近 7 条预览：", file=sys.stderr)
    for r in recs[-7:]:
        print("  " + json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
