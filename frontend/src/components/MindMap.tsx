import { useMemo, useState } from "react";
import { ReactFlow, Background, Controls, Node, Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

interface GraphNode { id: string; label: string; kind: string; summary?: string; quickref?: string }
interface GraphEdge { source: string; target: string; label?: string }
export interface Graph { nodes: GraphNode[]; edges: GraphEdge[] }

const KIND_COLORS: Record<string, string> = {
  concept: "#4f83cc",
  tool: "#43a047",
  technique: "#ef6c00",
  technology: "#8e24aa",
};

/** Column-per-kind layout: stable, readable without a physics engine. */
function layout(graph: Graph): Node[] {
  const kinds = ["concept", "technology", "tool", "technique"];
  const columns: Record<string, GraphNode[]> = {};
  for (const n of graph.nodes) {
    const k = kinds.includes(n.kind) ? n.kind : "concept";
    (columns[k] ??= []).push(n);
  }
  const out: Node[] = [];
  kinds.forEach((kind, col) => {
    (columns[kind] ?? []).forEach((n, row) => {
      out.push({
        id: n.id,
        position: { x: col * 320, y: row * 90 },
        data: { ...n },
        style: {
          background: "#fff",
          border: `2px solid ${KIND_COLORS[kind]}`,
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
      graph.edges
        .filter((e) => graph.nodes.some((n) => n.id === e.source) &&
                       graph.nodes.some((n) => n.id === e.target))
        .map((e, i) => ({
          id: `e${i}`,
          source: e.source,
          target: e.target,
          label: e.label,
          style: { stroke: "#999" },
          labelStyle: { fontSize: 10, fill: "#666" },
        })),
    [graph]
  );

  return (
    <div className="mindmap">
      <div className="canvas">
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
          <p className="kind" style={{ color: KIND_COLORS[selected.kind] }}>{selected.kind}</p>
          <p>{selected.summary}</p>
          {selected.quickref && (
            <p><a href={`/quickrefs?path=${encodeURIComponent(selected.quickref)}`}>quick-reference →</a></p>
          )}
          <button onClick={() => setSelected(null)}>close</button>
        </aside>
      )}
      <div className="legend">
        {Object.entries(KIND_COLORS).map(([k, c]) => (
          <span key={k}><i style={{ background: c }} /> {k}</span>
        ))}
      </div>
    </div>
  );
}
