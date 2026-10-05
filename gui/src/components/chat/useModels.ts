import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { ApiError, api } from "@/api/client";
import type { ModelEntry, ModelsResponse } from "@/api/types";
import { TIERS, flattenModels, indexModels, resolveModel, type ModelSel } from "@/components/dispatch";

/* 文字模型清單：整個分頁共用一份（切對話不重抓）。
   1.3-M4：設定頁改了等級別名、key（settings.changed／catalog.refreshed）時作廢重抓，聊天與派工的等級對應跟著變。 */
let cache: ModelsResponse | null = null;
let pending: Promise<ModelsResponse> | null = null;
let version = 0;
const listeners = new Set<() => void>();

/** 設定改了：下一次用到時重抓（已掛著的元件馬上重抓） */
export function invalidateTextModels(): void {
  cache = null;
  pending = null;
  version++;
  listeners.forEach((l) => l());
}

export interface ChatModels {
  data: ModelsResponse | null;
  error: string | null;
  list: ModelEntry[];
  idx: Map<string, ModelEntry>;
}

export function useModels(): ChatModels {
  const v = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => version,
  );
  const [data, setData] = useState<ModelsResponse | null>(cache);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (cache) {
      setData(cache);
      return;
    }
    let alive = true;
    pending ??= api.models("text");
    const p = pending;
    p.then((r) => {
      if (p === pending) cache = r;
      if (alive) {
        setData(r);
        setError(null);
      }
    }).catch((e: unknown) => {
      if (p === pending) pending = null;
      if (alive) setError(e instanceof ApiError || e instanceof Error ? e.message : String(e));
    });
    return () => {
      alive = false;
    };
  }, [v]);
  const list = useMemo(() => flattenModels(data), [data]);
  const idx = useMemo(() => indexModels(list), [list]);
  return { data, error, list, idx };
}

/** 等級別名的名字：照 /api/models 的 tiers（沒讀到時用內建的三個） */
export const tierNames = (data: ModelsResponse | null): string[] => (data?.tiers ? Object.keys(data.tiers) : [...TIERS]);

/** 模型字串（等級別名或 id）→ ModelPicker 的選取值 */
export const toSel = (model: string, data: ModelsResponse | null = cache): ModelSel => (tierNames(data).includes(model) ? { kind: "tier", tier: model } : { kind: "id", id: model });

/** 模型字串 → 送出字串、實際 id（等級別名才會不同）、目錄那筆 */
export function resolveChatModel(model: string, m: ChatModels) {
  return resolveModel(toSel(model, m.data), m.data?.tiers ?? {}, m.idx);
}
