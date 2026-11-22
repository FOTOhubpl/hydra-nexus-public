import { lazy, Suspense } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router";
import { shouldRetry } from "./api/client";
import { runAwareQueryKeyHash } from "./lib/runSelection";
import { AppShell } from "./components/AppShell";
import { Skeleton } from "./components/Skeleton";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ItemPage } from "./pages/ItemPage";
import { AnalysePage } from "./pages/task/AnalysePage";
import { CampaignCardPage } from "./pages/task/CampaignCardPage";
import { CampaignsPage } from "./pages/task/CampaignsPage";
import { DashboardPage } from "./pages/task/DashboardPage";
import { HowItWorksPage } from "./pages/task/HowItWorksPage";
import { VariantDiffPage } from "./pages/task/VariantDiffPage";
import { HuntPage } from "./pages/task/HuntPage";
import { IncidentPage } from "./pages/task/IncidentPage";
import { IncidentsPage } from "./pages/task/IncidentsPage";
import { LabPage } from "./pages/task/LabPage";
import { MethodologyPage } from "./pages/task/MethodologyPage";
import { PairPage } from "./pages/task/PairPage";
import { PairsPage } from "./pages/task/PairsPage";
import { QueuePage } from "./pages/task/QueuePage";
import { SchedulerPage } from "./pages/task/SchedulerPage";
import { SearchPage } from "./pages/task/SearchPage";
import { SimulationPage } from "./pages/task/SimulationPage";
import { WatchPage } from "./pages/task/WatchPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { OverviewPage } from "./pages/OverviewPage";
import { SettingsPage } from "./pages/SettingsPage";
import { IntegrationsPage } from "./pages/integrations/IntegrationsPage";
import { AuthGuard } from "./pages/auth/AuthGuard";
import { LoginPage } from "./pages/auth/LoginPage";
import { ForcedPasswordPage } from "./pages/auth/PasswordChangeForm";
import { AccountPasswordPage, TokensPage, UsersPage } from "./pages/account/AccountPages";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        refetchOnWindowFocus: false,
        staleTime: 5_000,
        // Run-scoped keys hash with the selected run (lib/runSelection.ts), so each run keeps its own cache.
        queryKeyHashFn: runAwareQueryKeyHash,
      },
      mutations: { retry: false },
    },
  });
}

// The graph stack (sigma + graphology + ForceAtlas2) is the bulk of the bundle; load it on demand.
const GraphTaskPage = lazy(() => import("./pages/task/GraphTaskPage").then((module) => ({ default: module.GraphTaskPage })));

/** Route table, separate from the router so tests can mount it in a MemoryRouter. */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="login" element={<LoginPage />} />
      <Route path="login/change-password" element={<ForcedPasswordPage />} />
      <Route element={<AuthGuard><AppShell /></AuthGuard>}>
        <Route index element={<DashboardPage />} />
        <Route path="campaigns" element={<CampaignsPage />} />
        <Route path="campaigns/:id" element={<CampaignCardPage />} />
        <Route
          path="graph"
          element={
            <Suspense fallback={<Skeleton label="Ładowanie grafu…" lines={6} className="p-6" />}>
              <GraphTaskPage />
            </Suspense>
          }
        />
        <Route path="analyse" element={<AnalysePage />} />
        <Route path="simulation" element={<SimulationPage />} />
        <Route path="how" element={<HowItWorksPage />} />
        <Route path="diff/:a/:b" element={<VariantDiffPage />} />
        <Route path="pairs" element={<PairsPage />} />
        <Route path="queue" element={<QueuePage />} />
        <Route path="pair/:a/:b" element={<PairPage />} />
        <Route path="incidents" element={<IncidentsPage />} />
        <Route path="incident/:id" element={<IncidentPage />} />
        <Route path="lab/:incident" element={<LabPage />} />
        <Route path="search" element={<SearchPage />} />
        <Route path="methodology" element={<MethodologyPage />} />
        <Route path="scheduler" element={<SchedulerPage />} />
        <Route path="watch" element={<WatchPage />} />
        <Route path="integrations" element={<IntegrationsPage />} />
        <Route path="hunt" element={<HuntPage />} />
        <Route path="data" element={<OverviewPage />} />
        <Route path="item/:id" element={<ItemPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="account/password" element={<AccountPasswordPage />} />
        <Route path="account/tokens" element={<TokensPage />} />
        <Route path="admin/users" element={<UsersPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

const queryClient = createQueryClient();

export function App() {
  return (
    <ErrorBoundary label="The application">
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
