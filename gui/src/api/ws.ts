/* WebSocket 客戶端：連 /ws、自動重連、斷線後用 /api/events?since_seq 補差（決策記錄 M4-c）。
   daemon 端每則匯流排事件都有遞增 seq；我們記住最後一個 seq，重連時先補漏再繼續。
   匯流排只保留 500 筆歷史，補不回來時丟出 "resync"，由 store 重抓 runs。 */
import { api } from "./client";
import type { BusEvent, WsMessage } from "./types";

export type SocketState = "connecting" | "open" | "closed";

export interface BoardSocketHandlers {
  onEvent: (e: BusEvent) => void;
  onState?: (s: SocketState) => void;
  /** 補差失敗或漏太多：呼叫端應整批重抓 */
  onResync?: () => void;
}

const MIN_BACKOFF = 800;
const MAX_BACKOFF = 10_000;

export class BoardSocket {
  private ws: WebSocket | null = null;
  private lastSeq = 0;
  private backoff = MIN_BACKOFF;
  private timer: number | null = null;
  private stopped = false;
  private everConnected = false;

  constructor(private h: BoardSocketHandlers) {}

  get seq(): number {
    return this.lastSeq;
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) window.clearTimeout(this.timer);
    this.timer = null;
    this.ws?.close();
    this.ws = null;
  }

  private url(): string {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws`;
  }

  private connect(): void {
    if (this.stopped) return;
    this.h.onState?.("connecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.backoff = MIN_BACKOFF;
      this.h.onState?.("open");
      // 重連：補回斷線期間漏掉的事件
      if (this.everConnected) void this.catchUp();
      this.everConnected = true;
    };
    ws.onmessage = (m) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(m.data as string) as WsMessage;
      } catch {
        return;
      }
      if (msg.type === "hello" || msg.type === "ping") return;
      this.take(msg as BusEvent);
    };
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      this.h.onState?.("closed");
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      /* onclose 會接著來 */
    };
  }

  private take(e: BusEvent): void {
    if (typeof e.seq === "number") {
      if (e.seq <= this.lastSeq) return; // 補差與即時重疊時去重
      this.lastSeq = e.seq;
    }
    this.h.onEvent(e);
  }

  private async catchUp(): Promise<void> {
    try {
      const missed = await api.events(this.lastSeq, 500);
      // daemon 重啟後 seq 會歸零：拿到的第一筆 seq 比我們記的小，代表不是同一條匯流排
      if (missed.length && missed[0].seq <= this.lastSeq && this.lastSeq > 0) {
        this.lastSeq = 0;
        this.h.onResync?.();
        return;
      }
      // 剛好拿滿 500 筆：可能還有更多漏掉的，保守起見整批重抓
      if (missed.length >= 500) {
        for (const e of missed) this.take(e);
        this.h.onResync?.();
        return;
      }
      for (const e of missed) this.take(e);
    } catch {
      this.h.onResync?.();
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.timer) return;
    const wait = this.backoff;
    this.backoff = Math.min(MAX_BACKOFF, Math.round(this.backoff * 1.7));
    this.timer = window.setTimeout(() => {
      this.timer = null;
      this.connect();
    }, wait);
  }
}
