/* 主題倉庫（決策記錄 D25）：D3 預設，I2 候選。
   換主題＝改 <html data-theme>，元件只吃語意 token（見 d3.css 註解）。 */
import { useSyncExternalStore } from "react";

export type ThemeId = "d3" | "i2";

export interface ThemeMeta {
  id: ThemeId;
  /** 主題全名（中文） */
  name: string;
  /** 短代號，放在切換鈕上 */
  short: string;
  /** 一句話 */
  note: string;
}

export const THEMES: ThemeMeta[] = [
  { id: "d3", name: "孔版雙色疊印", short: "D3", note: "3＋1 色：粉＝Claude Code、藍＝Codex、黃＝Gemini CLI，墨負責其他一切" },
  { id: "i2", name: "文庫組版 × 京の色", short: "I2", note: "候選主題：日本傳統色帳對到同一套 token（柿・錆青磁・深支子）" },
];

const KEY = "omniapi.theme";
const DEFAULT: ThemeId = "d3";
const listeners = new Set<() => void>();

function read(): ThemeId {
  const attr = document.documentElement.getAttribute("data-theme");
  return THEMES.some((t) => t.id === attr) ? (attr as ThemeId) : DEFAULT;
}

export function getTheme(): ThemeId {
  return read();
}

export function setTheme(id: ThemeId): void {
  document.documentElement.setAttribute("data-theme", id);
  try {
    localStorage.setItem(KEY, id);
  } catch {
    /* private mode 等情況：不記住也沒關係 */
  }
  listeners.forEach((l) => l());
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useTheme(): [ThemeId, (id: ThemeId) => void] {
  const id = useSyncExternalStore(subscribe, read, () => DEFAULT);
  return [id, setTheme];
}
