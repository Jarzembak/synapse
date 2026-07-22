import { FormEvent, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, PAPER_AUDIENCES, PaperAudience, Project } from "../api";

const MAX_BYTES = 250 * 1024 * 1024;
const OCR_LANGUAGES = [
  { key: "eng", label: "English" },
  { key: "spa", label: "Spanish" },
  { key: "fra", label: "French" },
  { key: "deu", label: "German" },
] as const;

type ImportMode = "upload" | "local";

interface PaperCreateResponse {
  project: Project;
}

function projectFromResponse(value: Project | PaperCreateResponse): Project {
  return "project" in value ? value.project : value;
}

export default function PaperImport() {
  const navigate = useNavigate();
  const uploadRequest = useRef<XMLHttpRequest | null>(null);
  const [mode, setMode] = useState<ImportMode>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [localPath, setLocalPath] = useState("");
  const [title, setTitle] = useState("");
  const [ocrLanguages, setOcrLanguages] = useState<string[]>(["eng"]);
  const [audiences, setAudiences] = useState<PaperAudience[]>(["generalist"]);
  const [localOnly, setLocalOnly] = useState(true);
  const [analyze, setAnalyze] = useState(true);
  const [working, setWorking] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [error, setError] = useState("");

  function toggleLanguage(language: string) {
    setOcrLanguages((current) => current.includes(language)
      ? current.filter((item) => item !== language)
      : [...current, language]);
  }

  function toggleAudience(audience: PaperAudience) {
    setAudiences((current) => current.includes(audience)
      ? current.filter((item) => item !== audience)
      : [...current, audience]);
  }

  function uploadPaper(selectedFile: File): Promise<Project> {
    const form = new FormData();
    form.append("file", selectedFile);
    if (title.trim()) form.append("title", title.trim());
    form.append("ocr_languages", JSON.stringify(ocrLanguages));
    form.append("audiences", JSON.stringify(audiences));
    form.append("local_only", String(localOnly));
    form.append("analyze", String(analyze));

    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      uploadRequest.current = request;
      request.open("POST", "/api/papers/upload");
      request.responseType = "json";
      request.upload.onprogress = (event) => {
        if (event.lengthComputable && event.total > 0) {
          setUploadProgress(Math.round((event.loaded / event.total) * 100));
        }
      };
      request.onload = () => {
        uploadRequest.current = null;
        const payload = request.response;
        if (request.status >= 200 && request.status < 300) {
          resolve(projectFromResponse(payload as Project | PaperCreateResponse));
        } else {
          reject(new Error(payload?.detail ?? request.statusText ?? "Paper upload failed"));
        }
      };
      request.onerror = () => {
        uploadRequest.current = null;
        reject(new Error("The upload connection failed."));
      };
      request.onabort = () => {
        uploadRequest.current = null;
        reject(new Error("Upload canceled."));
      };
      request.send(form);
    });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (ocrLanguages.length === 0) {
      setError("Select at least one OCR language.");
      return;
    }
    if (audiences.length === 0) {
      setError("Select at least one audience track.");
      return;
    }
    if (mode === "upload") {
      if (!file) {
        setError("Choose a PDF to import.");
        return;
      }
      if (!file.name.toLocaleLowerCase().endsWith(".pdf")) {
        setError("Research paper projects accept PDF files only.");
        return;
      }
      if (file.size > MAX_BYTES) {
        setError("This PDF exceeds the 250 MiB paper limit.");
        return;
      }
    } else if (!localPath.trim().toLocaleLowerCase().endsWith(".pdf")) {
      setError("Enter a mounted local path ending in .pdf.");
      return;
    }

    setWorking(true);
    try {
      const project = mode === "upload"
        ? await uploadPaper(file as File)
        : projectFromResponse(await api<Project | PaperCreateResponse>("/papers", {
            method: "POST",
            body: JSON.stringify({
              path: localPath.trim(),
              title: title.trim() || null,
              ocr_languages: ocrLanguages,
              audiences,
              local_only: localOnly,
              analyze,
            }),
          }));
      navigate(`/projects/${project.id}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The paper could not be imported.");
    } finally {
      setWorking(false);
      setUploadProgress(null);
    }
  }

  return (
    <div className="paper-import">
      <header className="paper-import-head">
        <div>
          <p className="eyebrow">New project</p>
          <h2>Import a research paper</h2>
          <p className="lead">
            Extract every admitted page into stable evidence, review extraction quality,
            then build independent audience series from one shared analysis.
          </p>
        </div>
        <Link to="/projects">Back to projects</Link>
      </header>

      <form className="paper-import-form" onSubmit={submit}>
        <section className="card paper-import-source" aria-labelledby="paper-source-title">
          <h3 id="paper-source-title">1. Choose the source PDF</h3>
          <div className="mode-switch" role="group" aria-label="Paper source type">
            <button type="button" className={mode === "upload" ? "on" : ""}
              aria-pressed={mode === "upload"} onClick={() => setMode("upload")}>
              Upload from this device
            </button>
            <button type="button" className={mode === "local" ? "on" : ""}
              aria-pressed={mode === "local"} onClick={() => setMode("local")}>
              Mounted local path
            </button>
          </div>

          {mode === "upload" ? (
            <label className="paper-drop-field">
              <span>PDF file</span>
              <input type="file" accept="application/pdf,.pdf" required
                onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
              <small>{file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MiB` : "Maximum 250 MiB and 500 pages."}</small>
            </label>
          ) : (
            <label className="stacked">
              Mounted PDF path
              <input value={localPath} onChange={(event) => setLocalPath(event.target.value)}
                placeholder="papers/example.pdf" required />
              <small>Use a path available inside the configured paper/source mount.</small>
            </label>
          )}

          <label className="stacked">
            Project title <small>(optional)</small>
            <input value={title} onChange={(event) => setTitle(event.target.value)}
              placeholder="Uses the PDF filename when blank" />
          </label>
        </section>

        <section className="card paper-import-options" aria-labelledby="paper-extraction-title">
          <h3 id="paper-extraction-title">2. Extraction and privacy</h3>
          <fieldset>
            <legend>OCR languages</legend>
            <p className="meta">Select every language that appears in scanned pages.</p>
            <div className="paper-language-grid">
              {OCR_LANGUAGES.map((language) => (
                <label key={language.key}>
                  <input type="checkbox" checked={ocrLanguages.includes(language.key)}
                    onChange={() => toggleLanguage(language.key)} />
                  {language.label}
                </label>
              ))}
            </div>
          </fieldset>

          <label className="paper-policy-option">
            <input type="checkbox" checked={localOnly}
              onChange={(event) => setLocalOnly(event.target.checked)} />
            <span>
              <b>Local-only processing</b>
              <small>Keep analysis, embeddings, Q&amp;A, voices, tags, and generated artifacts on local providers.</small>
            </span>
          </label>
          <p className="notice">
            The original PDF never cloud-syncs. This processing choice locks when the first job starts;
            import a revised PDF as a new project.
          </p>

          <label className="paper-policy-option">
            <input type="checkbox" checked={analyze}
              onChange={(event) => setAnalyze(event.target.checked)} />
            <span>
              <b>Analyze and draft audience plans after import</b>
              <small>Extraction stops for your review if any nontrivial page is graded poor.</small>
            </span>
          </label>

          <fieldset className="paper-audience-select">
            <legend>Audience plans to draft</legend>
            <p className="meta">Tracks share evidence, but remain independently editable, approvable, runnable, and deletable.</p>
            <div className="paper-import-audience-grid">
              {PAPER_AUDIENCES.map((audience) => (
                <label key={audience.key}>
                  <input type="checkbox" checked={audiences.includes(audience.key)}
                    onChange={() => toggleAudience(audience.key)} />
                  <span><b>{audience.label}</b><small>{audience.description}</small></span>
                </label>
              ))}
            </div>
          </fieldset>
        </section>

        <section className="paper-import-submit">
          <div>
            <b>Dense-paper limits</b>
            <p className="meta">250 MiB · 500 pages · 5 million extracted characters. Oversized inputs fail visibly; they are never sampled or silently truncated.</p>
          </div>
          <button type="submit" className="primary" disabled={working}>
            {working
              ? uploadProgress !== null ? `Uploading ${uploadProgress}%…` : "Importing…"
              : analyze ? "Import and analyze paper" : "Import paper"}
          </button>
          {working && mode === "upload" && (
            <button type="button" onClick={() => uploadRequest.current?.abort()}>Cancel upload</button>
          )}
        </section>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}
