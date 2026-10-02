import React from "react";
import ReactDOM from "react-dom/client";
import AccessGate from "./AccessGate";
import AccessAdminApp from "./AccessAdminApp";
import "./styles.css";
import "./access-admin.css";

const accessAdministration = /^\/access-admin\/?$/.test(window.location.pathname);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {accessAdministration ? <AccessAdminApp /> : <AccessGate />}
  </React.StrictMode>,
);
