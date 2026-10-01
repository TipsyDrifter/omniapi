import { useBoard } from "@/store/board";

/* 還沒設定任何供應商的 key 時的提示（M7：乾淨安裝驗證發現新使用者第一眼看不出「為什麼什麼都不能用」）。
   判斷依據是 /api/status 的 providers.text；status 還沒載到就不顯示，避免閃一下。 */

export function useNoProvider(): { none: boolean; sandbox: boolean } {
  const status = useBoard((s) => s.status);
  return { none: !!status && status.providers.text.length === 0, sandbox: !!status?.dev };
}

/** 看板用的整條提示 */
export default function NoProviderNotice() {
  const { none, sandbox } = useNoProvider();
  if (!none) return null;
  return (
    <section className="wrap" style={{ paddingTop: 22 }}>
      <div className="warn" role="status">
        <b>還沒有設定任何供應商的 API key</b>，所以派工和聊天都會失敗。在 <span className="code">mcp/</span> 資料夾把{" "}
        <span className="code">.env.example</span> 複製成 <span className="code">.env</span>，填入 key 並把對應的{" "}
        <span className="code">ENABLED</span> 設成 <span className="code">true</span>，然後重新啟動服務（
        <span className="code">omni stop</span> 再 <span className="code">omni serve</span>）。
        {sandbox ? " 這是開發沙盒：echo 模型與重播不需要 key，可以照常試用。" : ""}
      </div>
    </section>
  );
}
