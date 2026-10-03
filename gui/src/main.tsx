import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./styles/base.css";
import "./styles/board.css";
import "./styles/dispatch.css";
import "./styles/thread.css";
import "./styles/chat.css";
import "./styles/make.css";
import "./styles/works.css";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
