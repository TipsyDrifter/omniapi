import { Link, NavLink } from "react-router-dom";
import { THEMES, useTheme } from "@/themes";
import { headDate } from "@/lib/format";
import { selectRunningCount, useMake } from "@/store/make";
import { useWorks } from "@/store/works";
import { selectNoKey, useSettings } from "@/store/settings";

/* 頂欄：wordmark（唯一允許疊印的地方之一）、導覽（三組：看板・費用・聊天｜生成・作品｜模型・設定）、主題切換、日期、「＋新對話」。
   「生成」旁的小章＝進行中的生成件數（0 件不顯示）。
   1.3-M3 RWD（styles/rwd.css）：L 拿掉導覽的拉丁小字；M 拿掉「派工中心」與日期、主題切換移到狀態列；
   S 只留 wordmark 與「＋新對話」，導覽換成底部分頁列（TabBar）。 */

/** 主題切換（頂欄右側；M 段在狀態列；S 段在「更多」單子） */
export function ThemeSwitch({ label = "主題" }: { label?: string }) {
  const [theme, setTheme] = useTheme();
  return (
    <div className="sim" role="group" aria-label="主題">
      {label ? <span className="k">{label}</span> : null}
      {THEMES.map((t) => (
        <button key={t.id} type="button" aria-pressed={theme === t.id} title={`${t.name}：${t.note}`} onClick={() => setTheme(t.id)}>
          {t.short}
        </button>
      ))}
    </div>
  );
}

export default function TopBar() {
  const running = useMake(selectRunningCount);
  const unseen = useWorks((s) => s.unseen);
  const noKey = useSettings(selectNoKey);
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
        <NavLink to="/models">
          模型<span>MODELS</span>
        </NavLink>
        <NavLink to="/settings">
          設定<span>SETTINGS</span>
          {noKey ? (
            <em className="navct need" title="還沒有任何 API key">
              缺 key
            </em>
          ) : null}
        </NavLink>
      </nav>
      <div className="top-r">
        <ThemeSwitch />
        <span className="date">{headDate()}</span>
        {__OMNI_SANDBOX__ ? <span className="sample" title="連的是隔離的測試 daemon（7799），不是主人的真實資料">沙盒</span> : null}
        <Link className="stamp-btn top-new" to="/new" style={{ textDecoration: "none" }}>
          <b>+</b>新對話
        </Link>
      </div>
    </header>
  );
}
