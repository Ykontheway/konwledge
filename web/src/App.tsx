import React, { useState, useEffect } from "react";
import { Layout, type View } from "./Layout";
import { OverviewPage } from "./OverviewPage";
import { TraceabilityPage } from "./TraceabilityPage";
import { VersionDiffPage } from "./VersionDiffPage";

const App: React.FC = () => {
  const [currentView, setCurrentView] = useState<View>("overview");

  // Simple hash-based routing for GitHub Pages
  useEffect(() => {
    const handleHash = () => {
      const hash = window.location.hash.replace("#", "");
      if (["overview", "trace", "diff", "lifecycle"].includes(hash)) {
        setCurrentView(hash as View);
      }
    };
    handleHash();
    window.addEventListener("hashchange", handleHash);
    return () => window.removeEventListener("hashchange", handleHash);
  }, []);

  const handleViewChange = (view: View) => {
    window.location.hash = view;
  };

  const renderContent = () => {
    switch (currentView) {
      case "overview":
        return <OverviewPage />;
      case "trace":
        return <TraceabilityPage />;
      case "diff":
        return <VersionDiffPage />;
      case "lifecycle":
        return <div className="p-20 text-center text-gray-400 italic">生命周期视图将在 V3 切片中交付。</div>;
      default:
        return <OverviewPage />;
    }
  };

  return (
    <Layout currentView={currentView} onViewChange={handleViewChange}>
      {renderContent()}
    </Layout>
  );
};

export default App;
