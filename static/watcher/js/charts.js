/**
 * 4in1 Watcher - 图表管理
 */

// Market Tide 迷你图的默认可视窗口（秒）。后端 series 是「当日开盘至今」的全量点，
// 默认只把最近这么长时间画进视口，早于此的点仍在数据里、只是初始不可见。
const TIDE_VISIBLE_WINDOW_SEC = 60 * 60;

// K 线缺口探测。后端实时帧只在「有 tick 的分钟」才推 current_bar，空分钟整根
// 不存在（app/core/bar_builder.py 纯 tick 驱动、无墙钟推进），而
// lightweight-charts 的 update() 只能追加/替换最后一根、无法回插。
//
// 关键事实（2026-08-13 实测）：那几分钟的**真 bar 后端一直都有** ——
// GET /api/history 走 data_manager 的完整 1m 序列，用户手动刷新即可补齐。
// 所以正确做法是**把真数据取回来**，而不是前端凭空造。
//
// 旧实现 synthesizeGapBars() 拿上一根收盘价伪造 open=high=low=close、volume=0 的
// flat bar 填进去，已废弃：① 伪造数据——后端有真 bar 却画一条「价格没动」的横线，
// 与「没有数据」含义相反；② 掩盖故障——最多能糊 30 分钟假平线，真出数据事故时
// 看到的是平静横线而非告警。用户报的「K 线变成 ---- 连续好几根平线」正是那些伪造根。
//
// 本函数只**探测并上报**，由 app.js 节流重取 /api/history 用真数据补。
function barGapDetected(bars, nextTime) {
    if (!Array.isArray(bars) || bars.length < 2) return false;
    const last = bars[bars.length - 1];
    if (!last || typeof last.time !== 'number') return false;
    const gap = nextTime - last.time;
    // 同一根 forming bar 反复刷（gap=0）和乱序旧帧（gap<0）都不是缺口。
    if (!(gap > 0)) return false;
    // 周期取相邻间隔的**众数**。别写死 60（详情图可能是 5m），更不能取最小值——
    // 后端会吐出不落在分钟边界上的 bar（实测 GET /api/history/SPY interval=1m
    // 返回 13:25:00 → 13:25:36 → 13:26:00，间隔分布 {60:297,36:1,24:1}）。
    // 取最小值会被那一根污染成 24 秒，于是正常前进 60 秒也被判成缺口，
    // 每分钟触发一次重取 —— 请求风暴。众数下 297 根规矩的 60 秒稳压 1 根噪声。
    const tally = new Map();
    for (let i = 1; i < bars.length; i++) {
        const step = bars[i].time - bars[i - 1].time;
        if (step > 0) tally.set(step, (tally.get(step) || 0) + 1);
    }
    let interval = null;
    let bestCount = 0;
    for (const [step, count] of tally) {
        // 频次高者胜；平票取较小的（真实缺口造成的整数倍大间隔不该当周期）。
        if (count > bestCount || (count === bestCount && step < interval)) {
            interval = step;
            bestCount = count;
        }
    }
    if (!interval) return false;
    return gap > interval;
}

export const ChartManager = {
    // 创建图表基础配置
    createBaseOptions(height, isMini = true) {
        return {
            height: height,
            layout: {
                background: { type: 'solid', color: 'transparent' },
                textColor: isMini ? '#64748b' : '#94a3b8',
                fontFamily: 'JetBrains Mono, monospace',
            },
            grid: {
                vertLines: { color: isMini ? 'rgba(45, 53, 72, 0.3)' : 'rgba(45, 53, 72, 0.5)' },
                horzLines: { color: isMini ? 'rgba(45, 53, 72, 0.3)' : 'rgba(45, 53, 72, 0.5)' },
            },
            localization: {
                timeFormatter: (time) => {
                    const date = new Date(time * 1000);
                    return new Intl.DateTimeFormat('en-US', {
                        timeZone: 'America/New_York',
                        month: '2-digit',
                        day: '2-digit',
                        hour: '2-digit',
                        minute: '2-digit',
                        hour12: false
                    }).format(date);
                }
            },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
                vertLine: {
                    color: 'rgba(100, 116, 139, 0.4)',
                    width: 1,
                    style: 1,
                },
                horzLine: {
                    color: 'rgba(100, 116, 139, 0.4)',
                    width: 1,
                    style: 1,
                },
            },
            rightPriceScale: {
                visible: !isMini,
                borderColor: isMini ? 'rgba(45, 53, 72, 0.5)' : 'rgba(45, 53, 72, 0.8)',
                scaleMargins: { top: 0.1, bottom: 0.1 },
                minimumWidth: 65,
                autoScale: true,
            },
            timeScale: {
                visible: !isMini,
                borderColor: 'rgba(45, 53, 72, 0.8)',
                timeVisible: true,
                secondsVisible: false,
                rightOffset: 20,
                shiftVisibleRangeOnNewBar: true,
                tickMarkFormatter: (time) => {
                    const date = new Date(time * 1000);
                    return new Intl.DateTimeFormat('en-US', {
                        timeZone: 'America/New_York',
                        hour: '2-digit',
                        minute: '2-digit',
                        hour12: false
                    }).format(date);
                }
            },
            // 重新启用手动拖拽和缩放，方便用户左右拖动图表查看细节
            handleScroll: true,
            handleScale: {
                mouseWheel: false,
            },
        };
    },

    // withVolume=false 时不建成交量/量能均线两条 series（迷你卡片用，砍掉成交量面板降渲染成本）
    addKlineSeries(chart, { withVolume = true } = {}) {
        const ma21 = chart.addLineSeries({
            color: '#fb9800',
            lineWidth: 1.5,
            priceScaleId: 'right',
            lastValueVisible: false,
            priceLineVisible: false,
            crosshairMarkerVisible: false,
        });

        const ma9 = chart.addLineSeries({
            color: '#f6c309',
            lineWidth: 1.5,
            priceScaleId: 'right',
            lastValueVisible: false,
            priceLineVisible: false,
            crosshairMarkerVisible: false,
        });

        const kline = chart.addCandlestickSeries({
            upColor: '#10b981', // emerald-500
            downColor: '#ef4444', // red-500
            borderVisible: false,
            wickUpColor: '#10b981',
            wickDownColor: '#ef4444',
            priceScaleId: 'right',
        });

        if (!withVolume) {
            return { kline, ma9, ma21 };
        }

        const volume = chart.addHistogramSeries({
            color: '#26a69a',
            priceFormat: { type: 'volume' },
            priceScaleId: 'volume', // Give it a specific ID
        });

        const volMa = chart.addLineSeries({
            color: '#2962FF', // blue
            lineWidth: 1.5,
            priceScaleId: 'volume', // Share volume scale
            lastValueVisible: false,
            priceLineVisible: false,
            crosshairMarkerVisible: false,
        });

        // Set the margins for the volume scale so it only takes up the bottom 20%
        chart.priceScale('volume').applyOptions({
            scaleMargins: {
                top: 0.8, // highest point of the series will be at 80% of the chart height
                bottom: 0,
            },
        });

        return { kline, ma9, ma21, volume, volMa };
    },

    addMacdSeries(chart) {
        const priceFormat = {
            type: 'price',
            precision: 2,
            minMove: 0.01,
        };
        return {
            hist: chart.addHistogramSeries({ 
                priceScaleId: 'right',
                priceFormat: priceFormat,
                priceLineVisible: false,
                lastValueVisible: true,
            }),
            line: chart.addLineSeries({ 
                color: '#2962FF', // TradingView MACD Line
                lineWidth: 1.5,
                priceScaleId: 'right',
                priceFormat: priceFormat,
                priceLineVisible: false,
                lastValueVisible: false,
            }),
            signal: chart.addLineSeries({ 
                color: '#FF6D00', // TradingView Signal Line
                lineWidth: 1.5,
                priceScaleId: 'right',
                priceFormat: priceFormat,
                priceLineVisible: false,
                lastValueVisible: false,
            }),
        };
    },

    // 初始化 RSI 序列
    addRsiSeries(chart) {
        const line = chart.addLineSeries({ 
            color: '#7E57C2', // TradingView RSI Line
            lineWidth: 2,
            priceScaleId: 'right',
            priceLineVisible: false,
            lastValueVisible: true,
            priceFormat: {
                type: 'price',
                precision: 2,
                minMove: 0.01,
            },
        });
        
        const signal = chart.addLineSeries({ 
            color: '#fbbf24', // RSI SMA Line
            lineWidth: 1.5,
            priceScaleId: 'right',
            priceLineVisible: false,
            lastValueVisible: true,
            priceFormat: {
                type: 'price',
                precision: 2,
                minMove: 0.01,
            },
        });

        return { line, signal };
    },

    // 控制 MA9/MA21 均线的显隐（供全局 MA 开关调用）
    setMaVisible(seriesObj, visible) {
        if (seriesObj?.ma9) seriesObj.ma9.applyOptions({ visible });
        if (seriesObj?.ma21) seriesObj.ma21.applyOptions({ visible });
    },

    // Market Tide 迷你双线图（net call 绿 / net put 红），供 hover popover 用
    createTideChart(container) {
        const chart = LightweightCharts.createChart(container, {
            width: container.clientWidth || 320,
            height: 160,
            layout: {
                background: { type: 'solid', color: 'transparent' },
                textColor: '#94a3b8',
                fontFamily: 'JetBrains Mono, monospace',
            },
            grid: {
                vertLines: { color: 'rgba(45, 53, 72, 0.3)' },
                horzLines: { color: 'rgba(45, 53, 72, 0.3)' },
            },
            rightPriceScale: { visible: false },
            leftPriceScale: { visible: false },
            timeScale: {
                visible: true,
                borderColor: 'rgba(45, 53, 72, 0.8)',
                timeVisible: true,
                secondsVisible: false,
                tickMarkFormatter: (time) => {
                    const date = new Date(time * 1000);
                    return new Intl.DateTimeFormat('en-US', {
                        timeZone: 'America/New_York',
                        hour: '2-digit', minute: '2-digit', hour12: false,
                    }).format(date);
                },
            },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            handleScroll: false,
            handleScale: false,
        });
        const call = chart.addLineSeries({ color: '#10b981', lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
        const put = chart.addLineSeries({ color: '#ef4444', lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
        return { chart, call, put };
    },

    // 用 build_tide_series 的紧凑结构刷新双线
    setTideData(tideChart, series) {
        if (!tideChart) return;
        const pts = series || [];
        tideChart.call.setData(pts.map(p => ({ time: p.t, value: p.call })));
        tideChart.put.setData(pts.map(p => ({ time: p.t, value: p.put })));

        // 视口只留最近 TIDE_VISIBLE_WINDOW_SEC；不足一窗（开盘初期）退回 fitContent，
        // 否则 setVisibleRange 会把 from 拉到数据之前，画面左侧留一大片空白。
        const ts = tideChart.chart.timeScale();
        const last = pts.length ? pts[pts.length - 1].t : null;
        if (last !== null && last - pts[0].t > TIDE_VISIBLE_WINDOW_SEC) {
            ts.setVisibleRange({ from: last - TIDE_VISIBLE_WINDOW_SEC, to: last });
        } else {
            ts.fitContent();
        }
    },

    // 同步两个图表的时间轴
    syncCharts(chartA, chartB) {
        const tsA = chartA.timeScale();
        const tsB = chartB.timeScale();
        tsA.subscribeVisibleLogicalRangeChange(range => tsB.setVisibleLogicalRange(range));
        tsB.subscribeVisibleLogicalRangeChange(range => tsA.setVisibleLogicalRange(range));
    },

    // 处理并设置图表数据
    setChartData(series, bars) {
        // 缓存最近30根K线用于实时MA计算
        series._bars = bars.slice(-30);

        const klineData = [];
        const ma9Data = [];
        const ma21Data = [];
        const volData = [];
        const volMaData = [];

        const macdHistData = [];
        const macdLineData = [];
        const signalLineData = [];
        const rsiData = [];
        const rsiSignalData = [];

        const isEma = series.maType === 'ema';
        const k9 = 2 / (9 + 1);
        const k21 = 2 / (21 + 1);
        let ema9 = null;
        let ema21 = null;
        let ema9Prev = null;   // 处理最后一根 bar 之前的 EMA 值，供 updateChartData 续算同一根 forming bar
        let ema21Prev = null;
        let ma9Sum = 0;
        let ma21Sum = 0;
        let volMaSum = 0;

        bars.forEach((bar, index) => {
            const time = bar.time;

            // Kline
            if (series.kline) {
                klineData.push({
                    time,
                    open: bar.open,
                    high: bar.high,
                    low: bar.low,
                    close: bar.close
                });
            }

            // MAs & Volume
            if (series.ma9) {
                if (isEma) {
                    ema9Prev = ema9;
                    ema9 = ema9 === null ? bar.close : bar.close * k9 + ema9 * (1 - k9);
                    ma9Data.push({ time, value: ema9 });
                } else {
                    ma9Sum += bar.close;
                    if (index >= 9) ma9Sum -= bars[index - 9].close;
                    if (index >= 8) ma9Data.push({ time, value: ma9Sum / 9 });
                    else ma9Data.push({ time });
                }
            }

            if (series.ma21) {
                if (isEma) {
                    ema21Prev = ema21;
                    ema21 = ema21 === null ? bar.close : bar.close * k21 + ema21 * (1 - k21);
                    ma21Data.push({ time, value: ema21 });
                } else {
                    ma21Sum += bar.close;
                    if (index >= 21) ma21Sum -= bars[index - 21].close;
                    if (index >= 20) ma21Data.push({ time, value: ma21Sum / 21 });
                    else ma21Data.push({ time });
                }
            }

            if (series.volume) {
                const color = bar.close >= bar.open ? 'rgba(16, 185, 129, 0.5)' : 'rgba(239, 68, 68, 0.5)';
                volData.push({ time, value: bar.volume, color });
            }

            if (series.volMa) {
                volMaSum += bar.volume;
                if (index >= 10) volMaSum -= bars[index - 10].volume;
                if (index >= 9) volMaData.push({ time, value: volMaSum / 10 });
                else volMaData.push({ time });
            }
            
            // MACD Hist
            if (series.macdHist) {
                const val = bar.macd_hist;
                if (val !== undefined && val !== null) {
                    const color = val >= 0 ? 'rgba(38, 166, 154, 0.8)' : 'rgba(255, 82, 82, 0.8)';
                    macdHistData.push({ time, value: val, color });
                } else {
                    macdHistData.push({ time });
                }
            }

            // MACD Line
            if (series.macdLine) {
                if (bar.macd !== undefined && bar.macd !== null) macdLineData.push({ time, value: bar.macd });
                else macdLineData.push({ time });
            }

            // MACD Signal
            if (series.signalLine) {
                if (bar.macd_signal !== undefined && bar.macd_signal !== null) signalLineData.push({ time, value: bar.macd_signal });
                else signalLineData.push({ time });
            }

            // RSI Line
            if (series.rsiLine) {
                if (bar.rsi !== undefined && bar.rsi !== null) rsiData.push({ time, value: bar.rsi });
                else rsiData.push({ time });
            }

            // RSI Signal
            if (series.rsiSignalLine) {
                if (bar.rsi_ma !== undefined && bar.rsi_ma !== null) rsiSignalData.push({ time, value: bar.rsi_ma });
                else rsiSignalData.push({ time });
            }
        });

        if (series.kline) series.kline.setData(klineData);
        if (series.ma9) series.ma9.setData(ma9Data);
        if (series.ma21) series.ma21.setData(ma21Data);
        if (series.volume) series.volume.setData(volData);
        if (series.volMa) series.volMa.setData(volMaData);
        if (series.macdHist) series.macdHist.setData(macdHistData);
        if (series.macdLine) series.macdLine.setData(macdLineData);
        if (series.signalLine) series.signalLine.setData(signalLineData);
        if (series.rsiLine) series.rsiLine.setData(rsiData);
        if (series.rsiSignalLine) series.rsiSignalLine.setData(rsiSignalData);

        // EMA 模式下把最后一根 bar 的 EMA 值（及其之前的基准值）存进 series，供 updateChartData 增量续算
        if (isEma) {
            series._emaState = {
                ema9, ema21, ema9Prev, ema21Prev,
                lastTime: bars.length ? bars[bars.length - 1].time : null,
            };
        }
    },

    // 实时更新单个数据点
    // 蜡烛/成交量/本地 MA 只依赖 bar（current_bar）即可渲染；indicators 仅供详情图的
    // MACD/RSI 子图（后端 live 帧不含 indicators，缺失时这些子图跳过、蜡烛照常更新）。
    //
    // 返回 { gapDetected }：探测到跳过了整根分钟就上报，由调用方（app.js）节流重取
    // /api/history 用**后端的真 bar** 补齐。本函数一根都不伪造 —— 详见 barGapDetected
    // 上方注释（伪造 flat bar 的旧实现已废弃）。
    updateChartData(series, bar, indicators) {
        if (!bar) return { gapDetected: false };
        indicators = indicators || {};
        const gapDetected = barGapDetected(series._bars, bar.timestamp);
        const time = bar.timestamp;

        // Kline
        if (series.kline) {
            series.kline.update({
                time,
                open: bar.open,
                high: bar.high,
                low: bar.low,
                close: bar.close
            });
        }

        // MAs & Volume 实时计算
        if (series._bars) {
            const normalizedBar = {
                time: bar.timestamp,
                open: bar.open,
                high: bar.high,
                low: bar.low,
                close: bar.close,
                volume: bar.volume
            };

            const lastBar = series._bars[series._bars.length - 1];
            if (lastBar && lastBar.time === time) {
                series._bars[series._bars.length - 1] = normalizedBar;
            } else {
                series._bars.push(normalizedBar);
                if (series._bars.length > 30) series._bars.shift();
            }

            const calcSMA = (period, key = 'close') => {
                if (series._bars.length < period) return null;
                let sum = 0;
                for (let i = series._bars.length - period; i < series._bars.length; i++) {
                    sum += series._bars[i][key];
                }
                return sum / period;
            };

            if (series.maType === 'ema') {
                const st = series._emaState;
                if (st) {
                    // 同一根 forming bar 反复更新：从"上一根收盘时"的基准值重算，不要累加漂移
                    if (st.lastTime !== null && time !== st.lastTime) {
                        st.ema9Prev = st.ema9;
                        st.ema21Prev = st.ema21;
                        st.lastTime = time;
                    }
                    const seed9 = st.ema9Prev !== null && st.ema9Prev !== undefined ? st.ema9Prev : bar.close;
                    const seed21 = st.ema21Prev !== null && st.ema21Prev !== undefined ? st.ema21Prev : bar.close;
                    const k9 = 2 / (9 + 1);
                    const k21 = 2 / (21 + 1);
                    st.ema9 = bar.close * k9 + seed9 * (1 - k9);
                    st.ema21 = bar.close * k21 + seed21 * (1 - k21);
                    if (series.ma9) series.ma9.update({ time, value: st.ema9 });
                    if (series.ma21) series.ma21.update({ time, value: st.ema21 });
                }
            } else {
                if (series.ma9) {
                    const val = calcSMA(9, 'close');
                    if (val !== null) series.ma9.update({ time, value: val });
                }

                if (series.ma21) {
                    const val = calcSMA(21, 'close');
                    if (val !== null) series.ma21.update({ time, value: val });
                }
            }

            if (series.volume) {
                const color = bar.close >= bar.open ? 'rgba(16, 185, 129, 0.5)' : 'rgba(239, 68, 68, 0.5)';
                series.volume.update({ time, value: bar.volume, color });
            }

            if (series.volMa) {
                const val = calcSMA(10, 'volume');
                if (val !== null) series.volMa.update({ time, value: val });
            }
        }

        // MACD Hist
        if (series.macdHist && indicators.macd_hist !== undefined && indicators.macd_hist !== null) {
            const val = indicators.macd_hist;
            const color = val >= 0 ? 'rgba(38, 166, 154, 0.8)' : 'rgba(255, 82, 82, 0.8)';
            series.macdHist.update({ time, value: val, color });
        }

        // MACD Line
        if (series.macdLine && indicators.macd !== undefined && indicators.macd !== null) {
            series.macdLine.update({ time, value: indicators.macd });
        }

        // MACD Signal
        if (series.signalLine && indicators.macd_signal !== undefined && indicators.macd_signal !== null) {
            series.signalLine.update({ time, value: indicators.macd_signal });
        }

        // RSI Line
        if (series.rsiLine && indicators.rsi !== undefined && indicators.rsi !== null) {
            series.rsiLine.update({ time, value: indicators.rsi });
        }

        // RSI Signal
        if (series.rsiSignalLine && indicators.rsi_ma !== undefined && indicators.rsi_ma !== null) {
            series.rsiSignalLine.update({ time, value: indicators.rsi_ma });
        }

        return { gapDetected };
    }
};
