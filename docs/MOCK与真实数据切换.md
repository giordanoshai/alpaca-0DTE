# Mock 与真实数据切换

> 当前“真实”模式是 **Yahoo 实时/K线 + Alpaca 官方 Paper API**。
> Alpaca 客户端代码仍为 `paper=True`，不是实盘资金账户。

## 先看两个开关

| 开关 | 作用 | Mock | 真实数据 |
|---|---|---|---|
| `.env` 的 `DATA_SOURCE` | Watcher 正股 tick 与历史预热来源 | `mock` | `yahoo` |
| `.env` 的 `ALPACA_END_POINT` | Alpaca 账户、期权链、报价、下单和持仓地址 | `http://127.0.0.1:9000` | 必须删除或注释 |

交易终端已经固定使用 WATCHER，不再存在 `datasource_mode.json`、`REAL 库真实`
或前端数据源切换按钮。`DATA_SOURCE=mock` 时，WATCHER 从本地回放服务器取行情；
切回 `les`/`yahoo` 时走对应真实行情入口。

---

## 一、切到 Mock

### 1. 先确认没有持仓

```powershell
Invoke-RestMethod http://127.0.0.1:8002/api/positions
Invoke-RestMethod http://127.0.0.1:9000/v2/positions
```

停止正在运行的 `tt_trading`，再修改配置。

### 2. 修改 `tt_trading/.env`

```env
DATA_SOURCE=mock
ALPACA_END_POINT=http://127.0.0.1:9000
```

如果当前 PowerShell 曾手工设置过同名环境变量，先清除，避免它覆盖 `.env`：

```powershell
Remove-Item Env:DATA_SOURCE -ErrorAction SilentlyContinue
Remove-Item Env:ALPACA_END_POINT -ErrorAction SilentlyContinue
```

### 3. 先启动 Mock 服务器

```powershell
cd D:\Dev_project\Python_Project\trading_test
.venv\Scripts\python.exe server\main.py
```

可选：指定回放日。不设置时使用全部已加载标的共同具备的最新交易日。

```powershell
$env:REPLAY_DATE = "2026-07-24"
.venv\Scripts\python.exe server\main.py
```

### 4. 再启动主项目

```powershell
cd D:\Dev_project\Python_Project\tt_trading
.venv\Scripts\python.exe app\main.py
```

### 5. 验证

```powershell
Invoke-RestMethod http://127.0.0.1:9000/
Invoke-RestMethod http://127.0.0.1:9000/sim/history/QQQ
Invoke-RestMethod http://127.0.0.1:8002/health
```

正确日志应包含：

```text
[Replay] 已载入 32 只标的：预热日=...，回放日=...
[WatcherManager] 使用本地回放服务器作为实时数据源
[Mock] 已订阅 32 只标的
```

---

## 二、刷新 33 个观察标的的回放数据

当前 33 个启动标的由两部分组成：

- 32 只可交易正股/ETF：从真实 Alpaca IEX 下载 1 分钟 RTH 数据；
- `^VIX`：市场状态专用的特殊行情标的，不下载正股或期权历史数据。

刷新数据时先停止 `trading_test`，然后运行：

```powershell
cd D:\Dev_project\Python_Project\trading_test
.venv\Scripts\python.exe scripts\download_replay.py
```

下载结果写入 `trading_test/data/replay/{SYMBOL}_1m.parquet`。脚本会忽略指向
`127.0.0.1:9000` 的 `ALPACA_END_POINT`，直接连接真实 Alpaca 数据 API；当前默认保留
20 个交易日的 IEX RTH 数据。重启 `trading_test` 后，引擎会自动发现全部 parquet。

期权数据不从 Alpaca 下载。Mock 根据当前回放正股价用 Black-Scholes 动态生成期权链、
bid/ask、delta 和其他 Greeks，因此同一份正股回放可以持续驱动止盈止损测试。

---

## 三、切回真实数据

### 1. 清空 Mock 持仓并停止主项目

确认主项目与 Mock 券商都没有持仓，然后停止 `tt_trading`。

### 2. 修改 `tt_trading/.env`

```env
DATA_SOURCE=yahoo
# ALPACA_END_POINT=http://127.0.0.1:9000
```

`ALPACA_END_POINT` 必须删除或注释。只改 `DATA_SOURCE=yahoo` 不够：
如果该端点仍存在，正股行情会走 Yahoo，但账户、期权链和下单仍会错误地发到本地 Mock。

同样清除当前 PowerShell 可能遗留的覆盖值：

```powershell
Remove-Item Env:DATA_SOURCE -ErrorAction SilentlyContinue
Remove-Item Env:ALPACA_END_POINT -ErrorAction SilentlyContinue
Remove-Item Env:REPLAY_DATE -ErrorAction SilentlyContinue
```

保持：

```json
{"mode": "watcher"}
```

### 3. 启动主项目

```powershell
cd D:\Dev_project\Python_Project\tt_trading
.venv\Scripts\python.exe app\main.py
```

Mock 服务器此时不再需要，可以关闭。

### 4. 验证

```powershell
Invoke-RestMethod http://127.0.0.1:8002/health
Select-String -Path .env -Pattern '^ALPACA_END_POINT='
```

检查结果：

- `/health` 返回 `{"status":"ok"}`。
- `Select-String` 不应匹配到未注释的 `ALPACA_END_POINT`。
- 日志应显示：

```text
[WatcherManager] 使用 Yahoo WebSocket 作为实时数据源
```

- 日志中不应再出现 `[Mock] 已订阅`。

---

## 四、最短检查清单

### Mock

```text
DATA_SOURCE=mock
ALPACA_END_POINT=http://127.0.0.1:9000
启动顺序：trading_test → tt_trading
```

### 真实数据

```text
DATA_SOURCE=yahoo
ALPACA_END_POINT 不存在或已注释
只需启动 tt_trading
```

每次切换都必须重启 `tt_trading`，因为数据源和 Alpaca 客户端在进程启动时读取配置。
