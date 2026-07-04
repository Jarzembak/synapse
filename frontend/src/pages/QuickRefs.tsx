import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";

interface Ref {
  id: number;
  kind: string;
  slug: string;
  title: string;
  path: string;
  aliases: string[];
  sources: { id: number; title: string }[];
}

interface RefDetail {
  ref: Ref;
  body: string;
  versions: string[];
}

export default function QuickRefs() {
  const [refs, setRefs] = useState<Ref[]>([]);
  const [kind, setKind] = useState("");
  const [open, setOpen] = useState<RefDetail | null>(null);
  const [versionBody, setVersionBody] = useState<{ name: string; body: string } | null>(null);

  function load() {
    api<Ref[]>(`/quickrefs?kind=${kind}`).then(setRefs).catch(() => {});
  }
  useEffect(load, [kind]);

  // deep link from mind map: ?path=tools/nmap.md
  useEffect(() => {
    const path = new URLSearchParams(location.search).get("path");
    if (path && refs.length) {
      const hit = refs.find((r) => r.path === path);
      if (hit) openRef(hit.id);
    }
  }, [refs]);

  async function openRef(id: number) {
    setVersionBody(null);
    setOpen(await api<RefDetail>(`/quickrefs/${id}`));
  }

  async function viewVersion(name: string) {
    if (!open) return;
    setVersionBody(await api(`/quickrefs/${open.ref.id}/versions/${name}`));
  }

  async function revert(name: string) {
    if (!open || !confirm(`Revert ${open.ref.title} to ${name}?`)) return;
    await api(`/quickrefs/${open.ref.id}/revert/${name}`, { method: "POST" });
    openRef(open.ref.id);
  }

  return (
    <div className="quickrefs">
      <aside>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">tools + techniques</option>
          <option value="tool">tools</option>
          <option value="technique">techniques</option>
        </select>
        <ul>
          {refs.map((r) => (
            <li key={r.id}>
              <button className={open?.ref.id === r.id ? "on" : ""} onClick={() => openRef(r.id)}>
                <span className={`kindmark ${r.kind}`}>{r.kind === "tool" ? "🔧" : "🎯"}</span>
                {r.title}
              </button>
            </li>
          ))}
          {refs.length === 0 && <li className="empty">No quick-refs yet — run the Quick-references step on a project.</li>}
        </ul>
      </aside>

      {open && (
        <section>
          <h2>{open.ref.title}</h2>
          {(open.ref.aliases ?? []).length > 0 && (
            <p className="meta">aka: {open.ref.aliases.join(", ")}</p>
          )}
          <p className="meta">
            from: {(open.ref.sources ?? []).map((s) => s.title).join(" · ")}
          </p>
          {open.versions.length > 0 && (
            <details>
              <summary>{open.versions.length} previous version(s)</summary>
              <ul>
                {open.versions.map((v) => (
                  <li key={v}>
                    <code>{v.split(".").slice(-2, -1)[0]}</code>{" "}
                    <button onClick={() => viewVersion(v)}>view</button>{" "}
                    <button onClick={() => revert(v)}>revert to this</button>
                  </li>
                ))}
              </ul>
            </details>
          )}
          {versionBody && (
            <div className="versionview">
              <p className="meta">viewing snapshot {versionBody.name} — <button onClick={() => setVersionBody(null)}>back to current</button></p>
            </div>
          )}
          <article className="markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {versionBody ? versionBody.body : open.body}
            </ReactMarkdown>
          </article>
        </section>
      )}
    </div>
  );
}
