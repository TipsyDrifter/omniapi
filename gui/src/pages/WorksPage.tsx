import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Glyph } from "@/components/make";
import { DateSlug, WorkCard } from "@/components/works/Cards";
import { Lightbox } from "@/components/works/Lightbox";
import { DATE_OPTS, KIND_TABS, KIND_ZH, cellsOf, filterParams, isFiltered, layoutRows, parseFilter, sourceZh, toQuery, type WallFilter } from "@/components/works/wall";
import { useBoard } from "@/store/board";
import { enterWall, leaveWall, loadFacets, loadMore, loadWall, markSeen, useWorks } from "@/store/works";

/* 作品牆 `/works`（1.1-M4）：所有入口（生成頁、Claude Code 透過 MCP、歷史回填）的作品一個地方看。
   滿版、所有模態混排、新的在前，日子之間插日期籤；篩選寫在網址 query。
   `/works/:id` 是同一個元件：牆留著不重掛，上面壓一層燈箱；關掉回到同一面牆、同一個捲動位置。 */
const GAP = 14;
const WALL_KINDS = ["image", "speech", "music", "lyrics", "transcript"];

export default function WorksPage() {
  const { id } = useParams();
  const [sp, setSp] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const f = useMemo(() => parseFilter(sp), [sp]);
  const query = useMemo(() => toQuery(f), [f]);
  const qkey = JSON.stringify(query);
  const search = sp.toString();

  const items = useWorks((s) => s.items);
  const counts = useWorks((s) => s.counts);
  const matching = useWorks((s) => s.matching);
  const next = useWorks((s) => s.next);
  const loading = useWorks((s) => s.loading);
  const loadingMore = useWorks((s) => s.loadingMore);
  const error = useWorks((s) => s.error);
  const facets = useWorks((s) => s.facets);
  const fresh = useWorks((s) => s.fresh);
  const live = useBoard((s) => s.socket);

  // 進牆：頂欄「新 N」歸零；離開：「新」算看過了。頂欄在這頁黏在上面（燈箱壓在它下面）
  useEffect(() => {
    enterWall();
    void loadFacets();
    document.documentElement.classList.add("on-works");
    return () => {
      leaveWall();
      document.documentElement.classList.remove("on-works", "wk-lock");
    };
  }, []);
  useEffect(() => {
    void loadWall(JSON.parse(qkey));
  }, [qkey]);
  // 燈箱開著：牆不捲（捲動位置留著）
  useEffect(() => {
    document.documentElement.classList.toggle("wk-lock", !!id);
    if (id) markSeen(id);
  }, [id]);

  /* ---- 篩選：寫進網址。點選＝新的一筆歷史（上一頁回得去）；打字＝取代 ---- */
  const setFilter = useCallback(
    (patch: Partial<WallFilter>, replace = false) => {
      const nf = { ...parseFilter(sp), ...patch };
      setSp(filterParams(nf), { replace });
      if (!replace) window.scrollTo({ top: 0 });
    },
    [sp, setSp],
  );
  const [qText, setQText] = useState(f.q);
  useEffect(() => setQText(f.q), [f.q]);
  useEffect(() => {
    if (qText.trim() === f.q.trim()) return;
    const t = window.setTimeout(() => setFilter({ q: qText }, true), 300);
    return () => window.clearTimeout(t);
  }, [qText, f.q, setFilter]);

  /* ---- 版面：量容器寬，排成兩端對齊的列 ---- */
  const rowsRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = rowsRef.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const rows = useMemo(() => layoutRows(cellsOf(items), width, GAP), [items, width]);

  /* ---- 捲到底接下一頁 ---- */
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el || next == null) return;
    const io = new IntersectionObserver((es) => es.some((e) => e.isIntersecting) && void loadMore(), { rootMargin: "900px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [next, items.length]);

  /* ---- 燈箱 ---- */
  const open = useCallback(
    (wid: string) => {
      markSeen(wid);
      navigate({ pathname: `/works/${wid}`, search }, { state: { fromWall: true } });
    },
    [navigate, search],
  );
  const fromWall = (location.state as { fromWall?: boolean } | null)?.fromWall === true;
  const close = useCallback(() => {
    if (fromWall) navigate(-1);
    else navigate({ pathname: "/works", search }, { replace: true });
  }, [fromWall, navigate, search]);
  const go = useCallback((nid: string) => navigate({ pathname: `/works/${nid}`, search }, { replace: true, state: location.state }), [navigate, search, location.state]);

  const kindsOf = (k: string) => (k ? k.split(",") : WALL_KINDS);
  const sumOf = (m: Record<string, number>, k: string) => kindsOf(k).reduce((n, x) => n + (m[x] ?? 0), 0);
  const total = WALL_KINDS.reduce((n, k) => n + (counts[k] ?? 0), 0);
  const shownTotal = f.hidden ? facets?.hidden ?? items.length : sumOf(matching, f.kind);
  const pos = useMemo(() => {
    if (!id) return null;
    const i = items.findIndex((x) => x.id === id);
    return i >= 0 ? { i: i + 1, n: Math.max(shownTotal, items.length) } : null;
  }, [id, items, shownTotal]);
  const freshSet = useMemo(() => new Set(fresh), [fresh]);

  const models = facets?.models ?? [];
  const sources = facets?.sources ?? [];
  const modelKnown = !f.model || models.some((m) => (m.value ?? "-") === f.model);

  return (
    <section className="wrap wk">
      <div className="sechead wk-head">
        <h2>
          <span className="ovp" data-t="Works">
            Works
          </span>
        </h2>
        <span className="zh">作品牆</span>
        <span className="dp-sub">所有入口的作品都在這：生成頁、聊天、Claude Code（MCP）、歷史回填</span>
        <div className="wk-count">
          {WALL_KINDS.map((k) => (
            <div key={k} className={counts[k] ? undefined : "zero"}>
              <b className="n">{counts[k] ?? 0}</b>
              <span>
                <Glyph kind={k} size="sm" />
                {KIND_ZH[k]}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="wk-tool">
        <div className="wk-grp" role="group" aria-label="模態">
          {KIND_TABS.map((t) => (
            <button key={t.v || "all"} type="button" className="wk-chip" aria-pressed={f.kind === t.v} onClick={() => setFilter({ kind: t.v })}>
              {t.glyphs.map((g) => (
                <Glyph key={g} kind={g} size="sm" />
              ))}
              {t.zh}
              <i className="n">{sumOf(matching, t.v)}</i>
            </button>
          ))}
        </div>
        <div className="wk-grp" role="group" aria-label="來源">
          <span className="k">來源</span>
          <button type="button" className="wk-chip" aria-pressed={!f.src} onClick={() => setFilter({ src: "" })}>
            全部
          </button>
          {sources.map((s) => (
            <button key={s.value} type="button" className="wk-chip" aria-pressed={f.src === s.value} onClick={() => setFilter({ src: s.value })} title={`${s.n} 件`}>
              {sourceZh(s.value)}
            </button>
          ))}
          {f.src && !sources.some((s) => s.value === f.src) ? (
            <button type="button" className="wk-chip" aria-pressed onClick={() => setFilter({ src: "" })}>
              {sourceZh(f.src)}
            </button>
          ) : null}
        </div>
        <label className="wk-grp">
          <span className="k">模型</span>
          <select className="wk-sel" value={f.model} onChange={(e) => setFilter({ model: e.target.value })} aria-label="模型">
            <option value="">全部模型</option>
            {models.map((m) => (
              <option key={m.value ?? "-"} value={m.value ?? "-"}>
                {m.value ?? "沒有記錄"}（{m.n}）
              </option>
            ))}
            {!modelKnown ? <option value={f.model}>{f.model === "-" ? "沒有記錄" : f.model}</option> : null}
          </select>
        </label>
        <div className="wk-grp" role="group" aria-label="日期">
          <span className="k">日期</span>
          {DATE_OPTS.map((d) => (
            <button key={d.v || "all"} type="button" className="wk-chip" aria-pressed={f.d === d.v} onClick={() => setFilter({ d: d.v })}>
              {d.zh}
            </button>
          ))}
        </div>
        <div className="wk-search">
          <input className="dp-in" type="search" value={qText} onChange={(e) => setQText(e.target.value)} placeholder="搜提示詞、標題、歌詞與逐字稿、檔名、模型" aria-label="搜尋作品" />
        </div>
        <button type="button" className="wk-chip wk-hid" aria-pressed={f.hidden} onClick={() => setFilter({ hidden: !f.hidden })} title="從牆上移除的作品（檔案都還在）">
          已移除<i className="n">{facets?.hidden ?? 0}</i>
        </button>
      </div>

      <div className="wk-live">
        <span className={`wk-dot${live === "open" ? " on" : ""}`} aria-hidden="true" />
        <span>
          <b>{live === "open" ? "即時" : "即時（重新連線中）"}</b>　別的入口剛做好的作品會直接插到最前面並標「新」；不符合目前篩選的只更新數字。
        </span>
        {f.hidden ? (
          <span className="wk-livenote">
            你在看<b>已移除</b>的作品：點開一件可以「放回牆上」。
          </span>
        ) : null}
        {isFiltered(f) ? (
          <button type="button" className="mk-mini wk-clear" onClick={() => setSp(new URLSearchParams())}>
            清掉篩選
          </button>
        ) : null}
      </div>

      {error ? (
        <div className="warn">
          <b>讀不到作品</b>：{error}
        </div>
      ) : null}

      <div className="wk-rows" ref={rowsRef}>
        {rows.map((r, ri) => (
          <div key={ri} className="wk-row" style={{ gap: GAP }}>
            {r.cells.map((c) =>
              c.t === "slug" ? (
                <DateSlug key={`d-${c.day}`} day={c.day} n={c.n} width={c.a * r.h} h={r.h} />
              ) : (
                <WorkCard key={c.w.id} w={c.w} width={c.a * r.h} h={r.h} fresh={freshSet.has(c.w.id)} onOpen={open} />
              ),
            )}
          </div>
        ))}
      </div>

      {!items.length && !loading && !error ? (
        <div className="wk-empty">
          {f.hidden ? (
            <>
              沒有移除的作品
              <small>從牆上移除的作品會收在這裡，檔案都還在。</small>
              <button type="button" className="mk-mini" onClick={() => setFilter({ hidden: false })}>
                回到作品牆
              </button>
            </>
          ) : isFiltered(f) ? (
            <>
              沒有符合的作品
              <small>換個關鍵字、模態或日期試試；或把篩選都清掉。</small>
              <button type="button" className="mk-mini strong" onClick={() => setSp(new URLSearchParams())}>
                清掉篩選
              </button>
            </>
          ) : (
            <>
              牆上還沒有任何作品
              <small>從「生成」做一張圖、一段語音，或在 Claude Code 叫 OmniAPI 的生成工具，做好就會出現在這裡。</small>
            </>
          )}
        </div>
      ) : null}

      <div ref={sentinel} className="wk-more">
        {loading && !items.length ? (
          <span>讀取作品…</span>
        ) : loadingMore ? (
          <span>載入更早的…</span>
        ) : next != null ? (
          <button type="button" className="mk-mini" onClick={() => void loadMore()}>
            載入更早的
          </button>
        ) : items.length ? (
          <span>
            到底了：這面牆共 <span className="n">{items.length}</span> 件
            {!f.hidden && isFiltered(f) ? (
              <>
                （整面牆 <span className="n">{total}</span> 件）
              </>
            ) : null}
          </span>
        ) : null}
      </div>

      {id ? <Lightbox id={id} query={query} pos={pos} onClose={close} onGo={go} /> : null}
    </section>
  );
}
