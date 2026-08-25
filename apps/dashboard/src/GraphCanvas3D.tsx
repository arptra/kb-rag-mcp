import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D, { ForceGraphMethods } from "react-force-graph-3d";
import SpriteText from "three-spritetext";

import type { GraphEdge, GraphNode, GraphPayload } from "./types";

const CONFIDENCE_LABELS: Record<string, string> = {
  DECLARED: "Декларативно",
  HIGH: "Подтверждено",
  MEDIUM: "Вероятно",
  LOW: "Сомнительно",
  UNRESOLVED: "Не определено",
};

function short(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function nodeColor(type: string): string {
  return ({ Service: "#b6f36b", ExternalSystem: "#ffb45d", BusinessOperation: "#78a7ff", BusinessRule: "#df83ff", EntryPoint: "#6ee7d8", ExitPoint: "#ff7e67", Event: "#ff7690", Table: "#f4d269", DomainEntity: "#a78bfa", Repository: "#8795aa" } as Record<string, string>)[type] || "#728096";
}

function confidenceColor(confidence: string): string {
  return ({ DECLARED: "#55e89a", HIGH: "#74a7ff", MEDIUM: "#f3c76b", LOW: "#ff9b55", UNRESOLVED: "#ff5f7d" } as Record<string, string>)[confidence] || "#8795aa";
}

function edgeSource(edge: GraphEdge): string {
  return typeof edge.source === "object" ? edge.source.id : edge.source;
}

function edgeTarget(edge: GraphEdge): string {
  return typeof edge.target === "object" ? edge.target.id : edge.target;
}

function useGraphSize() {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 900, height: 650 });
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => {
      setSize({
        width: Math.max(320, Math.floor(entry.contentRect.width)),
        height: Math.max(480, Math.floor(entry.contentRect.height)),
      });
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return { ref, size };
}

export default function GraphCanvas3D({ graph, selected, onSelect }: {
  graph: GraphPayload;
  selected: string | null;
  onSelect: (node: GraphNode) => void;
}) {
  const forceRef = useRef<ForceGraphMethods<GraphNode, GraphEdge>>();
  const { ref: containerRef, size } = useGraphSize();
  const graphData = useMemo(() => ({
    nodes: graph.nodes.map((node) => ({ ...node })),
    links: graph.edges.map((edge) => ({
      ...edge,
      source: edgeSource(edge),
      target: edgeTarget(edge),
    })),
  }), [graph]);
  const focusNode = (node: GraphNode & { x?: number; y?: number; z?: number }) => {
    onSelect(node);
    if (node.x === undefined || node.y === undefined || node.z === undefined) return;
    const distance = 105;
    const length = Math.hypot(node.x, node.y, node.z) || 1;
    const ratio = 1 + distance / length;
    forceRef.current?.cameraPosition(
      { x: node.x * ratio, y: node.y * ratio, z: node.z * ratio },
      { x: node.x, y: node.y, z: node.z },
      750,
    );
  };
  return (
    <div className="graph-3d" ref={containerRef} role="img" aria-label="Интерактивный 3D-граф связей системы">
      <div className="graph-camera-actions">
        <button onClick={() => forceRef.current?.zoomToFit(700, 65)}>Вписать</button>
        <button onClick={() => forceRef.current?.cameraPosition({ x: 0, y: 0, z: 480 }, { x: 0, y: 0, z: 0 }, 700)}>Сбросить камеру</button>
        <span>ЛКМ — вращать · колесо — zoom · ПКМ — pan</span>
      </div>
      <ForceGraph3D<GraphNode, GraphEdge>
        ref={forceRef}
        width={size.width}
        height={size.height}
        graphData={graphData}
        backgroundColor="#0b100d"
        showNavInfo={false}
        nodeLabel={(node) => `<b>${node.type}</b><br/>${node.label}`}
        nodeColor={(node) => selected === node.id ? "#ffffff" : nodeColor(node.type)}
        nodeVal={(node) => node.type === "Service" ? 8 : node.type === "Repository" ? 5 : 3}
        nodeOpacity={0.94}
        nodeResolution={14}
        nodeThreeObjectExtend
        nodeThreeObject={(node) => {
          const sprite = new SpriteText(short(node.label, node.type === "Service" ? 32 : 22));
          sprite.color = selected === node.id ? "#ffffff" : nodeColor(node.type);
          sprite.textHeight = node.type === "Service" ? 4.7 : 3.1;
          sprite.backgroundColor = "rgba(7, 12, 9, .78)";
          sprite.padding = 1.4;
          sprite.borderRadius = 2;
          sprite.position.y = node.type === "Service" ? 8 : 5;
          return sprite;
        }}
        linkLabel={(edge) => `${edge.type} · ${CONFIDENCE_LABELS[edge.confidence] || edge.confidence}`}
        linkColor={(edge) => confidenceColor(edge.confidence)}
        linkWidth={(edge) => edge.confidence === "DECLARED" ? 2.8 : edge.confidence === "HIGH" ? 2.2 : edge.confidence === "MEDIUM" ? 1.5 : 1.1}
        linkOpacity={0.78}
        linkDirectionalArrowLength={4.2}
        linkDirectionalArrowRelPos={0.88}
        linkDirectionalArrowColor={(edge) => confidenceColor(edge.confidence)}
        linkDirectionalParticles={(edge) => edge.confidence === "DECLARED" || edge.confidence === "HIGH" ? 2 : edge.confidence === "MEDIUM" ? 1 : 0}
        linkDirectionalParticleWidth={1.7}
        linkDirectionalParticleSpeed={0.004}
        linkDirectionalParticleColor={(edge) => confidenceColor(edge.confidence)}
        cooldownTicks={160}
        d3VelocityDecay={0.28}
        onEngineStop={() => forceRef.current?.zoomToFit(600, 65)}
        onNodeClick={(node) => focusNode(node)}
      />
    </div>
  );
}
