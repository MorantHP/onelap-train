#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_onelap.py — onelap_report.py 纯函数单测（stdlib unittest，免 pip）。

跑法：
    python -m unittest test_onelap            # 全部
    python -m unittest test_onelap.ParseTests  # 单个用例类

只覆盖纯函数（解析 / PMC 投影 / readiness 打分 / 阶段建议）。涉及 OTM/LLM 网络
的函数（create_workout / assign_plan / call_llm …）不在单测范围，需用 --sample
或真实账号做端到端验证。
"""
import sys
import os
import unittest
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import onelap_report as R  # noqa: E402


class ParseMinTests(unittest.TestCase):
    def test_various(self):
        self.assertEqual(R._parse_min("1.5h"), 90)
        self.assertEqual(R._parse_min("90m"), 90)
        self.assertEqual(R._parse_min("1h30m"), 90)
        self.assertEqual(R._parse_min("45"), 45)
        self.assertEqual(R._parse_min("0.5h"), 30)
        self.assertEqual(R._parse_min(""), 0)


class GuessZoneTests(unittest.TestCase):
    def test_zones(self):
        self.assertEqual(R._guess_zone("Z2 长距离"), "Z2")
        self.assertEqual(R._guess_zone("甜区骑行"), "甜区")
        self.assertEqual(R._guess_zone("VO2max 间歇"), "Z5")
        self.assertEqual(R._guess_zone("无氧间歇"), "Z5")
        self.assertEqual(R._guess_zone("阈值课"), "Z4")
        self.assertEqual(R._guess_zone("恢复骑"), "Z1")
        self.assertEqual(R._guess_zone("耐力"), "Z2")
        self.assertEqual(R._guess_zone(""), "")


class ZoneToPowerTests(unittest.TestCase):
    def test_known(self):
        self.assertEqual(R.zone_to_power("Z2", 0), (60, 72))
        self.assertEqual(R.zone_to_power("甜区", 0), (88, 94))
        self.assertEqual(R.zone_to_power("Z5", 0), (105, 120))

    def test_fallback_by_if(self):
        # 未知 zone + IF=0.9 → 中点 90，±6
        self.assertEqual(R.zone_to_power("?", 0.9), (84, 96))

    def test_fallback_default(self):
        self.assertEqual(R.zone_to_power("", 0), (60, 72))


class BuildIntervalsTests(unittest.TestCase):
    def _day(self, action="train", duration_min=120, zone="Z2", ifscore=0.7, name="耐力"):
        return {"action": action, "duration_min": duration_min, "zone": zone,
                "if": ifscore, "name": name}

    def test_train_day_three_segments(self):
        segs = R.build_intervals(self._day(duration_min=120))
        # 120min=7200s：热身 600 / 主体 6000 / 放松 600
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0]["name"], "热身")
        self.assertEqual(segs[1]["duration_s"], 6000)
        self.assertEqual(segs[2]["name"], "放松")

    def test_rest_day_empty(self):
        self.assertEqual(R.build_intervals(self._day(action="rest", duration_min=0)), [])

    def test_short_ride_still_has_main(self):
        segs = R.build_intervals(self._day(duration_min=20))  # 1200s
        self.assertGreaterEqual(len(segs), 1)
        # 主体段（热身/放松之间）至少 60s
        main_segs = [s for s in segs if s["name"] not in ("热身", "放松")]
        self.assertTrue(main_segs)
        self.assertGreaterEqual(main_segs[0]["duration_s"], 60)


class NormalizePlanTests(unittest.TestCase):
    def test_filters_out_of_window_and_rest_action(self):
        today = date(2026, 7, 27)
        plan = {"days": [
            {"date": "2026-07-28", "action": "train", "name": "Z2", "duration_min": 90, "tss": 60, "if": 0.7, "zone": "Z2"},
            {"date": "2026-07-20", "action": "train", "name": "过去", "duration_min": 60, "tss": 50},  # 窗口外，丢
            {"date": "2026-07-29", "action": "休息", "name": "", "duration_min": 0, "tss": 0},
        ]}
        out = R.normalize_plan_days(plan, today, days_ahead=7)  # 窗口 [07-28, 08-03]
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["date"], date(2026, 7, 28))
        self.assertEqual(out[1]["action"], "rest")
        self.assertEqual(out[1]["name"], "休息")  # 空名补默认

    def test_empty_plan(self):
        self.assertEqual(R.normalize_plan_days(None, date(2026, 7, 27), 7), [])
        self.assertEqual(R.normalize_plan_days({}, date(2026, 7, 27), 7), [])


class CoachResponseTests(unittest.TestCase):
    def test_json_block_extracted(self):
        text = ("整体判断：状态不错。\n```json\n"
                '{"summary":"ok","notes":["a"],"days":['
                '{"date":"2026-07-28","action":"train","name":"Z2","duration_min":90,'
                '"tss":60,"if":0.7,"zone":"Z2","purpose":""}]}'
                "\n```\n尾部说明")
        md, plan = R.parse_coach_response(text)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["days"]), 1)
        self.assertIn("整体判断", md)
        self.assertNotIn("```json", md)

    def test_no_json_returns_text(self):
        md, plan = R.parse_coach_response("就是一段纯文本，没有 JSON")
        self.assertIsNone(plan)
        self.assertEqual(md, "就是一段纯文本，没有 JSON")


class ReadinessScoreTests(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(R.readiness_score(None))

    def test_sleep_subjective_only(self):
        sc = R.readiness_score({"sleep_h": 8, "subjective": 8})
        self.assertIsNotNone(sc)
        self.assertGreaterEqual(sc["score"], 80)  # 绿灯

    def test_low_sleep(self):
        sc = R.readiness_score({"sleep_h": 4, "subjective": 3})
        self.assertLess(sc["score"], 50)

    def test_hrv_vs_baseline(self):
        base = {"hrv_ms": 50.0, "rhr_bpm": 55.0, "sleep_h": 7.5, "n": 7}
        sc = R.readiness_score({"sleep_h": 7.5, "hrv_ms": 55.0, "rhr_bpm": 52.0, "subjective": 8}, base)
        self.assertGreaterEqual(sc["score"], 75)  # 各项都偏好
        sc2 = R.readiness_score({"sleep_h": 7.5, "hrv_ms": 40.0, "rhr_bpm": 62.0, "subjective": 4}, base)
        self.assertLess(sc2["score"], 55)


class ProjectTsbTests(unittest.TestCase):
    def test_pushes_ctl_atl_toward_tss(self):
        days = [{"date": date(2026, 7, 28), "tss": 100}]
        proj = R.project_tsb(days, ctl0=50, atl0=30)
        self.assertEqual(len(proj), 1)
        # CTL 50→~51.2（向 100 缓慢靠拢），ATL 30→~39.3（向 100 快速靠拢）
        self.assertGreater(proj[0]["ctl"], 50)
        self.assertGreater(proj[0]["atl"], 30)
        self.assertAlmostEqual(proj[0]["tsb"], proj[0]["ctl"] - proj[0]["atl"])

    def test_high_tss_drives_tsb_negative(self):
        # 连续大 TSS：ATL 涨得比 CTL 快，TSB 转负
        days = [{"date": date(2026, 7, 28) + timedelta(i), "tss": 200} for i in range(7)]
        proj = R.project_tsb(days, ctl0=50, atl0=30)
        self.assertLess(proj[-1]["tsb"], proj[0]["tsb"])  # 一路走低

    def test_missing_inputs(self):
        self.assertEqual(R.project_tsb([], 50, 30), [])
        self.assertEqual(R.project_tsb([{"date": date(2026, 7, 28), "tss": 50}], None, 30), [])


class PhaseTests(unittest.TestCase):
    def test_suggest_phase_buckets(self):
        t = date(2026, 7, 27)
        self.assertEqual(R.suggest_phase(t - timedelta(days=7), t), "transition")  # 已过
        self.assertEqual(R.suggest_phase(t + timedelta(days=4), t), "peak")         # ≤1 周
        self.assertEqual(R.suggest_phase(t + timedelta(days=14), t), "build")       # 1-3 周
        self.assertEqual(R.suggest_phase(t + timedelta(days=70), t), "base")        # >3 周
        self.assertIsNone(R.suggest_phase(None, t))

    def test_phase_for_season(self):
        self.assertEqual(R.phase_for_season(7), "build")    # 旺季
        self.assertEqual(R.phase_for_season(11), "build")   # 11月仍户外
        self.assertEqual(R.phase_for_season(10), "build")
        self.assertEqual(R.phase_for_season(12), "transition")  # 冬休交叉训练
        self.assertEqual(R.phase_for_season(2), "transition")
        self.assertEqual(R.phase_for_season(4), "base")         # 季前
        self.assertIsNone(R.phase_for_season(None))

    def test_phase_advisory_season_fallback_no_event(self):
        t = date(2026, 7, 27)  # 7月旺季 → build
        cfg = {"coach_profile": {"phase": "recovery"}}  # 无 target_event / target_date
        out = R.phase_advisory(cfg, t)
        self.assertIsNotNone(out)
        self.assertIn("build", out)
        self.assertIn("户外季", out)
        self.assertEqual(cfg["coach_profile"]["phase"], "recovery")  # 未开 autosync，不改

    def test_phase_advisory_season_no_change_when_matching(self):
        t = date(2026, 12, 15)  # 冬休 → transition
        cfg = {"coach_profile": {"phase": "transition"}}  # 已是 transition
        self.assertIsNone(R.phase_advisory(cfg, t))

    def test_extract_event_date_from_text(self):
        cfg = {"coach_profile": {"target_event": "2026-10-01 北京周边骑游"}}
        self.assertEqual(R._extract_event_date(cfg), date(2026, 10, 1))
        cfg2 = {"coach_profile": {"target_date": "2026-09-15"}}
        self.assertEqual(R._extract_event_date(cfg2), date(2026, 9, 15))
        self.assertIsNone(R._extract_event_date({"coach_profile": {"target_event": "随便骑骑"}}))

    def test_phase_advisory_advisory_only(self):
        t = date(2026, 7, 27)
        cfg = {"coach_profile": {"phase": "recovery", "target_event": "2026-10-01 骑游"}}
        out = R.phase_advisory(cfg, t)
        self.assertIsNotNone(out)
        self.assertIn("base", out)              # 建议进 base
        self.assertNotIn("自动写回", out)        # 未开 autosync，不写
        self.assertEqual(cfg["coach_profile"]["phase"], "recovery")  # 未改

    def test_phase_advisory_autosync_writes_back(self):
        t = date(2026, 7, 27)
        cfg = {"coach_profile": {"phase": "recovery", "target_event": "2026-10-01 骑游"}, "phase_autosync": True}
        orig = R.save_config
        saved = {}
        R.save_config = lambda c: saved.update(c)  # 拦截，绝不动真实 config.json
        try:
            out = R.phase_advisory(cfg, t)
            self.assertIn("自动写回", out)
            self.assertEqual(cfg["coach_profile"]["phase"], "base")
            self.assertEqual(saved.get("coach_profile", {}).get("phase"), "base")
        finally:
            R.save_config = orig

    def test_phase_advisory_no_change(self):
        t = date(2026, 7, 27)
        cfg = {"coach_profile": {"phase": "base", "target_event": "2026-10-01 骑游"}}
        self.assertIsNone(R.phase_advisory(cfg, t))  # 已是建议值
        self.assertIsNone(R.phase_advisory({}, t))   # 空 cfg

    def test_extract_event_date_takes_last_when_multiple(self):
        # 回归：多个日期时取最后一个（真正的目标），不抓总结里的历史日期
        cfg = {"coach_profile": {"target_event": "去年2025-5 张家口赛后总结，目标2026-9 怀柔"}}
        self.assertEqual(R._extract_event_date(cfg), date(2026, 9, 1))

    def test_phase_advisory_past_event_no_negative_weeks(self):
        # 回归：目标已过不应渲染「约 -N 周」
        t = date(2026, 7, 27)
        cfg = {"coach_profile": {"phase": "base", "target_event": "2024-06-01 老比赛"}}
        out = R.phase_advisory(cfg, t)
        self.assertIsNotNone(out)
        self.assertIn("已过", out)
        self.assertNotIn("约 -", out)
        self.assertIn("transition", out)

    def test_phase_advisory_autosync_creates_missing_coach_profile(self):
        # 回归：coach_profile 缺失时 setdefault 必须真正写进 cfg（旧 `or {}` 会丢、却谎称已写回）
        t = date(2026, 7, 27)  # 旺季→build
        cfg = {"phase_autosync": True}  # 故意不带 coach_profile
        saved = {}
        orig = R.save_config
        R.save_config = lambda c: saved.update(c)
        try:
            out = R.phase_advisory(cfg, t)
        finally:
            R.save_config = orig
        self.assertIn("自动写回", out)
        self.assertEqual(cfg.get("coach_profile", {}).get("phase"), "build")  # 真写进 cfg
        self.assertEqual(saved.get("coach_profile", {}).get("phase"), "build")


class ImportOkCountTests(unittest.TestCase):
    def test_only_wid_and_no_error_counted(self):
        # 回归：assign 失败且回滚也失败 (rolled_back=False) 的条目【不能】算成功
        results = [
            {"date": "2026-07-28", "name": "Z2", "wid": 1, "response": {}},                  # 成功
            {"date": "2026-07-30", "name": "x", "error": "创建失败: HTTP 500"},               # 创建失败
            {"date": "2026-07-31", "name": "x", "error": "无 wid: null"},                     # 无 wid
            {"date": "2026-08-01", "name": "x", "wid": 5, "rolled_back": True, "error": "排期失败: 500"},   # 排期失败+回滚成功
            {"date": "2026-08-02", "name": "x", "wid": 6, "rolled_back": False, "error": "排期失败: 500"},  # 排期失败+回滚失败（旧逻辑误计为成功）
            {"date": "2026-08-03", "name": "y", "wid": 7, "response": {}},                   # 成功
        ]
        self.assertEqual(R._import_ok_count(results), 2)

    def test_empty(self):
        self.assertEqual(R._import_ok_count([]), 0)


class ExecutionRateTests(unittest.TestCase):
    def test_basic_ratio_excludes_rest_days(self):
        today = date(2026, 7, 27)
        planned = {"2026-07-26": 100, "2026-07-25": 60, "2026-07-24": 0}   # 24 号休息
        actual = {"2026-07-26": 95, "2026-07-25": 30, "2026-07-24": 0}
        r = R.execution_rate(planned, actual, today, days_back=5)
        self.assertEqual(r["train_days"], 2)              # 只两个训练日进分母
        self.assertAlmostEqual(r["planned_tss"], 160)
        self.assertAlmostEqual(r["actual_tss"], 125)
        self.assertAlmostEqual(r["rate"], 125 / 160)
        self.assertEqual(len(r["rows"]), 2)

    def test_all_rest_days_no_rate(self):
        today = date(2026, 7, 27)
        r = R.execution_rate({"2026-07-26": 0}, {"2026-07-26": 50}, today, days_back=3)
        self.assertEqual(r["train_days"], 0)
        self.assertIsNone(r["rate"])                      # 无训练日 → 不算执行率

    def test_overexecution_captured(self):
        today = date(2026, 7, 27)
        r = R.execution_rate({"2026-07-26": 60}, {"2026-07-26": 200}, today, days_back=3)
        self.assertAlmostEqual(r["rate"], 200 / 60)       # 实际远超计划，照实记
        self.assertAlmostEqual(r["rows"][0]["rate"], 200 / 60)

    def test_planned_tss_by_date_both_shapes(self):
        a = [{"date": date(2026, 7, 26), "tss": 80}]       # planned_workouts 输出
        b = [{"date": "2026-07-26", "TSS": 40}]            # list_planned_workouts 原始
        self.assertEqual(R.planned_tss_by_date(a), {"2026-07-26": 80})
        self.assertEqual(R.planned_tss_by_date(b), {"2026-07-26": 40})

    def test_actual_tss_from_pmc_skips_none(self):
        pmc = [(date(2026, 7, 26), 50, 30, 20, 88),
               (date(2026, 7, 25), 50, 30, 20, None)]
        self.assertEqual(R.actual_tss_by_date(pmc), {"2026-07-26": 88})


class IntervalSplitTests(unittest.TestCase):
    def _day(self, zone, duration_min=60, name="课目", action="train"):
        return {"action": action, "duration_min": duration_min, "zone": zone,
                "if": 1.0, "name": name}

    def test_z5_splits_into_on_off_with_cap(self):
        segs = R.build_intervals(self._day("Z5", 60, name="VO2"))
        names = [s["name"] for s in segs]
        self.assertIn("热身", names)
        self.assertIn("放松", names)
        self.assertTrue(any("冲" in n for n in names), f"缺 on 冲段: {names}")
        self.assertTrue(any("恢复" in n for n in names), f"缺恢复段: {names}")
        on_total = sum(s["duration_s"] for s in segs if "冲" in s["name"])
        self.assertLessEqual(on_total, 18 * 60 + 1)        # 高强度封顶 18min
        on_seg = next(s for s in segs if "冲" in s["name"])
        self.assertEqual((on_seg["lo"], on_seg["hi"]), (105, 120))   # Z5 功率
        rec_seg = next(s for s in segs if "恢复" in s["name"])
        self.assertEqual((rec_seg["lo"], rec_seg["hi"]), (50, 60))   # 恢复 Z1
        # 总时长守恒（热身+主体+放松 ≈ 整课）
        self.assertAlmostEqual(sum(s["duration_s"] for s in segs), 60 * 60, delta=2)

    def test_z6_shorter_on_higher_cap_low(self):
        segs = R.build_intervals(self._day("Z6", 50, name="无氧"))
        on_segs = [s for s in segs if "冲" in s["name"]]
        self.assertTrue(on_segs)
        self.assertEqual(on_segs[0]["duration_s"], 60)     # Z6: 1min on
        self.assertLessEqual(sum(s["duration_s"] for s in on_segs), 8 * 60 + 1)

    def test_z4_stays_continuous(self):
        segs = R.build_intervals(self._day("Z4", 60, name="阈值"))
        main_segs = [s for s in segs if s["name"] not in ("热身", "放松")]
        self.assertEqual(len(main_segs), 1)                # 阈值=连续块，不拆
        self.assertNotIn("冲", main_segs[0]["name"])

    def test_z2_continuous(self):
        segs = R.build_intervals(self._day("Z2", 120, name="耐力"))
        main_segs = [s for s in segs if s["name"] not in ("热身", "放松")]
        self.assertEqual(len(main_segs), 1)
        self.assertEqual(main_segs[0]["name"], "耐力")


class PlannedActualRowsTests(unittest.TestCase):
    def test_only_active_days_ascending(self):
        today = date(2026, 7, 27)
        pbd = {"2026-07-25": 60, "2026-07-26": 0, "2026-07-27": 80}
        abd = {"2026-07-25": 50, "2026-07-26": 40, "2026-07-27": 0}
        rows = R._planned_actual_rows(pbd, abd, today, days_back=5)
        # 25(有计划)、26(有实际)、27(有计划) 都保留；24 无活动剔除；升序
        self.assertEqual([r[0] for r in rows], ["2026-07-25", "2026-07-26", "2026-07-27"])
        self.assertEqual(rows[0], ("2026-07-25", 60.0, 50.0))

    def test_no_pil_returns_none(self):
        # 有 HAVE_PIL 时正常返回 bytes；只验证非空 + PNG 头
        if not R.HAVE_PIL:
            self.assertIsNone(R.chart_planned_vs_actual([("2026-07-25", 60, 50)]))
        else:
            png = R.chart_planned_vs_actual([("2026-07-25", 60, 50), ("2026-07-26", 70, 30)])
            self.assertTrue(png and png[:8] == b"\x89PNG\r\n\x1a\n")


class ReadinessBaselineTests(unittest.TestCase):
    def _hist(self, pairs, key="hrv_ms"):
        return [{"date": (date(2026, 7, 27) - timedelta(days=o)).isoformat(), key: v}
                for o, v in pairs]

    def test_median_ignores_outlier(self):
        # 7 天含一个离群 200，中位数应忽略它
        recs = self._hist([(1, 50), (2, 52), (3, 48), (4, 200), (5, 51), (6, 49), (7, 53)])
        orig = R._read_readiness_history
        R._read_readiness_history = lambda: recs
        try:
            b = R.readiness_baseline(date(2026, 7, 27), window=14, min_n=6)
            self.assertIsNotNone(b)
            self.assertAlmostEqual(b["hrv_ms"], 51)   # [48,49,50,51,52,53,200] 中位数 51
        finally:
            R._read_readiness_history = orig

    def test_insufficient_returns_none(self):
        recs = self._hist([(1, 50), (2, 52)])          # 仅 2 天 < min_n
        orig = R._read_readiness_history
        R._read_readiness_history = lambda: recs
        try:
            self.assertIsNone(R.readiness_baseline(date(2026, 7, 27), window=14, min_n=6))
        finally:
            R._read_readiness_history = orig


class ReadinessTrendChartTests(unittest.TestCase):
    def test_renders_png_or_none(self):
        recs = [{"date": (date(2026, 7, 27) - timedelta(days=o)).isoformat(),
                 "hrv_ms": 40 + o, "rhr_bpm": 52 - 0.1 * o, "sleep_h": 6 + 0.1 * o}
                for o in range(1, 15)]
        bl = {"hrv_ms": 45.0, "rhr_bpm": 53.0, "sleep_h": 6.5, "n": 14}
        if not R.HAVE_PIL:
            self.assertIsNone(R.chart_readiness_trend(recs, bl, date(2026, 7, 27)))
        else:
            png = R.chart_readiness_trend(recs, bl, date(2026, 7, 27))
            self.assertTrue(png and png[:8] == b"\x89PNG\r\n\x1a\n")

    def test_too_few_returns_none(self):
        self.assertIsNone(R.chart_readiness_trend(
            [{"date": "2026-07-26", "hrv_ms": 40}], None, date(2026, 7, 27)))


class RefreshTokenDaysLeftTests(unittest.TestCase):
    def _make(self, exp, segs):
        # segs=2 → OTM 真实格式 payload.signature；segs=3 → 标准 JWT header.payload.signature
        import base64, json
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode("ascii")).rstrip(b"=").decode("ascii")
        return f"{payload}.sig" if segs == 2 else f"hdr.{payload}.sig"

    def test_two_segment_otm_format(self):
        import time
        now = int(time.time())
        d = R.refresh_token_days_left({"refresh_token": self._make(now + 10 * 86400, 2)})
        self.assertIsNotNone(d)
        self.assertTrue(9.9 < d < 10.1, d)

    def test_three_segment_jwt(self):
        import time
        now = int(time.time())
        d = R.refresh_token_days_left({"refresh_token": self._make(now + 5 * 86400, 3)})
        self.assertIsNotNone(d)
        self.assertTrue(4.9 < d < 5.1, d)

    def test_non_jwt_returns_none(self):
        self.assertIsNone(R.refresh_token_days_left({"refresh_token": "not-a-jwt"}))
        self.assertIsNone(R.refresh_token_days_left({}))
        self.assertIsNone(R.refresh_token_days_left({"refresh_token": ""}))


class ExecutionStatusTests(unittest.TestCase):
    def test_rows_and_done_flags(self):
        today = date(2026, 7, 29)
        planned = {"2026-07-28": 100, "2026-07-29": 50}   # 昨天100 今天50
        actual = {"2026-07-28": 80, "2026-07-29": 0}      # 昨天80%(✅) 今天0(❌)
        rows = R.execution_status_rows(planned, actual, today, days_back=1)  # 含 07-28, 07-29
        by = {r["date"]: r for r in rows}
        self.assertTrue(by["2026-07-28"]["done"])     # 80 ≥ 70
        self.assertFalse(by["2026-07-29"]["done"])    # 0 < 35
        rows2 = R.execution_status_rows({}, {}, today, days_back=1)  # 无计划日
        self.assertTrue(all(r["done"] is None for r in rows2))

    def test_yesterday_missed(self):
        today = date(2026, 7, 29)
        self.assertTrue(R.yesterday_missed({"2026-07-28": 100}, {"2026-07-28": 20}, today))   # 实质性课(100) 20% 漏练 → 重排
        self.assertFalse(R.yesterday_missed({"2026-07-28": 100}, {"2026-07-28": 75}, today))  # 75% 完成
        self.assertFalse(R.yesterday_missed({"2026-07-28": 15}, {"2026-07-28": 0}, today))    # 轻恢复日(TSS15) 漏练 → 不重排
        self.assertFalse(R.yesterday_missed({}, {"2026-07-28": 50}, today))                   # 昨天无计划


if __name__ == "__main__":
    unittest.main(verbosity=2)
