import { useEffect, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { headDate } from "@/lib/format";
import { callable, isRetired, soonSunset, usableOf, allModels } from "@/lib/catalog";
import { selectRunningCount, useMake } from "@/store/make";
import { useWorks } from "@/store/works";
import { selectNoKey, useSettings } from "@/store/settings";
import { StatusBits } from "./Footer";
import Sheet from "./Sheet";
import { ThemeSwitch } from "./TopBar";

/* 手機（S 段）的底部分頁列（1.3-M3，D48 第 1 題 A）：看板・費用・聊天｜生成・作品｜更多。
   組間 1px 墨 25 直線（同頂欄的分組）；件數章放在拉丁小字的位置（生成＝滾筒＋件數、作品＝「新 N」），不疊在中文上。
   「更多」升起一張單子：模型、設定（1.3-M4 接通）、主題切換、狀態列原本的內容。
   在模型頁、設定頁時「更多」格標選中，小字換成「模型」「設定」；還沒有任何 key 時小字換成虛線的「缺 key」章。
   其他段整條藏起來（styles/rwd.css）。 */
export default function TabBar() {
  const running = useMake(selectRunningCount);
  const unseen = useWorks((s) => s.unseen);
  const noKey = useSettings(selectNoKey);
  const settings = useSettings((s) => s.settings);
  const models = useSettings((s) => s.models);
  const [more, setMore] = useState(false);
  const { pathname } = useLocation();
  const moreCur = pathname === "/models" ? "模型" : pathname === "/settings" ? "設定" : null;
  useEffect(() => {
    setMore(false);
  }, [pathname]);
  const nOk = settings ? Object.values(settings.providers).filter((p) => callable(usableOf(p))).length : null;
  const live = allModels(models).filter((m) => !isRetired(m));
  return (
    <>
      <nav className="tabbar" aria-label="主導覽（分頁列）">
        <NavLink to="/" end>
          <b>看板</b>
          <small>BOARD</small>
        </NavLink>
        <NavLink to="/costs">
          <b>費用</b>
          <small>LEDGER</small>
        </NavLink>
        <NavLink to="/chat">
          <b>聊天</b>
          <small>CHAT</small>
        </NavLink>
        <i className="tab-sep" aria-hidden="true" />
        <NavLink to="/make" aria-label={running ? `生成（${running} 件生成中）` : undefined}>
          <b>生成</b>
          {running ? (
            <em className="navct">
              <span className="drum" aria-hidden="true" />
              {running}
            </em>
          ) : (
            <small>MAKE</small>
          )}
        </NavLink>
        <NavLink to="/works" aria-label={unseen ? `作品（新做好 ${unseen} 件）` : undefined}>
          <b>作品</b>
          {unseen ? <em className="navct new">新 {unseen}</em> : <small>WORKS</small>}
        </NavLink>
        <i className="tab-sep" aria-hidden="true" />
        <button type="button" aria-expanded={more} aria-haspopup="dialog" aria-current={moreCur ? "page" : undefined} onClick={() => setMore(true)}>
          <b>更多</b>
          {noKey ? <em className="navct need">缺 key</em> : <small>{moreCur ?? "MORE"}</small>}
        </button>
      </nav>
      <Sheet open={more} onClose={() => setMore(false)} label="更多">
        <Link className="sheet-it" to="/models" onClick={() => setMore(false)}>
          <b>模型</b>
          <small>MODELS</small>
          <span className="r">{models ? `${live.length} 個 · ${live.filter(soonSunset).length} 個快下架` : null}</span>
        </Link>
        <Link className="sheet-it" to="/settings" onClick={() => setMore(false)}>
          <b>設定</b>
          <small>SETTINGS</small>
          <span className="r">{noKey ? <em className="navct need">缺 key</em> : nOk != null ? `${nOk}／${Object.keys(settings!.providers).length} 家能用` : null}</span>
        </Link>
        <div className="sheet-it">
          <b>主題</b>
          <small>THEME</small>
          <span className="r">
            <ThemeSwitch label="" />
          </span>
        </div>
        <div className="sheet-status">
          <StatusBits s />
          <span className="n">{headDate()}</span>
        </div>
      </Sheet>
    </>
  );
}
