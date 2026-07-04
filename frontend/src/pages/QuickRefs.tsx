import { useEffect, useMemo, useState } from "react";
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
  tags: string[];
  updated: string | null;
}

interface RefDetail {
  ref: Ref;
  body: string;
  versions: string[];
}

const KIND_META: Record<string, { label: string; plural: string; icon: string }> = {
  tool: { label: "Tool", plural: "Tools", icon: "🔧" },
  technique: { label: "Technique", plural: "Techniques", icon: "🎯" },
  concept: { label: "Concept", plural: "Concepts", icon: "💡" },
};
const KIND_ORDER = ["tool", "technique", "concept"];

export default function QuickRefs() {
  const [refs, setRefs] = useState<Ref[]>([]);
  const [search, setSearch] = useState("");
  const [kinds, setKinds] = useState<Set<string>>(new Set());
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set());
  const [sort, setSort] = useState<"title" | "updated" | "sources">("title");
  const [open, setOpen] = useState<RefDetail | null>(null);
  const [versionBody, setVersionBody] = useState<{ name: string; body: string } | null>(null);

  function load() {
    api<Ref[]>("/quickrefs").then(setRefs).catch(() => {});
  }
  useEffect(load, []);

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

  function toggle<T>(set: Set<T>, v: T, apply: (s: Set<T>) => void) {
    const next = new Set(set);
    next.has(v) ? next.delete(v) : next.add(v);
    apply(next);
  }

  const allTags = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of refs) for (const t of r.tags ?? []) m.set(t, (m.get(t) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [refs]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    let out = refs.filter((r) => {
      if (kinds.size && !kinds.has(r.kind)) return false;
      if (tagFilter.size && !(r.tags ?? []).some((t) => tagFilter.has(t))) return false;
      if (needle) {
        const hay = [r.title, ...(r.aliases ?? []), ...(r.tags ?? [])]
          .join(" ").toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
    out.sort((a, b) => {
      if (sort === "updated") return (b.updated ?? "").localeCompare(a.updated ?? "");
      if (sort === "sources") return (b.sources?.length ?? 0) - (a.sources?.length ?? 0);
      return a.title.localeCompare(b.title);
    });
    return out;
  }, [refs, search, kinds, tagFilter, sort]);

  const sections = useMemo(
    () => KIND_ORDER
      .map((k) => [k, filtered.filter((r) => r.kind === k)] as const)
      .filter(([, list]) => list.length > 0),
    [filtered]
  );

  return (
    <div className="quickrefs">
      <aside>
        <input
          className="search"
          placeholder="Search name, alias, tag…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="segmented">
          {KIND_ORDER.map((k) => (
            <button
              key={k}
              className={kinds.has(k) ? "on" : ""}
              onClick={() => toggle(kinds, k, setKinds)}
            >
              {KIND_META[k].icon} {KIND_META[k].plural}
            </button>
          ))}
        </div>
        <select value={sort} onChange={(e) => setSort(e.target.value as any)}>
          <option value="title">sort: name</option>
          <option value="updated">sort: recently updated</option>
          <option value="sources">sort: most sources</option>
        </select>
        <div className="tagcloud">
          {allTags.map(([t, n]) => (
            <button
              key={t}
              className={`tag ${tagFilter.has(t) ? "on" : ""}`}
              onClick={() => toggle(tagFilter, t, setTagFilter)}
            >
              {t} <small>{n}</small>
            </button>
          ))}
        </div>

        {sections.map(([kind, list]) => (
          <div key={kind} className="ref-section">
            <h3>{KIND_META[kind].plural} <small>({list.length})</small></h3>
            <ul>
              {list.map((r) => (
                <li key={r.id}>
                  <button className={open?.ref.id === r.id ? "on" : ""} onClick={() => openRef(r.id)}>
                    <span className="kindmark">{KIND_META[r.kind]?.icon}</span>
                    {r.title}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
        {filtered.length === 0 && (
          <p className="empty">
            {refs.length === 0
              ? "No quick-refs yet — run the Quick-references step on a project."
              : "No quick-refs match the filters."}
          </p>
        )}
      </aside>

      {open && (
        <section>
          <h2>
            {KIND_META[open.ref.kind]?.icon} {open.ref.title}
            <span className="kindbadge">{KIND_META[open.ref.kind]?.label ?? open.ref.kind}</span>
          </h2>
          {(open.ref.aliases ?? []).length > 0 && (
            <p className="meta">aka: {open.ref.aliases.join(", ")}</p>
          )}
          <p className="meta">
            from: {(open.ref.sources ?? []).map((s) => s.title).join(" · ")}
          </p>
          <p className="tags">
            {(open.ref.tags ?? []).map((t) => (
              <button
                key={t}
                className={`tag ${tagFilter.has(t) ? "on" : ""}`}
                onClick={() => toggle(tagFilter, t, setTagFilter)}
              >
                {t}
              </button>
            ))}
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
              <p className="meta">
                viewing snapshot {versionBody.name} —{" "}
                <button onClick={() => setVersionBody(null)}>back to current</button>
              </p>
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
