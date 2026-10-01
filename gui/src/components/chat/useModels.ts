import { useEffect, useMemo, useState } from "react";
import { ApiError, api } from "@/api/client";
import type { ModelEntry, ModelsResponse } from "@/api/types";
import { TIERS, flattenModels, indexModels, resolveModel, type ModelSel } from "@/components/dispatch";

/* 文字模型清單：整個分頁共用一份（切對話不重抓） */
let cache: ModelsResponse | null = null;
let pending: Promise<ModelsResponse> | null = null;

export interface ChatModels {
  data: ModelsResponse | null;
  error: string | null;
  list: ModelEntry[];
  idx: Map<string, ModelEntry>;
}

export function useModels(): ChatModels {
  const [data, setData] = useState<ModelsResponse | null>(cache);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (cache) return;
    let alive = true;
    pending ??= api.models("text");
    pending
      .then((r) => {
        cache = r;
        if (alive) setData(r);
      })
      .catch((e: unknown) => {
        pending = null;
        if (alive) setError(e instanceof ApiError || e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);
  const list = useMemo(() => flattenModels(data), [data]);
  const idx = useMemo(() => indexModels(list), [list]);
  return { data, error, list, idx };
}

/** 模型字串（等級別名或 id）→ ModelPicker 的選取值 */
export const toSel = (model: string): ModelSel => ((TIERS as readonly string[]).includes(model) ? { kind: "tier", tier: model } : { kind: "id", id: model });

/** 模型字串 → 送出字串、實際 id（等級別名才會不同）、目錄那筆 */
export function resolveChatModel(model: string, m: ChatModels) {
  return resolveModel(toSel(model), m.data?.tiers ?? {}, m.idx);
}
