/* 挑模型的篩選列（v1.4）：新對話頁／派工頁／聊天換模型（dispatch/ModelPicker）、生成頁四種表單（make/bits ModelList）、
   影片表單（make/VideoForm）共用同一組狀態與同一排控制項。
   預設＝熱門序：照 Artificial Analysis 各榜的名次（後端 catalog/popularity.py 給的 rank），不分廠商一條扁平清單；
   廠商是篩選條件（可多選），不是分組。排序切到「廠商」才按廠商分段（舊的看法，留給想要的人）。
   狀態不存網址、不存 localStorage：每次進頁都是熱門序、什麼都沒篩。 */
import { useDeferredValue, useMemo, useState, type ReactNode } from "react";
import type { ModelEntry, RankBadgeInfo } from "@/api/types";
import { priceMain } from "@/lib/catalog";

export type ModelSort = "popular" | "vendor" | "price";
export const SORT_LABEL: Record<ModelSort, string> = { popular: "熱門", vendor: "廠商", price: "價格" };

/** 篩得動的最小形狀（ModelEntry、GenModel 都符合） */
export interface FilterableModel {
  id: string;
  provider: string;
  name?: string;
  status: string;
  vendor?: string;
  vendor_label?: string;
  rank?: number;
  popularity?: Record<string, number>;
  rank_badge?: RankBadgeInfo;
  pricing?: unknown;
}

/** 一顆能力 chip：看圖、改圖、要首幀… */
export interface CapFilter<M> {
  key: string;
  label: string;
  test: (m: M) => boolean;
  title?: string;
}

export interface ModelFilterOptions<M> {
  /** 能力 chips（依模態；沒有就不出那一排） */
  caps?: CapFilter<M>[];
  /** 有給才出「只看可用」 */
  usable?: (m: M) => boolean;
  /** 「顯示全部（含未整理）」：預設只列整理過的（status 不是 discovered）。all／onAll 給了就是受控的 */
  all?: boolean;
  onAll?: (v: boolean) => void;
  /** 選中的那個永遠留在清單裡（搜尋、篩選都不把它藏起來） */
  keep?: string | null;
  /** 現在的順序（沒上榜的之間、同名次之間照它） */
  baseOrder?: (a: M, b: M) => number;
  /** 不管怎麼排都墊底的（影片：原廠已關閉） */
  sink?: (m: M) => boolean;
  /** 「價格」排序的鍵；預設取模型頁同一個主價格 */
  priceKey?: (m: M) => number | null;
  /** 直連供應商的顯示名（/api/models 的 providers）；沒有就用內建的那幾家 */
  providerLabels?: Record<string, { label?: string } | undefined>;
  /** 「廠商」排序的先後（目錄裡供應商的順序）；沒列到的照名稱 */
  providerOrder?: string[];
  /** 搜尋比對的字串；預設 id、名稱、供應商、原廠 */
  haystack?: (m: M) => string;
}

const KNOWN_LABEL: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  google: "Google",
  deepseek: "DeepSeek",
  openrouter: "OpenRouter",
  elevenlabs: "ElevenLabs",
  kie: "kie.ai (Suno)",
  "x-ai": "xAI",
  "z-ai": "Z.ai",
  qwen: "Qwen",
  meta: "Meta",
  "meta-llama": "Meta",
  moonshotai: "Moonshot",
  mistralai: "Mistral",
  minimax: "MiniMax",
  "black-forest-labs": "Black Forest Labs",
  bytedance: "ByteDance",
  "bytedance-seed": "ByteDance",
  microsoft: "Microsoft",
  alibaba: "Alibaba",
  kwaivgi: "Kling",
  runway: "Runway",
  echo: "echo",
};

/** 原廠（AA 的 creator）：經 OpenRouter 的看名單寫的原廠或 id 前綴，直連的就是供應商本身 */
export function vendorOf(m: FilterableModel, labels?: ModelFilterOptions<FilterableModel>["providerLabels"]): { id: string; label: string } {
  let id = m.provider;
  let label: string | undefined;
  if (m.vendor) {
    id = m.vendor;
    label = m.vendor_label;
  } else if (m.provider === "openrouter" && m.id.includes("/")) id = m.id.replace(/^~/, "").split("/")[0];
  // 經 OpenRouter 的同一家（openai/gpt-image-2）跟直連的那家（openai）併成同一顆 chip
  if (id === "bytedance-seed") id = "bytedance";
  if (id === "meta-llama") id = "meta";
  return { id, label: labels?.[id]?.label ?? KNOWN_LABEL[id] ?? label ?? id };
}

const defaultHay = (m: FilterableModel) => `${m.id} ${m.name ?? ""} ${m.provider} ${m.vendor_label ?? ""} ${m.vendor ?? ""}`.toLowerCase();
const defaultPrice = (m: FilterableModel) => priceMain(m as unknown as ModelEntry)?.key ?? null;
/** 同樣是榜首（文生影片第一、圖生影片第一）時，看另一張榜 */
const secondRank = (m: FilterableModel) => {
  const rs = Object.values(m.popularity ?? {}).sort((a, b) => a - b);
  return rs.length > 1 ? rs[1] : Infinity;
};

/** 只排一次，不篩：模型頁那類自己有篩選的地方用 */
export function byPopularity<M extends FilterableModel>(a: M, b: M): number {
  return (a.rank ?? Infinity) - (b.rank ?? Infinity) || secondRank(a) - secondRank(b);
}

export function useModelFilter<M extends FilterableModel>(models: M[], o: ModelFilterOptions<M> = {}) {
  const [q, setQ] = useState("");
  const dq = useDeferredValue(q.trim().toLowerCase());
  const [vendors, setVendors] = useState<string[]>([]);
  const [caps, setCaps] = useState<string[]>([]);
  const [usableOnly, setUsableOnly] = useState(false);
  const [allOwn, setAllOwn] = useState(false);
  const [sort, setSort] = useState<ModelSort>("popular");
  const all = o.all ?? allOwn;
  const setAll = (v: boolean) => (o.onAll ? o.onAll(v) : setAllOwn(v));
  const hay = (o.haystack ?? defaultHay) as (m: M) => string;
  const capDefs = o.caps ?? [];
  const vOf = (m: M) => vendorOf(m, o.providerLabels);

  const r = useMemo(() => {
    const curated = models.filter((m) => m.status !== "discovered");
    const pool = all ? models : curated;
    const passQ = (m: M) => !dq || hay(m).includes(dq);
    const passU = (m: M) => !usableOnly || !o.usable || o.usable(m);
    const passC = (m: M, extra?: string) => [...caps, ...(extra ? [extra] : [])].every((k) => capDefs.find((c) => c.key === k)?.test(m) ?? true);
    const passV = (m: M) => !vendors.length || vendors.includes(vOf(m).id);
    // base＝搜尋＋只看可用＋顯示全部；廠商的數字＝base∩能力，能力的數字＝base∩廠商∩（已選能力＋這一顆）
    const base = pool.filter((m) => passQ(m) && passU(m));
    const counts = new Map<string, { id: string; label: string; count: number }>();
    for (const m of base) {
      if (!passC(m)) continue;
      const v = vOf(m);
      const c = counts.get(v.id) ?? { ...v, count: 0 };
      c.count++;
      counts.set(v.id, c);
    }
    for (const id of vendors) if (!counts.has(id)) counts.set(id, { id, label: o.providerLabels?.[id]?.label ?? KNOWN_LABEL[id] ?? id, count: 0 });
    const vendorChips = [...counts.values()].sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
    const capChips = capDefs.map((c) => ({ key: c.key, label: c.label, title: c.title, count: base.filter((m) => passV(m) && passC(m, c.key)).length }));
    let rows = base.filter((m) => passV(m) && passC(m));
    const kept = o.keep ? models.find((m) => m.id === o.keep) : undefined;
    if (kept && !rows.includes(kept)) rows = [kept, ...rows];
    // 排序：墊底的、再照模式；同分照現在的順序、再照原本出現的先後
    const idx = new Map(models.map((m, i) => [m, i]));
    const order = o.providerOrder ?? [];
    const vrank = (m: M) => {
      const v = vOf(m);
      const i = order.indexOf(v.id);
      return i < 0 ? order.length : i;
    };
    const price = (o.priceKey ?? defaultPrice) as (m: M) => number | null;
    const tie = (a: M, b: M) => (o.baseOrder ? o.baseOrder(a, b) : 0) || (idx.get(a) ?? 0) - (idx.get(b) ?? 0);
    const sunk = (m: M) => (o.sink?.(m) ? 1 : 0);
    const cmp =
      sort === "popular"
        ? (a: M, b: M) => sunk(a) - sunk(b) || byPopularity(a, b) || tie(a, b)
        : sort === "vendor"
          ? (a: M, b: M) => sunk(a) - sunk(b) || vrank(a) - vrank(b) || vOf(a).label.localeCompare(vOf(b).label) || tie(a, b)
          : (a: M, b: M) => sunk(a) - sunk(b) || (price(a) ?? Infinity) - (price(b) ?? Infinity) || tie(a, b);
    const shown = [...rows].sort(cmp);
    // 「顯示全部」沒開時，搜得到但藏在未整理裡的筆數（搜尋落空時告訴使用者東西在哪）
    const hiddenByAll = all || !dq ? 0 : models.filter((m) => m.status === "discovered" && passQ(m)).length;
    const groupCount = new Map<string, number>();
    if (sort === "vendor") for (const m of shown) groupCount.set(vOf(m).id, (groupCount.get(vOf(m).id) ?? 0) + 1);
    return { shown, vendorChips, capChips, curatedCount: curated.length, baseCount: base.filter((m) => passC(m)).length, hiddenByAll, groupCount };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models, all, dq, usableOnly, caps, vendors, sort, o.keep, o.usable, o.providerLabels, o.providerOrder, capDefs]);

  const toggle = (arr: string[], v: string) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);
  return {
    ...r,
    q,
    setQ,
    vendors,
    toggleVendor: (id: string) => setVendors((v) => toggle(v, id)),
    clearVendors: () => setVendors([]),
    caps,
    toggleCap: (k: string) => setCaps((c) => toggle(c, k)),
    usableOnly,
    setUsableOnly,
    hasUsable: !!o.usable,
    all,
    setAll,
    /** 未整理的有幾個（0＝不必出「顯示全部」） */
    discovered: models.length - r.curatedCount,
    total: models.length,
    sort,
    setSort,
    vendorOf: vOf,
    /** 「廠商」排序時第 i 列前面要不要插段落標題 */
    head: (rows: M[], i: number): { id: string; label: string; count: number } | null => {
      if (sort !== "vendor") return null;
      const v = vOf(rows[i]);
      if (i > 0 && vOf(rows[i - 1]).id === v.id) return null;
      return { ...v, count: r.groupCount.get(v.id) ?? 0 };
    },
    filtered: vendors.length + caps.length + (usableOnly ? 1 : 0) + (dq ? 1 : 0) > 0,
  };
}

export type ModelFilter<M extends FilterableModel = FilterableModel> = ReturnType<typeof useModelFilter<M>>;

/** 廠商 chips 一次只列這麼多（其餘收在「＋N 家」裡，點開才全列） */
const VENDOR_FOLD = 8;

/** 篩選列本體：搜尋＋顯示全部＋筆數／排序＋只看可用／廠商（多選）／能力。
   allAlways：「顯示全部」永遠出現（新對話頁：OpenRouter 也在裡面）；否則只在有未整理的模型時出現 */
export function ModelFilterBar<M extends FilterableModel>({
  f,
  placeholder = "搜尋 id／名稱／廠商",
  allAlways = false,
  allLabel = "顯示全部（含未整理）",
  count,
  tail,
  disabled,
}: {
  f: ModelFilter<M>;
  placeholder?: string;
  allAlways?: boolean;
  allLabel?: string;
  /** 筆數旁的補充（「，列前 200」） */
  count?: ReactNode;
  /** 排序那一排最右邊多放的東西（影片的「比較全部 →」） */
  tail?: ReactNode;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const chips = f.vendorChips;
  const visible = open || chips.length <= VENDOR_FOLD + 1 ? chips : chips.filter((c, i) => i < VENDOR_FOLD || f.vendors.includes(c.id));
  const folded = chips.length - visible.length;
  return (
    <div className="mf-bar">
      <div className="mf-top">
        <input type="search" className="dp-in" placeholder={placeholder} value={f.q} onChange={(e) => f.setQ(e.target.value)} aria-label="搜尋模型" disabled={disabled} />
        <span className="dp-count">
          <span className="n">{f.shown.length}</span> 筆{count}
        </span>
      </div>
      <div className="mf-row" role="group" aria-label="排序與範圍">
        <span className="k">排序</span>
        <div className="mk-seg mf-seg" role="group" aria-label="排序">
          {(Object.keys(SORT_LABEL) as ModelSort[]).map((s) => (
            <button key={s} type="button" aria-pressed={f.sort === s} onClick={() => f.setSort(s)} disabled={disabled}>
              {SORT_LABEL[s]}
            </button>
          ))}
        </div>
        {f.hasUsable ? (
          <button type="button" className="wk-chip" aria-pressed={f.usableOnly} onClick={() => f.setUsableOnly(!f.usableOnly)} disabled={disabled}>
            只看可用
          </button>
        ) : null}
        {allAlways || f.discovered > 0 ? (
          <button type="button" className="wk-chip" aria-pressed={f.all} onClick={() => f.setAll(!f.all)} disabled={disabled} title={f.discovered ? `另有 ${f.discovered} 個還沒整理的` : undefined}>
            {allLabel}
          </button>
        ) : null}
        {tail}
      </div>
      {chips.length > 1 || f.vendors.length ? (
        <div className="dp-vendors mf-vendors" role="group" aria-label="廠商（可多選）">
          <button type="button" aria-pressed={!f.vendors.length} onClick={f.clearVendors} disabled={disabled}>
            全部 <span className="n">{f.baseCount}</span>
          </button>
          {visible.map((v) => (
            <button key={v.id} type="button" aria-pressed={f.vendors.includes(v.id)} onClick={() => f.toggleVendor(v.id)} disabled={disabled} data-vendor={v.id}>
              {v.label} <span className="n">{v.count}</span>
            </button>
          ))}
          {folded > 0 || open ? (
            <button type="button" className="mf-more" aria-expanded={open} onClick={() => setOpen(!open)} disabled={disabled}>
              {open ? "收起" : `＋${folded} 家`}
            </button>
          ) : null}
        </div>
      ) : null}
      {f.capChips.length ? (
        <div className="mf-row mf-caps" role="group" aria-label="能力">
          <span className="k">只看</span>
          {f.capChips.map((c) => (
            <button key={c.key} type="button" className="wk-chip" aria-pressed={f.caps.includes(c.key)} onClick={() => f.toggleCap(c.key)} title={c.title} disabled={disabled} data-cap={c.key}>
              {c.label} <i>{c.count}</i>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** 「廠商」排序時的段落標題（黏在清單頂端） */
export function VendorHead({ label, count }: { label: string; count: number }) {
  return (
    <div className="dp-mgroup" role="presentation">
      <span className="zh">{label}</span>
      <span className="n">{count}</span>
    </div>
  );
}

/** 名次徽章：「AA #3」，滑過看榜名與分數；猜的對應沒有 rank_badge，就不出 */
export function RankBadge({ m }: { m: { rank_badge?: RankBadgeInfo } }) {
  const b = m.rank_badge;
  if (!b) return null;
  const score = b.score == null ? "" : b.board === "text" ? `，指數 ${b.score}` : `，Elo ${Math.round(b.score)}`;
  return (
    <span className={`aa-badge${b.rank <= 3 ? " aa-top" : ""}`} title={`Artificial Analysis ${b.label}榜第 ${b.rank} 名${score}`} data-rank={b.rank}>
      AA #{b.rank}
    </span>
  );
}
