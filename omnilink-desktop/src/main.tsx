import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "@/App";
import { bootstrapAppearance } from "@/lib/theme";
import "./index.css";

bootstrapAppearance();

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element was not found");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
