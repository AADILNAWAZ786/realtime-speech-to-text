import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import { ConfigError, loadConfig } from "./config/env";
import "./styles/index.css";

const root = createRoot(document.getElementById("root") as HTMLElement);

try {
  const config = loadConfig();
  root.render(
    <StrictMode>
      <App apiBaseUrl={config.apiBaseUrl} />
    </StrictMode>,
  );
} catch (error) {
  if (!(error instanceof ConfigError)) throw error;
  root.render(
    <div className="mx-auto max-w-lg p-8">
      <h1 className="text-lg font-semibold text-red-700">Configuration error</h1>
      <p className="mt-2 text-sm text-slate-700">{error.message}</p>
      <p className="mt-2 text-sm text-slate-500">
        Copy <code>.env.example</code> to <code>.env</code> and restart the dev server.
      </p>
    </div>,
  );
}
