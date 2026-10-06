import React from "react";
import ReactDOM from "react-dom/client";
import AccessGate from "./AccessGate";
import AccessAdminApp from "./AccessAdminApp";
import McpConnectApp from "./McpConnectApp";
import "./styles.css";
import "./access-admin.css";

const accessAdministration = /^\/access-admin\/?$/.test(window.location.pathname);
const mcpConnection = /^\/connect\/?$/.test(window.location.pathname);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {mcpConnection ? <McpConnectApp /> : accessAdministration ? <AccessAdminApp /> : <AccessGate />}
  </React.StrictMode>,
);
