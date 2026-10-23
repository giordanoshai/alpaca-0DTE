/**
 * 4in1 Watcher - WebSocket 管理器
 */

export class WSManager {
    constructor(onMessage, onStatusChange) {
        this.onMessage = onMessage;
        this.onStatusChange = onStatusChange;
        this.ws = null;
        this.pingTimer = null;
    }

    connect() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${location.host}/watcher/ws`;

        try {
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                this.onStatusChange(true);
                this.startPing();
            };

            this.ws.onclose = () => {
                this.onStatusChange(false);
                this.stopPing();
                setTimeout(() => this.connect(), 3000);
            };

            this.ws.onerror = () => {
                this.onStatusChange(false);
            };

            this.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'pong') return;
                    this.onMessage(data);
                } catch (e) {
                    console.error('WebSocket 消息解析失败', e);
                }
            };
        } catch (e) {
            console.error('WebSocket 连接异常', e);
            setTimeout(() => this.connect(), 5000);
        }
    }

    startPing() {
        this.stopPing();
        this.pingTimer = setInterval(() => {
            if (this.ws?.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 30000);
    }

    stopPing() {
        if (this.pingTimer) {
            clearInterval(this.pingTimer);
            this.pingTimer = null;
        }
    }

    send(data) {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(data));
        }
    }

    subscribe(ticker) {
        this.send({ type: 'subscribe', ticker });
    }

    unsubscribe(ticker) {
        this.send({ type: 'unsubscribe', ticker });
    }
}
