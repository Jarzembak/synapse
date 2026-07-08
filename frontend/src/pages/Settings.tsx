import { useEffect, useState } from "react";
import { api, QuickRefCategory } from "../api";

interface ModelCfg { provider: string; model: string }
interface TagInfo { id: number; name: string; kind: string; count: number }
interface PromptInfo { label: string; value: string; modified: boolean }
interface Params { temperature?: number | null; max_tokens?: number | null }
interface CloudState {
  provider: string;
  providers: string[];
  all_fields: Record<string, Record<string, boolean>>;
  config: Record<string, string>;
  remote_base: string;
  auto: boolean;
  last_sync: { status: string; detail: string; at: string } | null;
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
  download: "Media download",
};

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

  function load() {
    Promise.all([
      api<{ functions: Record<string, ModelCfg>; providers: string[] }>("/settings/models")
        .then((r) => { setFunctions(r.functions); setProviders(r.providers); }),
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
  useEffect(load, []);

  function flash(msg: string, isError = false) {
    setSaved(msg);
    setSavedError(isError);
    setTimeout(() => setSaved(""), isError ? 4000 : 1500);
  }

  async function saveModel(fn: string, cfg: ModelCfg) {
    const prev = functions[fn];
    setFunctions((p) => ({ ...p, [fn]: cfg }));
    try {
      await api(`/settings/models/${fn}`, { method: "PUT", body: JSON.stringify(cfg) });
      flash(`saved ${fn}`);
    } catch (e: any) {
      setFunctions((p) => ({ ...p, [fn]: prev }));  // revert the optimistic edit
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

  async function savePrompt(name: string) {
    try {
      await api(`/settings/prompts/${name}`, {
        method: "PUT", body: JSON.stringify({ value: prompts[name].value }),
      });
      api<Record<string, PromptInfo>>("/settings/prompts").then(setPrompts);
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
        }),
      });
      flash("cloud settings saved");
      api<CloudState>("/settings/cloud").then((r) => { setCloud(r); setCloudEdit(r.config); });
    } catch (e: any) { flash(`save failed: ${e.message}`, true); }
  }

  async function cloudSyncNow() {
    try {
      await api("/settings/cloud/sync", { method: "POST" });
      flash("full sync queued — watch the job ticker");
    } catch (e: any) {
      alert(e.message);
    }
  }

  // tag ops refresh only the tag list — a full load() would clobber unsaved
  // edits elsewhere on the page (prompts, category drafts)
  const reloadTags = () => api<TagInfo[]>("/tags").then(setTags);

  async function renameTag(t: TagInfo) {
    const name = prompt(`Rename tag "${t.name}" to:`, t.name);
    if (!name || name === t.name) return;
    await api(`/tags/${t.id}`, { method: "PUT", body: JSON.stringify({ name }) });
    reloadTags();
  }

  async function deleteTag(t: TagInfo) {
    if (!confirm(`Delete tag "${t.name}" (used ${t.count}×)?`)) return;
    await api(`/tags/${t.id}`, { method: "DELETE" });
    reloadTags();
  }

  async function addTag() {
    const name = prompt("New tag name:");
    if (!name) return;
    await api("/tags", { method: "POST", body: JSON.stringify({ name }) });
    reloadTags();
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
    } catch (e: any) {
      alert(e.message);
      return;
    }
    setCatBanner(newCat.label);
    setNewCat(null);
    reloadCats();
    flash("category added");
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
      alert(e.message);
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
    } catch (e: any) {
      alert(e.message);
      return;
    }
    reloadCats();
    flash("category deleted");
  }

  return (
    <div className="settings">
      {saved && <div className={`flash ${savedError ? "flash-error" : ""}`}>{saved}</div>}

      <h2>Model matrix</h2>
      <p className="meta">
        Which model runs each pipeline function. Providers: <b>ollama</b> = local
        (or a remote box via OLLAMA_BASE_URL), <b>anthropic</b>/<b>gemini</b> = frontier APIs.
        ASR providers: <b>faster-whisper</b> (local CPU) or <b>gemini</b>. TTS providers:
        <b> kokoro</b> (local) or <b>gemini</b>.
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
                  {[...new Set([...providers, "faster-whisper", "kokoro", "piper", cfg.provider])].map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </td>
              <td>
                <input
                  defaultValue={cfg.model}
                  onBlur={(e) => e.target.value !== cfg.model &&
                    saveModel(fn, { ...cfg, model: e.target.value })}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

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
