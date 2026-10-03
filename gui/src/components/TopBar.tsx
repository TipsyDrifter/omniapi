import { Link, NavLink } from "react-router-dom";
import { THEMES, useTheme } from "@/themes";
import { headDate } from "@/lib/format";
import { selectRunningCount, useMake } from "@/store/make";
import { useWorks } from "@/store/works";

/* 頂欄：wordmark（唯一允許疊印的地方之一）、導覽（三組：看板・費用・聊天｜生成・作品｜模型・設定）、主題切換、日期、「＋新對話」。
   「生成」旁的小章＝進行中的生成件數（0 件不顯示） */
export default function TopBar() {
  const [theme, setTheme] = useTheme();
  const running = useMake(selectRunningCount);
  const unseen = useWorks((s) => s.unseen);
  return (
    <header className="top wrap">
      <div className="logo">
        <span className="ovp" data-t="OmniAPI">
          OmniAPI
        </span>
        <small>派工中心</small>
      </div>
      <nav aria-label="主導覽">
        <NavLink to="/" end>
          看板<span>BOARD</span>
        </NavLink>
        <NavLink to="/costs">
          費用<span>LEDGER</span>
        </NavLink>
        <NavLink to="/chat">
          聊天<span>CHAT</span>
        </NavLink>
        <i className="navsep" aria-hidden="true" />
        <NavLink to="/make">
          生成<span>MAKE</span>
          {running ? (
            <em className="navct" title={`${running} 件生成中`}>
              <span className="drum" aria-hidden="true" />
              {running}
            </em>
          ) : null}
        </NavLink>
        <NavLink to="/works">
          作品<span>WORKS</span>
          {unseen ? (
            <em className="navct new" title={`離開作品牆後新做好 ${unseen} 件`}>
              新 {unseen}
            </em>
          ) : null}
        </NavLink>
        <i className="navsep" aria-hidden="true" />
        <a href="#" aria-disabled="true" title="v1.x" onClick={(e) => e.preventDefault()}>
          模型<span>MODELS</span>
        </a>
        <a href="#" aria-disabled="true" title="v1.x" onClick={(e) => e.preventDefault()}>
          設定<span>SETTINGS</span>
        </a>
      </nav>
      <div className="top-r">
        <div className="sim" role="group" aria-label="主題">
          <span className="k">主題</span>
          {THEMES.map((t) => (
            <button key={t.id} type="button" aria-pressed={theme === t.id} title={`${t.name}：${t.note}`} onClick={() => setTheme(t.id)}>
              {t.short}
            </button>
          ))}
        </div>
        <span className="date">{headDate()}</span>
        {__OMNI_SANDBOX__ ? <span className="sample" title="連的是隔離的測試 daemon（7799），不是主人的真實資料">沙盒</span> : null}
        <Link className="stamp-btn" to="/new" style={{ textDecoration: "none" }}>
          <b>+</b>新對話
        </Link>
      </div>
    </header>
  );
}
