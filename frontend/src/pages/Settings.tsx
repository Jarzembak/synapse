import { useEffect, useRef, useState } from "react";
import {
  api,
  GitHubCredentialStatus,
  QuickRefCategory,
  RepositorySettings,
} from "../api";

interface ModelCfg { provider: string; model: string }
interface TagInfo { id: number; name: string; kind: string; count: number }
interface PromptInfo { label: string; value: string; modified: boolean }
interface Params { temperature?: number | null; max_tokens?: number | null }
interface VoicesState { kokoro: Record<string, string>; piper: Record<string, string>; gemini: Record<string, string> }
interface ProfileInfo { label: string; description: string; steps: string[]; custom?: boolean }
interface StepInfo { name: string; label: string }
interface SearchConfig { semantic_enabled: boolean; embedding_provider: string; embedding_model: string }
interface ProviderModelsInfo { configured: boolean; ok: boolean; models: string[]; detail: string }
interface SearchStatus { chunks: number; embeddings: number; semantic_enabled: boolean; embedding_model: string }
interface BackupConfig {
  retention: number;
  schedule_hours: number;
  include_media: boolean;
  include_repositories: boolean;
  last?: { at?: string; status?: string; path?: string } | null;
}
interface CloudState {
  provider: string;
  providers: string[];
  all_fields: Record<string, Record<string, boolean>>;
  config: Record<string, string>;
  remote_base: string;
  auto: boolean;
  mode: string;
  last_sync: { status: string; detail: string; at: string } | null;
}

// A model field that offers the provider's actual models as a dropdown while
// still allowing any custom name. Falls back to a plain input when the
// provider's model list isn't available (server down, key not set).
function ModelPicker({ value, models, onCommit, onDraft }: {
  value: string;
  models: string[];
  onCommit: (model: string) => void;   // save (dropdown pick / input blur)
  onDraft: (model: string) => void;    // local state only (while typing)
}) {
  // Explicit state, not derived from value∈models: deriving would unmount the
  // input mid-typing the moment a draft matches a listed model, swallowing
  // the blur that commits it.
  const [customMode, setCustomMode] = useState(false);
  if (models.length === 0) {
    return (
      <input value={value} placeholder="model name"
        onChange={(e) => onDraft(e.target.value)}
        onBlur={(e) => { if (e.target.value.trim()) onCommit(e.target.value.trim()); }} />
    );
  }
  const showCustom = customMode || !models.includes(value);
  return (
    <span className="row">
      <select value={showCustom ? "__custom__" : value}
        onChange={(e) => {
          if (e.target.value === "__custom__") setCustomMode(true);
          else { setCustomMode(false); onCommit(e.target.value); }
        }}>
        {models.map((m) => <option key={m} value={m}>{m}</option>)}
        <option value="__custom__">custom…</option>
      </select>
      {showCustom && (
        <input value={value} placeholder="model name"
          onChange={(e) => onDraft(e.target.value)}
          onBlur={(e) => {
            const draft = e.target.value.trim();
            if (draft) { onCommit(draft); setCustomMode(false); }
          }} />
      )}
    </span>
  );
}

const FN_LABELS: Record<string, string> = {
  asr: "Transcription (ASR)",
  correct: "Transcript correction",
  summarize: "Summary",
  deepdive_claude: "Deep dive — Claude",
  deepdive_gemini: "Deep dive — Gemini",
  merge: "Deep-dive merge",
  quickref: "Quick-references",
  podcast_script: "Podcast script",
  tts: "Podcast TTS",
  trim_spans: "Trim span detection",
  mindmap: "Mind map",
  tag: "Auto-tagging",
  library_qa: "Library grounded Q&A",
  download: "Media download",
};

const LOCAL_PROVIDERS = ["ollama", "openai_compat"];

const CLOUD_LABELS: Record<string, string> = {
  s3: "S3-compatible (AWS / MinIO / B2 / Wasabi)",
  webdav: "WebDAV (Nextcloud / ownCloud)",
  drive: "Google Drive",
  dropbox: "Dropbox",
  onedrive: "OneDrive",
};

const OAUTH_HINT = "Run `rclone authorize \"<provider>\"` on any machine with a " +
  "browser (rclone.org downloads), approve access, and paste the token JSON here.";

interface CatDraft {
  label: string; plural: string; icon: string; description: string; prompt: string;
}

const NEW_CAT_PROMPT = `Create a quick-reference document for the given subject, based on this
deep-dive material. Structure (markdown):
# <name>
## What it is
## Why it matters
## Details
## Examples          (from the source material, cited as 'From: <video title>')
## Further study`;

export default function Settings() {
  const [functions, setFunctions] = useState<Record<string, ModelCfg>>({});
  const [providers, setProviders] = useState<string[]>([]);
  const [providerOptions, setProviderOptions] = useState<Record<string, string[]>>({});
  const [providerModels, setProviderModels] = useState<Record<string, ProviderModelsInfo>>({});
  const [pullName, setPullName] = useState("");
  const [voices, setVoices] = useState<VoicesState | null>(null);
  const [profiles, setProfiles] = useState<Record<string, ProfileInfo>>({});
  const [steps, setSteps] = useState<StepInfo[]>([]);
  const [profileDraft, setProfileDraft] = useState({
    key: "", label: "", description: "", steps: [] as string[],
  });
  const [editingProfileKey, setEditingProfileKey] = useState<string | null>(null);
  const [searchConfig, setSearchConfig] = useState<SearchConfig | null>(null);
  const [searchStatus, setSearchStatus] = useState<SearchStatus | null>(null);
  const [backupConfig, setBackupConfig] = useState<BackupConfig | null>(null);
  const [githubCredential, setGithubCredential] = useState<GitHubCredentialStatus | null>(null);
  const [githubToken, setGithubToken] = useState("");
  const [githubPending, setGithubPending] = useState<"save" | "remove" | "settings" | "">("");
  const [repositorySettings, setRepositorySettings] = useState<RepositorySettings | null>(null);
  const [reindexing, setReindexing] = useState(false);
  const [jobNotifications, setJobNotifications] = useState(
    () => localStorage.getItem("synapse.jobNotifications") === "on",
  );
  const [glossary, setGlossary] = useState("");
  const [tags, setTags] = useState<TagInfo[]>([]);
  const [maxHeight, setMaxHeight] = useState(1080);
  const [prompts, setPrompts] = useState<Record<string, PromptInfo>>({});
  const [params, setParams] = useState<Record<string, Params>>({});
  const [adv, setAdv] = useState<Record<string, Record<string, any>>>({});
  const [cloud, setCloud] = useState<CloudState | null>(null);
  const [cloudEdit, setCloudEdit] = useState<Record<string, string>>({});
  const [qrCats, setQrCats] = useState<QuickRefCategory[]>([]);
  const [newCat, setNewCat] = useState<CatDraft | null>(null);
  const [catBanner, setCatBanner] = useState("");
  const [saved, setSaved] = useState("");
  const [savedError, setSavedError] = useState(false);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function load() {
    Promise.all([
      api<{ functions: Record<string, ModelCfg>; providers: string[]; provider_options: Record<string, string[]> }>("/settings/models")
        .then((r) => {
          setFunctions(r.functions);
          setProviders(r.providers);
          setProviderOptions(r.provider_options);
        }),
      api<Record<string, ProviderModelsInfo>>("/settings/provider-models").then(setProviderModels),
      api<VoicesState>("/settings/voices").then(setVoices),
      api<Record<string, ProfileInfo>>("/settings/profiles").then(setProfiles),
      api<StepInfo[]>("/projects/steps").then(setSteps),
      api<SearchConfig>("/settings/search").then(setSearchConfig),
      api<SearchStatus>("/library/index/status").then(setSearchStatus),
      api<BackupConfig>("/settings/backup").then(setBackupConfig),
      api<GitHubCredentialStatus>("/repositories/credentials").then(setGithubCredential),
      api<RepositorySettings>("/repositories/settings").then(setRepositorySettings),
      api<{ terms: string[] }>("/settings/glossary").then((r) => setGlossary(r.terms.join("\n"))),
      api<TagInfo[]>("/tags").then(setTags),
      api<{ max_height: number }>("/settings/download").then((r) => setMaxHeight(r.max_height)),
      api<Record<string, PromptInfo>>("/settings/prompts").then(setPrompts),
      api<Record<string, Params>>("/settings/params").then(setParams),
      api<{ groups: Record<string, Record<string, any>> }>("/settings/advanced")
        .then((r) => setAdv(r.groups)),
      api<CloudState>("/settings/cloud").then((r) => { setCloud(r); setCloudEdit(r.config); }),
      api<QuickRefCategory[]>("/quickrefs/categories").then(setQrCats),
    ]).catch((e) => flash(`couldn't load settings: ${e.message}`, true));
  }
  useEffect(() => {
    load();
    return () => {
      if (flashTimer.current) clearTimeout(flashTimer.current);
    };
  }, []);

  function flash(msg: string, isError = false) {
    setSaved(msg);
    setSavedError(isError);
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setSaved(""), isError ? 4000 : 1500);
  }

  async function saveModel(fn: string, cfg: ModelCfg) {
    const prev = functions[fn];
    setFunctions((p) => ({ ...p, [fn]: cfg }));
    try {
      await api(`/settings/models/${fn}`, { method: "PUT", body: JSON.stringify(cfg) });
      flash(`saved ${fn}`);
    } catch (e: any) {
      setFunctions((p) => ({ ...p, [fn]: prev }));
      api<{ functions: Record<string, ModelCfg> }>("/settings/models")
        .then((result) => setFunctions(result.functions)).catch(() => {});
      flash(`save failed: ${e.message}`, true);
    }
  }

  async function saveGlossary() {
    try {
      await api("/settings/glossary", {
        method: "PUT", body: JSON.stringify({ terms: glossary.split("\n") }),
      });
      flash("glossary saved");
    } catch (e: any) { flash(`save failed: ${e.message}`, true); }
  }

  async function saveMaxHeight(v: number) {
    const prev = maxHeight;
    setMaxHeight(v);
    try {
      await api("/settings/download", { method: "PUT", body: JSON.stringify({ max_height: v }) });
      flash("download quality saved");
    } catch (e: any) {
      setMaxHeight(prev);
      flash(`save failed: ${e.message}`, true);
    }
  }

  async function saveVoices() {
    if (!voices) return;
    try {
      await api("/settings/voices", { method: "PUT", body: JSON.stringify(voices) });
      flash("voice assignments saved");
    } catch (e: any) { flash(`save failed: ${e.message}`, true); }
  }

  async function saveProfile() {
    const key = profileDraft.key.trim();
    if (!key || !profileDraft.label.trim() || profileDraft.steps.length === 0) {
      flash("a profile needs a key, label, and at least one step", true);
      return;
    }
    try {
      const stored = await api<ProfileInfo & { key: string }>(`/settings/profiles/${encodeURIComponent(key)}`, {
        method: "PUT",
        body: JSON.stringify({
          label: profileDraft.label,
          description: profileDraft.description,
          steps: profileDraft.steps,
        }),
      });
      setProfiles((current) => ({ ...current, [stored.key]: stored }));
      setProfileDraft({ key: "", label: "", description: "", steps: [] });
      setEditingProfileKey(null);
      flash("pipeline profile saved");
    } catch (e: any) { flash(`profile save failed: ${e.message}`, true); }
  }

  async function deleteProfile(key: string) {
    if (!confirm(`Delete the pipeline profile “${profiles[key].label}”?`)) return;
    try {
      await api(`/settings/profiles/${encodeURIComponent(key)}`, { method: "DELETE" });
      setProfiles((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      if (editingProfileKey === key) {
        setEditingProfileKey(null);
        setProfileDraft({ key: "", label: "", description: "", steps: [] });
      }
      flash("pipeline profile deleted");
    } catch (e: any) { flash(`delete failed: ${e.message}`, true); }
  }

  // every control in Library intelligence saves on change (same self-saving
  // behavior as the model matrix), so this takes the next config explicitly
  async function saveSearchConfig(next: SearchConfig) {
    setSearchConfig(next);
    try {
      await api("/settings/search", { method: "PUT", body: JSON.stringify(next) });
      setSearchStatus((current) => current && ({
        ...current,
        semantic_enabled: next.semantic_enabled,
        embedding_model: next.embedding_model,
      }));
      flash("library search settings saved");
    } catch (e: any) { flash(`save failed: ${e.message}`, true); }
  }

  async function rebuildSearchIndex() {
    setReindexing(true);
    try {
      await api("/library/reindex", { method: "POST" });
      flash("search reindex queued — progress appears in Jobs");
    } catch (e: any) { flash(`reindex failed: ${e.message}`, true); }
    finally { setReindexing(false); }
  }

  async function saveBackup() {
    if (!backupConfig) return;
    try {
      await api("/settings/backup", { method: "PUT", body: JSON.stringify({
        retention: backupConfig.retention,
        schedule_hours: backupConfig.schedule_hours,
        include_media: backupConfig.include_media,
        include_repositories: backupConfig.include_repositories,
      }) });
      flash("backup policy saved");
    } catch (e: any) { flash(`save failed: ${e.message}`, true); }
  }

  async function saveGitHubCredential() {
    const token = githubToken.trim();
    if (!token) {
      flash("paste a GitHub token to save it", true);
      return;
    }
    setGithubPending("save");
    try {
      const status = await api<GitHubCredentialStatus>("/repositories/credentials", {
        method: "PUT",
        body: JSON.stringify({ token }),
      });
      setGithubCredential(status);
      setGithubToken("");
      flash("GitHub token encrypted and saved; repository access is checked during inspection");
    } catch (e: any) {
      flash(`GitHub token could not be saved: ${e.message}`, true);
    } finally {
      setGithubPending("");
    }
  }

  async function removeGitHubCredential() {
    if (!confirm("Remove the saved GitHub token? Existing snapshots and generated guides remain available, but private repositories cannot be imported or updated.")) return;
    setGithubPending("remove");
    try {
      const status = await api<GitHubCredentialStatus>("/repositories/credentials", {
        method: "DELETE",
      });
      setGithubCredential(status);
      setGithubToken("");
      flash("GitHub token removed");
    } catch (e: any) {
      flash(`could not remove GitHub token: ${e.message}`, true);
    } finally {
      setGithubPending("");
    }
  }

  async function saveRepositorySettings() {
    if (!repositorySettings) return;
    setGithubPending("settings");
    try {
      const stored = await api<RepositorySettings>("/repositories/settings", {
        method: "PUT",
        body: JSON.stringify({
          local_model: repositorySettings.local_model,
          limits: repositorySettings.limits,
          default_exclusions: repositorySettings.default_exclusions,
        }),
      });
      setRepositorySettings(stored);
      flash("repository analysis settings saved");
    } catch (e: any) {
      flash(`repository settings save failed: ${e.message}`, true);
    } finally {
      setGithubPending("");
    }
  }

  async function toggleJobNotifications() {
    if (jobNotifications) {
      localStorage.removeItem("synapse.jobNotifications");
      setJobNotifications(false);
      flash("completion notifications disabled");
      return;
    }
    if (!("Notification" in window)) {
      flash("this browser does not support desktop notifications", true);
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      flash("notification permission was not granted", true);
      return;
    }
    localStorage.setItem("synapse.jobNotifications", "on");
    setJobNotifications(true);
    flash("completion notifications enabled");
  }

  async function savePrompt(name: string) {
    try {
      await api(`/settings/prompts/${name}`, {
        method: "PUT", body: JSON.stringify({ value: prompts[name].value }),
      });
      setPrompts(await api<Record<string, PromptInfo>>("/settings/prompts"));
      flash(`prompt saved: ${name}`);
    } catch (e: any) { flash(`save failed: ${e.message}`, true); }
  }

  async function resetPrompt(name: string) {
    try {
      const r = await api<{ default: string }>(`/settings/prompts/${name}`, { method: "DELETE" });
      setPrompts((p) => ({ ...p, [name]: { ...p[name], value: r.default, modified: false } }));
      flash(`prompt reset: ${name}`);
    } catch (e: any) { flash(`reset failed: ${e.message}`, true); }
  }

  async function saveParams(fn: string) {
    try {
      await api(`/settings/params/${fn}`, {
        method: "PUT", body: JSON.stringify(params[fn] ?? {}),
      });
      flash(`params saved: ${fn}`);
    } catch (e: any) { flash(`save failed: ${e.message}`, true); }
  }

  async function saveAdvanced(group: string) {
    try {
      await api(`/settings/advanced/${group}`, {
        method: "PUT", body: JSON.stringify({ values: adv[group] }),
      });
      flash(`${group} settings saved`);
    } catch (e: any) { flash(`save failed: ${e.message}`, true); }
  }

  async function saveCloud() {
    if (!cloud) return;
    try {
      await api("/settings/cloud", {
        method: "PUT",
        body: JSON.stringify({
          provider: cloud.provider,
          config: cloudEdit,
          remote_base: cloud.remote_base,
          auto: cloud.auto,
          mode: cloud.mode,
        }),
      });
      const refreshed = await api<CloudState>("/settings/cloud");
      setCloud(refreshed);
      setCloudEdit(refreshed.config);
      flash("cloud settings saved");
    } catch (e: any) { flash(`save failed: ${e.message}`, true); }
  }

  async function cloudSyncNow() {
    try {
      await api("/settings/cloud/sync", { method: "POST" });
      flash("full sync queued — watch the job ticker");
    } catch (e: any) {
      flash(`sync failed: ${e.message}`, true);
    }
  }

  const refreshProviderModels = () =>
    api<Record<string, ProviderModelsInfo>>("/settings/provider-models")
      .then(setProviderModels).catch(() => {});

  async function pullOllamaModel() {
    const model = pullName.trim();
    if (!model) return;
    try {
      await api("/settings/ollama/pull", {
        method: "POST", body: JSON.stringify({ model }),
      });
      setPullName("");
      flash(`installing ${model} — progress shows in Jobs; use ⟳ when it finishes`);
    } catch (e: any) { flash(`install failed: ${e.message}`, true); }
  }

  // tag ops refresh only the tag list — a full load() would clobber unsaved
  // edits elsewhere on the page (prompts, category drafts)
  const reloadTags = () => api<TagInfo[]>("/tags").then(setTags);

  async function renameTag(t: TagInfo) {
    const name = prompt(`Rename tag "${t.name}" to:`, t.name);
    if (!name || name === t.name) return;
    try {
      await api(`/tags/${t.id}`, { method: "PUT", body: JSON.stringify({ name }) });
      await reloadTags();
      flash("tag renamed");
    } catch (e: any) { flash(`rename failed: ${e.message}`, true); }
  }

  async function deleteTag(t: TagInfo) {
    if (!confirm(`Delete tag "${t.name}" (used ${t.count}×)?`)) return;
    try {
      await api(`/tags/${t.id}`, { method: "DELETE" });
      await reloadTags();
      flash("tag deleted");
    } catch (e: any) { flash(`delete failed: ${e.message}`, true); }
  }

  async function addTag() {
    const name = prompt("New tag name:");
    if (!name) return;
    try {
      await api("/tags", { method: "POST", body: JSON.stringify({ name }) });
      await reloadTags();
      flash("tag added");
    } catch (e: any) { flash(`add failed: ${e.message}`, true); }
  }

  const setAdvValue = (group: string, key: string, value: any) =>
    setAdv((a) => ({ ...a, [group]: { ...a[group], [key]: value } }));

  const reloadCats = () =>
    api<QuickRefCategory[]>("/quickrefs/categories").then(setQrCats);

  const setCatField = (key: string, field: string, value: string) =>
    setQrCats((cs) => cs.map((c) => (c.key === key ? { ...c, [field]: value } : c)));

  async function addCategory() {
    if (!newCat) return;
    try {
      await api("/quickrefs/categories", { method: "POST", body: JSON.stringify(newCat) });
      setCatBanner(newCat.label);
      setNewCat(null);
      await reloadCats();
      flash("category added");
    } catch (e: any) {
      flash(`category creation failed: ${e.message}`, true);
    }
  }

  async function saveCategory(c: QuickRefCategory) {
    let stored: QuickRefCategory;
    try {
      stored = await api<QuickRefCategory>(`/quickrefs/categories/${c.key}`, {
        method: "PUT",
        body: JSON.stringify({ label: c.label, plural: c.plural, icon: c.icon,
                               description: c.description, prompt: c.prompt }),
      });
    } catch (e: any) {
      flash(`category save failed: ${e.message}`, true);
      return;
    }
    // show what the server actually stored (it trims whitespace)
    setQrCats((cs) => cs.map((x) => (x.key === c.key ? { ...x, ...stored } : x)));
    flash(`category saved: ${stored.label}`);
  }

  async function deleteCategory(c: QuickRefCategory) {
    if (!confirm(`Delete quick-ref category "${c.label}"?`)) return;
    try {
      await api(`/quickrefs/categories/${c.key}`, { method: "DELETE" });
      await reloadCats();
      flash("category deleted");
    } catch (e: any) {
      flash(`category delete failed: ${e.message}`, true);
    }
  }

  return (
    <div className="settings">
      {saved && (
        <div className={`flash ${savedError ? "flash-error" : ""}`}
          role={savedError ? "alert" : "status"}>{saved}</div>
      )}

      <section id="github-access" className="settings-section github-settings" aria-labelledby="github-access-title">
        <h2 id="github-access-title">GitHub repository access</h2>
        <p className="meta">
          Public repositories work without credentials. For private repositories, use a
          fine-grained personal access token limited to selected repositories with read-only
          <b> Contents</b> permission. Synapse encrypts the token before storing it and never
          puts it in repository URLs, logs, jobs, or generated guides.
        </p>
        <div className="settings-grid github-settings-grid">
          <div className="card credential-card">
            <h3>Private repository token</h3>
            {githubCredential?.configured ? (
              <p className="credential-status">
                <span className={`jobstatus ${githubCredential.valid === false ? "error" : "done"}`}>
                  {githubCredential.valid === false ? "Needs attention" : "Configured"}
                </span>
                {(githubCredential.masked_token || githubCredential.token) && (
                  <code>{githubCredential.masked_token || githubCredential.token}</code>
                )}
                {githubCredential.login && <span>GitHub account: <b>{githubCredential.login}</b></span>}
              </p>
            ) : (
              <p className="notice">No token is stored. Private repository imports will ask you to configure one.</p>
            )}
            <label className="stacked" htmlFor="github-token">
              {githubCredential?.configured ? "Replace token" : "Fine-grained token"}
              <input id="github-token" type="password" autoComplete="new-password"
                value={githubToken} placeholder="github_pat_..."
                onChange={(event) => setGithubToken(event.target.value)} />
            </label>
            <div className="row">
              <button type="button" onClick={() => void saveGitHubCredential()}
                disabled={!githubToken.trim() || githubPending !== ""}>
                {githubPending === "save" ? "Saving..." : "Encrypt and save token"}
              </button>
              {githubCredential?.configured && (
                <button type="button" className="linkish danger"
                  onClick={() => void removeGitHubCredential()} disabled={githubPending !== ""}>
                  {githubPending === "remove" ? "Removing..." : "Remove token"}
                </button>
              )}
            </div>
            {githubCredential?.message && <p className="meta" role="status">{githubCredential.message}</p>}
          </div>

          <div className="card repository-model-card">
            <h3>Local repository analysis</h3>
            <p>
              Public and private repository source are <b>always local-only</b> in this release.
              Every language-model step is forced through a local Ollama endpoint, regardless of
              the model matrix below. Repository-derived artifacts are excluded from cloud sync.
            </p>
            {repositorySettings && (
              <>
                <label className="stacked" htmlFor="repository-local-model">Ollama model
                  <input id="repository-local-model" value={repositorySettings.local_model}
                    placeholder="qwen3:8b"
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings, local_model: event.target.value,
                    })} />
                </label>
                <p className="hint">The model must already be available to the configured Ollama service.</p>
              </>
            )}
          </div>
        </div>

        {repositorySettings && (
          <details className="advanced repository-limits">
            <summary>Repository safety limits <small>— bounds for static snapshots and analysis</small></summary>
            <div className="knobs">
              {repositorySettings.limits.max_download_bytes !== undefined && (
                <label>Maximum archive size (MB)
                  <input type="number" min="1" value={Math.round(repositorySettings.limits.max_download_bytes / 1_048_576)}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: {
                        ...repositorySettings.limits,
                        max_download_bytes: Number(event.target.value) * 1_048_576,
                      },
                    })} />
                </label>
              )}
              {repositorySettings.limits.max_unpacked_bytes !== undefined && (
                <label>Maximum expanded size (MB)
                  <input type="number" min="1" value={Math.round(repositorySettings.limits.max_unpacked_bytes / 1_048_576)}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: {
                        ...repositorySettings.limits,
                        max_unpacked_bytes: Number(event.target.value) * 1_048_576,
                      },
                    })} />
                </label>
              )}
              {repositorySettings.limits.max_files !== undefined && (
                <label>Maximum files
                  <input type="number" min="1" value={repositorySettings.limits.max_files}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: { ...repositorySettings.limits, max_files: Number(event.target.value) },
                    })} />
                </label>
              )}
              {repositorySettings.limits.max_file_bytes !== undefined && (
                <label>Maximum single file (MB)
                  <input type="number" min="1" value={Math.round(repositorySettings.limits.max_file_bytes / 1_048_576)}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: {
                        ...repositorySettings.limits,
                        max_file_bytes: Number(event.target.value) * 1_048_576,
                      },
                    })} />
                </label>
              )}
              {repositorySettings.limits.max_text_file_bytes !== undefined && (
                <label>Maximum indexed text file (MB)
                  <input type="number" min="1" value={Math.round(repositorySettings.limits.max_text_file_bytes / 1_048_576)}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: {
                        ...repositorySettings.limits,
                        max_text_file_bytes: Number(event.target.value) * 1_048_576,
                      },
                    })} />
                </label>
              )}
              {repositorySettings.limits.max_indexed_bytes !== undefined && (
                <label>Maximum indexed source (MB)
                  <input type="number" min="1" value={Math.round(repositorySettings.limits.max_indexed_bytes / 1_048_576)}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: {
                        ...repositorySettings.limits,
                        max_indexed_bytes: Number(event.target.value) * 1_048_576,
                      },
                    })} />
                </label>
              )}
              {repositorySettings.limits.chunk_lines !== undefined && (
                <label>Evidence chunk lines
                  <input type="number" min="10" max="5000" value={repositorySettings.limits.chunk_lines}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: { ...repositorySettings.limits, chunk_lines: Number(event.target.value) },
                    })} />
                </label>
              )}
              {repositorySettings.limits.chunk_chars !== undefined && (
                <label>Evidence chunk characters
                  <input type="number" min="1000" value={repositorySettings.limits.chunk_chars}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: { ...repositorySettings.limits, chunk_chars: Number(event.target.value) },
                    })} />
                </label>
              )}
              {repositorySettings.limits.max_map_chunks !== undefined && (
                <label>Maximum model map chunks
                  <input type="number" min="1" max="5000" value={repositorySettings.limits.max_map_chunks}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: { ...repositorySettings.limits, max_map_chunks: Number(event.target.value) },
                    })} />
                </label>
              )}
              {repositorySettings.limits.max_map_input_chars !== undefined && (
                <label>Maximum map input characters
                  <input type="number" min="10000" value={repositorySettings.limits.max_map_input_chars}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: { ...repositorySettings.limits, max_map_input_chars: Number(event.target.value) },
                    })} />
                </label>
              )}
              {repositorySettings.limits.max_compression_ratio !== undefined && (
                <label>Maximum archive compression ratio
                  <input type="number" min="2" max="1000" value={repositorySettings.limits.max_compression_ratio}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      limits: { ...repositorySettings.limits, max_compression_ratio: Number(event.target.value) },
                    })} />
                </label>
              )}
              {repositorySettings.default_exclusions && (
                <label className="stacked">Default exclusions
                  <textarea rows={7} value={repositorySettings.default_exclusions.join("\n")}
                    onChange={(event) => setRepositorySettings({
                      ...repositorySettings,
                      default_exclusions: event.target.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
                    })} />
                </label>
              )}
              <button type="button" onClick={() => void saveRepositorySettings()}
                disabled={githubPending !== "" || !repositorySettings.local_model.trim()}>
                {githubPending === "settings" ? "Saving..." : "Save repository settings"}
              </button>
            </div>
          </details>
        )}
      </section>

      <h2>Model matrix</h2>
      <p className="meta">
        Which model runs each pipeline function. A provider is a <i>kind of
        server</i>, not a location: <b>ollama</b> talks to an Ollama server
        (the bundled container, or any box via OLLAMA_BASE_URL);{" "}
        <b>openai_compat</b> talks to an OpenAI-compatible server that{" "}
        <i>isn't</i> OpenAI itself — LM Studio, llama.cpp, vLLM, LocalAI —
        local or remote, via OPENAI_COMPAT_BASE_URL;{" "}
        <b>anthropic</b>/<b>gemini</b>/<b>openai</b> are the vendors' cloud
        APIs. The model dropdown lists what the selected provider actually
        offers (installed models for local servers; the vendor's catalog for
        cloud APIs) — or pick "custom…" to type any name. ASR providers:{" "}
        <b>faster-whisper</b> (local) or <b>gemini</b>. TTS:{" "}
        <b>Piper</b>/<b>Kokoro</b> (local) or <b>gemini</b>.
      </p>
      <table className="list">
        <thead><tr><th>Function</th><th>Provider</th><th>Model</th></tr></thead>
        <tbody>
          {Object.entries(functions).map(([fn, cfg]) => (
            <tr key={fn}>
              <td>{FN_LABELS[fn] ?? fn}</td>
              <td>
                <select
                  value={cfg.provider}
                  onChange={(e) => saveModel(fn, { ...cfg, provider: e.target.value })}
                >
                  {[...new Set([...(providerOptions[fn] ?? providers), cfg.provider])].map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </td>
              <td>
                <ModelPicker
                  value={cfg.model}
                  models={providerModels[cfg.provider]?.models ?? []}
                  onCommit={(model) => void saveModel(fn, { ...cfg, model })}
                  onDraft={(model) => setFunctions((current) => ({
                    ...current, [fn]: { ...cfg, model },
                  }))}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="meta">
        {LOCAL_PROVIDERS.concat(["anthropic", "gemini", "openai"]).map((p) => {
          const info = providerModels[p];
          if (!info) return null;
          const status = !info.configured ? "not configured"
            : info.ok ? `${info.models.length} model(s)`
            : `unreachable — ${info.detail.slice(0, 160)}`;
          return <span key={p}><b>{p}</b>: {status} · </span>;
        })}
        <button type="button" className="linkish" title="Refresh model lists"
          onClick={() => void refreshProviderModels()}>⟳ refresh</button>
      </p>
      <div className="row">
        <input value={pullName} placeholder="Install an Ollama model, e.g. qwen3:8b"
          onChange={(e) => setPullName(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void pullOllamaModel(); }} />
        <button type="button" onClick={() => void pullOllamaModel()}>Install</button>
      </div>
      <p className="meta">
        Ollama models must be downloaded before a step can use them. They come
        from Ollama's registry — browse{" "}
        <a href="https://ollama.com/library" target="_blank" rel="noreferrer">
          ollama.com/library</a> for names and sizes ("name:tag", e.g.{" "}
        <code>qwen3:8b</code>). Installs run as background jobs (see Jobs) and
        land on whichever Ollama server OLLAMA_BASE_URL points at.
      </p>

      <h2>Podcast voices</h2>
      <p className="meta">
        Assign the two podcast hosts for every supported speech engine. The active TTS
        provider in the model matrix chooses which pair is used.
      </p>
      {voices && (
        <div className="settings-grid voice-grid">
          {(["piper", "kokoro", "gemini"] as const).map((engine) => (
            <fieldset key={engine} className="card">
              <legend>{engine}</legend>
              {(["HOST_A", "HOST_B"] as const).map((host) => (
                <label key={host}>{host === "HOST_A" ? "Host A" : "Host B"}
                  <input value={voices[engine][host] ?? ""}
                    onChange={(event) => setVoices((current) => current && ({
                      ...current,
                      [engine]: { ...current[engine], [host]: event.target.value },
                    }))} />
                </label>
              ))}
            </fieldset>
          ))}
          <div><button type="button" onClick={() => void saveVoices()}>Save voices</button></div>
        </div>
      )}

      <h2>Pipeline profiles</h2>
      <p className="meta">
        Profiles let each project run only the outputs you need. Missing prerequisites are
        included automatically, and completed outputs only run again when their inputs or
        settings have changed.
      </p>
      <div className="profile-list">
        {Object.entries(profiles).map(([key, profile]) => (
          <article className="card profile-card" key={key}>
            <h3>{profile.label} {profile.custom && <small>custom</small>}</h3>
            <p>{profile.description}</p>
            <p className="meta">{profile.steps.map((name) =>
              steps.find((step) => step.name === name)?.label ?? name).join(" · ")}</p>
            {profile.custom && (
              <div className="row">
                <button type="button" onClick={() => {
                  setProfileDraft({
                    key, label: profile.label, description: profile.description,
                    steps: [...profile.steps],
                  });
                  setEditingProfileKey(key);
                }}>Edit</button>
                <button type="button" className="linkish danger"
                  onClick={() => void deleteProfile(key)}>Delete</button>
              </div>
            )}
          </article>
        ))}
      </div>
      <details className="advanced" open={profileDraft.key !== ""}>
        <summary>{editingProfileKey ? "Edit custom profile" : "Create a custom profile"}</summary>
        <div className="knobs">
          <label>Key
            <input value={profileDraft.key} placeholder="weekly-review"
              disabled={editingProfileKey !== null}
              onChange={(event) => setProfileDraft({ ...profileDraft, key: event.target.value })} />
          </label>
          <label>Label
            <input value={profileDraft.label} placeholder="Weekly review"
              onChange={(event) => setProfileDraft({ ...profileDraft, label: event.target.value })} />
          </label>
          <label>Description
            <input value={profileDraft.description}
              onChange={(event) => setProfileDraft({ ...profileDraft, description: event.target.value })} />
          </label>
          <fieldset className="step-choices">
            <legend>Outputs</legend>
            {steps.map((step) => (
              <label className="checkline" key={step.name}>
                <input type="checkbox" checked={profileDraft.steps.includes(step.name)}
                  onChange={(event) => setProfileDraft((current) => ({
                    ...current,
                    steps: event.target.checked
                      ? [...current.steps, step.name]
                      : current.steps.filter((name) => name !== step.name),
                  }))} />
                {step.label}
              </label>
            ))}
          </fieldset>
          <div className="row">
            <button type="button" onClick={() => void saveProfile()}>Save profile</button>
            {editingProfileKey && (
              <button type="button" onClick={() => {
                setEditingProfileKey(null);
                setProfileDraft({ key: "", label: "", description: "", steps: [] });
              }}>Cancel edit</button>
            )}
          </div>
        </div>
      </details>

      <h2>Library intelligence</h2>
      {searchConfig && (
        <div className="knobs">
          <label className="checkline">
            <input type="checkbox" checked={searchConfig.semantic_enabled}
              onChange={(event) => void saveSearchConfig({
                ...searchConfig, semantic_enabled: event.target.checked,
              })} />
            blend semantic similarity with exact text search
          </label>
          <label>Embedding provider
            <select value={searchConfig.embedding_provider}
              onChange={(event) => void saveSearchConfig({
                ...searchConfig, embedding_provider: event.target.value,
              })}>
              <option value="ollama">ollama</option>
              <option value="openai_compat">openai_compat</option>
            </select>
          </label>
          <label>Embedding model
            <ModelPicker
              value={searchConfig.embedding_model}
              models={providerModels[searchConfig.embedding_provider]?.models ?? []}
              onCommit={(model) => void saveSearchConfig({ ...searchConfig, embedding_model: model })}
              onDraft={(model) => setSearchConfig({ ...searchConfig, embedding_model: model })}
            />
          </label>
          <div className="row">
            <button type="button" onClick={() => void rebuildSearchIndex()} disabled={reindexing}>
              {reindexing ? "Queuing…" : "Rebuild search index"}
            </button>
          </div>
          <p className="meta">Semantic search remains optional; exact full-text search always works.</p>
          {searchStatus && (
            <p className="meta">
              {searchStatus.chunks.toLocaleString()} retrieval chunks · {searchStatus.embeddings.toLocaleString()} embedded
              {searchStatus.semantic_enabled && searchStatus.embeddings < searchStatus.chunks
                ? " · rebuild pending or incomplete" : ""}
            </p>
          )}
        </div>
      )}

      <h2>Backups</h2>
      {backupConfig && (
        <div className="knobs">
          <label>Keep newest backups
            <input type="number" min="1" max="100" value={backupConfig.retention}
              onChange={(event) => setBackupConfig({
                ...backupConfig, retention: Number(event.target.value),
              })} />
          </label>
          <label>Automatic interval (hours; 0 disables)
            <input type="number" min="0" max={24 * 30} value={backupConfig.schedule_hours}
              onChange={(event) => setBackupConfig({
                ...backupConfig, schedule_hours: Number(event.target.value),
              })} />
          </label>
          <label className="checkline">
            <input type="checkbox" checked={backupConfig.include_media}
              onChange={(event) => setBackupConfig({
                ...backupConfig, include_media: event.target.checked,
              })} />
            include archived source and generated audio
          </label>
          <label className="checkline">
            <input type="checkbox" checked={!!backupConfig.include_repositories}
              onChange={(event) => setBackupConfig({
                ...backupConfig, include_repositories: event.target.checked,
              })} />
            include retained raw repository snapshots
          </label>
          <p className="warning">
            Raw repository snapshots may be large and can contain sensitive code.
            Generated guides and the repository evidence index are included regardless of this
            option. If any repository analysis exists, Synapse refuses to create an unencrypted
            backup; set <code>BACKUP_ENCRYPTION_KEY</code> first.
          </p>
          <button type="button" onClick={() => void saveBackup()}>Save backup policy</button>
          <p className="meta">
            Create, verify, and download snapshots from System. Set BACKUP_ENCRYPTION_KEY
            before creating a backup if it must be encrypted at rest.
          </p>
        </div>
      )}

      <h2>Notifications</h2>
      <p className="meta">Optionally show a desktop notification when a top-level job finishes.</p>
      <button type="button" onClick={() => void toggleJobNotifications()}>
        {jobNotifications ? "Disable completion notifications" : "Enable completion notifications"}
      </button>

      <h2>Media downloads</h2>
      <p className="meta">
        Resolution cap for the "Download &amp; keep media" step (video is merged to mp4;
        an audio-only copy is always kept alongside it).
      </p>
      <select value={maxHeight} onChange={(e) => saveMaxHeight(Number(e.target.value))}>
        <option value={720}>720p</option>
        <option value={1080}>1080p</option>
        <option value={1440}>1440p</option>
        <option value={0}>best available</option>
      </select>

      <h2>Correction glossary</h2>
      <p className="meta">One term per line — known-correct commands, acronyms, product names.</p>
      <textarea rows={8} value={glossary} onChange={(e) => setGlossary(e.target.value)} />
      <button onClick={saveGlossary}>Save glossary</button>

      <h2>Tag vocabulary</h2>
      <button onClick={addTag}>+ add tag</button>
      <div className="tagcloud">
        {tags.map((t) => (
          <span key={t.id} className="tag managed">
            {t.name} <small>{t.count}</small>
            <button title="rename" onClick={() => renameTag(t)}>✎</button>
            <button title="delete" onClick={() => deleteTag(t)}>×</button>
          </span>
        ))}
      </div>

      <h2>Quick-ref categories</h2>
      <p className="meta">
        What the Quick-references step files docs under. Built-in categories are fixed
        (their doc prompts live under Advanced → Prompt editor); custom categories carry
        their own doc prompt, and their description is announced to the entity-extraction
        call automatically.
      </p>
      {catBanner && (
        <div className="banner">
          <p>
            <b>“{catBanner}” added.</b> Entity extraction is told about it automatically,
            but these prompts were written around the built-in categories — review them in
            Advanced → Prompt editor so future runs actually surface this material:
          </p>
          <ul>
            <li><b>Deep dive (both models)</b> — decides what the source document covers in depth</li>
            <li><b>Quick-ref: entity extraction</b> — its category definitions steer classification</li>
            <li><b>Mind map: topic graph</b> — its node-kind list is spelled out in the prompt</li>
          </ul>
          <button onClick={() => setCatBanner("")}>dismiss</button>
        </div>
      )}
      <div className="tagcloud">
        {qrCats.filter((c) => c.builtin).map((c) => (
          <span key={c.key} className="tag">
            {c.icon} {c.plural} <small>built-in · {c.count}</small>
          </span>
        ))}
      </div>
      {qrCats.filter((c) => !c.builtin).map((c) => (
        <details key={c.key} className="prompt-item">
          <summary>
            {c.icon} {c.plural} <small>— {c.count} doc(s) in {c.dir}/</small>
          </summary>
          <div className="catfields">
            <label>Label
              <input value={c.label}
                onChange={(e) => setCatField(c.key, "label", e.target.value)} />
            </label>
            <label>Plural
              <input value={c.plural}
                onChange={(e) => setCatField(c.key, "plural", e.target.value)} />
            </label>
            <label>Icon
              <input value={c.icon} style={{ width: "3.5rem" }}
                onChange={(e) => setCatField(c.key, "icon", e.target.value)} />
            </label>
          </div>
          <label className="stacked">What belongs here (guides entity extraction)
            <textarea rows={3} value={c.description ?? ""}
              onChange={(e) => setCatField(c.key, "description", e.target.value)} />
          </label>
          <label className="stacked">Doc-writing prompt
            <textarea rows={8} value={c.prompt ?? ""}
              onChange={(e) => setCatField(c.key, "prompt", e.target.value)} />
          </label>
          <div className="row">
            <button onClick={() => saveCategory(c)}>save</button>
            <button className="linkish danger" onClick={() => deleteCategory(c)}>delete</button>
          </div>
        </details>
      ))}
      {newCat ? (
        <div className="catnew">
          <div className="catfields">
            <label>Label
              <input value={newCat.label} placeholder="Framework"
                onChange={(e) => setNewCat({ ...newCat, label: e.target.value })} />
            </label>
            <label>Plural
              <input value={newCat.plural} placeholder="Frameworks"
                onChange={(e) => setNewCat({ ...newCat, plural: e.target.value })} />
            </label>
            <label>Icon
              <input value={newCat.icon} style={{ width: "3.5rem" }}
                onChange={(e) => setNewCat({ ...newCat, icon: e.target.value })} />
            </label>
          </div>
          <label className="stacked">What belongs here (guides entity extraction)
            <textarea rows={3} value={newCat.description}
              placeholder="e.g. a named methodology or compliance framework practitioners align work to (MITRE ATT&CK, NIST CSF, CIS benchmarks)"
              onChange={(e) => setNewCat({ ...newCat, description: e.target.value })} />
          </label>
          <label className="stacked">Doc-writing prompt
            <textarea rows={8} value={newCat.prompt}
              onChange={(e) => setNewCat({ ...newCat, prompt: e.target.value })} />
          </label>
          <div className="row">
            <button onClick={addCategory}>create category</button>
            <button onClick={() => setNewCat(null)}>cancel</button>
          </div>
        </div>
      ) : (
        <button onClick={() => setNewCat({
          label: "", plural: "", icon: "📄", description: "", prompt: NEW_CAT_PROMPT,
        })}>
          + add category
        </button>
      )}

      <h2>Advanced</h2>

      <details className="advanced">
        <summary>Prompt editor <small>— the exact instructions each pipeline step sends its model</small></summary>
        <p className="meta">
          Edits apply on the next run of that step. "Modified" prompts survive updates;
          reset returns to the shipped default.
        </p>
        {Object.entries(prompts).map(([name, p]) => (
          <details key={name} className="prompt-item">
            <summary>
              {p.label} {p.modified && <span className="modbadge">modified</span>}
            </summary>
            <textarea
              rows={10}
              value={p.value}
              onChange={(e) =>
                setPrompts((prev) => ({ ...prev, [name]: { ...p, value: e.target.value } }))}
            />
            <div className="row">
              <button onClick={() => savePrompt(name)}>save</button>
              <button onClick={() => resetPrompt(name)}>reset to default</button>
            </div>
          </details>
        ))}
      </details>

      <details className="advanced">
        <summary>Generation parameters <small>— temperature / max tokens per function</small></summary>
        <p className="meta">Blank = provider default. Temperature 0–1 (creativity); max tokens caps output length.</p>
        <table className="list">
          <thead><tr><th>Function</th><th>Temperature</th><th>Max tokens</th><th></th></tr></thead>
          <tbody>
            {Object.keys(functions)
              .filter((fn) => !["asr", "tts", "download"].includes(fn))
              .map((fn) => (
                <tr key={fn}>
                  <td>{FN_LABELS[fn] ?? fn}</td>
                  <td>
                    <input type="number" step="0.1" min="0" max="2" style={{ width: "5rem" }}
                      value={params[fn]?.temperature ?? ""}
                      onChange={(e) => setParams((p) => ({
                        ...p,
                        [fn]: { ...p[fn], temperature: e.target.value === "" ? null : Number(e.target.value) },
                      }))}
                    />
                  </td>
                  <td>
                    <input type="number" step="1024" min="256" style={{ width: "7rem" }}
                      value={params[fn]?.max_tokens ?? ""}
                      onChange={(e) => setParams((p) => ({
                        ...p,
                        [fn]: { ...p[fn], max_tokens: e.target.value === "" ? null : Number(e.target.value) },
                      }))}
                    />
                  </td>
                  <td><button onClick={() => saveParams(fn)}>save</button></td>
                </tr>
              ))}
          </tbody>
        </table>
      </details>

      <details className="advanced">
        <summary>Audio tuning <small>— TTS pacing, silence trimming</small></summary>
        {adv.audio && (
          <div className="knobs">
            <label>TTS speaking speed
              <input type="number" step="0.05" min="0.5" max="2"
                value={adv.audio.tts_speed}
                onChange={(e) => setAdvValue("audio", "tts_speed", Number(e.target.value))} />
            </label>
            <label>Gap between lines (s)
              <input type="number" step="0.1" min="0" max="3"
                value={adv.audio.tts_gap}
                onChange={(e) => setAdvValue("audio", "tts_gap", Number(e.target.value))} />
            </label>
            <label title="Mainly speeds up Piper (each line is its own process). Kokoro is already multi-core per line and gains most from the GPU."
              >TTS parallel workers (0 = auto)
              <input type="number" step="1" min="0" max="16"
                value={adv.audio.tts_workers}
                onChange={(e) => setAdvValue("audio", "tts_workers", Number(e.target.value))} />
            </label>
            <label>Silence threshold (dB)
              <input type="number" step="1" min="-70" max="-10"
                value={adv.audio.trim_db}
                onChange={(e) => setAdvValue("audio", "trim_db", Number(e.target.value))} />
            </label>
            <label>Min silence to cut (s)
              <input type="number" step="0.1" min="0.3" max="10"
                value={adv.audio.trim_silence}
                onChange={(e) => setAdvValue("audio", "trim_silence", Number(e.target.value))} />
            </label>
            <label className="checkline">
              <input type="checkbox" checked={!!adv.audio.keep_intermediates}
                onChange={(e) => setAdvValue("audio", "keep_intermediates", e.target.checked)} />
              keep temporary synthesis files for debugging
            </label>
            <button onClick={() => saveAdvanced("audio")}>Save audio settings</button>
          </div>
        )}
      </details>

      <details className="advanced">
        <summary>Pipeline behavior <small>— chunking, depth, tagging rules</small></summary>
        {adv.pipeline && (
          <div className="knobs">
            <label>Correction chunk size (chars)
              <input type="number" step="1000" min="4000" max="100000"
                value={adv.pipeline.chunk_chars}
                onChange={(e) => setAdvValue("pipeline", "chunk_chars", Number(e.target.value))} />
            </label>
            <label>Deep-dive depth
              <select value={adv.pipeline.deepdive_depth}
                onChange={(e) => setAdvValue("pipeline", "deepdive_depth", e.target.value)}>
                <option value="concise">concise</option>
                <option value="standard">standard</option>
                <option value="exhaustive">exhaustive</option>
              </select>
            </label>
            <label>Podcast segments (0 = auto)
              <input type="number" step="1" min="0" max="30"
                value={adv.pipeline.podcast_segments}
                onChange={(e) => setAdvValue("pipeline", "podcast_segments", Number(e.target.value))} />
            </label>
            <label>Max tags per artifact
              <input type="number" step="1" min="1" max="20"
                value={adv.pipeline.max_tags}
                onChange={(e) => setAdvValue("pipeline", "max_tags", Number(e.target.value))} />
            </label>
            <label className="checkline">
              <input type="checkbox" checked={!!adv.pipeline.allow_new_tags}
                onChange={(e) => setAdvValue("pipeline", "allow_new_tags", e.target.checked)} />
              tagger may create new vocabulary tags
            </label>
            <button onClick={() => saveAdvanced("pipeline")}>Save pipeline settings</button>
          </div>
        )}
      </details>

      <details className="advanced">
        <summary>ASR options <small>— local Whisper behavior</small></summary>
        {adv.asr && (
          <div className="knobs">
            <label className="checkline">
              <input type="checkbox" checked={!!adv.asr.vad}
                onChange={(e) => setAdvValue("asr", "vad", e.target.checked)} />
              voice-activity-detection filter (skips silence; disable if words get dropped)
            </label>
            <label>Language hint (blank = auto)
              <input type="text" placeholder="en, de, ja…" value={adv.asr.language}
                onChange={(e) => setAdvValue("asr", "language", e.target.value)} />
            </label>
            <p className="meta">Whisper model size is set in the Model matrix (asr row): tiny / base / small / medium / distil-large-v3 / large-v3.</p>
            <button onClick={() => saveAdvanced("asr")}>Save ASR settings</button>
          </div>
        )}
      </details>

      <details className="advanced">
        <summary>Local models <small>— context window, keep-alive, thinking, timeouts</small></summary>
        {adv.local && (
          <div className="knobs">
            <label title="Requested per call from Ollama. Ollama's own default (4k in current releases) silently truncates long transcript chunks — raise this if a local model seems to 'forget' the start of its input. Higher values use more RAM/VRAM."
              >Context window (tokens, Ollama only)
              <input type="number" step="1024" min="1024" max="262144"
                value={adv.local.num_ctx}
                onChange={(e) => setAdvValue("local", "num_ctx", Number(e.target.value))} />
            </label>
            <label title='How long Ollama keeps the model in memory after a call. "5m" default; "-1" pins it loaded; "0" unloads immediately. Blank = server default.'
              >Keep model loaded (Ollama only)
              <input type="text" placeholder="5m" value={adv.local.keep_alive}
                onChange={(e) => setAdvValue("local", "keep_alive", e.target.value)} />
            </label>
            <label title="Thinking models (qwen3, deepseek-r1). auto = model default; off answers faster and avoids reasoning loops in tagging/correction; on reasons harder. Leave auto for models without thinking support — forcing a value errors on them."
              >Thinking (Ollama only)
              <select value={adv.local.think}
                onChange={(e) => setAdvValue("local", "think", e.target.value)}>
                <option value="auto">auto (model default)</option>
                <option value="on">on</option>
                <option value="off">off</option>
              </select>
            </label>
            <label title="Per-request ceiling for both local providers. CPU boxes generating long outputs may need more than the default 300s."
              >Request timeout (seconds)
              <input type="number" step="30" min="30" max="3600"
                value={adv.local.timeout_seconds}
                onChange={(e) => setAdvValue("local", "timeout_seconds", Number(e.target.value))} />
            </label>
            <label className="checkline"
              title="Ask the server to constrain structured steps (trim spans, mind map, quick-ref matching, tagging, podcast outline) to valid JSON — Ollama format, OpenAI-compatible response_format. Turn off only if your server errors on it.">
              <input type="checkbox" checked={!!adv.local.json_mode}
                onChange={(e) => setAdvValue("local", "json_mode", e.target.checked)} />
              enforce native JSON output on structured steps
            </label>
            <p className="meta">
              Applies to steps assigned to the <b>ollama</b> or <b>openai_compat</b>{" "}
              providers. For openai_compat, set the context window in the server
              itself (LM Studio's model settings, llama.cpp's <code>-c</code> flag).
            </p>
            <button onClick={() => saveAdvanced("local")}>Save local model settings</button>
          </div>
        )}
      </details>

      <details className="advanced">
        <summary>Compute <small>— GPU vs CPU for local models</small></summary>
        {adv.compute && (
          <div className="knobs">
            <label>Whisper device
              <select value={adv.compute.whisper_device}
                onChange={(e) => setAdvValue("compute", "whisper_device", e.target.value)}>
                <option value="auto">auto (GPU if available)</option>
                <option value="cpu">cpu</option>
                <option value="cuda">cuda</option>
              </select>
            </label>
            <label>Whisper compute type
              <select value={adv.compute.whisper_compute_type}
                onChange={(e) => setAdvValue("compute", "whisper_compute_type", e.target.value)}>
                <option value="auto">auto</option>
                <option value="int8">int8 (CPU / lowest memory)</option>
                <option value="int8_float16">int8_float16 (GPU)</option>
                <option value="float16">float16 (GPU / best quality)</option>
              </select>
            </label>
            <label>Kokoro TTS device
              <select value={adv.compute.kokoro_device}
                onChange={(e) => setAdvValue("compute", "kokoro_device", e.target.value)}>
                <option value="auto">auto (GPU if available)</option>
                <option value="cpu">cpu</option>
                <option value="cuda">cuda</option>
              </select>
            </label>
            <p className="meta">
              GPU use requires starting the stack with the GPU overlay:&nbsp;
              <code>docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build</code>.
              That overlay also gives the Ollama container GPU access — Ollama
              uses it automatically for every step assigned to the ollama provider.
              Without the overlay, "auto" safely falls back to CPU.
            </p>
            <button onClick={() => saveAdvanced("compute")}>Save compute settings</button>
          </div>
        )}
      </details>

      <details className="advanced">
        <summary>Cloud storage <small>— sync artifacts to S3 / Nextcloud / Drive / Dropbox / OneDrive</small></summary>
        {cloud && (
          <div className="knobs">
            <label>Provider
              <select value={cloud.provider}
                onChange={(e) => {
                  setCloud({ ...cloud, provider: e.target.value });
                  setCloudEdit({});
                }}>
                <option value="">(disabled)</option>
                {cloud.providers.map((p) => (
                  <option key={p} value={p}>{CLOUD_LABELS[p] ?? p}</option>
                ))}
              </select>
            </label>
            {cloud.provider && Object.entries(cloud.all_fields[cloud.provider] ?? {}).map(
              ([field, secret]) => (
                <label key={field}>{field.replace(/_/g, " ")}
                  {field === "token" ? (
                    <textarea rows={3}
                      placeholder={OAUTH_HINT.replace("<provider>", cloud.provider)}
                      value={cloudEdit[field] ?? ""}
                      onChange={(e) => setCloudEdit({ ...cloudEdit, [field]: e.target.value })} />
                  ) : (
                    <input type={secret ? "password" : "text"}
                      value={cloudEdit[field] ?? ""}
                      placeholder={secret ? "unchanged unless set" : ""}
                      onChange={(e) => setCloudEdit({ ...cloudEdit, [field]: e.target.value })} />
                  )}
                </label>
              ))}
            {cloud.provider && (
              <>
                <label>Remote base folder
                  <input type="text" value={cloud.remote_base}
                    onChange={(e) => setCloud({ ...cloud, remote_base: e.target.value })} />
                </label>
                <label className="checkline">
                  <input type="checkbox" checked={cloud.auto}
                    onChange={(e) => setCloud({ ...cloud, auto: e.target.checked })} />
                  auto-upload each artifact when it's produced
                </label>
                <label>Sync direction ("Sync everything now")
                  <select value={cloud.mode}
                    onChange={(e) => setCloud({ ...cloud, mode: e.target.value })}>
                    <option value="push">One-way: local → cloud (default)</option>
                    <option value="bisync">Two-way: local ↔ cloud (library only)</option>
                  </select>
                </label>
                {cloud.mode === "bisync" && (
                  <p className="meta">
                    Two-way sync (rclone bisync) propagates edits <b>and
                    deletions in both directions</b> for the library — deleting
                    a document in your cloud folder deletes it here on the next
                    sync (a run that would remove more than half of either side
                    aborts as a safety stop). Conflicting edits keep the newer
                    version; the older is kept renamed with a ".conflict"
                    suffix. The first two-way run establishes a baseline, and
                    every run finishes by rebuilding the search index from the
                    vault. Archived media and per-artifact auto-upload remain
                    one-way (push).
                  </p>
                )}
              </>
            )}
            <div className="row">
              <button onClick={saveCloud}>Save cloud settings</button>
              {cloud.provider && <button onClick={cloudSyncNow}>Sync everything now</button>}
            </div>
            {cloud.last_sync && (
              <p className={`meta ${cloud.last_sync.status === "error" ? "error" : ""}`}>
                last sync: {cloud.last_sync.status} — {cloud.last_sync.detail}{" "}
                ({new Date(cloud.last_sync.at).toLocaleString()})
              </p>
            )}
          </div>
        )}
      </details>
    </div>
  );
}
