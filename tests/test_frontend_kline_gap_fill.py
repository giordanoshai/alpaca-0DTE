"""前端 K 线缺口处理（charts.js / barGapDetected + app.js 重取真数据）。

现象与归因（2026-08-13 定案）：
后端实时推送 `_ingest_tick()` 纯 tick 驱动、无墙钟推进（该设计自 07-23 未变），
某分钟一笔成交都没有时 `current_bar` 停在上一分钟，前端永远收不到那一分钟。
但 `GET /api/history` 走的是 `data_manager` 的完整 1m 序列，**那几分钟的真 bar
后端一直都有**——用户手动刷新即可补齐，实测确认。

历史修法（6fcac81，已废弃）：`synthesizeGapBars()` 拿上一根收盘价伪造
`open=high=low=close`、`volume=0` 的 flat bar 填进去。两个问题：
① **伪造数据**——后端有真 bar 却画一条「价格没动」的横线，与「没有数据」含义相反；
② **掩盖故障**——`MAX_GAP_FILL_BARS=30` 最多糊 30 分钟假平线，真出数据事故时
   看到的是一条平静横线而非告警。
用户报的「K 线变成 ---- 连续好几根平线」正是这些伪造 bar。

现行方案（A）：检测到缺口只**上报**，由 app.js 节流重取 `/api/history` 用真数据补，
等同于把手动刷新自动化。前端不再凭空造任何一根 bar。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _charts_js() -> str:
    return (ROOT / "static" / "watcher" / "js" / "charts.js").read_text(encoding="utf-8")


def _app_js() -> str:
    return (ROOT / "static" / "watcher" / "app.js").read_text(encoding="utf-8")


def _run_detect(bars, next_time):
    """在 node 里执行真的 barGapDetected，不在测试里重写一遍分支逻辑。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 运行时校验（源码契约断言仍生效）")
    match = re.search(
        r"function barGapDetected\(bars, nextTime\) \{.*?\n\}", _charts_js(), re.S)
    assert match, "charts.js 里找不到 barGapDetected 定义"
    script = (match.group(0)
              + "\nconsole.log(JSON.stringify(barGapDetected("
              + json.dumps(bars) + ", " + json.dumps(next_time) + ")));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def _bars(*specs):
    """[(time, close), ...] → charts.js 里 series._bars 的形状。"""
    return [{"time": t, "open": c, "high": c, "low": c, "close": c, "volume": 100}
            for t, c in specs]


# ── 缺口探测 ─────────────────────────────────────────────────────────────────

def test_跳两分钟算缺口():
    assert _run_detect(_bars((600, 49.0), (660, 50.0)), 840) is True


def test_只跳一分钟也算缺口():
    assert _run_detect(_bars((600, 49.0), (660, 50.0)), 780) is True


def test_相邻分钟不算缺口():
    assert _run_detect(_bars((600, 49.0), (660, 50.0)), 720) is False


def test_同一根反复刷新不算缺口():
    """forming bar 每秒都在刷，time 不变时必须是 no-op。"""
    assert _run_detect(_bars((600, 49.0), (660, 50.0)), 660) is False


def test_乱序的旧帧不算缺口():
    assert _run_detect(_bars((600, 49.0), (660, 50.0)), 600) is False


def test_bar不足两根时无法推断周期就不报缺口():
    assert _run_detect(_bars((660, 50.0)), 840) is False
    assert _run_detect([], 840) is False


def test_5m图表按300秒周期判缺口():
    """详情图可能是 5m 周期，别写死 60。"""
    # 0,300 之后的 600 是连续的下一根，不是缺口
    assert _run_detect(_bars((0, 49.0), (300, 50.0)), 600) is False
    # 跳过 600 直接来 900，才是缺口
    assert _run_detect(_bars((0, 49.0), (300, 50.0)), 900) is True


# ── 周期推断必须抗「不对齐时间戳」污染 ────────────────────────────────────────
# 2026-08-13 实测 GET /api/history/SPY?interval=1m&limit=300 的真实返回：
#   相邻间隔分布 = {60:297, 36:1, 24:1}，断点 13:25:00 → 13:25:36 → 13:26:00
# 周期若按**最小正间隔**推断会被那一根污染成 24 秒，于是正常前进 60 秒也被判成缺口，
# 每分钟都触发一次重取 —— 请求风暴。必须取众数。

def _bars_with_offbeat():
    """10 根规矩的 1m + 一根 36 秒偏移根（复刻线上 _bars 缓冲的真实形态）。"""
    specs = [(600 + 60 * i, 50.0) for i in range(10)]   # 600 … 1140
    specs.insert(6, (936, 49.5))                        # 900 → 936 → 960
    return _bars(*specs)


def test_混入不对齐时间戳后正常前进一分钟不算缺口():
    assert _run_detect(_bars_with_offbeat(), 1200) is False


def test_混入不对齐时间戳后真实缺口仍能识别():
    assert _run_detect(_bars_with_offbeat(), 1320) is True


# ── 端到端：真跑 updateChartData ──────────────────────────────────────────────

def _run_update_chart(bars, incoming):
    """把 charts.js 当真模块 import 进 node 跑一遍，录下 series.update 调用与返回值。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 运行时校验（源码契约断言仍生效）")
    with tempfile.TemporaryDirectory() as td:
        mod = Path(td) / "charts.mjs"
        mod.write_text(_charts_js(), encoding="utf-8")
        script = f"""
const {{ ChartManager }} = await import({json.dumps(mod.as_uri())});
const calls = [];
const rec = (name) => ({{ update: (d) => calls.push([name, d]) }});
const series = {{ kline: rec('kline'), volume: rec('volume'),
                 _bars: {json.dumps(bars)} }};
const ret = ChartManager.updateChartData(series, {json.dumps(incoming)}, {{}});
console.log(JSON.stringify({{ calls, ret }}));
"""
        out = subprocess.run([node, "--input-type=module", "-e", script],
                             capture_output=True, text=True, encoding="utf-8",
                             timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip())


def _kline_times(calls):
    return [d["time"] for name, d in calls if name == "kline"]


def test_缺口时绝不伪造任何bar只画真实来的那根():
    """核心回归：用户看到的 ---- 平线就是伪造 bar，前端一根都不许造。"""
    got = _run_update_chart(
        _bars((600, 49.0), (660, 50.0)),
        {"timestamp": 840, "open": 51.0, "high": 52.0, "low": 50.5,
         "close": 51.5, "volume": 300},
    )
    assert _kline_times(got["calls"]) == [840], (
        f"除真实 bar 外还画了 {len(_kline_times(got['calls'])) - 1} 根伪造 bar"
    )
    real = [d for n, d in got["calls"] if n == "kline"][0]
    assert (real["open"], real["high"], real["low"], real["close"]) == \
        (51.0, 52.0, 50.5, 51.5), "真实 bar 的 OHLC 不得被改写"


def test_缺口必须上报给调用方以便重取真数据():
    got = _run_update_chart(
        _bars((600, 49.0), (660, 50.0)),
        {"timestamp": 840, "open": 51.0, "high": 52.0, "low": 50.5,
         "close": 51.5, "volume": 300},
    )
    assert got["ret"] and got["ret"].get("gapDetected") is True, \
        f"缺口未上报，app.js 无从触发重取：ret={got['ret']}"


def test_无缺口时不得上报以免每帧都重取():
    got = _run_update_chart(
        _bars((600, 49.0), (660, 50.0)),
        {"timestamp": 720, "open": 51.0, "high": 52.0, "low": 50.5,
         "close": 51.5, "volume": 300},
    )
    assert got["ret"] and got["ret"].get("gapDetected") is False, \
        f"正常步进被误报成缺口，会导致每分钟一次重取：ret={got['ret']}"


# ── 伪造逻辑必须真的被移除，而不是改名留着 ────────────────────────────────────

def test_伪造平线的函数与常量必须彻底移除():
    """只禁**定义**，不禁注释里提名字——注释保留旧名是决策考古，有价值。"""
    js = _charts_js()
    assert not re.search(r"^\s*function synthesizeGapBars", js, re.M), \
        "伪造函数的定义仍在，改名保留不算移除"
    assert not re.search(r"^\s*const MAX_GAP_FILL_BARS", js, re.M), \
        "伪造上限常量的定义仍在"
    # 也不许还有人调用它
    assert "synthesizeGapBars(" not in js.replace("synthesizeGapBars() ", ""), \
        "仍有对伪造函数的调用"


# ── app.js 接线契约：检测到缺口要节流重取 ─────────────────────────────────────

def test_appjs_消费缺口信号并节流重取历史():
    """光有上报没接线等于没修；没节流则缺口频发时会打爆 /api/history。"""
    js = _app_js()
    assert "gapDetected" in js, "app.js 未消费 updateChartData 的缺口信号"
    m = re.search(r"refillChartGap\(symbol\)\s*\{.*?\n        \}", js, re.S)
    assert m, "app.js 里找不到 refillChartGap 定义"
    body = m.group(0)
    assert "loadChartData" in body, "重取必须走 loadChartData 拿后端真数据"
    assert re.search(r"_gapRefillAt|throttle|节流", body), \
        "重取必须节流，否则缺口频发时会打爆 /api/history"
