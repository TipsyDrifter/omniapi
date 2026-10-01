import { useNoProvider } from "@/components/NoProviderNotice";
import { useBoard } from "@/store/board";

/* footer：daemon 狀態（真資料，來自 /api/status 與 WS 連線狀態） */
export default function Footer() {
  const status = useBoard((s) => s.status);
  const { none } = useNoProvider();
  const socket = useBoard((s) => s.socket);
  const live = useBoard((s) => s.status?.live_runs ?? 0);
  const online = status ? Object.values(status.discovery).reduce((a, d) => a + (d.models || 0), 0) : null;
  const dot = socket === "open" ? "on" : socket === "connecting" ? "" : "off";
  return (
    <footer className="foot wrap">
      <span>
        <span className={`dot ${dot}`} />
        daemon{" "}
        <span className="v">{status ? `v${status.version} · 在線 ${Math.round(status.uptime_s / 3600)} 小時` : socket === "open" ? "連線中" : "離線"}</span>
      </span>
      <span>
        pid <span className="v n">{status?.pid ?? "—"}</span>
      </span>
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
        <span className="v" title="在 mcp/ 把 .env.example 複製成 .env、填 key、ENABLED 設成 true，再重啟服務">
          <b>尚未設定 API key</b>
        </span>
      ) : null}
      <span className="r">
        ws <span className="v">{socket === "open" ? "即時" : socket === "connecting" ? "重連中…" : "斷線"}</span>
        {status ? (
          <>
            {" "}
            · 資料庫 <span className="v n">{status.store.runs}</span> runs · <span className="v n">{status.store.events}</span> events
          </>
        ) : null}
      </span>
    </footer>
  );
}
