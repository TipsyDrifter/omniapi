import { Link } from "react-router-dom";
import { useNoProvider } from "@/components/NoProviderNotice";
import { useBoard } from "@/store/board";
import { ThemeSwitch } from "./TopBar";

/* footer：daemon 狀態（真資料，來自 /api/status 與 WS 連線狀態）。
   1.3-M3：M 段頂欄放不下主題切換，改放在這一列右邊（.foot-theme，其他段藏起來）；S 段沒有狀態列，
   同樣的內容（StatusBits）放進底部分頁列的「更多」單子。 */

/** 狀態列的內容：daemon、pid、port、在線模型、執行中、key 提醒（s＝放在「更多」單子裡的精簡版） */
export function StatusBits({ s }: { s?: boolean }) {
  const status = useBoard((st) => st.status);
  const { none } = useNoProvider();
  const socket = useBoard((st) => st.socket);
  const live = useBoard((st) => st.status?.live_runs ?? 0);
  const online = status ? Object.values(status.discovery).reduce((a, d) => a + (d.models || 0), 0) : null;
  const dot = socket === "open" ? "on" : socket === "connecting" ? "" : "off";
  return (
    <>
      <span>
        <span className={`dot ${dot}`} />
        daemon{" "}
        <span className="v">{status ? `v${status.version} · 在線 ${Math.round(status.uptime_s / 3600)} 小時` : socket === "open" ? "連線中" : "離線"}</span>
      </span>
      {s ? null : (
        <span className="x-m">
          pid <span className="v n">{status?.pid ?? "—"}</span>
        </span>
      )}
      <span>
        port <span className="v n">{status?.port ?? "—"}</span>
      </span>
      <span>
        在線模型 <span className="v n">{online ?? "—"}</span>
      </span>
      <span>
        執行中 <span className="v n">{live}</span>
      </span>
      {none ? (
        <Link className="v" to="/settings?sec=s-keys" title="到設定頁貼上第一把 key（不用重啟）">
          <b>尚未設定 API key</b>
        </Link>
      ) : null}
      {s ? (
        <span>
          ws <span className="v">{socket === "open" ? "即時" : socket === "connecting" ? "重連中…" : "斷線"}</span>
        </span>
      ) : null}
    </>
  );
}

export default function Footer() {
  const status = useBoard((s) => s.status);
  const socket = useBoard((s) => s.socket);
  return (
    <footer className="foot wrap">
      <StatusBits />
      <span className="r">
        ws <span className="v">{socket === "open" ? "即時" : socket === "connecting" ? "重連中…" : "斷線"}</span>
        {status ? (
          <span className="x-m">
            {" "}
            · 資料庫 <span className="v n">{status.store.runs}</span> runs · <span className="v n">{status.store.events}</span> events
          </span>
        ) : null}
      </span>
      <span className="foot-theme">
        <ThemeSwitch />
      </span>
    </footer>
  );
}
