# 部署文档：Onelap OTM 每日自动训练计划（Linux + cron / Windows + 任务计划）

每天凌晨自动：抓最新训练数据 → glm-5.2 教练重新生成未来 14 天计划 → 删掉旧的「（计划）」课 → 写入新计划到 OTM 日历 → 推送报告到微信 → 写日志。纯 Python 标准库，**无需 pip 安装任何依赖**。

---

## 0. 工作机制（先了解再部署）

`--auto` 模式每个流程都有时间戳日志，顺序如下（任何一步失败都不会破坏已有计划）：

1. **刷新 token**：若 config 里填了 `refresh_token`，自动换新 access token 并写回 config；没有就沿用静态 token。
2. **抓数据**：PMC（CTL/ATL/TSB）+ 近 30 天骑行记录。
3. **教练生成计划**：glm-5.2 基于最新数据生成**从今天起 14 天**的计划（含 IF/TSS/区间）。失败自动重试 3 次。
4. **删旧计划**：删除日历上所有 ≥ 今天、名字带「（计划）」后缀的旧课。**只动脚本自己导入的课**，你手动建的课、历史课一律不动。
5. **写入新计划**：按新课表创建训练课并排到对应日期（休息日跳过）。
6. **推送微信**：经 Server酱 把报告推到你微信。
7. **日志**：全部输出落到 `logs/auto.log`。

> 安全说明：脚本只删除名字含「（计划）」的课（即它自己创建的）。你在 OTM 手动建/官方套用的课不受影响。

---

## 1. 文件清单

部署只需要这几个文件，放到服务器同一目录（下文以 `/opt/onelap-train` 为例）：

```
/opt/onelap-train/
├── onelap_report.py      # 主程序
├── config.json           # 配置（token / refresh_token / 三个 key）
└── logs/                 # 运行日志（程序自动创建）
```

> 把本机的 `onelap_report.py` 上传到服务器；`config.json` 在服务器上新建（见第 3 步），**不要**把本机含密钥的 config.json 直接传公网。

---

## 2. 第一步：拿 OTM 的 token 和 refresh_token（关键）

OTM 的 access token **约 48 小时过期**。要每天无人值守跑，必须用 `refresh_token` 自动续期。拿法：

1. 浏览器打开 https://otm.onelap.cn 并**退出登录**，然后**重新登录一次**（手机号 + 验证码）。重新登录会触发完整的登录流程，写入 refresh_token。
2. 登录后按 `F12` → **Application（应用）** → **Local Storage** → 点 `https://otm.onelap.cn`。
3. 找到并复制这两个值：
   - `token` → access token（eyJ... 开头）
   - `refresh_token` → 续期用的 token（没有这一项见下方说明）

> **如果重新登录后 Local Storage 里仍然没有 `refresh_token`**：说明该登录方式不签发 refresh_token，无法自动续期。此时只能退而求其次：config 里只填 `token`，每 ~48 小时手动更新一次（见第 7 节「token 过期」）。建议先确认这一步能否拿到 refresh_token 再继续。

---

## 3. 第二步：填 config.json

在服务器上创建 `/opt/onelap-train/config.json`（参考 `config.example.json`）：

```json
{
  "token": "第2步复制的 token",
  "refresh_token": "第2步复制的 refresh_token",
  "cookie": "",
  "days_back": 14,
  "days_ahead": 14,
  "glm_api_key": "智谱开放平台的 key（open.bigmodel.cn）",
  "glm_endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
  "serverchan_key": "Server酱 SendKey（sct.ftqq.com 用微信登录拿）"
}
```

三个 key 的获取：
- **glm_api_key**：智谱开放平台 → API Keys。模型固定用 `glm-5.2`（IF/功率分区准确，不要换成 flash）。余额不足会报 1113。
- **serverchan_key**：https://sct.ftqq.com 用微信登录 → SendKey。
- 没填 `glm_api_key` 则不生成计划（导入也无课可导）；没填 `serverchan_key` 则推送步骤跳过。

权限：这个目录要可写（cron 运行用户能写 `config.json`、`logs/`、`report_*.md`、`imported_*.json`）。

---

## 4. 第三步：手动跑一次验证

```bash
cd /opt/onelap-train
python3 onelap_report.py --auto
```

预期看到（带时间戳）：
```
[2026-.. ..:..:..] ==== 自动运行开始 ====
[2026-.. ..:..:..] token 已刷新并写回 config          # 填了 refresh_token 才有
正在抓取数据……
正在请 glm-5.2 教练生成计划……
✅ 报告已推送到微信。
[2026-.. ..:..:..] 清理 ≥ ..-.. 且含「（计划）」的旧计划课……
[2026-.. ..:..:..]   删除旧计划课 wid=... (...)
[2026-.. ..:..:..] 清理完成：删除 N，保留 M
[2026-.. ..:..:..] 导入完成：成功 N/14 天
[2026-.. ..:..:..] ==== 自动运行结束 ====
```

打开 OTM 日历确认有新计划课、微信收到推送，即通过。`logs/auto.log` 也会记录（见第 5 步的 cron 把输出重定向进去；手动跑时输出在终端）。

---

## 5. 第四步：配 cron（每天凌晨 4 点）

编辑 crontab：
```bash
crontab -e
```

加一行（路径换成你的；`>> logs/auto.log 2>&1` 把全部输出落盘）：

```cron
0 4 * * * cd /opt/onelap-train && mkdir -p logs && /usr/bin/python3 onelap_report.py --auto >> logs/auto.log 2>&1
```

> `python3` 路径用 `which python3` 确认，写绝对路径（cron 的 PATH 很小，别只写 `python3`）。

**时区**：cron 用服务器系统时区。要跑「北京时间 4:00」：
- 推荐：把服务器时区设成上海 → `sudo timedatectl set-timezone Asia/Shanghai`，上面 `0 4 * * *` 即北京 4:00。
- 不改系统时区的话：在 crontab 顶部加 `CRON_TZ=Asia/Shanghai`，或用 UTC 时间换算（北京 4:00 = UTC 20:00 前一天 → `0 20 * * *`）。

确认已生效：
```bash
crontab -l                       # 能看到那行
systemctl status cron            # Debian/Ubuntu；CentOS 是 crond
```

---

## 5B. Windows 部署（任务计划程序）

Windows 上脚本本身不用改，只是把「定时」从 cron 换成任务计划程序。仓库里带了 `run_auto.bat` 启动脚本。

**前置**：装好 Python 3.7+（[python.org](https://python.org)，安装时勾选 *Add Python to PATH*）；`config.json` 和 Linux 一样填（token / refresh_token / glm_api_key / serverchan_key）。

**先手动验证**——在项目目录开 cmd 或 PowerShell：
```
cd C:\Users\你\onelap-train
python onelap_report.py --auto
```
跑通后再设定时。

**方式 A：图形界面**
1. 开始菜单搜「任务计划程序」(Task Scheduler) → 右侧「创建基本任务…」。
2. 名称填 `OnelapAuto` → 触发器选「每天」→ 时间设 `04:00:00`。
3. 操作选「启动程序」→ 程序填 `run_auto.bat` 的完整路径，如 `C:\Users\你\onelap-train\run_auto.bat`；「起始于(可选)」填项目目录 `C:\Users\你\onelap-train`。
4. 完成。之后右键该任务 → 属性，可勾选「不管用户是否登录都要运行」「使用最高权限运行」。

**方式 B：命令一行**（cmd / PowerShell，建议管理员身份）
```
schtasks /create /tn "OnelapAuto" /tr "C:\Users\你\onelap-train\run_auto.bat" /sc daily /st 04:00 /f
```

**查看 / 删除**
```
schtasks /query /tn "OnelapAuto"        # 查看
schtasks /delete /tn "OnelapAuto" /f    # 删除
```

> Windows 通常有完整根证书，不会触发 SSL 拦截回退；若处公司代理网络有拦截，脚本同样会自动回退。运行日志在 `logs\auto.log`，和 Linux 一致。
> 若 `run_auto.bat` 报「找不到 python」，说明 Python 没加进 PATH——按 `run_auto.bat` 顶部的注释把 `python` 换成完整路径（如 `C:\Users\你\AppData\Local\Programs\Python\Python313\python.exe`）或改用 `py -3`。

---

## 6. 日志与日常监控

```bash
# 看最近一次运行
tail -n 80 /opt/onelap-train/logs/auto.log

# 只看每天的开始/结束/结果摘要
grep -E "自动运行|token|清理完成|导入完成|失败|Error" /opt/onelap-train/logs/auto.log | tail

# 日志会一直增长，按月轮转（可选）
sudo tee /etc/logrotate.d/onelap-train >/dev/null <<'EOF'
/opt/onelap-train/logs/auto.log {
    monthly
    rotate 6
    compress
    missingok
    notifempty
    copytruncate
}
EOF
```

每天的完整报告另存为 `report_YYYY-MM-DD.md`，导入结果存为 `imported_YYYY-MM-DD.json`，可随时翻阅。

---

## 7. 常见问题排查

| 现象 | 原因 / 处理 |
|---|---|
| `认证失败（HTTP 401）` / `token 刷新失败` | access token 过期且没 refresh_token（或 refresh_token 也过期）。重新登录 OTM，更新 config 里的 `token`（和 `refresh_token`）。 |
| token 每两天就过期 | 没填 `refresh_token` 或 OTM 该登录方式不发。只能定期手动更新 token（见第 2 步重新拿）。填了 refresh_token 则脚本自动续、长期免维护。 |
| `教练调用失败 … 改用兜底说明` / 计划没生成 | glm-5.2 接口超时或余额不足（1113=余额不足，1211=模型名错）。`--auto` 已重试 3 次；查余额/换时段/重跑即可。 |
| 微信没收到 | `serverchan_key` 没填或失效；或 Server酱 推送频率限制。看日志里 `微信推送失败` 行。 |
| 日历没更新 / 重复课 | 看 `导入完成：成功 N/14`；失败看 `[FAIL]` 行。重复课一般是被手动跑过两次——`--auto` 每次先删旧「（计划）」课再重建，正常不会重复。 |
| cron 没跑 | `python3` 没用绝对路径；或时区不对；或目录不可写。手动 `cd ... && python3 onelap_report.py --auto` 复现。 |
| `SSL: CERTIFICATE_VERIFY_FAILED` / `self signed certificate in certificate chain` | 服务器网络有 TLS 拦截（代理/防火墙重签证书）或缺根证书。脚本已内置容错：证书校验失败时**自动改用不校验证书模式**继续（只警告一次），不影响功能。想恢复校验：`yum install -y ca-certificates && update-ca-trust`（或把你们的代理根证书导入系统 CA）。⚠️不校验=拦截设备理论上能看到流量（含 token），自建/可信网络下可接受。 |

**先手动跑通，再上 cron**——这是最快的排错方式。

---

## 8. 命令速查

```bash
# 日常自动跑（cron 用这个）
python3 onelap_report.py --auto

# 只生成报告 + 推微信，不动 OTM 日历
python3 onelap_report.py --push

# 预览会把哪些课导入（不写入，安全）
python3 onelap_report.py --dry-run-import

# 只在某日期建 1 条课做验证
python3 onelap_report.py --import-test-date 2026-07-24

# 计划从明天起（默认行为；--auto 会覆盖为从今天起）
python3 onelap_report.py --import --push

# 只重新刷新并保存 token（调试用）
python3 -c "import onelap_report as R,json; c=R.load_config(); c,_=R.refresh_access_token(c); print('done')"
```

---

## 9. 已知限制

- **token 依赖 refresh_token 续期**：没有 refresh_token 时每 ~48 小时要手动更新一次 token（OTM 的限制，不是脚本问题）。
- **计划由 LLM 生成**：glm-5.2 每次输出会有细微差异（正常），核心区间/TSS 稳定；偶发接口超时会自动重试 3 次。
- **只管理「（计划）」后缀的课**：脚本绝不删除你手动建或官方套用的训练课。
- 服务器需能访问 `otm.onelap.cn`、`open.bigmodel.cn`、`sctapi.ftqq.com`（都在国内，一般无障碍）。
