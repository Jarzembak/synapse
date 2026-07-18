import { FormEvent, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  api,
  Project,
  RepositoryCreateConfig,
  RepositoryCreateResponse,
  RepositoryCoverage,
  RepositoryPreflight,
  shortSha,
} from "../api";

type ScopeMode = "whole" | "folder" | "custom";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected error";
}

function pathLines(value: string): string[] {
  return [...new Set(
    value
      .split(/\r?\n/)
      .map((path) => path.trim().replace(/^\.\//, "").replace(/^\/+|\/+$/g, ""))
      .filter(Boolean),
  )];
}

function formatBytes(bytes?: number): string {
  if (bytes === undefined || !Number.isFinite(bytes)) return "Not reported";
  const units = ["B", "KB", "MB", "GB"];
  let value = Math.max(0, bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

function coveragePercent(coverage: RepositoryCoverage): number {
  if (coverage.percent !== undefined) return Math.max(0, Math.min(100, coverage.percent));
  if (!coverage.total_files) return 0;
  return Math.round(((coverage.included_files ?? coverage.eligible_files ?? 0) / coverage.total_files) * 100);
}

export default function RepositoryImport() {
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [requestedRef, setRequestedRef] = useState("");
  const [title, setTitle] = useState("");
  const [scopeMode, setScopeMode] = useState<ScopeMode>("whole");
  const [folder, setFolder] = useState("");
  const [includeText, setIncludeText] = useState("");
  const [excludeText, setExcludeText] = useState(
    ".git\nnode_modules\nvendor\ndist\nbuild\ncoverage\n.next\n",
  );
  const [inspection, setInspection] = useState<RepositoryPreflight | null>(null);
  const [inspectedKey, setInspectedKey] = useState("");
  const [inspecting, setInspecting] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [createdProject, setCreatedProject] = useState<Project | null>(null);

  const includePaths = useMemo(() => {
    if (scopeMode === "whole") return [];
    if (scopeMode === "folder") return pathLines(folder);
    return pathLines(includeText);
  }, [folder, includeText, scopeMode]);
  const excludePaths = useMemo(() => pathLines(excludeText), [excludeText]);
  const currentKey = useMemo(() => JSON.stringify([
    url.trim(), requestedRef.trim(), scopeMode, includePaths, excludePaths,
  ]), [url, requestedRef, scopeMode, includePaths, excludePaths]);
  const inspectionCurrent = Boolean(inspection && inspectedKey === currentKey);
  const scopeInvalid = scopeMode !== "whole" && includePaths.length === 0;

  async function inspect(event: FormEvent) {
    event.preventDefault();
    setInspecting(true);
    setInspection(null);
    setCreatedProject(null);
    setError("");
    const key = currentKey;
    try {
      const result = await api<RepositoryPreflight>("/repositories/preflight", {
        method: "POST",
        body: JSON.stringify({
          url: url.trim(),
          ref: requestedRef.trim() || undefined,
          include_paths: includePaths.length ? includePaths : undefined,
          exclude_paths: excludePaths.length ? excludePaths : undefined,
        }),
      });
      setInspection(result);
      setInspectedKey(key);
      if (!title.trim()) setTitle(result.source.full_name);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setInspecting(false);
    }
  }

  async function queueAnalysis(projectId: number) {
    let profile = "repository";
    try {
      const detail = await api<{ profiles?: Record<string, unknown> }>(`/projects/${projectId}`);
      if (!detail.profiles?.repository) profile = detail.profiles?.full ? "full" : profile;
    } catch {
      // Creation already succeeded. The run endpoint will provide the useful error
      // if the detail endpoint is briefly unavailable during project initialization.
    }
    await api(`/projects/${projectId}/run_all`, {
      method: "POST",
      body: JSON.stringify({ profile }),
    });
  }

  async function create(analyze: boolean) {
    if (!inspectionCurrent || !inspection) {
      setError("Inspect the current URL and revision before creating the project.");
      return;
    }
    if (scopeInvalid) {
      setError(scopeMode === "folder"
        ? "Enter the repository folder to analyze."
        : "Add at least one include path.");
      return;
    }

    setCreating(true);
    setCreatedProject(null);
    setError("");
    const payload: RepositoryCreateConfig = {
      url: url.trim(),
      ref: requestedRef.trim() || undefined,
      title: title.trim() || undefined,
      include_paths: includePaths.length ? includePaths : undefined,
      exclude_paths: excludePaths.length ? excludePaths : undefined,
      analyze,
      expected_sha: inspection.source.commit_sha,
    };

    try {
      const result = await api<RepositoryCreateResponse>("/repositories", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setCreatedProject(result.project);
      if (analyze) {
        try {
          await queueAnalysis(result.project.id);
        } catch (caught) {
          setError(
            `The repository project was created, but analysis could not be queued: ${errorMessage(caught)}`,
          );
          return;
        }
      }
      navigate(`/projects/${result.project.id}`);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setCreating(false);
    }
  }

  const privacy = inspection?.source.private ? "private" : inspection?.source.privacy;
  const coverage = inspection?.coverage_preview ?? inspection?.coverage;
  const warnings = [
    ...(inspection?.warnings ?? []),
    ...(coverage?.warnings ?? []),
  ];

  return (
    <div className="repository-import">
      <header className="repository-import-head">
        <div>
          <p className="eyebrow">New source</p>
          <h2>Understand a GitHub repository</h2>
          <p className="meta">
            Synapse snapshots one exact commit, reads the code without executing it, and builds
            a plain-language guide with file-and-line citations.
          </p>
        </div>
        <Link to="/projects">Back to projects</Link>
      </header>

      <form className="repository-inspect card" onSubmit={(event) => void inspect(event)}>
        <h3>1. Choose a repository</h3>
        <label htmlFor="repository-url">GitHub repository URL</label>
        <div className="repository-url-row">
          <input
            id="repository-url"
            type="url"
            inputMode="url"
            autoComplete="url"
            required
            placeholder="https://github.com/owner/repository"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            aria-describedby="repository-url-help"
          />
          <button type="submit" disabled={inspecting || creating || !url.trim()}>
            {inspecting ? "Inspecting..." : "Inspect repository"}
          </button>
        </div>
        <p id="repository-url-help" className="hint">
          GitHub.com only for this release. For a private repository, configure a read-only
          fine-grained token in <Link to="/settings#github-access">Settings</Link>.
        </p>
        <label htmlFor="repository-ref">Branch, tag, or commit <span className="muted">(optional)</span></label>
        <input
          id="repository-ref"
          value={requestedRef}
          placeholder="Leave blank for the default branch"
          onChange={(event) => setRequestedRef(event.target.value)}
        />
        {!inspectionCurrent && inspection && (
          <p className="notice" role="status">The URL or revision changed. Inspect it again before creating.</p>
        )}
        {inspecting && <p className="meta" role="status">Checking access and resolving an exact commit...</p>}
      </form>

      {error && <p className="error" role="alert">{error}</p>}
      {createdProject && error && (
        <p><Link to={`/projects/${createdProject.id}`}>Open the created project</Link> to start analysis manually.</p>
      )}

      {inspectionCurrent && inspection && (
        <>
          <section className="repository-preflight" aria-labelledby="repository-preflight-title">
            <div className="card repository-identity">
              <p className="eyebrow">Inspection complete</p>
              <h3 id="repository-preflight-title">{inspection.source.full_name}</h3>
              {inspection.source.description && <p>{inspection.source.description}</p>}
              <dl className="repository-facts">
                <div><dt>Access</dt><dd><span className={`source-badge ${privacy}`}>{privacy}</span></dd></div>
                <div><dt>Revision</dt><dd>{inspection.source.resolved_ref || inspection.source.default_branch}</dd></div>
                <div><dt>Exact commit</dt><dd><code>{shortSha(inspection.source.commit_sha)}</code></dd></div>
                <div><dt>Repository size</dt><dd>{formatBytes(inspection.size_bytes ?? coverage?.total_bytes ?? undefined)}</dd></div>
              </dl>
            </div>

            {coverage ? <div className="card coverage-card">
              <h3>Analysis coverage</h3>
              <div className="coverage-value">
                <strong>{coverage.included_files ?? coverage.analyzable_files ?? coverage.eligible_files ?? "—"}</strong>
                <span>of {coverage.total_files ?? inspection.file_count ?? "unknown"} files included</span>
              </div>
              <div
                className="coverage-meter"
                role="progressbar"
                aria-label="Repository analysis file coverage"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={coveragePercent(coverage)}
              >
                <i style={{ width: `${coveragePercent(coverage)}%` }} />
              </div>
              <p className="meta">
                {coverage.analyzable_files ?? coverage.included_files ?? coverage.eligible_files ?? "Supported"} files are eligible for text analysis.
                {coverage.binary_files ? ` ${coverage.binary_files} binary files are catalogued but not read.` : ""}
              </p>
              {coverage.languages && !Array.isArray(coverage.languages) && Object.keys(coverage.languages).length > 0 && (
                <div className="tagcloud" aria-label="Detected languages">
                  {Object.entries(coverage.languages).slice(0, 8).map(([language, count]) => (
                    <span className="tag" key={language}>{language} <small>{count}</small></span>
                  ))}
                </div>
              )}
            </div> : (
              <div className="card coverage-card">
                <h3>Repository limits</h3>
                <p>
                  File-level coverage will appear after Synapse safely snapshots and inventories
                  the selected commit.
                </p>
                <dl className="repository-facts">
                  <div><dt>Reported files</dt><dd>{inspection.file_count ?? "Not reported"}</dd></div>
                  <div><dt>Reported size</dt><dd>{formatBytes(inspection.size_bytes)}</dd></div>
                  {inspection.limits && (
                    <>
                      <div><dt>Maximum files</dt><dd>{inspection.limits.max_files.toLocaleString()}</dd></div>
                      <div><dt>Expanded size limit</dt><dd>{formatBytes(inspection.limits.max_unpacked_bytes)}</dd></div>
                    </>
                  )}
                </dl>
              </div>
            )}
          </section>

          <section className="repository-scope card" aria-labelledby="repository-scope-title">
            <h3 id="repository-scope-title">2. Set the analysis scope</h3>
            <fieldset className="scope-options">
              <legend>Files to include</legend>
              <label>
                <input type="radio" name="scope" value="whole" checked={scopeMode === "whole"}
                  onChange={() => setScopeMode("whole")} />
                <span><b>Whole repository</b><small>Use the safe defaults and read every supported file.</small></span>
              </label>
              <label>
                <input type="radio" name="scope" value="folder" checked={scopeMode === "folder"}
                  onChange={() => setScopeMode("folder")} />
                <span><b>One folder</b><small>Useful for a package or service in a monorepository.</small></span>
              </label>
              <label>
                <input type="radio" name="scope" value="custom" checked={scopeMode === "custom"}
                  onChange={() => setScopeMode("custom")} />
                <span><b>Custom paths</b><small>Include several folders or individual files.</small></span>
              </label>
            </fieldset>

            {scopeMode === "folder" && (
              <label className="stacked" htmlFor="repository-folder">Repository folder
                <input id="repository-folder" value={folder} placeholder="packages/web"
                  onChange={(event) => setFolder(event.target.value)} />
              </label>
            )}
            {scopeMode === "custom" && (
              <label className="stacked" htmlFor="repository-includes">Include paths, one per line
                <textarea id="repository-includes" rows={5} value={includeText}
                  placeholder={"src\ndocs\npackage.json"}
                  onChange={(event) => setIncludeText(event.target.value)} />
              </label>
            )}
            <label className="stacked" htmlFor="repository-excludes">Exclude paths, one per line
              <textarea id="repository-excludes" rows={6} value={excludeText}
                onChange={(event) => setExcludeText(event.target.value)} />
            </label>
            <p className="hint">
              Generated, vendored, cached, binary, minified, and oversized files are excluded by
              the scanner even when they are inside the selected scope.
            </p>
          </section>

          <section className="repository-privacy card" aria-labelledby="repository-privacy-title">
            <h3 id="repository-privacy-title">3. Review privacy and processing</h3>
            <ul className="assurance-list">
              <li><b>Static only:</b> Synapse will not install dependencies, run hooks, build, test, or execute code.</li>
              <li><b>Immutable:</b> this analysis is pinned to <code>{shortSha(inspection.source.commit_sha)}</code>.</li>
              <li><b>Secrets:</b> likely credentials are excluded before model context is built and their values are never displayed.</li>
              <li>
                <b>Local-only:</b> public and private repository excerpts stay on this machine.
                Every model step is forced through Ollama
                {inspection.local_model ? <> using <code>{inspection.local_model}</code></> : null},
                regardless of the global model matrix, and derived artifacts are not cloud-synced.
              </li>
              <li><b>Git features:</b> submodules and Git LFS pointers are reported but their contents are not fetched.</li>
            </ul>
            {warnings.length > 0 && (
              <div className="repository-warnings" role="status">
                <strong>Inspection notes</strong>
                <ul>{warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}</ul>
              </div>
            )}
          </section>

          <section className="repository-create card" aria-labelledby="repository-create-title">
            <h3 id="repository-create-title">4. Create the learning project</h3>
            <label htmlFor="repository-title">Project title</label>
            <input id="repository-title" value={title} placeholder={inspection.source.full_name}
              onChange={(event) => setTitle(event.target.value)} />
            <p className="meta">
              Analysis creates an overview, setup guide, architecture map, knowledge guide,
              dependency guide, deep dives, quick-references, podcast, and mind map. It does not
              create transcript, correction, download, or source-media artifacts.
            </p>
            <div className="repository-create-actions">
              <button type="button" className="primary" onClick={() => void create(true)}
                disabled={creating || scopeInvalid}>
                {creating ? "Creating project..." : "Create and analyze"}
              </button>
              <button type="button" onClick={() => void create(false)}
                disabled={creating || scopeInvalid}>
                Create only
              </button>
            </div>
            {creating && <p className="meta" role="status">Creating the repository project...</p>}
          </section>
        </>
      )}
    </div>
  );
}
