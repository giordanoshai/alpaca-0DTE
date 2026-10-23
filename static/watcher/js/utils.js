/**
 * 4in1 Watcher - 实用工具函数
 */

// 浏览器 autoplay 解锁 — 首次用户交互时静默预播放音频
let _audioUnlocked = false;
let _lastAlertTime = 0;
const _unlockAudio = () => {
    if (_audioUnlocked) return;
    const audio = document.getElementById('alert-sound-2');
    if (audio) {
        const vol = audio.volume;
        audio.volume = 0;
        audio.play().then(() => { audio.pause(); audio.volume = vol; audio.currentTime = 0; }).catch(() => {});
        _audioUnlocked = true;
    }
};
document.addEventListener('click',   _unlockAudio, { once: true });
document.addEventListener('keydown', _unlockAudio, { once: true });

export const Utils = {
    // 时钟
    startClock(callback) {
        const update = () => {
            const now = new Date();
            const timeStr = now.toLocaleTimeString('en-US', {
                timeZone: 'America/New_York',
                hour12: false,
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            });
            callback(timeStr);
        };
        update();
        setInterval(update, 1000);
    },

    // 通知权限
    requestNotificationPermission() {
        if ('Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }
    },

    // 发送通知
    sendNotification(ticker, data) {
        if (!('Notification' in window) || Notification.permission !== 'granted') return;

        const { signals } = data;
        const parts = [];

        if (signals.bull_pattern) parts.push('🎯 高胜率看涨');
        if (signals.bear_pattern) parts.push('🎯 高胜率看跌');
        if (signals.golden_cross) parts.push('▲ 金叉');
        if (signals.death_cross) parts.push('▼ 死叉');

        if (parts.length > 0) {
            new Notification(`${ticker} 信号`, {
                body: parts.join(' | '),
                icon: '/static/icon.svg',
                tag: ticker,
                renotify: true,
            });
        }
    },

    // 播放提示音（2 秒冷却，防止多 ticker 同时触发互相打断）
    playAlert() {
        const now = Date.now();
        if (now - (_lastAlertTime || 0) < 2000) return;
        _lastAlertTime = now;
        const audio = document.getElementById('alert-sound-2');
        if (audio) {
            audio.currentTime = 0;
            audio.play().catch(e => console.warn('[Sound] play blocked:', e));
        }
    }
};
