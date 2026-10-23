# Changelog

All notable changes to this project will be documented in this file.

### [2026-10-23] 废弃云端 Supabase，全面重构为开箱即用本地 SQLite3 存储
* **新增 (Added)**: 
  * 创建本地 SQLite3 引擎 [`app/db/sqlite_engine.py`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/app/db/sqlite_engine.py)，支持全表自动幂等建表（`ttt_template_store`, `ttt_order_events`, `ttt_trades`, `ttt_intercept_log`, `user_watchlist`, `signals` 等）。
  * 首次启动自动播种默认风控模板与默认美股盯盘名单（`SPY`, `QQQ`, `IWM`），彻底实现开源用户零配置即开即用。
  * 在 [`app/core/sb_client.py`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/app/core/sb_client.py) 中实现轻量本地 `LocalDatabaseClient`，零外部依赖无缝替代 Supabase Client。
  * 提供透明 SQL 语法适配层，自动映射 `%s` 占位符并注入 `now()`, `least()`, `greatest()`, `coalesce()` 等原生函数。
* **修改 (Changed)**: 
  * 将 [`app/db/__init__.py`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/app/db/__init__.py) 默认连接切换为本地 SQLite3 引擎（`data/trading.db`）。
  * 从 [`.env.example`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/.env.example) 中彻底移除 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` 及 `DATABASE_URL`，改为可选的 `SQLITE_DB_PATH`。
  * 从 [`requirements.txt`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/requirements.txt) 和 [`requirements-dev.txt`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/requirements-dev.txt) 中移除 `supabase` 与 `psycopg2-binary` 依赖，减轻环境安装门槛。
  * 更新 [`README.md`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/README.md) 架构图与配置指南，体现本地 SQLite3 存储设计。

### [2026-10-23] 变更开源许可证为 PolyForm Noncommercial 1.0.0 (禁止商用)
* **新增 (Added)**: 
  * 引入专为代码设计的标准非商业许可证 [`LICENSE`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/LICENSE)（PolyForm Noncommercial License 1.0.0），严格限制任何企业、自营交易公司、量化对冲基金或收费服务的商业使用。
  * 在 [`README.md`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/README.md) 中更新许可徽章并增加 Dual-Licensing（双重许可）说明，开放个人研究、教育与非商业使用，商业用途需申请授权。
* **修改 (Changed)**: 
  * 在 [`pyproject.toml`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/pyproject.toml) 中声明 `PolyForm Noncommercial License 1.0.0`。
  * 废除并替换原有的宽泛 MIT 许可条款。

### [2026-10-23] 废弃 UW 中转站并完善数据源降级架构文档
* **新增 (Added)**: 
  * 在 [`README.md`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/README.md) 中增加完整的**支持数据源对比矩阵 (Supported Data Sources & Fallback Architecture)**，详尽说明 Alpaca、Unusual Whales、Massive 及本地 Mock 的职责与参数。
  * 详尽阐述 **UW 未配置时的优雅降级 (Graceful Degradation)** 行为：确认核心交易风控、看门狗巡检 100% 独立平稳运行，仅降级展示类指标（资金潮徽章与 Gamma 距墙告警），绝不阻断任何交易。
* **修复 (Fixed)**: 
  * 修复缺少 `UW_T_KEY` 时的虚假降级告警，统一规范异常处理与日志输出。
* **修改 (Changed)**: 
  * 彻底废除并删除私有 UW 中转站机制 (`UW_T_ENDPOINT`, `UW_T_KEY`)，全库统一为官方直连的 `UW_ENDPOINT` 与 `UW_API_KEY`。
  * 同步重构 [`app/config.py`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/app/config.py)、[`app/watcher/quote_loops.py`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/app/watcher/quote_loops.py)、[`app/services/gex_wall_service.py`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/app/services/gex_wall_service.py)、[`app/integrations/uw.py`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/app/integrations/uw.py) 及 [`app/integrations/uw_ws.py`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/app/integrations/uw_ws.py)。
  * 更新 [`.env.example`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/.env.example) 移除中转站字段。

### [2026-10-23] 国际化改造与开源发布准备
* **新增 (Added)**: 
  * 编写工业级、全英文的专业开源 [`README.md`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/README.md)，包含详细免责声明、系统架构 Mermaid 图、模块说明、1400+ 测试验证指引与部署流程。
  * 创建开源发布计划 [`PLAN.md`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/PLAN.md)。
* **修复 (Fixed)**: 
  * 清理 [`pyproject.toml`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/pyproject.toml) 中的开发机个人路径与中文历史注释。
* **修改 (Changed)**: 
  * 将 [`.env.example`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/.env.example) 配置文件模板所有中文注释英文化。
  * 将 [`requirements.txt`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/requirements.txt) 与 [`requirements-dev.txt`](file:///d:/Dev_project/Python_Project/alpaca-0DTE/requirements-dev.txt) 依赖清单注释全面英文化。
  * 完成全库敏感凭证与账户数据排查，确认无泄漏风险。
