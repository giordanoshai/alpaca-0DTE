/**
 * 4in1 Watcher - API 服务
 */

export const ApiService = {
    async loadWatchlist() {
        const res = await fetch('/api/watchlist');
        return await res.json();
    },

    async addTicker(symbol) {
        const res = await fetch(`/api/watchlist?ticker=${symbol}`, { method: 'POST' });
        return await res.json();
    },

    async removeTicker(symbol) {
        const res = await fetch(`/api/watchlist/${symbol}`, { method: 'DELETE' });
        return await res.json();
    },

    async fetchHistory(symbol, interval = '5m', limit = null) {
        const q = limit ? `&limit=${limit}` : '';
        const res = await fetch(`/api/history/${symbol}?interval=${interval}${q}`);
        return await res.json();
    },

    async getWatcherStatus() {
        const res = await fetch('/api/watcher/status');
        return await res.json();
    },

    async setWatcherStatus(running) {
        const res = await fetch('/api/watcher/status', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ running })
        });
        return await res.json();
    }
};
