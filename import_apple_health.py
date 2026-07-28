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
HISTORY = os.path.join(HERE, "readiness_history.jsonl")

RX_TYPE = re.compile(r'type="(HK[^"]*)"')
RX_START = re.compile(r'startDate="(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?)')
RX_END = re.compile(r'endDate="(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?)')
RX_VAL = re.compile(r'value="([^"]*)"')


def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if not vals:
        return None
    return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 1)


def parse(path, days):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    today = date.today().isoformat()
    hrv = defaultdict(list)        # date(ISO) -> [hrv...]
    rhr = defaultdict(list)
    sleep = defaultdict(float)     # wake date -> hours
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
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
    return out, {"hrv": len(hrv), "rhr": len(rhr), "sleep": len(sleep), "lines": n}


def main():
    ap = argparse.ArgumentParser(description="Apple Health 导出 → readiness_history.jsonl 回填真实基线")
    ap.add_argument("xml", help="Apple Health export.xml / 导出.xml 路径")
    ap.add_argument("--days", type=int, default=90, help="只取最近 N 天（默认 90）")
    ap.add_argument("--merge", action="store_true", help="与现有历史合并（同日以导出为准）；默认整体覆盖")
    args = ap.parse_args()

    if not os.path.exists(args.xml):
        sys.exit(f"找不到文件：{args.xml}")
    print(f"解析 {args.xml}（最近 {args.days} 天，1GB 约需 1-2 分钟）…", file=sys.stderr)
    recs, stat = parse(args.xml, args.days)
    print(f"完成：HRV {stat['hrv']} 天 / 静息心率 {stat['rhr']} 天 / 睡眠 {stat['sleep']} 天（扫 {stat['lines']} 行）",
          file=sys.stderr)
    if not recs:
        sys.exit("该窗口内没解析到任何 readiness 数据，未写入。试试加大 --days 或确认文件。")

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
