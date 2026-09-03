import { memo, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D, { ForceGraphMethods } from "react-force-graph-3d";
import { BackSide, Group, Mesh, MeshBasicMaterial, SphereGeometry } from "three";
import SpriteText from "three-spritetext";

import {
  CONFIDENCE_INFO,
  EDGE_ORIGIN_LABELS,
  EDGE_SCOPE_INFO,
  EDGE_TYPE_INFO,
  NODE_TYPE_INFO,
  confidenceColor,
  edgeServiceScope,
  graphNodeServiceId,
  nodeColor,
  serviceColor,
} from "./graphSemantics";
import type { GraphEdge, GraphNode, GraphPayload } from "./types";

const MAX_RENDER_NODES = 5_000;
const MAX_RENDER_EDGES = 6_000;
const CONFIDENCE_RANK: Record<string, number> = { DECLARED: 5, HIGH: 4, MEDIUM: 3, LOW: 2, UNRESOLVED: 1 };
const NODE_RENDER_PRIORITY: Record<string, number> = { Service: 0, ExternalSystem: 1, EntryPoint: 2, ExitPoint: 2, BusinessOperation: 3, Event: 3, Repository: 4, BusinessRule: 5, DomainEntity: 5, Table: 5, Column: 6, CodeSymbol: 7 };
const CLUSTER_SPHERE_GEOMETRY = new SphereGeometry(1, 12, 9);
const CLUSTER_CENTER_GEOMETRY = new SphereGeometry(1, 9, 7);

type RenderGraphNode = GraphNode & {
  isClusterShell?: boolean;
  clusterRadius?: number;
  clusterMemberCount?: number;
  x?: number;
  y?: number;
  z?: number;
  vx?: number;
  vy?: number;
  vz?: number;
  fx?: number;
  fy?: number;
  fz?: number;
};

function short(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function edgeSource(edge: GraphEdge): string {
  return typeof edge.source === "object" ? edge.source.id : edge.source;
}

function edgeTarget(edge: GraphEdge): string {
  return typeof edge.target === "object" ? edge.target.id : edge.target;
}

function positionHash(value: string, salt: number): number {
  let hash = 2166136261 ^ salt;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4_294_967_295;
}

function buildClusterCloud(nodes: GraphNode[]): {
  positions: Map<string, { x: number; y: number; z: number }>;
  clusters: Array<{
    key: string;
    serviceId: string | null;
    center: { x: number; y: number; z: number };
    radius: number;
    memberCount: number;
  }>;
  serviceCount: number;
  extent: number;
} {
  const groups = new Map<string, GraphNode[]>();
  nodes.forEach((node) => {
    const key = graphNodeServiceId(node) || "__unknown_service__";
    const group = groups.get(key) || [];
    group.push(node);
    groups.set(key, group);
  });
  const keys = [...groups.keys()].sort((left, right) => {
    if (left === "__unknown_service__") return 1;
    if (right === "__unknown_service__") return -1;
    return left.localeCompare(right);
  });
  const clusterRadius = (count: number) => Math.max(13, Math.min(58, Math.cbrt(Math.max(1, count)) * 7.5));
  const descriptors = keys
    .map((key) => ({
      key,
      serviceId: key === "__unknown_service__" ? null : key,
      radius: clusterRadius(groups.get(key)?.length || 1),
      memberCount: groups.get(key)?.length || 0,
      personalGap: 28 + positionHash(key, 71) * 34,
    }))
    .sort((left, right) => right.radius - left.radius || left.key.localeCompare(right.key));
  const sortedRadii = descriptors.map((descriptor) => descriptor.radius).sort((left, right) => left - right);
  const typicalRadius = sortedRadii[Math.floor(Math.max(0, sortedRadii.length - 1) * 0.75)] || 13;
  const cloudRadius = Math.max(120, Math.cbrt(Math.max(1, descriptors.length)) * (typicalRadius * 2 + 48) * 0.96);
  const positions = new Map<string, { x: number; y: number; z: number }>();
  const clusters: Array<{
    key: string;
    serviceId: string | null;
    center: { x: number; y: number; z: number };
    radius: number;
    memberCount: number;
  }> = [];
  const placed: Array<{
    center: { x: number; y: number; z: number };
    radius: number;
    personalGap: number;
  }> = [];
  descriptors.forEach((descriptor) => {
    let center = { x: 0, y: 0, z: 0 };
    let bestClearance = Number.NEGATIVE_INFINITY;
    for (let attempt = 0; attempt < 96; attempt += 1) {
      const azimuth = positionHash(`${descriptor.key}:${attempt}`, 11) * Math.PI * 2;
      const vertical = positionHash(`${descriptor.key}:${attempt}`, 29) * 2 - 1;
      const horizontal = Math.sqrt(Math.max(0, 1 - vertical * vertical));
      const radial = Math.cbrt(0.025 + positionHash(`${descriptor.key}:${attempt}`, 47) * 0.975) * cloudRadius;
      const candidate = {
        x: Math.cos(azimuth) * horizontal * radial,
        y: vertical * radial,
        z: Math.sin(azimuth) * horizontal * radial,
      };
      let clearance = cloudRadius - descriptor.radius - radial;
      placed.forEach((other) => {
        const distance = Math.hypot(
          candidate.x - other.center.x,
          candidate.y - other.center.y,
          candidate.z - other.center.z,
        );
        clearance = Math.min(
          clearance,
          distance - descriptor.radius - other.radius - Math.max(descriptor.personalGap, other.personalGap),
        );
      });
      if (clearance > bestClearance) {
        bestClearance = clearance;
        center = candidate;
      }
      if (clearance >= 0) break;
    }
    placed.push({
      center,
      radius: descriptor.radius,
      personalGap: descriptor.personalGap,
    });
    const group = [...(groups.get(descriptor.key) || [])].sort((left, right) => left.id.localeCompare(right.id));
    clusters.push({
      key: descriptor.key,
      serviceId: descriptor.serviceId,
      center,
      radius: descriptor.radius,
      memberCount: descriptor.memberCount,
    });
    group.forEach((node, nodeIndex) => {
      if (group.length === 1) {
        const azimuth = positionHash(node.id, 101) * Math.PI * 2;
        const vertical = positionHash(node.id, 131) * 2 - 1;
        const horizontal = Math.sqrt(Math.max(0, 1 - vertical * vertical));
        const distance = descriptor.radius * (0.48 + positionHash(node.id, 151) * 0.18);
        positions.set(node.id, {
          x: center.x + Math.cos(azimuth) * horizontal * distance,
          y: center.y + vertical * distance,
          z: center.z + Math.sin(azimuth) * horizontal * distance,
        });
        return;
      }
      const progress = (nodeIndex + 0.5) / group.length;
      const vertical = 1 - progress * 2;
      const horizontal = Math.sqrt(Math.max(0, 1 - vertical * vertical));
      const angle = nodeIndex * Math.PI * (3 - Math.sqrt(5));
      const distance = (0.28 + Math.cbrt(progress) * 0.72) * descriptor.radius * 0.72;
      positions.set(node.id, {
        x: center.x + Math.cos(angle) * horizontal * distance,
        y: center.y + vertical * distance,
        z: center.z + Math.sin(angle) * horizontal * distance,
      });
    });
  });
  return {
    positions,
    clusters,
    serviceCount: keys.filter((key) => key !== "__unknown_service__").length,
    extent: Math.max(
      260,
      ...clusters.map((cluster) => (
        Math.max(Math.abs(cluster.center.x), Math.abs(cluster.center.y), Math.abs(cluster.center.z)) + cluster.radius
      ) * 2),
    ),
  };
}

function createClusterShell(node: RenderGraphNode): Group {
  const color = node.service_id ? serviceColor(node.service_id) : EDGE_SCOPE_INFO.unknown.color;
  const radius = node.clusterRadius || 13;
  const group = new Group();
  const fill = new Mesh(
    CLUSTER_SPHERE_GEOMETRY,
    new MeshBasicMaterial({
      color,
      transparent: true,
      opacity: 0.055,
      depthWrite: false,
      side: BackSide,
    }),
  );
  const border = new Mesh(
    CLUSTER_SPHERE_GEOMETRY,
    new MeshBasicMaterial({
      color,
      transparent: true,
      opacity: 0.26,
      depthWrite: false,
      wireframe: true,
    }),
  );
  const centerSize = Math.max(4.8, Math.min(7.5, radius * 0.38));
  const centerHalo = new Mesh(
    CLUSTER_CENTER_GEOMETRY,
    new MeshBasicMaterial({ color, transparent: true, opacity: 0.42, depthWrite: false, wireframe: true }),
  );
  centerHalo.scale.setScalar(centerSize * 1.75 / radius);
  const center = new Mesh(
    CLUSTER_CENTER_GEOMETRY,
    new MeshBasicMaterial({ color, transparent: true, opacity: 0.14, depthWrite: false, side: BackSide }),
  );
  center.scale.setScalar(centerSize / radius);
  const centerPin = new Mesh(
    CLUSTER_CENTER_GEOMETRY,
    new MeshBasicMaterial({ color, transparent: true, opacity: 0.28, depthWrite: false }),
  );
  centerPin.scale.setScalar(1.8 / radius);
  group.add(fill, border, centerHalo, center, centerPin);
  group.scale.setScalar(radius);
  return group;
}

function fitGraphToRenderBudget(graph: GraphPayload, selected: string | null): {
  graph: GraphPayload;
  hiddenNodes: number;
  hiddenEdges: number;
} {
  if (graph.nodes.length <= MAX_RENDER_NODES && graph.edges.length <= MAX_RENDER_EDGES) {
    return { graph, hiddenNodes: 0, hiddenEdges: 0 };
  }
  const degree = new Map<string, number>();
  graph.edges.forEach((edge) => {
    const source = edgeSource(edge), target = edgeTarget(edge);
    degree.set(source, (degree.get(source) || 0) + 1);
    degree.set(target, (degree.get(target) || 0) + 1);
  });
  const nodes = [...graph.nodes]
    .sort((left, right) => {
      if (left.id === selected) return -1;
      if (right.id === selected) return 1;
      const typeDifference = (NODE_RENDER_PRIORITY[left.type] ?? 99) - (NODE_RENDER_PRIORITY[right.type] ?? 99);
      if (typeDifference) return typeDifference;
      const degreeDifference = (degree.get(right.id) || 0) - (degree.get(left.id) || 0);
      return degreeDifference || left.id.localeCompare(right.id);
    })
    .slice(0, MAX_RENDER_NODES);
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = graph.edges
    .filter((edge) => nodeIds.has(edgeSource(edge)) && nodeIds.has(edgeTarget(edge)))
    .sort((left, right) => {
      const leftSelected = Number(edgeSource(left) === selected || edgeTarget(left) === selected);
      const rightSelected = Number(edgeSource(right) === selected || edgeTarget(right) === selected);
      return rightSelected - leftSelected
        || (CONFIDENCE_RANK[right.confidence] || 0) - (CONFIDENCE_RANK[left.confidence] || 0)
        || left.id.localeCompare(right.id);
    })
    .slice(0, MAX_RENDER_EDGES);
  return {
    graph: { ...graph, nodes, edges },
    hiddenNodes: graph.nodes.length - nodes.length,
    hiddenEdges: graph.edges.length - edges.length,
  };
}

function useGraphSize() {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 900, height: 650 });
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => {
      const next = {
        width: Math.max(320, Math.floor(entry.contentRect.width)),
        height: Math.max(480, Math.floor(entry.contentRect.height)),
      };
      setSize((current) => current.width === next.width && current.height === next.height
        ? current
        : next);
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return { ref, size };
}

function GraphCanvas3D({ graph, selected, onSelect }: {
  graph: GraphPayload;
  selected: string | null;
  onSelect: (node: GraphNode) => void;
}) {
  const forceRef = useRef<ForceGraphMethods<RenderGraphNode, GraphEdge>>();
  const nodeCache = useRef(new Map<string, RenderGraphNode>());
  const fittedSignature = useRef("");
  const [colorMode, setColorMode] = useState<"service" | "type">("service");
  const { ref: containerRef, size } = useGraphSize();
  const renderPlan = useMemo(() => fitGraphToRenderBudget(graph, selected), [graph, selected]);
  const renderedGraph = renderPlan.graph;
  const largeGraph = renderedGraph.nodes.length > 350 || renderedGraph.edges.length > 900;
  const veryLargeGraph = renderedGraph.nodes.length > 1_500 || renderedGraph.edges.length > 3_500;
  const serviceNodeOverview = renderedGraph.nodes.length > 0
    && renderedGraph.nodes.every((node) => node.type === "Service" || node.type === "ExternalSystem");
  const persistentLabels = !largeGraph;
  const nodesById = useMemo(
    () => new Map(renderedGraph.nodes.map((node) => [node.id, node])),
    [renderedGraph.nodes],
  );
  const clusterGrid = useMemo(
    () => buildClusterCloud(renderedGraph.nodes),
    [renderedGraph.nodes],
  );
  const serviceCount = clusterGrid.serviceCount;
  const graphSignature = useMemo(
    () => `${renderedGraph.snapshot_id || renderedGraph.generated_at}::${renderedGraph.nodes.map((node) => node.id).join("|")}::${renderedGraph.edges.map((edge) => `${edge.id}:${edge.confidence}:${edge.status}`).join("|")}`,
    [renderedGraph.edges, renderedGraph.generated_at, renderedGraph.nodes, renderedGraph.snapshot_id],
  );
  const graphData = useMemo(() => {
    const activeNodeIds = new Set(renderedGraph.nodes.map((node) => node.id));
    nodeCache.current.forEach((_node, id) => {
      if (!activeNodeIds.has(id)) nodeCache.current.delete(id);
    });
    const nodes = renderedGraph.nodes.map((node) => {
      const gridPosition = clusterGrid.positions.get(node.id);
      const existing = nodeCache.current.get(node.id);
      if (existing) {
        Object.assign(existing, node);
        if (largeGraph && gridPosition) {
          Object.assign(existing, gridPosition, {
            fx: gridPosition.x,
            fy: gridPosition.y,
            fz: gridPosition.z,
          });
        } else {
          delete existing.fx;
          delete existing.fy;
          delete existing.fz;
        }
        return existing;
      }
      const created = {
        ...node,
        ...(gridPosition || {}),
        ...(largeGraph && gridPosition ? {
          fx: gridPosition.x,
          fy: gridPosition.y,
          fz: gridPosition.z,
        } : {}),
      };
      nodeCache.current.set(node.id, created);
      return created;
    });
    const linksById = new Map<string, GraphEdge>();
    renderedGraph.edges.forEach((edge) => {
      if (!linksById.has(edge.id)) {
        linksById.set(edge.id, {
          ...edge,
          source: edgeSource(edge),
          target: edgeTarget(edge),
        });
      }
    });
    const clusterShells: RenderGraphNode[] = serviceNodeOverview ? [] : clusterGrid.clusters.map((cluster) => ({
      id: `__service_cluster_shell__:${cluster.key}`,
      type: "ServiceCluster",
      label: cluster.serviceId || "Граница сервиса неизвестна",
      service_id: cluster.serviceId,
      metadata: { visual_group: true, member_count: cluster.memberCount },
      evidence_ids: [],
      isClusterShell: true,
      clusterRadius: cluster.radius,
      clusterMemberCount: cluster.memberCount,
      ...cluster.center,
      fx: cluster.center.x,
      fy: cluster.center.y,
      fz: cluster.center.z,
    }));
    return { nodes: [...clusterShells, ...nodes], links: [...linksById.values()] };
  }, [clusterGrid, graphSignature, largeGraph, renderedGraph.edges, renderedGraph.nodes, serviceNodeOverview]);
  const nodeDisplayColor = (node: RenderGraphNode): string => {
    if (selected === node.id) return "#ffffff";
    if (colorMode === "type") return nodeColor(node.type);
    const serviceId = graphNodeServiceId(node);
    return serviceId ? serviceColor(serviceId) : nodeColor(node.type);
  };
  const linkDisplayColor = (edge: GraphEdge): string => {
    if (colorMode === "type") return confidenceColor(edge.confidence);
    const scope = edgeServiceScope(edge, nodesById);
    if (scope === "internal") {
      const source = nodesById.get(edgeSource(edge));
      const serviceId = source ? graphNodeServiceId(source) : null;
      return serviceId ? serviceColor(serviceId) : EDGE_SCOPE_INFO.internal.color;
    }
    return EDGE_SCOPE_INFO[scope].color;
  };
  const frameWholeGraph = (duration = 700) => {
    const distance = Math.max(480, clusterGrid.extent * 1.85);
    forceRef.current?.cameraPosition(
      { x: distance, y: distance * 0.72, z: distance * 1.08 },
      { x: 0, y: 0, z: 0 },
      0,
    );
    forceRef.current?.zoomToFit(duration, 65);
  };
  const focusNode = (node: RenderGraphNode) => {
    if (node.x === undefined || node.y === undefined || node.z === undefined) return;
    if (node.isClusterShell) {
      const distance = Math.max(90, (node.clusterRadius || 13) * 4.5);
      forceRef.current?.cameraPosition(
        { x: node.x + distance * 0.78, y: node.y + distance * 0.5, z: node.z + distance },
        { x: node.x, y: node.y, z: node.z },
        750,
      );
      return;
    }
    onSelect(node);
    if (largeGraph) {
      forceRef.current?.cameraPosition(
        { x: node.x + 82, y: node.y + 52, z: node.z + 105 },
        { x: node.x, y: node.y, z: node.z },
        750,
      );
      return;
    }
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
        <button title="Показать весь граф под диагональным углом, чтобы была видна глубина по X, Y и Z." onClick={() => frameWholeGraph()}>Вписать</button>
        <button title="Вернуть камеру к исходному объёмному ракурсу; фильтры и выбранные данные не меняются." onClick={() => frameWholeGraph()}>Сбросить камеру</button>
        <span>ЛКМ — вращать · колесо — zoom · ПКМ — pan</span>
      </div>
      <div className="graph-color-panel">
        <div className="graph-color-toggle" aria-label="Режим окраски графа">
          <button aria-pressed={colorMode === "service"} className={colorMode === "service" ? "active" : ""} title="Одинаковый цвет узлов означает один service_id. Внутренние рёбра окрашены цветом сервиса, оранжевые идут между сервисами, серые не имеют определённой границы." onClick={() => setColorMode("service")}>По сервисам</button>
          <button aria-pressed={colorMode === "type"} className={colorMode === "type" ? "active" : ""} title="Цвет узла показывает его тип, а цвет ребра — уровень уверенности анализа." onClick={() => setColorMode("type")}>По типам</button>
        </div>
        {colorMode === "service" && <div className="graph-scope-legend">
          <span title={`${EDGE_SCOPE_INFO.internal.meaning} Пример: OrderController → OrderService внутри orders-service.`}><i className="scope-internal" />внутри сервиса</span>
          <span title={`${EDGE_SCOPE_INFO.cross.meaning} Пример: orders-service → payments-service.`}><i className="scope-cross" />между сервисами</span>
          <span title={`${EDGE_SCOPE_INFO.unknown.meaning} Пример: вызов к URL, который не сопоставился с сервисом.`}><i className="scope-unknown" />граница неизвестна</span>
          <small>{serviceNodeOverview
            ? "Режим сервисов: один шар = один настоящий сервис; дополнительных центров нет"
            : `${serviceCount} сервисных оболочек · устойчивый случайный разброс в 3D · прозрачное ядро = центр сервиса`}</small>
        </div>}
      </div>
      {(largeGraph || renderPlan.hiddenNodes > 0 || renderPlan.hiddenEdges > 0) && <div className="graph-performance-note" title="Для больших snapshot отключены постоянные подписи, анимированные частицы и часть геометрии стрелок. Полные данные остаются в фильтрах и деталях; подпись узла видна при наведении.">{renderPlan.hiddenNodes || renderPlan.hiddenEdges ? `Безопасный лимит: скрыто ${renderPlan.hiddenNodes} узлов и ${renderPlan.hiddenEdges} рёбер. Сузьте типы или поиск.` : "Режим производительности: подписи — по наведению, анимация связей отключена."}</div>}
      <ForceGraph3D<RenderGraphNode, GraphEdge>
        ref={forceRef}
        width={size.width}
        height={size.height}
        graphData={graphData}
        backgroundColor="#0b100d"
        showNavInfo={false}
        nodeLabel={(node) => {
          if (node.isClusterShell) {
            return `<b>Сервисный кластер: ${escapeHtml(node.label)}</b><br/>${node.clusterMemberCount || 0} узлов внутри оболочки<br/><small>Полупрозрачное цветное ядро отмечает центр, но не перекрывает внутренние связи. Ядро, узлы и оболочка одного цвета означают один service_id.</small>`;
          }
          const info = NODE_TYPE_INFO[node.type];
          const serviceId = graphNodeServiceId(node);
          return `<b>${escapeHtml(info?.label || node.type)} · ${escapeHtml(node.type)}</b><br/>${escapeHtml(node.label)}${serviceId ? `<br/><b>Сервисный кластер: ${escapeHtml(serviceId)}</b>` : "<br/><small>Сервисный кластер не определён</small>"}${info ? `<br/><small>${escapeHtml(info.meaning)}</small><br/><small>Пример: ${escapeHtml(info.example)}</small><br/><small>Источник: ${escapeHtml(info.source)}</small>` : ""}<br/><small>Evidence: ${node.evidence_ids.length}</small>`;
        }}
        nodeVisibility={(node) => !node.isClusterShell || colorMode === "service"}
        nodeColor={nodeDisplayColor}
        nodeVal={(node) => node.isClusterShell
          ? 0.001
          : veryLargeGraph
            ? node.type === "Service" ? 2.2 : node.type === "Repository" ? 1.2 : 0.55
            : node.type === "Service" ? 8 : node.type === "Repository" ? 5 : 3}
        nodeOpacity={veryLargeGraph ? 0.78 : 0.94}
        nodeResolution={veryLargeGraph ? 5 : largeGraph ? 8 : 14}
        nodeThreeObjectExtend={(node) => !node.isClusterShell}
        nodeThreeObject={(node) => {
          if (node.isClusterShell) return createClusterShell(node);
          if (!persistentLabels) return null as unknown as Group;
          const sprite = new SpriteText(short(node.label, node.type === "Service" ? 32 : 22));
          sprite.color = nodeDisplayColor(node);
          sprite.textHeight = node.type === "Service" ? 4.7 : 3.1;
          sprite.backgroundColor = "rgba(7, 12, 9, .78)";
          sprite.padding = 1.4;
          sprite.borderRadius = 2;
          sprite.position.y = node.type === "Service" ? 8 : 5;
          return sprite;
        }}
        linkLabel={(edge) => {
          const count = Number(edge.metadata?.operation_count || 0);
          const operations = Array.isArray(edge.metadata?.operations)
            ? edge.metadata.operations.slice(0, 8).map(escapeHtml).join("<br/>")
            : escapeHtml(edge.label);
          const info = EDGE_TYPE_INFO[edge.type];
          const scope = edgeServiceScope(edge, nodesById);
          const scopeInfo = EDGE_SCOPE_INFO[scope];
          return `<b>${escapeHtml(info?.label || edge.type)} · ${escapeHtml(edge.type)}</b><br/><b>${escapeHtml(scopeInfo.label)}</b> — ${escapeHtml(scopeInfo.meaning)}${info ? `<br/>${escapeHtml(info.direction)}<br/><small>${escapeHtml(info.meaning)}</small><br/><small>Пример: ${escapeHtml(info.example)}</small>` : ""}<br/>Уверенность: ${escapeHtml(CONFIDENCE_INFO[edge.confidence]?.label || edge.confidence)}<br/>Источник: ${escapeHtml(EDGE_ORIGIN_LABELS[edge.origin] || edge.origin)} · evidence ${edge.evidence_ids.length}${count > 1 ? `<br/><b>${count} вызовов</b>` : ""}${operations ? `<br/>${operations}` : ""}`;
        }}
        linkColor={linkDisplayColor}
        linkWidth={(edge) => colorMode === "service" && edgeServiceScope(edge, nodesById) === "cross" ? (largeGraph ? 1.5 : 3.2) : largeGraph ? (edge.confidence === "DECLARED" || edge.confidence === "HIGH" ? 1.15 : 0.65) : edge.confidence === "DECLARED" ? 2.8 : edge.confidence === "HIGH" ? 2.2 : edge.confidence === "MEDIUM" ? 1.5 : 1.1}
        linkOpacity={largeGraph ? 0.64 : 0.78}
        linkCurvature={(edge) => colorMode === "service" && edgeServiceScope(edge, nodesById) === "cross" ? 0.18 : 0}
        linkCurveRotation={(edge) => positionHash(edge.id, 211) * Math.PI * 2}
        linkDirectionalArrowLength={(edge) => veryLargeGraph ? (edgeServiceScope(edge, nodesById) === "cross" ? 2.2 : 0) : largeGraph ? 2.2 : 4.2}
        linkDirectionalArrowRelPos={0.88}
        linkDirectionalArrowColor={linkDisplayColor}
        linkDirectionalParticles={largeGraph ? 0 : (edge) => edge.confidence === "DECLARED" || edge.confidence === "HIGH" ? 2 : edge.confidence === "MEDIUM" ? 1 : 0}
        linkDirectionalParticleWidth={1.7}
        linkDirectionalParticleSpeed={0.004}
        linkDirectionalParticleColor={linkDisplayColor}
        cooldownTicks={veryLargeGraph ? 24 : largeGraph ? 42 : 90}
        d3AlphaDecay={veryLargeGraph ? 0.14 : largeGraph ? 0.08 : 0.045}
        d3VelocityDecay={veryLargeGraph ? 0.62 : largeGraph ? 0.52 : 0.42}
        onEngineStop={() => {
          if (fittedSignature.current === graphSignature) return;
          fittedSignature.current = graphSignature;
          frameWholeGraph(600);
        }}
        onNodeClick={(node) => focusNode(node)}
      />
    </div>
  );
}

export default memo(GraphCanvas3D);
