// Fonts are bundled from npm (no CDN): the app renders identically offline.
import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "@react-sigma/core/lib/style.css";
import "./index.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { applyTheme, themeStore } from "./lib/theme";

applyTheme(themeStore.get());

const root = document.getElementById("root");
if (!root) throw new Error("#root element missing from index.html");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
