import React, { useEffect, useMemo, useState } from "react";

import { Layout, type View } from "./Layout";
import { OverviewPage } from "./OverviewPage";
import { TraceabilityPage } from "./TraceabilityPage";
import { VersionDiffPage } from "./VersionDiffPage";
import { LifecyclePage } from "./management/LifecyclePage";
import {
  demoHash,
  getBreadcrumbMeta,
  isLifecycleRoute,
  lifecycleEntryHash,
  parseHashRoute,
  type HashRoute,
} from "./management/route";

function currentHash(): string {
  return typeof window === "undefined" ? "#overview" : window.location.hash || "#overview";
}

const App: React.FC = () => {
  const [route, setRoute] = useState<HashRoute>(() => parseHashRoute(currentHash()));

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const handleHashChange = () => setRoute(parseHashRoute(currentHash()));
    handleHashChange();
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  const breadcrumb = useMemo(() => getBreadcrumbMeta(route), [route]);
  const currentView: View = route.view;

  const handleViewChange = (view: View) => {
    if (typeof window === "undefined") return;
    window.location.hash = view === "lifecycle" ? lifecycleEntryHash() : demoHash(view);
  };

  const navigate = (hash: string) => {
    if (typeof window === "undefined") return;
    window.location.hash = hash;
  };

  const content = isLifecycleRoute(route) ? (
    <LifecyclePage navigate={navigate} route={route} />
  ) : route.view === "trace" ? (
    <TraceabilityPage />
  ) : route.view === "diff" ? (
    <VersionDiffPage />
  ) : (
    <OverviewPage />
  );

  return (
    <Layout
      currentSection={breadcrumb.section}
      currentTitle={breadcrumb.title}
      currentTone={breadcrumb.tone}
      currentView={currentView}
      onViewChange={handleViewChange}
    >
      {content}
    </Layout>
  );
};

export default App;
