"""通过 SOCKS5h 连续测量 Alpaca SPXW REST / WebSocket 延迟。

默认运行 5 分钟，固定使用 ``socks5h://127.0.0.1:1080``：

* REST：请求当日 SPXW 窄期权链；
* WS：测量建连、认证、Ping/Pong、订阅确认、首条 Quote、取消订阅确认；
* 每次采样立即追加到 JSONL，结束后生成 JSON 与 Markdown 汇总。

本脚本只读，不调用任何下单、撤单或持仓接口。Alpaca 期权行情流通常只允许一个并发
连接，运行前应停止主服务，避免与 ``app/option_stream.py`` 抢占连接。

用法：

    python scripts/benchmark_alpaca_socks5.py

结果默认写入 ``data/benchmarks/``。行权价区间可通过 ``--strike-min`` /
``--strike-max`` 调整。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import msgpack
import websockets
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest

from app.config import OPTION_STREAM_URL
from app.integrations.alpaca.client import read_keys

METRICS = (
    "rest_chain",
    "ws_connect",
    "ws_auth",
    "ws_ping",
    "ws_subscribe",
    "ws_first_quote",
    "ws_unsubscribe",
)
PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def socks5h_url(host: str, port: int) -> str:
    """构造由代理端解析 DNS 的 SOCKS5 URL。"""
    host = host.strip()
    if not host:
        raise ValueError("proxy host cannot be empty")
    if not 1 <= port <= 65535:
        raise ValueError("proxy port must be between 1 and 65535")
    return f"socks5h://{host}:{port}"


def configure_socks5_proxy(proxy_url: str) -> None:
    """让 requests/alpaca-py 与 websockets 使用同一 SOCKS5h 代理。"""
    for name in PROXY_ENV_VARS:
        os.environ.pop(name, None)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[name] = proxy_url
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"


def percentile(values: list[float], quantile: float) -> float:
    """线性插值百分位；调用方保证 values 非空。"""
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 metric 聚合成功耗时，并保留失败数。"""
    summary: dict[str, dict[str, Any]] = {}
    for metric in METRICS:
        matching = [record for record in records if record.get("metric") == metric]
        values = [
            float(record["latency_ms"])
            for record in matching
            if record.get("ok") and record.get("latency_ms") is not None
        ]
        failed = len(matching) - len(values)
        summary[metric] = {
            "success_count": len(values),
            "error_count": failed,
            "average_ms": round(statistics.fmean(values), 3) if values else None,
            "minimum_ms": round(min(values), 3) if values else None,
            "maximum_ms": round(max(values), 3) if values else None,
            "p50_ms": round(percentile(values, 0.50), 3) if values else None,
            "p95_ms": round(percentile(values, 0.95), 3) if values else None,
        }
    return summary


def _option_strike(symbol: str) -> float:
    try:
        return int(symbol[-8:]) / 1000
    except (ValueError, IndexError):
        return float("inf")


def choose_symbols(symbols: list[str], strike_min: float, strike_max: float, count: int = 3) -> list[str]:
    """选择区间中心附近的少量 CALL/PUT 合约用于 WS 控制与 Quote 测量。"""
    center = (strike_min + strike_max) / 2
    return sorted(symbols, key=lambda symbol: (abs(_option_strike(symbol) - center), symbol))[:count]


def decode_messages(raw: bytes | str) -> list[dict[str, Any]]:
    decoded = msgpack.unpackb(raw, timestamp=3) if isinstance(raw, bytes) else json.loads(raw)
    return decoded if isinstance(decoded, list) else [decoded]


async def wait_for_message(
    ws,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_s: float,
) -> dict[str, Any] | None:
    deadline = time.perf_counter() + timeout_s
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except TimeoutError:
            return None
        for message in decode_messages(raw):
            if message.get("T") == "error":
                raise RuntimeError(
                    f"Alpaca WS {message.get('code')}: {message.get('msg', 'unknown error')}"
                )
            if predicate(message):
                return message


class ResultWriter:
    def __init__(self, raw_path: Path):
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_path = raw_path
        self.records: list[dict[str, Any]] = []
        self._handle = raw_path.open("a", encoding="utf-8", buffering=1)

    def record(
        self,
        metric: str,
        *,
        cycle: int,
        ok: bool,
        latency_ms: float | None = None,
        error: str | None = None,
        **extra: Any,
    ) -> None:
        item = {
            "timestamp_utc": dt.datetime.now(dt.UTC).isoformat(),
            "metric": metric,
            "cycle": cycle,
            "ok": ok,
            "latency_ms": round(latency_ms, 3) if latency_ms is not None else None,
        }
        if error:
            item["error"] = error[:240]
        item.update(extra)
        self.records.append(item)
        self._handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")

    def close(self) -> None:
        self._handle.close()


def write_summary_files(
    *,
    writer: ResultWriter,
    summary_path: Path,
    report_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    metrics = summarize_records(writer.records)
    payload = {
        "metadata": metadata,
        "metrics": metrics,
        "raw_jsonl": str(writer.raw_path.resolve()),
    }
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    labels = {
        "rest_chain": "REST 窄链",
        "ws_connect": "WS 建连",
        "ws_auth": "WS 认证",
        "ws_ping": "WS Ping/Pong",
        "ws_subscribe": "WS 订阅确认",
        "ws_first_quote": "WS 首条 Quote",
        "ws_unsubscribe": "WS 取消确认",
    }
    lines = [
        "# Alpaca SOCKS5 五分钟延迟基准",
        "",
        f"- 开始时间：`{metadata['started_at_utc']}`",
        f"- 实际时长：`{metadata['actual_duration_seconds']:.1f}` 秒",
        f"- 代理：`{metadata['proxy']}`",
        f"- 标的/到期：`{metadata['underlying']}` / `{metadata['expiry']}`",
        f"- 行权价范围：`{metadata['strike_min']}–{metadata['strike_max']}`",
        f"- WS 合约：`{', '.join(metadata['ws_symbols'])}`",
        "",
        "| 指标 | 成功 | 失败 | 平均 ms | 最低 ms | 最高 ms | P50 ms | P95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metric in METRICS:
        row = metrics[metric]

        def cell(name: str) -> str:
            value = row[name]
            return "—" if value is None else f"{value:.3f}"

        lines.append(
            f"| {labels[metric]} | {row['success_count']} | {row['error_count']} | "
            f"{cell('average_ms')} | {cell('minimum_ms')} | {cell('maximum_ms')} | "
            f"{cell('p50_ms')} | {cell('p95_ms')} |"
        )
    lines.extend(
        [
            "",
            f"- 原始采样：`{writer.raw_path.resolve()}`",
            f"- JSON 汇总：`{summary_path.resolve()}`",
            "",
            "> `WS 首条 Quote` 取决于合约是否产生新报价，不等同于网络往返延迟；",
            "> 超时会计入失败数，而不会伪造耗时。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


async def run_benchmark(args: argparse.Namespace) -> tuple[Path, Path, Path, dict[str, Any]]:
    proxy_url = socks5h_url(args.proxy_host, args.proxy_port)
    configure_socks5_proxy(proxy_url)
    key, secret = read_keys()
    client = OptionHistoricalDataClient(key, secret)
    expiry = dt.date.fromisoformat(args.expiry)
    request = OptionChainRequest(
        underlying_symbol=args.underlying,
        expiration_date=expiry,
        strike_price_gte=args.strike_min,
        strike_price_lte=args.strike_max,
    )

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"alpaca_socks5_{args.underlying.lower()}_{stamp}"
    raw_path = output_dir / f"{stem}.jsonl"
    summary_path = output_dir / f"{stem}.summary.json"
    report_path = output_dir / f"{stem}.summary.md"
    writer = ResultWriter(raw_path)

    started_at = dt.datetime.now(dt.UTC)
    benchmark_started = time.monotonic()
    deadline = benchmark_started + args.duration
    ws = None
    ws_symbols: list[str] = []
    cycle = 0
    next_cycle = benchmark_started

    try:
        while time.monotonic() < deadline:
            cycle += 1
            next_cycle += args.interval

            rest_started = time.perf_counter()
            try:
                snapshots = await asyncio.to_thread(client.get_option_chain, request)
                rest_ms = (time.perf_counter() - rest_started) * 1000
                symbols = sorted(snapshots)
                writer.record(
                    "rest_chain",
                    cycle=cycle,
                    ok=True,
                    latency_ms=rest_ms,
                    contract_count=len(symbols),
                )
                if not ws_symbols:
                    ws_symbols = choose_symbols(
                        symbols,
                        args.strike_min,
                        args.strike_max,
                        count=args.ws_symbol_count,
                    )
                    if not ws_symbols:
                        raise RuntimeError("REST 窄链没有返回可订阅的 SPXW 合约")
            except Exception as exc:  # noqa: BLE001 — 探针必须落盘后继续
                writer.record(
                    "rest_chain",
                    cycle=cycle,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )

            if ws is None and ws_symbols:
                connect_started = time.perf_counter()
                try:
                    ws = await websockets.connect(
                        OPTION_STREAM_URL,
                        proxy=proxy_url,
                        open_timeout=args.timeout,
                        close_timeout=3,
                        max_size=2**20,
                    )
                    writer.record(
                        "ws_connect",
                        cycle=cycle,
                        ok=True,
                        latency_ms=(time.perf_counter() - connect_started) * 1000,
                    )
                    await ws.send(msgpack.packb({"action": "auth", "key": key, "secret": secret}))
                    auth_started = time.perf_counter()
                    authenticated = await wait_for_message(
                        ws,
                        lambda message: message.get("T") == "success"
                        and message.get("msg") == "authenticated",
                        args.timeout,
                    )
                    auth_ms = (time.perf_counter() - auth_started) * 1000
                    if not authenticated:
                        raise TimeoutError("WS 认证确认超时")
                    writer.record("ws_auth", cycle=cycle, ok=True, latency_ms=auth_ms)
                except Exception as exc:  # noqa: BLE001
                    metric = "ws_connect" if ws is None else "ws_auth"
                    writer.record(
                        metric,
                        cycle=cycle,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    if ws is not None:
                        await ws.close()
                    ws = None

            if ws is not None:
                ws_stage = "ws_ping"
                try:
                    ping_started = time.perf_counter()
                    pong_waiter = await ws.ping()
                    await asyncio.wait_for(pong_waiter, timeout=args.timeout)
                    writer.record(
                        "ws_ping",
                        cycle=cycle,
                        ok=True,
                        latency_ms=(time.perf_counter() - ping_started) * 1000,
                    )

                    ws_stage = "ws_subscribe"
                    subscribe_started = time.perf_counter()
                    await ws.send(msgpack.packb({"action": "subscribe", "quotes": ws_symbols}))
                    subscribed = await wait_for_message(
                        ws,
                        lambda message: message.get("T") == "subscription"
                        and set(ws_symbols).issubset(set(message.get("quotes") or [])),
                        args.timeout,
                    )
                    subscribe_ms = (time.perf_counter() - subscribe_started) * 1000
                    if not subscribed:
                        raise TimeoutError("WS 订阅确认超时")
                    writer.record(
                        "ws_subscribe",
                        cycle=cycle,
                        ok=True,
                        latency_ms=subscribe_ms,
                    )

                    ws_stage = "ws_first_quote"
                    quote_started = time.perf_counter()
                    quote = await wait_for_message(
                        ws,
                        lambda message: message.get("T") == "q"
                        and message.get("S") in ws_symbols,
                        args.quote_timeout,
                    )
                    quote_ms = (time.perf_counter() - quote_started) * 1000
                    writer.record(
                        "ws_first_quote",
                        cycle=cycle,
                        ok=quote is not None,
                        latency_ms=quote_ms if quote is not None else None,
                        error=None if quote is not None else "timeout_without_quote",
                        symbol=quote.get("S") if quote else None,
                        quote_timestamp=str(quote.get("t")) if quote else None,
                    )

                    ws_stage = "ws_unsubscribe"
                    unsubscribe_started = time.perf_counter()
                    await ws.send(msgpack.packb({"action": "unsubscribe", "quotes": ws_symbols}))
                    unsubscribed = await wait_for_message(
                        ws,
                        lambda message: message.get("T") == "subscription"
                        and not set(ws_symbols).intersection(set(message.get("quotes") or [])),
                        args.timeout,
                    )
                    unsubscribe_ms = (time.perf_counter() - unsubscribe_started) * 1000
                    if not unsubscribed:
                        raise TimeoutError("WS 取消订阅确认超时")
                    writer.record(
                        "ws_unsubscribe",
                        cycle=cycle,
                        ok=True,
                        latency_ms=unsubscribe_ms,
                    )
                except Exception as exc:  # noqa: BLE001
                    writer.record(
                        ws_stage,
                        cycle=cycle,
                        ok=False,
                        error=f"WS cycle failed: {type(exc).__name__}: {exc}",
                    )
                    await ws.close()
                    ws = None

            if cycle == 1 or cycle % args.progress_every == 0:
                elapsed = time.monotonic() - benchmark_started
                print(
                    f"[{elapsed:6.1f}s/{args.duration:.0f}s] cycle={cycle} "
                    f"records={len(writer.records)}",
                    flush=True,
                )

            await asyncio.sleep(max(0.0, min(next_cycle, deadline) - time.monotonic()))
    finally:
        if ws is not None:
            await ws.close()
        actual_duration = time.monotonic() - benchmark_started
        writer.close()

    metadata = {
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "requested_duration_seconds": args.duration,
        "actual_duration_seconds": actual_duration,
        "interval_seconds": args.interval,
        "quote_timeout_seconds": args.quote_timeout,
        "proxy": proxy_url,
        "underlying": args.underlying,
        "expiry": expiry.isoformat(),
        "strike_min": args.strike_min,
        "strike_max": args.strike_max,
        "ws_symbols": ws_symbols,
    }
    payload = write_summary_files(
        writer=writer,
        summary_path=summary_path,
        report_path=report_path,
        metadata=metadata,
    )
    return raw_path, summary_path, report_path, payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=300.0, help="采样时长，默认 300 秒")
    parser.add_argument("--interval", type=float, default=5.0, help="采样周期，默认 5 秒")
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=1080)
    parser.add_argument("--underlying", default="SPXW")
    parser.add_argument("--expiry", default=dt.date.today().isoformat())
    parser.add_argument("--strike-min", type=float, default=7200.0)
    parser.add_argument("--strike-max", type=float, default=7300.0)
    parser.add_argument("--ws-symbol-count", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--quote-timeout", type=float, default=2.0)
    parser.add_argument("--progress-every", type=int, default=6)
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "benchmarks"))
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.duration <= 0 or args.interval <= 0:
        raise ValueError("duration and interval must be positive")
    if args.strike_min >= args.strike_max:
        raise ValueError("strike-min must be smaller than strike-max")
    if args.ws_symbol_count <= 0:
        raise ValueError("ws-symbol-count must be positive")
    if args.progress_every <= 0:
        raise ValueError("progress-every must be positive")


def main() -> None:
    args = build_parser().parse_args()
    validate_args(args)
    raw_path, summary_path, report_path, payload = asyncio.run(run_benchmark(args))
    print("\n完成：")
    print(f"  原始数据: {raw_path.resolve()}")
    print(f"  JSON 汇总: {summary_path.resolve()}")
    print(f"  Markdown : {report_path.resolve()}")
    print(json.dumps(payload["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
