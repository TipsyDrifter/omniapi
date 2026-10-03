import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/* markdown 的實際渲染（react-markdown＋remark-gfm，不吃原始 HTML）。
   這個檔只由 Markdown.tsx 以 dynamic import 載入：整包 markdown 套件因此是獨立 chunk，
   看板首屏不載；真的要畫 markdown 時才抓。不要在別處直接 import 這個檔，否則會被打回主 bundle。 */

const components: Components = {
  a: ({ node: _n, ...p }) => <a {...p} target="_blank" rel="noreferrer" />,
  // 表格超寬時在自己的框裡橫捲
  table: ({ node: _n, ...p }) => (
    <div className="md-table">
      <table {...p} />
    </div>
  ),
};

const plugins = [remarkGfm];

export default function MarkdownRender({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={plugins} components={components}>
      {text}
    </ReactMarkdown>
  );
}
