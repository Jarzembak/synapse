import { lazy, Suspense } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import ErrorBoundary from "./components/ErrorBoundary";
import JobTicker from "./components/JobTicker";
import ThemeSelect from "./components/ThemeSelect";

const Library = lazy(() => import("./pages/Library"));
const Projects = lazy(() => import("./pages/Projects"));
const RepositoryImport = lazy(() => import("./pages/RepositoryImport"));
const PaperImport = lazy(() => import("./pages/PaperImport"));
const PaperSeries = lazy(() => import("./pages/PaperSeries"));
const ProjectDetail = lazy(() => import("./pages/ProjectDetail"));
const ArtifactView = lazy(() => import("./pages/ArtifactView"));
const QuickRefs = lazy(() => import("./pages/QuickRefs"));
const Jobs = lazy(() => import("./pages/Jobs"));
const System = lazy(() => import("./pages/System"));
const Logs = lazy(() => import("./pages/Logs"));
const Settings = lazy(() => import("./pages/Settings"));

function PageFallback() {
  return <p className="page-loading" role="status">Loading page...</p>;
}

function NotFound() {
  return (
    <section className="not-found">
      <p className="eyebrow">404</p>
      <h2>Page not found</h2>
      <p>The address may be out of date, or the page may have moved.</p>
      <Link to="/">Return to the library</Link>
    </section>
  );
}

export default function App() {
  const location = useLocation();
  return (
    <div className="app">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <nav className="topnav" aria-label="Primary navigation">
        <Link className="brand" to="/" aria-label="Synapse home">Synapse</Link>
        <div className="navlinks">
          <NavLink to="/" end>Library</NavLink>
          <NavLink to="/projects">Projects</NavLink>
          <NavLink to="/quickrefs">Quick-refs</NavLink>
          <NavLink to="/jobs">Jobs</NavLink>
          <NavLink to="/system">System</NavLink>
          <NavLink to="/logs">Logs</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </div>
        <JobTicker />
        <ThemeSelect />
      </nav>
      <main id="main-content" tabIndex={-1}>
        <ErrorBoundary key={location.pathname}>
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/" element={<Library />} />
              <Route path="/projects" element={<Projects />} />
              <Route path="/projects/new/repository" element={<RepositoryImport />} />
              <Route path="/projects/new/paper" element={<PaperImport />} />
              <Route path="/projects/:id" element={<ProjectDetail />} />
              <Route path="/paper-series/:id" element={<PaperSeries />} />
              <Route path="/artifacts/:id" element={<ArtifactView />} />
              <Route path="/quickrefs" element={<QuickRefs />} />
              <Route path="/jobs" element={<Jobs />} />
              <Route path="/system" element={<System />} />
              <Route path="/logs" element={<Logs />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </Suspense>
        </ErrorBoundary>
      </main>
    </div>
  );
}
