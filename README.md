# Feishu Marketing Daily Bot

每日自动抓取英文主流媒体中与品牌/营销相关的动态，并推送到飞书机器人。

## 1. 功能

- 多源抓取（Reuters、Bloomberg、FT、WSJ、Ad Age、Campaign、Marketing Week、The Drum、Digiday、HBR）
- 关键词过滤 + 来源权重 + 新鲜度打分
- 去重（避免重复推送）
- 支持 `--dry-run` 预览
- 支持 `--daemon` 每日定时自动推送

## 2. 环境变量

在 PowerShell 里先设置：

```powershell
$env:FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
# 可选：如果你的机器人开启了签名校验，再设置这个
$env:FEISHU_SECRET="your_bot_secret"
# 可选：启用 DeepSeek 中文精炼摘要
$env:DEEPSEEK_API_KEY="your_deepseek_api_key"
```

## 3. 运行

先试跑（不发飞书）：

```powershell
python .\feishu_marketing_daily\daily_marketing_bot.py --dry-run
```

立即执行一次真实推送：

```powershell
python .\feishu_marketing_daily\daily_marketing_bot.py
```

每日 09:00 自动执行（常驻）：

```powershell
python .\feishu_marketing_daily\daily_marketing_bot.py --daemon --daily-time 09:00 --timezone Asia/Shanghai
```

## 4. 常用参数

- `--lookback-hours 30`：抓取最近多少小时（默认 30）
- `--max-items 10`：最多推送多少条
- `--force`：即便今天已经推送过，也强制再推一次
- `--state-path xxx`：自定义状态文件路径

## 5. 建议部署（Windows）

如果你不想常驻终端，建议用“任务计划程序”每天触发一次：

程序：

```text
python
```

参数：

```text
C:\Users\MSI-PC\Desktop\sth. to mind\feishu_marketing_daily\daily_marketing_bot.py --timezone Asia/Shanghai --lookback-hours 30 --max-items 10
```

起始于：

```text
C:\Users\MSI-PC\Desktop\sth. to mind
```

## 6. GitHub Actions（每天北京时间 10:00）

项目已包含工作流文件：

- `.github/workflows/daily_push.yml`

其中 `cron: "0 2 * * *"` 对应 UTC 02:00，也就是北京时间 10:00（UTC+8）。

### 需要在 GitHub 仓库里设置 Secrets

- `FEISHU_WEBHOOK_URL`（必填）
- `FEISHU_SECRET`（可选）
- `DEEPSEEK_API_KEY`（可选）

### 本地推到 GitHub（示例）

```powershell
cd "C:\Users\MSI-PC\Desktop\sth. to mind\feishu_marketing_daily"
git init
git add .
git commit -m "feat: feishu marketing daily bot with github actions"
git branch -M main
git remote add origin https://github.com/<your_name>/<your_repo>.git
git push -u origin main
```
