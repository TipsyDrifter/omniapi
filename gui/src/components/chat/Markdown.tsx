import { Component, memo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/* 模型回覆的 markdown（M6-g）：react-markdown＋remark-gfm，不吃原始 HTML（沒有 rehype-raw）。
   串流中的半截 markdown（例如只有開頭的 ```）照樣能畫；萬一渲染丟錯，退回純文字，不讓整頁壞掉。 */

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

function MarkdownInner({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={plugins} components={components}>
      {text}
    </ReactMarkdown>
  );
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
    return this.state.failed ? <pre className="md-plain">{this.props.text}</pre> : this.props.children;
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
        <MarkdownInner text={text} />
      </Guard>
    </div>
  );
}

export default memo(Markdown);
