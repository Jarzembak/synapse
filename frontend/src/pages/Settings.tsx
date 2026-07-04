import { useEffect, useState } from "react";
import { api } from "../api";

interface ModelCfg { provider: string; model: string }
interface TagInfo { id: number; name: string; kind: string; count: number }

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
};

export default function Settings() {
  const [functions, setFunctions] = useState<Record<string, ModelCfg>>({});
  const [providers, setProviders] = useState<string[]>([]);
  const [glossary, setGlossary] = useState("");
  const [tags, setTags] = useState<TagInfo[]>([]);
  const [saved, setSaved] = useState("");

  function load() {
    api<{ functions: Record<string, ModelCfg>; providers: string[] }>("/settings/models")
      .then((r) => { setFunctions(r.functions); setProviders(r.providers); });
    api<{ terms: string[] }>("/settings/glossary").then((r) => setGlossary(r.terms.join("\n")));
    api<TagInfo[]>("/tags").then(setTags);
  }
  useEffect(load, []);

  async function saveModel(fn: string, cfg: ModelCfg) {
    setFunctions((prev) => ({ ...prev, [fn]: cfg }));
    await api(`/settings/models/${fn}`, { method: "PUT", body: JSON.stringify(cfg) });
    flash(`saved ${fn}`);
  }

  async function saveGlossary() {
    await api("/settings/glossary", {
      method: "PUT",
      body: JSON.stringify({ terms: glossary.split("\n") }),
    });
    flash("glossary saved");
  }

  async function renameTag(t: TagInfo) {
    const name = prompt(`Rename tag "${t.name}" to:`, t.name);
    if (!name || name === t.name) return;
    await api(`/tags/${t.id}`, { method: "PUT", body: JSON.stringify({ name }) });
    load();
  }

  async function deleteTag(t: TagInfo) {
    if (!confirm(`Delete tag "${t.name}" (used ${t.count}×)?`)) return;
    await api(`/tags/${t.id}`, { method: "DELETE" });
    load();
  }

  async function addTag() {
    const name = prompt("New tag name:");
    if (!name) return;
    await api("/tags", { method: "POST", body: JSON.stringify({ name }) });
    load();
  }

  function flash(msg: string) {
    setSaved(msg);
    setTimeout(() => setSaved(""), 1500);
  }

  return (
    <div className="settings">
      {saved && <div className="flash">{saved}</div>}

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
                  {[...new Set([...providers, "faster-whisper", "kokoro", cfg.provider])].map((p) => (
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
    </div>
  );
}
