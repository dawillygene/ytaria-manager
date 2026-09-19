import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { shouldRetry } from "./features/downloads/queries";
import { applyTheme, readTheme } from "./lib/hooks";
import "./styles.css";

applyTheme(readTheme());

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: shouldRetry,
      retryDelay: (attempt) => Math.min(30_000, 1000 * 2 ** attempt),
      refetchOnWindowFocus: true,
      staleTime: 5_000,
      gcTime: 10 * 60_000, // keep last known data around so offline screens stay populated
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
