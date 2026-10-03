import { Component, Suspense, lazy, memo, type ReactNode } from "react";

/* 模型回覆／agent 說的 markdown（M6-g）。真正的渲染器（react-markdown＋remark-gfm）在 MarkdownRender.tsx，
   這裡用 React.lazy 延後載入，讓那包套件成為獨立 chunk、不進主 bundle。
   - 載入中：先把原文當純文字畫出來（同字型同字級，pre-wrap），不留白。
   - 串流中的半截 markdown（例如只有開頭的 ```）照樣能畫。
   - 渲染丟錯或 chunk 抓不到：退回純文字，不讓整頁壞掉。 */

const loadRender = () => import("./MarkdownRender");
const MarkdownRender = lazy(loadRender);

/** 提早抓 markdown chunk（例如進聊天頁時），讓第一則訊息不必先閃純文字 */
export function preloadMarkdown(): void {
  void loadRender().catch(() => {
    /* 抓不到就等真的要畫時再試；Guard 會退回純文字 */
  });
}

function Plain({ text }: { text: string }) {
  return <div className="md-plain">{text}</div>;
}

class Guard extends Component<{ text: string; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidUpdate(prev: { text: string }) {
    // 內容變了（串流又來了字）就再試一次
    if (this.state.failed && prev.text !== this.props.text) this.setState({ failed: false });
  }
  render() {
    return this.state.failed ? <Plain text={this.props.text} /> : this.props.children;
  }
}

export interface MarkdownProps {
  text: string;
  className?: string;
}

function Markdown({ text, className }: MarkdownProps) {
  return (
    <div className={`md${className ? " " + className : ""}`}>
      <Guard text={text}>
        <Suspense fallback={<Plain text={text} />}>
          <MarkdownRender text={text} />
        </Suspense>
      </Guard>
    </div>
  );
}

export default memo(Markdown);

/* 這段文字看起來有沒有 markdown 語法。活動流用它決定要不要動用 markdown 渲染：
   一般的一句話照舊畫成純文字（不抓 chunk、也不會從純文字換成 markdown 而跳版）。
   寧可多判（多判只是多抓一次 chunk、渲染結果與純文字幾乎一樣），不要漏判。 */
const MD_HINTS: RegExp[] = [
  /^\s{0,3}#{1,6}\s/m, // 標題
  /^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+\S/m, // 清單
  /^\s{0,3}>\s?/m, // 引言
  /^\s{0,3}(?:```|~~~)/m, // 程式碼區塊
  /`[^`\n]+`/, // 行內程式碼
  /\*\*[^*\n]+\*\*|__[^_\n]+__/, // 粗體
  /(?:^|[^*\w])\*[^*\s][^*\n]*\*(?!\*)/, // 斜體
  /~~[^~\n]+~~/, // 刪除線
  /\[[^\]\n]+\]\([^)\s]+\)/, // 連結
  /^\s*\|.*\|\s*$/m, // 表格列
  /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/m, // 分隔線
];

export function looksLikeMarkdown(text: string): boolean {
  return MD_HINTS.some((re) => re.test(text));
}
