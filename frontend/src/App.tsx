import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import ErrorBoundary from "./components/ErrorBoundary";
import Library from "./pages/Library";
import Projects from "./pages/Projects";
import ProjectDetail from "./pages/ProjectDetail";
import ArtifactView from "./pages/ArtifactView";
import QuickRefs from "./pages/QuickRefs";
import Jobs from "./pages/Jobs";
import System from "./pages/System";
import Logs from "./pages/Logs";
import Settings from "./pages/Settings";
import JobTicker from "./components/JobTicker";
import ThemeSelect from "./components/ThemeSelect";

export default function App() {
  const location = useLocation();
  return (
    <div className="app">
      <nav className="topnav">
        <span className="brand">Synapse</span>
        <NavLink to="/">Library</NavLink>
        <NavLink to="/projects">Projects</NavLink>
        <NavLink to="/quickrefs">Quick-refs</NavLink>
        <NavLink to="/jobs">Jobs</NavLink>
        <NavLink to="/system">System</NavLink>
        <NavLink to="/logs">Logs</NavLink>
        <NavLink to="/settings">Settings</NavLink>
        <JobTicker />
        <ThemeSelect />
      </nav>
      <main>
        <ErrorBoundary key={location.pathname}>
          <Routes>
            <Route path="/" element={<Library />} />
            <Route path="/projects" element={<Projects />} />
            <Route path="/projects/:id" element={<ProjectDetail />} />
            <Route path="/artifacts/:id" element={<ArtifactView />} />
            <Route path="/quickrefs" element={<QuickRefs />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/system" element={<System />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </ErrorBoundary>
      </main>
    </div>
  );
}
