import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Terminal from "./features/terminal/Terminal";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>{window.location.pathname === "/terminal" ? <Terminal /> : <App />}</React.StrictMode>,
);
