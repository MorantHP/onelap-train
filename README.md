# Onelap OTM 每日训练计划 · 自动化教练

抓取 [Onelap OTM](https://otm.onelap.cn)（智能骑行台/训练平台）的训练数据，让 **glm-5.2（扮演自行车教练）** 基于你的 PMC（体能 CTL / 疲劳 ATL / 状态 TSB）和近期真实骑行，**每天自动重排未来 14 天的训练计划**，写入 OTM 日历，并把报告推送到微信。

纯 Python 标准库实现，**无需 pip install**，Python 3.7+ 即可，Linux / Windows 通用。

> ⚠️ 本项目为个人训练自动化工具，非 Onelap 官方产品。接口与字段均从前端逆向得到，仅供个人账号使用，请遵守 Onelap 服务条款。

---

## 它做什么

每天定时（如凌晨 4 点）跑一遍 `--auto`，自动完成：

1. **刷新 token**（用 refresh_token 续期 access token，写回 config）。
2. **抓取最新数据**：PMC 每日点 + 近 30 天骑行记录（TSS / 距离 / 时长 / 功率 / 心率）。
3. **glm-5.2 教练生成计划**：基于当前疲劳状态生成从今天起 14 天的逐日计划（含 IF / TSS / 功率分区 Z1–Z5 / 甜区），失败自动重试。
4. **删旧计划**：清理日历上之前导入的、带「（计划）」后缀的未来课（**只动脚本自己造的，你手建的不动**）。
5. **导入新计划**：把每天翻译成间歇训练课（热身 + 主体 + 放松，按 %FTP）写入 OTM 日历。
6. **推送微信**：经 Server酱 把完整报告推到你手机。
7. **写日志**：全程带时间戳落到 `logs/auto.log`。

---

## 快速开始

```bash
git clone <本仓库地址>
cd onelap-train
cp config.example.json config.json   # Windows: copy config.example.json config.json
# 编辑 config.json，填入 token / glm_api_key / serverchan_key（见下）
python onelap_report.py --auto        # 跑一次看效果
```

不想先配 token，可以先看报告长什么样：
```bash
python onelap_report.py --sample      # 用假数据预览，不联网
```

---

## 认证与密钥

编辑 `config.json`（已在 `.gitignore`，不会被提交）：

| 字段 | 必填 | 怎么拿 |
|---|---|---|
| `token` | ✅ | otm.onelap.cn 登录 → F12 → Application → Local Storage → 复制 `token` 的值（约 48h 过期） |
| `refresh_token` | 建议（自动续期用） | otm.onelap.cn **退出后重新登录**一次 → 同上位置复制 `refresh_token`（约 60 天有效） |
| `glm_api_key` | 生成计划必填 | [open.bigmodel.cn](https://open.bigmodel.cn) 控制台 → API Keys。**账户需有 glm-5.2 额度**（glm-4-flash 免费但算出的训练强度不准，不建议） |
| `serverchan_key` | 推送微信必填 | [sct.ftqq.com](https://sct.ftqq.com) 微信登录 → SendKey |

> 没填 `refresh_token` 也能用，只是 access token 约 48h 后过期需手动更新一次。填了就能长期无人值守。
> 教练只读取 PMC + 骑行摘要，不发送你的 token 或其他隐私。

---

## 私人教练档案 & 每日 readiness（可选，强烈推荐）

在 `config.json` 填 `coach_profile`，AI 教练就会按**你的体重 / FTP / 目标 / 训练时间窗 / 季节阶段**排课，并给减脂增肌的饮食建议：

```json
"coach_profile": {
  "weight_kg": 79, "ftp": 244, "goal": "提高FTP+爬坡+速度；同时减脂增肌",
  "location": "beijing", "phase": "base",
  "schedule": {
    "weekday_am": "5:30-7:30 可骑行，须 8:00 前到家",
    "weekday_pm": "17:30 之后可骑行/力量/跑步",
    "weekend": "可安排长骑",
    "winter_note": "冬天无法户外，改室内骑行台"
  },
  "devices": ["apple_watch"]
}
```

> `phase` 取 `recovery / base / build / peak / in_season / transition`，随训练阶段手动调整。

**每日 readiness**（睡眠 / HRV / 静息心率 / 主观）写进 `readiness.json` 后，教练会据此**当天升降强度**：综合打分 0-100（绿≥80 / 黄 65-79 / 橙 50-64 / 红<50），其中 HRV、静息心率相对**你的个人滚动基线**评判（基线从历史自动累积，约 1 周后开始生效）。

readiness 数据怎么自动来？用 **iPhone「快捷指令」每天早晨把 Apple Watch 健康数据 POST 到服务器**——纯标准库接收端 `readiness_server.py`（带 token 鉴权 + 字段校验，可选「数据到达后自动重跑 `--auto`」）。完整搭建见 **[DEPLOY.md「5C. Apple Watch 健康数据接入」](DEPLOY.md#5c-可选-apple-watch-健康数据--readinessjson)**。

---

## 每日自动运行

详细部署见 **[DEPLOY.md](DEPLOY.md)**（含排错表）。两平台要点：

- **Linux（cron）**：
  ```cron
  0 4 * * * cd /opt/onelap-train && mkdir -p logs && /usr/bin/python3 onelap_report.py --auto >> logs/auto.log 2>&1
  ```
  （服务器时区设为 `Asia/Shanghai` 即北京 4 点）

- **Windows（任务计划程序）**：用仓库里的 `run_auto.bat`，建一个每天 04:00 触发的任务调用它。详见 DEPLOY.md「Windows 部署」章节。

> 部分服务器网络有 TLS 拦截，脚本已内置 SSL 容错（证书校验失败时自动回退并警告一次）。

---

## 命令参数

| 参数 | 作用 |
|---|---|
| `--auto` | **每日自动模式**：刷新token→生成计划→删旧→导入→推送→写日志（cron/任务计划用这个） |
| `--retries N` | `--auto` 时教练 LLM 失败的重试次数（默认 12） |
| `--start-today` | 计划从今天起（默认从明天起；`--auto` 自动开启） |
| `--no-cleanup` | `--auto` 时导入前不删除旧的「（计划）」课 |
| `--days-back 14` | 回看几天（默认 14） |
| `--days-ahead 14` | 展望几天（默认 14） |
| `--push` | 把报告推送到微信 |
| `--import` | 把计划批量写入 OTM 日历 |
| `--dry-run-import` | 预览导入的训练课（不写入，安全） |
| `--import-test-date YYYY-MM-DD` | 只在该日期创建 1 条课做验证 |
| `--sample` | 用假数据预览，不联网 |
| `--no-coach` | 不调教练（只看数据，不生成计划） |
| `--raw` | 原始 JSON 落盘（调试字段用） |
| `--no-save` | 只打印到屏幕，不生成 .md |

---

## 输出指标说明

- **CTL**：长期体能负荷（≈体能水平），越高越强。
- **ATL**：短期疲劳负荷，越高越累。
- **TSB = CTL − ATL**：当前状态。>5 清爽（适合冲强度），−10~5 中性，<−10 疲劳，<−30 严重疲劳。
- **TSS**：单次训练压力分数。轻松骑 ~30，阈值课 ~80–100，比赛 200+。
- **IF**：强度系数（相对 FTP，>1.0 超过 FTP）。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| `认证失败（HTTP 401）` / `token 刷新失败` | refresh_token 过期或未填。重新登录 OTM，更新 `token`（和 `refresh_token`）。 |
| glm 报 `1113 余额不足` | glm-5.2 需要额度。智谱控制台充值，或确认 key 对应账户开了 glm-5.2 资源包（glm-4-flash 免费但强度不准）。 |
| `SSL: CERTIFICATE_VERIFY_FAILED` | 服务器有 TLS 拦截。脚本已自动回退到不校验证书模式继续跑；想恢复校验可更新 `ca-certificates`。 |
| glm 频繁超时 | 接口抖动，`--auto` 已自动重试 12 次；可换时段重跑。 |
| 日历没更新 | 看 `logs/auto.log` 的 `导入完成：成功 N/14 天`；失败看 `[FAIL]` 行。 |

---

## 文件清单

```
onelap-train/
├── onelap_report.py        # 主程序（纯标准库）
├── readiness_server.py     # （可选）Apple Watch 健康数据接收端
├── config.example.json     # 配置模板（复制为 config.json 后填值）
├── config.json             # 你的配置（自行创建，.gitignore 已忽略）
├── readiness.json          # （可选）当日 readiness（iPhone 快捷指令写入，.gitignore 已忽略）
├── run_auto.bat            # Windows 任务计划程序启动脚本
├── DEPLOY.md               # 部署文档（Linux cron / Windows 任务计划 / readiness 接收端）
├── README.md
├── LICENSE                 # MIT
├── logs/auto.log           # 运行日志（自动生成）
├── report_*.md             # 训练分析报告
└── imported_*.json         # 导入结果（含 wid）
```

---

## 免责声明

本脚本仅读取/写入你本人 OTM 账号的数据用于个人训练参考，非 Onelap 官方工具。接口为逆向所得，可能随官方更新而失效。`token` / `refresh_token` 等于你的登录凭据，**切勿分享或提交到公开仓库**。训练计划由 AI 生成，仅供参考，请结合自身感受与专业人士建议执行。
