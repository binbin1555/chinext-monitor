# 创业板大周期择时 · 自动监测系统

> 仅监测 / 提醒 / 记账，**绝不自动交易**。

---

## 快速开始（本地）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 从本地 CSV 初始化数据（无需 API key）
python src/init_from_csv.py

# 3. 查看仪表盘
python -m http.server 7700 --directory docs
# 浏览器打开 http://localhost:7700
```

---

## 部署到 GitHub（全自动、每日推送）

### 第一步：建仓库

1. GitHub 新建仓库（推荐公开，GitHub Pages 免费）
2. 将本目录推送为仓库根目录：
   ```bash
   git init && git add -A && git commit -m "init"
   git remote add origin https://github.com/你的用户名/仓库名.git
   git push -u origin main
   ```

### 第二步：开启 GitHub Pages

进入仓库 → Settings → Pages → Source 选 **Deploy from branch** → Branch: `main` / Folder: `/docs` → Save

### 第三步：填入 Secrets

进入仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Secret 名称       | 填入内容                                   |
|-------------------|--------------------------------------------|
| `LIXINGER_TOKEN`  | 理杏仁 API Token（lixinger.com）           |
| `QIEMAN_KEY`      | 且慢 x-api-key（见且慢温度计MCP抓取指南.txt）|
| `BARK_KEY`        | Bark iOS 推送 Key（Bark App → 首页复制）   |
| `DASHBOARD_URL`   | GitHub Pages 网址（如 `https://user.github.io/repo/`）|

### 第四步：手动触发一次验证

仓库 → Actions → **创业板每日监测** → Run workflow → 查看日志

如果日志最后显示 `data/history.csv` 和 `docs/data.json` 已提交，说明部署成功。

### 第五步：用 cron-job.org 做定时器

> GitHub Actions 的 `schedule` 定时功能在仓库低活跃期会被自动暂停，因此改用外部定时器。

1. 注册 [cron-job.org](https://cron-job.org)（免费）
2. 创建 → **New Cronjob**，填入：

   | 字段 | 内容 |
   |------|------|
   | URL | `https://api.github.com/repos/你的用户名/仓库名/actions/workflows/daily.yml/dispatches` |
   | Request method | `POST` |
   | Request headers | `Authorization: Bearer 你的GitHub_PAT`<br>`Accept: application/vnd.github+json`<br>`Content-Type: application/json` |
   | Request body | `{"ref":"main"}` |
   | Schedule | 周一至周五，北京时间 16:30（UTC 08:30） |

3. GitHub PAT 需要 **`workflow` 权限**（GitHub → Settings → Developer settings → Personal access tokens → 勾选 `workflow`）

这样每个交易日收盘后，cron-job.org 触发 GitHub Actions，自动拉取数据 → 更新仪表盘。

---

## 填写实际成交流水

策略信号发出后，手动下单，成交后编辑 `ledger.csv`：

```csv
date,action,fen,price,note
2023-04-24,buy,30,2301,T1触发
2023-04-24,buy,3,2301,周定投
2023-04-25,buy,20,2259,T2触发
...
```

- `action`: `buy`（买入）/ `reduce`（减仓50%）/ `exit`（清仓）
- `fen`: 份数
- `price`: 实际成交点位
- 提交后自动重算均价和浮盈

---

## 回放验收（历史金标准）

```bash
python src/backtest.py
```

验证引擎是否能复现第12节金标准（2018轮 + 2023轮）。

若结果不符，说明规则实现有误，需修正后再上线。

---

## 数据探针（测试 API 连通）

```bash
LIXINGER_TOKEN=xxx QIEMAN_KEY=xxx python src/probe.py
```

---

## 文件说明

| 文件 | 说明 |
|------|------|
| `config.yaml` | 策略配置（本金、份数、起始日等） |
| `state.json` | 引擎状态（每日自动更新） |
| `ledger.csv` | **用户填写**实际成交流水 |
| `data/history.csv` | 行情缓存（自动更新） |
| `docs/index.html` | 仪表盘静态页面 |
| `docs/data.json` | 仪表盘数据（自动生成） |
| `src/main.py` | 主入口（CI每日运行） |
| `src/init_from_csv.py` | 首次本地初始化 |
| `src/backtest.py` | 历史回放验收 |
| `src/probe.py` | API 连通测试 |

---

## 注意事项

- 系统**只监测不交易**，所有下单由用户手动执行后回填 ledger.csv
- 规则严格按操作手册实现，不增删
- 三个密钥绝不提交进仓库
- GitHub Pages 公开仓库时 ledger.csv 中只含数字无个人信息
