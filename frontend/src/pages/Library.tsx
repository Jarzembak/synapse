import { Fragment, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, Artifact, TYPE_LABELS } from "../api";

interface TagInfo { id: number; name: string; kind: string; count: number }

type Col = "title" | "type" | "project" | "tags" | "updated";

const COLS: { key: Col; label: string; filterable: boolean }[] = [
  { key: "title", label: "Title", filterable: true },
  { key: "type", label: "Type", filterable: true },
  { key: "project", label: "Project", filterable: true },
  { key: "tags", label: "Tags", filterable: true },
  { key: "updated", label: "Updated", filterable: false },
];

const FunnelIcon = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
    <path d="M3 5h18l-7 8v6l-4-2v-4L3 5z" />
  </svg>
);

function sortValue(a: Artifact, col: Col): string | number {
  switch (col) {
    case "title": return a.title.toLowerCase();
    case "type": return (TYPE_LABELS[a.type] ?? a.type).toLowerCase();
    case "project": return a.project_slug ?? "";
    case "tags": return (a.tags ?? []).slice().sort().join(",");
    case "updated": return new Date(a.updated).getTime();
  }
}

export default function Library() {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<Artifact[]>([]);
  const [allTags, setAllTags] = useState<TagInfo[]>([]);
  const [error, setError] = useState("");

  const [sortCol, setSortCol] = useState<Col>("updated");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [titleFilter, setTitleFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState<Set<string>>(new Set());
  const [projectFilter, setProjectFilter] = useState<Set<string>>(new Set());
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set());
  const [openFilter, setOpenFilter] = useState<Col | null>(null);

  const [grouped, setGrouped] = useState(true);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  useEffect(() => {
    api<TagInfo[]>("/tags").then(setAllTags).catch(() => {});
  }, []);

  // server does full-text search only; sorting/filtering happen client-side
  useEffect(() => {
    const t = setTimeout(() => {
      api<Artifact[]>(`/library/search?${new URLSearchParams({ q, limit: "500" })}`)
        .then((r) => { setItems(r); setError(""); })
        .catch((e) => setError(e.message));
    }, 250);
    return () => clearTimeout(t);
  }, [q]);

  function toggleSet<T>(set: Set<T>, value: T): Set<T> {
    const next = new Set(set);
    next.has(value) ? next.delete(value) : next.add(value);
    return next;
  }

  const toggleTag = (name: string) => setTagFilter((s) => toggleSet(s, name));

  function clickSort(col: Col) {
    if (sortCol === col) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(col);
      setSortDir(col === "updated" ? "desc" : "asc");
    }
  }

  const distinct = useMemo(() => {
    const count = (vals: (string | undefined)[]) => {
      const m = new Map<string, number>();
      for (const v of vals) if (v) m.set(v, (m.get(v) ?? 0) + 1);
      return [...m.entries()].sort((x, y) => x[0].localeCompare(y[0]));
    };
    return {
      type: count(items.map((a) => a.type)),
      project: count(items.map((a) => a.project_slug ?? "")),
      tags: count(items.flatMap((a) => a.tags ?? [])),
    };
  }, [items]);

  const rows = useMemo(() => {
    let out = items.filter((a) => {
      if (titleFilter && !a.title.toLowerCase().includes(titleFilter.toLowerCase())) return false;
      if (typeFilter.size && !typeFilter.has(a.type)) return false;
      if (projectFilter.size && !projectFilter.has(a.project_slug ?? "")) return false;
      if (tagFilter.size && !(a.tags ?? []).some((t) => tagFilter.has(t))) return false;
      return true;
    });
    const dir = sortDir === "asc" ? 1 : -1;
    out.sort((a, b) => {
      const va = sortValue(a, sortCol);
      const vb = sortValue(b, sortCol);
      return (va < vb ? -1 : va > vb ? 1 : 0) * dir;
    });
    return out;
  }, [items, titleFilter, typeFilter, projectFilter, tagFilter, sortCol, sortDir]);

  const groups = useMemo(() => {
    if (!grouped) return null;
    const m = new Map<string, Artifact[]>();
    for (const a of rows) {
      const key = a.project_slug ?? "(no project)";
      (m.get(key) ?? m.set(key, []).get(key)!).push(a);
    }
    return [...m.entries()].sort((x, y) => x[0].localeCompare(y[0]));
  }, [rows, grouped]);

  const anyFilter = titleFilter || typeFilter.size || projectFilter.size || tagFilter.size;

  function clearFilters() {
    setTitleFilter("");
    setTypeFilter(new Set());
    setProjectFilter(new Set());
    setTagFilter(new Set());
  }

  function filterState(col: Col): { active: boolean } {
    switch (col) {
      case "title": return { active: !!titleFilter };
      case "type": return { active: typeFilter.size > 0 };
      case "project": return { active: projectFilter.size > 0 };
      case "tags": return { active: tagFilter.size > 0 };
      default: return { active: false };
    }
  }

  function renderPopover(col: Col) {
    if (col === "title") {
      return (
        <div className="filter-pop">
          <input
            type="text"
            autoFocus
            placeholder="title contains…"
            value={titleFilter}
            onChange={(e) => setTitleFilter(e.target.value)}
          />
          <div className="pop-actions">
            <button onClick={() => setTitleFilter("")}>clear</button>
            <button onClick={() => setOpenFilter(null)}>close</button>
          </div>
        </div>
      );
    }
    const [values, selected, setter]: [
      [string, number][], Set<string>, (s: Set<string>) => void
    ] = col === "type"
      ? [distinct.type, typeFilter, setTypeFilter]
      : col === "project"
        ? [distinct.project, projectFilter, setProjectFilter]
        : [distinct.tags, tagFilter, setTagFilter];
    return (
      <div className="filter-pop">
        {values.map(([v, n]) => (
          <label key={v}>
            <input
              type="checkbox"
              checked={selected.has(v)}
              onChange={() => setter(toggleSet(selected, v))}
            />
            {col === "type" ? TYPE_LABELS[v] ?? v : v} <small>({n})</small>
          </label>
        ))}
        {values.length === 0 && <p className="empty">no values</p>}
        <div className="pop-actions">
          <button onClick={() => setter(new Set())}>clear</button>
          <button onClick={() => setOpenFilter(null)}>close</button>
        </div>
      </div>
    );
  }

  const renderRow = (a: Artifact) => (
    <tr key={a.id}>
      <td><Link to={`/artifacts/${a.id}`}>{a.title}</Link></td>
      <td>{TYPE_LABELS[a.type] ?? a.type}</td>
      <td>{a.project_slug}</td>
      <td>
        {a.tags?.map((t) => (
          <button
            key={t}
            className={`tag ${tagFilter.has(t) ? "on" : ""}`}
            title={`filter by ${t}`}
            onClick={() => toggleTag(t)}
          >
            {t}
          </button>
        ))}
      </td>
      <td>{new Date(a.updated).toLocaleDateString()}</td>
    </tr>
  );

  return (
    <div className="library">
      <aside className="filters">
        <h3>Tags</h3>
        <p className="hint">click to toggle — shows items matching any selected tag</p>
        <div className="tagcloud">
          {allTags.filter((t) => t.count > 0).map((t) => (
            <button
              key={t.id}
              className={`tag ${tagFilter.has(t.name) ? "on" : ""}`}
              onClick={() => toggleTag(t.name)}
            >
              {t.name} <small>{t.count}</small>
            </button>
          ))}
        </div>
        {anyFilter ? (
          <p><button onClick={clearFilters}>clear all filters</button></p>
        ) : null}
      </aside>

      <section>
        <div className="toolbar">
          <input
            className="search"
            placeholder="Full-text search the library…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <button className={grouped ? "on" : ""} onClick={() => setGrouped(!grouped)}>
            group by project
          </button>
          {grouped && (
            <>
              <button onClick={() => setCollapsed(new Set())}>expand all</button>
              <button onClick={() => setCollapsed(new Set((groups ?? []).map(([g]) => g)))}>
                collapse all
              </button>
            </>
          )}
        </div>
        {error && <p className="error">{error}</p>}

        {openFilter && <div className="filter-overlay" onClick={() => setOpenFilter(null)} />}

        <table className="list">
          <thead>
            <tr>
              {COLS.map((c) => (
                <th key={c.key}>
                  <div className="th-wrap">
                    <button className="th-sort" onClick={() => clickSort(c.key)}>
                      {c.label}
                      {sortCol === c.key && (
                        <span className="dir">{sortDir === "asc" ? "▲" : "▼"}</span>
                      )}
                    </button>
                    {c.filterable && (
                      <button
                        className={`th-filter ${filterState(c.key).active ? "on" : ""}`}
                        title={`filter ${c.label.toLowerCase()}`}
                        onClick={() => setOpenFilter(openFilter === c.key ? null : c.key)}
                      >
                        <FunnelIcon />
                      </button>
                    )}
                  </div>
                  {openFilter === c.key && renderPopover(c.key)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groups
              ? groups.map(([slug, groupRows]) => (
                  <Fragment key={slug}>
                    <tr
                      className="group-row"
                      onClick={() => setCollapsed((s) => toggleSet(s, slug))}
                    >
                      <td colSpan={5}>
                        <span className="chev">{collapsed.has(slug) ? "▶" : "▼"}</span>
                        {slug}
                        <small>({groupRows.length} artifact{groupRows.length === 1 ? "" : "s"})</small>
                      </td>
                    </tr>
                    {!collapsed.has(slug) && groupRows.map(renderRow)}
                  </Fragment>
                ))
              : rows.map(renderRow)}
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} className="empty">
                  {items.length === 0
                    ? "Nothing here yet — add a video under Projects."
                    : "No items match the current filters."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
