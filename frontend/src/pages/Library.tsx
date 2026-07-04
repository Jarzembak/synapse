import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Artifact, TYPE_LABELS } from "../api";

interface TagInfo { id: number; name: string; kind: string; count: number }

export default function Library() {
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [tag, setTag] = useState("");
  const [sort, setSort] = useState("updated");
  const [order, setOrder] = useState("desc");
  const [items, setItems] = useState<Artifact[]>([]);
  const [tags, setTags] = useState<TagInfo[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api<TagInfo[]>("/tags").then(setTags).catch(() => {});
  }, []);

  useEffect(() => {
    const t = setTimeout(() => {
      const params = new URLSearchParams({ q, type, tag, sort, order });
      api<Artifact[]>(`/library/search?${params}`)
        .then((r) => { setItems(r); setError(""); })
        .catch((e) => setError(e.message));
    }, 250);
    return () => clearTimeout(t);
  }, [q, type, tag, sort, order]);

  return (
    <div className="library">
      <aside className="filters">
        <h3>Type</h3>
        <select value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">all types</option>
          {Object.entries(TYPE_LABELS).map(([v, l]) => (
            <option key={v} value={v}>{l}</option>
          ))}
        </select>
        <h3>Tag</h3>
        <div className="tagcloud">
          {tags.filter((t) => t.count > 0).map((t) => (
            <button
              key={t.id}
              className={`tag ${tag === t.name ? "on" : ""}`}
              onClick={() => setTag(tag === t.name ? "" : t.name)}
            >
              {t.name} <small>{t.count}</small>
            </button>
          ))}
        </div>
        <h3>Sort</h3>
        <select value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="updated">updated</option>
          <option value="created">created</option>
          <option value="title">title</option>
          <option value="type">type</option>
        </select>
        <select value={order} onChange={(e) => setOrder(e.target.value)}>
          <option value="desc">desc</option>
          <option value="asc">asc</option>
        </select>
      </aside>

      <section>
        <input
          className="search"
          placeholder="Full-text search the library…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        {error && <p className="error">{error}</p>}
        <table className="list">
          <thead>
            <tr><th>Title</th><th>Type</th><th>Project</th><th>Tags</th><th>Updated</th></tr>
          </thead>
          <tbody>
            {items.map((a) => (
              <tr key={a.id}>
                <td><Link to={`/artifacts/${a.id}`}>{a.title}</Link></td>
                <td>{TYPE_LABELS[a.type] ?? a.type}</td>
                <td>{a.project_slug}</td>
                <td>{a.tags?.map((t) => <span key={t} className="tag">{t}</span>)}</td>
                <td>{new Date(a.updated).toLocaleDateString()}</td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr><td colSpan={5} className="empty">Nothing here yet — add a video under Projects.</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
