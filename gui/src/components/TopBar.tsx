import { Link, NavLink } from "react-router-dom";
import { THEMES, useTheme } from "@/themes";
import { headDate } from "@/lib/format";

/* 頂欄：wordmark（唯一允許疊印的地方之一）、導覽、主題切換、日期、「＋新對話」（M5 才有功能） */
export default function TopBar() {
  const [theme, setTheme] = useTheme();
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
