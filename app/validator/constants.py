"""T1 铁律常量 —— 2026-05 定版，来自真实亏损教训，禁止参数化。

这些值写成代码常量、不进配置表；改动必须改代码（README 铁律）。
金额单位：美元。
"""
from decimal import Decimal

MAX_POSITION_SIZE = Decimal("500")   # 单笔最大仓位 $
POSITION_TOLERANCE = Decimal("0.20")  # ⑥ 手数守卫容差：数量×单手成本 ≤ 最大仓位×(1+容差) 才放行（模板可覆盖）
MAX_LOSS_PER_TRADE = Decimal("80")   # 单笔最大亏损 $（与百分比止损取先触发者）
MAX_DAILY_LOSS = Decimal("150")      # 日亏熔断，唯一硬刹车
MAX_WEEKLY_LOSS = Decimal("300")     # 周亏熔断（以 Supabase trading_rules 为准：300）
CONSECUTIVE_LOSS_COOLDOWN = 600      # 2 连亏后强制冷却秒数
OPENING_BLACKOUT_MIN = 45            # 开盘后禁入分钟数（2026-07-17 降级为 T2 模板可调；此值仅兜底默认。模板：默认/激进 10、保守 45）
MAX_TRADES_PER_DAY = 6               # ⑨ 今日交易次数上限（达上限熔断锁单）
DAILY_PROFIT_TARGET = Decimal("300")  # ⑨ 日止盈锁利线：当日盈利达标即收工（防赢了回吐）

# 入场筛选二期（2026-07-17，1211 笔实盘+模拟回放标定，spec: entry-filters-design）
# （同期的 WALL_ROOM_ATR_THRESHOLD 距墙阈值已随 GEX 于 2026-08-11 整体下线）
ENTRY_CUTOFF_MINS = Decimal("150")        # 午后禁入：开盘 150 分钟(=12:00 ET)后禁新开仓

# ⑥ 在途挂单超时自动撤单（2026-07-25 用户需求）：买单/平仓卖单挂满这么多秒仍未成交即撤。
# 订单是 TimeInForce.DAY，被动侧挂单不撤就会一路排到收盘（QQQ 减仓事件）。0 = 停用。
ORDER_TIMEOUT_S = Decimal("30")

# 合约选择器 ③ 的硬门槛（用于规则9 合约检查）
MIN_DELTA = Decimal("0.40")          # delta ≥ 0.40
MAX_SPREAD_PCT = Decimal("0.03")     # spread ≤ 3% of mid

# 期权合约乘数（1 张 = 100 股权利金）
CONTRACT_MULTIPLIER = Decimal("100")

# 2026-09-01 删除的常量（四条闸一并下线，见 app/validator/core.py 文件头）：
#   CHOP_ER_THRESHOLD      规则 5 震荡闸的效率比门槛
#   MTF_5M_DEAD_ZONE       规则 6 方向闸的 5min 静态死区
#   MTF_5M_CONVERGE_BARS   规则 6 方向闸的 5min 收缩判据（连缩几根）
#   MTF_5M_CONVERGE_ETA    同上（预计几根内穿）
#   STALL_CALIBRATED_LO/HI stall 动能闸旧比值机制的标定端点（早已是纯历史记录）
#   STALL_EXHAUST_LO/HI    同上，模板里那两个输入框的默认值
#   STALL_PEAK_BARS        stall 峰值回看根数
