/* 送出鍵設定（決策記錄 D40）：Ctrl+Enter（預設）或 Enter 直接送出。
   聊天、追問、派工、生成頁的輸入框共用這一個設定與這一個判斷函式；記在這個瀏覽器（localStorage），
   別的分頁改了也跟著變（storage 事件）。之後有設定頁會搬過去。 */
import { useSyncExternalStore } from "react";
import { isTouch } from "./rwd";

export type SendKey = "ctrl" | "enter";

const KEY = "omniapi.sendKey";
const DEFAULT: SendKey = "ctrl";
const listeners = new Set<() => void>();

function read(): SendKey {
  try {
    return localStorage.getItem(KEY) === "enter" ? "enter" : DEFAULT;
  } catch {
    return DEFAULT;
  }
}

let current: SendKey = typeof window === "undefined" ? DEFAULT : read();

export function getSendKey(): SendKey {
  return current;
}

export function setSendKey(k: SendKey): void {
  current = k;
  try {
    localStorage.setItem(KEY, k);
  } catch {
    /* private mode 等情況：這個分頁照樣生效，只是不記住 */
  }
  listeners.forEach((l) => l());
}

// 別的分頁改了：storage 事件只送到「其他」分頁
if (typeof window !== "undefined") {
  window.addEventListener("storage", (e) => {
    if (e.key !== KEY && e.key !== null) return;
    const next = read();
    if (next === current) return;
    current = next;
    listeners.forEach((l) => l());
  });
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useSendKey(): [SendKey, (k: SendKey) => void] {
  const k = useSyncExternalStore(subscribe, getSendKey, () => DEFAULT);
  return [k, setSendKey];
}

/** 鍵盤事件的最小形狀（React 的合成事件與原生事件都吃） */
interface KeyLike {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  nativeEvent?: { isComposing?: boolean; keyCode?: number };
  isComposing?: boolean;
  keyCode?: number;
}

/** 這一下是不是「送出」。
 *  - 輸入法選字中（注音、倉頡、日文…）的 Enter 一律不是：isComposing，或舊瀏覽器的 keyCode 229。
 *  - Ctrl／⌘＋Enter 在兩種模式都送出（選了 Enter 的人，舊習慣的手也不會失靈）。
 *  - Enter 模式：單按 Enter 送出、Shift+Enter 換行。plain＝false 的地方（例如表單裡的單行欄位）單按 Enter 不送。 */
export function isSendKey(e: KeyLike, opts: { mode?: SendKey; plain?: boolean } = {}): boolean {
  if (e.key !== "Enter") return false;
  const n = e.nativeEvent ?? e;
  if (n.isComposing || n.keyCode === 229) return false;
  if (e.ctrlKey || e.metaKey) return true;
  // 1.3-M3：觸控裝置的軟鍵盤 Enter 一律換行，按鈕送出（外接鍵盤的 Ctrl+Enter 照樣送）
  if (isTouch()) return false;
  const mode = opts.mode ?? current;
  return mode === "enter" && opts.plain !== false && !e.shiftKey && !e.altKey;
}

/** 表單層級的鍵盤處理（派工、生成頁）：Enter 模式下單按 Enter 只在標了 data-enter-sends 的欄位送出
 *  （主要的 prompt 欄）；其他欄位（標題、歌詞、按鈕…）單按 Enter 照舊，Ctrl+Enter 在哪都送。 */
export function enterSends(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && target.dataset.enterSends !== undefined;
}

/** 生成頁表單的 onKeyDown：送出鍵照設定（取代原本寫死的 Ctrl+Enter） */
export const onSendKey =
  (fn: () => void) =>
  (e: KeyLike & { target: EventTarget | null; preventDefault: () => void }): void => {
    if (isSendKey(e, { plain: enterSends(e.target) })) {
      e.preventDefault();
      fn();
    }
  };

/** 提示字：「Ctrl+Enter 送出」／「Enter 送出，Shift+Enter 換行」 */
export function sendHint(mode: SendKey = current): string {
  if (isTouch()) return "按鈕送出";
  return mode === "enter" ? "Enter 送出，Shift+Enter 換行" : "Ctrl+Enter 送出";
}

/** 按鈕的 title 用的短版 */
export function sendKeyName(mode: SendKey = current): string {
  return mode === "enter" ? "Enter" : "Ctrl+Enter";
}
