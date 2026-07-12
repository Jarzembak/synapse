import { useMemo, useState } from "react";
import { ReactFlow, Background, Controls, Node, Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

interface GraphNode {
  id: string;
  label: string;
  kind: string;
  summary?: string;
  quickref?: string;
  path?: string;
  start_line?: number;
  end_line?: number;
  url?: string;
}
interface GraphEdge { source: string; target: string; label?: string }
export interface Graph { nodes: GraphNode[]; edges: GraphEdge[] }

function safeRepositoryUrl(value?: string): string | null {
  if (!value || /[\\\u0000-\u001f]/.test(value)) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && parsed.hostname === "github.com"
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
}

const KIND_COLORS: Record<string, string> = {
  repository: "#1565c0",
  directory: "#00838f",
  module: "#2e7d32",
  component: "#558b2f",
  entrypoint: "#c62828",
  service: "#ad1457",
  dependency: "#6a1b9a",
  configuration: "#ef6c00",
  data: "#5d4037",
  test: "#455a64",
  concept: "#4f83cc",
  tool: "#43a047",
  technique: "#ef6c00",
  technology: "#8e24aa",
};
const FALLBACK_COLOR = "#78909c"; // custom quick-ref categories

/** Column-per-kind layout: stable, readable without a physics engine. */
function layout(graph: Graph): Node[] {
  const known = [
    "repository", "entrypoint", "directory", "module", "component", "service",
    "data", "dependency", "configuration", "test",
    "concept", "technology", "tool", "technique",
  ];
  const extra = [...new Set(graph.nodes.map((n) => n.kind))]
    .filter((k) => !known.includes(k)).sort();
  const kinds = [...known, ...extra];
  const columns: Record<string, GraphNode[]> = {};
  for (const n of graph.nodes) {
    (columns[n.kind] ??= []).push(n);
  }
  const out: Node[] = [];
  kinds.forEach((kind, col) => {
    (columns[kind] ?? []).forEach((n, row) => {
      out.push({
        id: n.id,
        position: { x: col * 320, y: row * 90 },
        data: { ...n },
        style: {
          background: "var(--panel)",
          color: "var(--text)",
          border: `2px solid ${KIND_COLORS[kind] ?? FALLBACK_COLOR}`,
          borderRadius: 8,
          padding: 6,
          fontSize: 13,
          width: 240,
        },
      });
    });
  });
  return out;
}

export default function MindMap({ graph }: { graph: Graph }) {
  const [selected, setSelected] = useState<GraphNode | null>(null);

  const nodes = useMemo(() => layout(graph), [graph]);
  const edges: Edge[] = useMemo(
    () =>
      // edges is optional in the stored graph JSON (the backend writes
      // graph.get("edges", [])), so guard against it being absent
      (graph.edges ?? [])
        .filter((e) => graph.nodes.some((n) => n.id === e.source) &&
                       graph.nodes.some((n) => n.id === e.target))
        .map((e, i) => ({
          id: `e${i}`,
          source: e.source,
          target: e.target,
          label: e.label,
          style: { stroke: "var(--border2)" },
          labelStyle: { fontSize: 10, fill: "var(--muted)" },
        })),
    [graph]
  );
  const groupedNodes = useMemo(() => {
    const groups = new Map<string, GraphNode[]>();
    for (const node of graph.nodes) {
      const items = groups.get(node.kind) ?? [];
      items.push(node);
      groups.set(node.kind, items);
    }
    return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [graph.nodes]);
  const nodeNames = useMemo(
    () => new Map(graph.nodes.map((node) => [node.id, node.label])),
    [graph.nodes],
  );

  return (
    <div className="mindmap">
      <div className="canvas" aria-label="Interactive mind map. An accessible outline follows the diagram.">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          fitView
          onNodeClick={(_, node) => setSelected(node.data as unknown as GraphNode)}
        >
          <Background />
          <Controls />
        </ReactFlow>
      </div>
      {selected && (
        <aside className="nodepanel">
          <h3>{selected.label}</h3>
          <p className="kind" style={{ color: KIND_COLORS[selected.kind] ?? FALLBACK_COLOR }}>{selected.kind}</p>
          <p>{selected.summary}</p>
          {selected.path && (
            <p className="mono">
              {safeRepositoryUrl(selected.url) ? (
                <a href={safeRepositoryUrl(selected.url)!} target="_blank" rel="noreferrer">
                  {selected.path}{selected.start_line ? `:${selected.start_line}` : ""}
                </a>
              ) : (
                <>{selected.path}{selected.start_line ? `:${selected.start_line}` : ""}</>
              )}
            </p>
          )}
          {selected.quickref && (
            <p><a href={`/quickrefs?path=${encodeURIComponent(selected.quickref)}`}>quick-reference →</a></p>
          )}
          <button onClick={() => setSelected(null)}>close</button>
        </aside>
      )}
      <div className="legend">
        {[...new Set(graph.nodes.map((node) => node.kind))].sort().map((kind) => (
          <span key={kind}>
            <i style={{ background: KIND_COLORS[kind] ?? FALLBACK_COLOR }} /> {kind}
          </span>
        ))}
      </div>
      <section className="mindmap-linear" aria-labelledby="mindmap-outline-title">
        <h3 id="mindmap-outline-title">Mind map outline</h3>
        <p className="meta">A keyboard- and screen-reader-friendly version of every node and relationship.</p>
        <div className="mindmap-linear-groups">
          {groupedNodes.map(([kind, items]) => (
            <section className="card" key={kind}>
              <h4><i style={{ background: KIND_COLORS[kind] ?? FALLBACK_COLOR }} /> {kind}</h4>
              <ul>
                {items.map((node) => (
                  <li key={node.id}>
                    <button type="button" className="linkish" onClick={() => setSelected(node)}>
                      {node.label}
                    </button>
                    {node.summary && <span> — {node.summary}</span>}
                    {node.path && (
                      <small className="mono">
                        {safeRepositoryUrl(node.url) ? (
                          <a href={safeRepositoryUrl(node.url)!} target="_blank" rel="noreferrer">
                            {node.path}{node.start_line ? `:${node.start_line}` : ""}
                          </a>
                        ) : <>{node.path}{node.start_line ? `:${node.start_line}` : ""}</>}
                      </small>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
        {(graph.edges ?? []).length > 0 && (
          <details>
            <summary>Relationships ({graph.edges.length})</summary>
            <ul>
              {graph.edges.map((edge, index) => (
                <li key={`${edge.source}-${edge.target}-${index}`}>
                  {nodeNames.get(edge.source) ?? edge.source}
                  {edge.label ? ` — ${edge.label} → ` : " → "}
                  {nodeNames.get(edge.target) ?? edge.target}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
    </div>
  );
}
