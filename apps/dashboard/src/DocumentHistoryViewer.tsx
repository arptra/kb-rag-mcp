import { useEffect, useState } from "react";
import { api } from "./api";
import { accessError } from "./accessApi";
import { integrationDate } from "./managementApi";
import type { IndexDocument, IndexDocumentDetail } from "./types";
import "./integrations.css";

interface Version {
  document_id: string; title: string; page_id?: string; revision_id?: string;
  source_version?: string | null; source_updated_at?: string | null; fetched_at?: string | null;
  is_latest?: boolean; history_baseline?: boolean; valid_from?: string | null; valid_to?: string | null;
}
interface Asset {
  id: string; filename: string; mime_type: string; source_id: string; version: string | number;
  status: string; extracted_text: string; description: string;
}
interface VersionPage { versions: Version[]; total?: number; has_more?: boolean }
type Selection = { document_id?: string; page_id?: string; source_version?: string; as_of?: string };
const origins: Record<string, string> = { repository: "Git / OpenSpec", upload: "Загружен вручную", ssot: "SSOT", local: "Локальный источник", confluence: "Confluence" };

function safeLink(value: string | null | undefined): string | null {
  if (!value) return null;
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : null; } catch { return null; }
}

function AssetPreview({ asset, password }: { asset: Asset; password: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [show, setShow] = useState(false);
  useEffect(() => {
    if (!show) return;
    const controller = new AbortController(); let objectUrl: string | null = null;
    setLoading(true); setError("");
    const load = async () => {
      try {
        const params = new URLSearchParams({ source_id: asset.source_id, version: String(asset.version) });
        const headers = new Headers(); if (password) headers.set("X-KB-Admin-Password", password);
        const response = await fetch(`/admin/api/confluence/assets/${encodeURIComponent(asset.id)}?${params}`, { headers, credentials: "same-origin", signal: controller.signal });
        if (!response.ok) throw new Error(`Не удалось загрузить оригинал (${response.status})`);
        const blob = await response.blob(); if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
      } catch (caught) { if (!controller.signal.aborted) setError(accessError(caught)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void load(); return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); setUrl(null); };
  }, [asset.id, asset.source_id, asset.version, password, show]);
  return <article className="history-asset"><div className="integration-job-head"><div><b>{asset.filename}</b><small className="integration-muted"> · {asset.status === "extracted" ? "Обработано GigaCode" : asset.status === "archived" ? "Оригинал сохранён" : asset.status}</small></div><button className="button secondary" onClick={() => setShow(current => !current)}>{show ? "Скрыть оригинал" : "Показать оригинал"}</button></div>
    {loading && <p className="integration-muted">Загружаем изображение…</p>}{error && <div className="form-error">{error}</div>}{show && url && <img src={url} alt={asset.description || `Оригинал: ${asset.filename}`} />}
    {asset.extracted_text && <><h4>Извлечённый текст</h4><pre>{asset.extracted_text}</pre></>}{asset.description && <><h4>Описание GigaCode</h4><p>{asset.description}</p></>}
    {!asset.extracted_text && !asset.description && <p className="integration-muted">Описание отсутствует. Состояние обработки доступно в журнале источника.</p>}
  </article>;
}

export default function DocumentHistoryViewer({ indexId, document, password, onClose }: { indexId: string; document: IndexDocument; password: string; onClose: () => void }) {
  const [detail, setDetail] = useState<IndexDocumentDetail | null>(null);
  const [selection, setSelection] = useState<Selection>({ document_id: document.document_id });
  const [tab, setTab] = useState<"content" | "history" | "assets">("content");
  const [versions, setVersions] = useState<Version[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [asOf, setAsOf] = useState("");
  const [loading, setLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const pageId = detail?.page_id || document.page_id || null;
  const assets = Array.isArray(detail?.metadata.assets) ? detail.metadata.assets.filter((asset): asset is Asset => Boolean(asset && typeof asset === "object" && "id" in asset && "source_id" in asset)) : [];
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError("");
    const params = new URLSearchParams({ index_id: indexId }); Object.entries(selection).forEach(([key, value]) => { if (value) params.set(key, value); });
    void api<IndexDocumentDetail>(`/admin/api/indexes/document?${params}`, password, { signal: controller.signal }, 30_000)
      .then(payload => { if (!controller.signal.aborted) setDetail(payload); })
      .catch(caught => { if (!controller.signal.aborted) setError(accessError(caught)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [indexId, password, selection]);
  useEffect(() => {
    if (tab !== "history" || !pageId) return;
    const controller = new AbortController(); setHistoryLoading(true); setHistoryError("");
    const params = new URLSearchParams({ index_id: indexId, page_id: pageId, offset: String(offset), limit: "50" });
    void api<VersionPage>(`/admin/api/indexes/document/versions?${params}`, password, { signal: controller.signal })
      .then(payload => { if (!controller.signal.aborted) { setVersions(current => offset ? [...current, ...payload.versions] : payload.versions); setHasMore(payload.has_more ?? (payload.total !== undefined ? offset + payload.versions.length < payload.total : payload.versions.length === 50)); } })
      .catch(caught => { if (!controller.signal.aborted) setHistoryError(accessError(caught)); })
      .finally(() => { if (!controller.signal.aborted) setHistoryLoading(false); });
    return () => controller.abort();
  }, [tab, pageId, indexId, password, offset]);
  const openVersion = (version: Version) => { setSelection({ document_id: version.document_id }); setTab("content"); };
  const latest = () => { if (pageId) { setSelection({ page_id: pageId }); setTab("content"); } };
  const onDate = () => {
    if (!asOf || !pageId) return;
    const date = new Date(asOf); if (!Number.isFinite(date.getTime())) return;
    setSelection({ page_id: pageId, as_of: date.toISOString() }); setTab("content");
  };
  const sourceLink = safeLink(detail?.source_url);
  return <div className="modal-backdrop" role="presentation" onMouseDown={onClose}><section className="modal document-modal" role="dialog" aria-modal="true" aria-labelledby="document-history-title" onMouseDown={event => event.stopPropagation()}>
    <header className="modal-header"><div><span className="eyebrow">Документ индекса</span><h2 id="document-history-title">{detail?.title || document.title}</h2></div><button className="icon-button" onClick={onClose} aria-label="Закрыть">×</button></header>
    <div className="document-viewer"><nav className="integration-tabs" aria-label="Просмотр документа"><button className={`button ${tab === "content" ? "primary" : "quiet"}`} onClick={() => setTab("content")}>Содержимое</button><button className={`button ${tab === "history" ? "primary" : "quiet"}`} disabled={!pageId} onClick={() => { setOffset(0); setTab("history"); }}>История</button>{assets.length > 0 && <button className={`button ${tab === "assets" ? "primary" : "quiet"}`} onClick={() => setTab("assets")}>Изображения · {assets.length}</button>}</nav>
      {detail?.is_latest === false && <div className="document-version-warning"><span>Историческая версия {detail.source_version || ""} · {integrationDate(detail.source_updated_at)}</span><button className="button quiet" onClick={latest}>Последняя в индексе</button></div>}
      {error && <div className="form-error" role="alert">{error}</div>}
      {error && pageId && <button className="button secondary" onClick={latest}>Вернуться к последней версии</button>}
      {loading ? <div className="document-loading">Загружаем версию…</div> : !error && detail && <>
        {tab === "content" && <><div className="document-facts"><span><small>Индекс</small><b>{detail.index.name}</b></span><span><small>Источник</small><b>{origins[detail.origin] || detail.origin}</b></span><span><small>Версия</small><b>{detail.source_version || (detail.history_baseline ? "Начальный снимок" : "—")}{detail.is_latest !== false ? " · последняя в индексе" : ""}</b></span><span><small>Изменена в источнике</small><b>{integrationDate(detail.source_updated_at)}</b></span><span><small>Получена нами</small><b>{integrationDate(detail.fetched_at || detail.loaded_at)}</b></span>{detail.revision_updated_at && detail.revision_updated_at !== detail.source_updated_at && <span><small>Снимок с учётом вложений</small><b>{integrationDate(detail.revision_updated_at)}</b></span>}</div><div className="document-source-line"><code>{detail.source_path}</code>{sourceLink && <a href={sourceLink} target="_blank" rel="noreferrer">Открыть источник ↗</a>}</div><section className="document-content"><div><span className="eyebrow">Содержимое выбранной версии</span><b>{(detail.content_chars ?? detail.content.length).toLocaleString("ru-RU")} символов</b></div><pre>{detail.content}</pre></section><details className="document-metadata"><summary>Метаданные и идентификаторы</summary><pre>{JSON.stringify({ document_id: detail.document_id, page_id: detail.page_id, revision_id: detail.revision_id, source_id: detail.source_id, metadata: detail.metadata }, null, 2)}</pre></details></>}
        {tab === "history" && <><div className="integration-actions"><label>Состояние на дату и время · местное время<input type="datetime-local" value={asOf} onChange={event => setAsOf(event.target.value)} /></label><button className="button secondary" disabled={!asOf || !pageId} onClick={onDate}>Открыть на дату</button><button className="button quiet" onClick={latest}>Последняя в индексе</button></div><p className="integration-muted">Показаны сохранённые версии. Для старых документов история начинается с первого сохранённого снимка; удалённые в источнике версии могут быть недоступны.</p>{historyError && <div className="form-error">{historyError}</div>}<div className="table-wrap"><table><thead><tr><th>Версия</th><th>Изменена в источнике</th><th>Получена нами</th><th /></tr></thead><tbody>{versions.map(version => <tr key={version.document_id}><td>{version.source_version || "Начальный снимок"}{version.is_latest && <small>Последняя в индексе</small>}</td><td>{integrationDate(version.source_updated_at)}</td><td>{integrationDate(version.fetched_at)}</td><td><button className="button secondary" onClick={() => openVersion(version)}>Открыть</button></td></tr>)}</tbody></table></div>{historyLoading && <p className="integration-muted">Загружаем историю…</p>}{!versions.length && !historyLoading && <p className="integration-muted">Сохранённых версий пока нет.</p>}{hasMore && <button className="button secondary" disabled={historyLoading} onClick={() => setOffset(current => current + 50)}>Ещё версии</button>}</>}
        {tab === "assets" && <div className="history-assets"><p className="integration-muted">Извлечённый текст и описания связаны с изображениями выбранной версии. Для прошлых версий набор вложений может быть неполным.</p>{assets.map(asset => <AssetPreview key={`${asset.id}:${asset.version}`} asset={asset} password={password} />)}</div>}
      </>}
    </div>
  </section></div>;
}
