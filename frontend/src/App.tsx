import { NavLink, Route, Routes } from "react-router-dom";
import Library from "./pages/Library";
import Projects from "./pages/Projects";
import ProjectDetail from "./pages/ProjectDetail";
import ArtifactView from "./pages/ArtifactView";
import QuickRefs from "./pages/QuickRefs";
import Settings from "./pages/Settings";
import JobTicker from "./components/JobTicker";

export default function App() {
  return (
    <div className="app">
      <nav className="topnav">
        <span className="brand">Synapse</span>
        <NavLink to="/">Library</NavLink>
        <NavLink to="/projects">Projects</NavLink>
        <NavLink to="/quickrefs">Quick-refs</NavLink>
        <NavLink to="/settings">Settings</NavLink>
        <JobTicker />
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<Library />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/:id" element={<ProjectDetail />} />
          <Route path="/artifacts/:id" element={<ArtifactView />} />
          <Route path="/quickrefs" element={<QuickRefs />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
