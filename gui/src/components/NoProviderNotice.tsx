import { Link } from "react-router-dom";
import { useBoard } from "@/store/board";
import { selectNoKey, useSettings } from "@/store/settings";

/* 還沒有任何 API key 時的提示（M7 起有；1.3-M4 改成版面稿 skipped-* 的常駐提示條）。
   判斷依據：/api/settings 七家都沒有 key（只看有沒有 key，不看有沒有略過引導）；設定還沒讀到就不顯示，避免閃一下。
   接上第一家之後，這一條與導覽上的「缺 key」章一起消失。 */

export function useNoProvider(): { none: boolean; sandbox: boolean } {
  const noKey = useSettings(selectNoKey);
  const status = useBoard((s) => s.status);
  return { none: noKey === true, sandbox: !!status?.dev };
}

/** 看板用的整條提示 */
export default function NoProviderNotice() {
  const { none, sandbox } = useNoProvider();
  if (!none) return null;
  return (
    <section className="wrap">
      <div className="nokey" role="status">
        <span className="k">缺 key</span>
        <p>
          還沒有任何 API key：聊天、生成，以及派工給 Codex、Gemini CLI 都還不能用。派工給 Claude Code（用訂閱）可以。
          <small>
            這一條在接上第一家之後就消失。{sandbox ? "這是開發沙盒：回音模型與重播不需要 key，可以照常試用。" : ""}
          </small>
        </p>
        <Link className="btn solid" to="/welcome">
          接上第一家 →
        </Link>
      </div>
    </section>
  );
}
