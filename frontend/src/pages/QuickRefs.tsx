import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, QuickRefCategory } from "../api";

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

const FALLBACK_META = { label: "", plural: "", icon: "📄" };

export default function QuickRefs() {
  const [refs, setRefs] = useState<Ref[]>([]);
  const [cats, setCats] = useState<QuickRefCategory[]>([]);
  const [search, setSearch] = useState("");
  const [kinds, setKinds] = useState<Set<string>>(new Set());
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set());
  const [sort, setSort] = useState<"title" | "updated" | "sources">("title");
  const [open, setOpen] = useState<RefDetail | null>(null);
  const [versionBody, setVersionBody] = useState<{ name: string; body: string } | null>(null);

  function load() {
    api<Ref[]>("/quickrefs").then(setRefs).catch(() => {});
    api<QuickRefCategory[]>("/quickrefs/categories").then(setCats).catch(() => {});
  }
  useEffect(load, []);

  // deep link from mind map: ?path=tools/nmap.md — consumed once, so the
  // param can't re-open the doc after the user navigates back to the columns
  useEffect(() => {
    const path = new URLSearchParams(location.search).get("path");
    if (path && refs.length) {
      const hit = refs.find((r) => r.path === path);
      if (hit) openRef(hit.id);
      history.replaceState(null, "", location.pathname);
    }
  }, [refs]);

  const meta = useMemo(() => {
    const m = new Map(cats.map((c) => [c.key, c]));
    return (kind: string) =>
      m.get(kind) ?? { ...FALLBACK_META, label: kind, plural: kind };
  }, [cats]);

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

  function closeDetail() {
    setOpen(null);
    setVersionBody(null);
  }

  async function deleteDoc() {
    if (!open || !confirm(`Delete "${open.ref.title}" and its doc file? This cannot be undone.`)) return;
    await api(`/quickrefs/${open.ref.id}`, { method: "DELETE" });
    closeDetail();
    load();
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

  // one column per category (registry order), plus any stray kinds at the end
  const columns = useMemo(() => {
    const known = cats.map((c) => c.key);
    const stray = [...new Set(filtered.map((r) => r.kind))]
      .filter((k) => !known.includes(k)).sort();
    return [...known, ...stray]
      .map((k) => [k, filtered.filter((r) => r.kind === k)] as const)
      .filter(([, list]) => list.length > 0);
  }, [filtered, cats]);

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
          {cats.map((c) => (
            <button
              key={c.key}
              className={kinds.has(c.key) ? "on" : ""}
              onClick={() => toggle(kinds, c.key, setKinds)}
            >
              {c.icon} {c.plural}
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
      </aside>

      {open ? (
        <section>
          <p className="detail-bar">
            <button className="linkish" onClick={closeDetail}>← all quick-refs</button>
            <button className="linkish danger" onClick={deleteDoc}>delete doc</button>
          </p>
          <h2>
            {meta(open.ref.kind).icon} {open.ref.title}
            <span className="kindbadge">{meta(open.ref.kind).label || open.ref.kind}</span>
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
      ) : (
        <section
          className="ref-columns"
          style={{ gridTemplateColumns: `repeat(${Math.max(columns.length, 1)}, minmax(220px, 1fr))` }}
        >
          {columns.map(([kind, list]) => (
            <div key={kind} className="ref-column">
              <h3>
                {meta(kind).icon} {meta(kind).plural || kind} <small>({list.length})</small>
              </h3>
              <ul>
                {list.map((r) => (
                  <li key={r.id}>
                    <button onClick={() => openRef(r.id)}>{r.title}</button>
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
        </section>
      )}
    </div>
  );
}
