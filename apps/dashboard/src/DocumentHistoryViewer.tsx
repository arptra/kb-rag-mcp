import { useEffect, useState } from "react";
import { api } from "./api";
import { accessError } from "./accessApi";
import { integrationDate } from "./managementApi";
import type { IndexDocument, IndexDocumentDetail } from "./types";
import "./integrations.css";

interface Asset {
  id: string; filename: string; mime_type: string; source_id: string; version: string | number;
  status: string; extracted_text: string; description: string;
}
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
  const [tab, setTab] = useState<"content" | "assets">("content");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const assets = Array.isArray(detail?.metadata.assets) ? detail.metadata.assets.filter((asset): asset is Asset => Boolean(asset && typeof asset === "object" && "id" in asset && "source_id" in asset)) : [];
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(""); setDetail(null); setTab("content");
    const params = new URLSearchParams({ index_id: indexId });
    if (document.page_id) params.set("page_id", document.page_id);
    else params.set("document_id", document.document_id);
    void api<IndexDocumentDetail>(`/admin/api/indexes/document?${params}`, password, { signal: controller.signal }, 30_000)
      .then(payload => { if (!controller.signal.aborted) setDetail(payload); })
      .catch(caught => { if (!controller.signal.aborted) setError(accessError(caught)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [indexId, password, document.document_id, document.page_id]);
  const sourceLink = safeLink(detail?.source_url);
  return <div className="modal-backdrop" role="presentation" onMouseDown={onClose}><section className="modal document-modal" role="dialog" aria-modal="true" aria-labelledby="document-viewer-title" onMouseDown={event => event.stopPropagation()}>
    <header className="modal-header"><div><span className="eyebrow">Документ индекса</span><h2 id="document-viewer-title">{detail?.title || document.title}</h2></div><button className="icon-button" onClick={onClose} aria-label="Закрыть">×</button></header>
    <div className="document-viewer"><nav className="integration-tabs" aria-label="Просмотр документа"><button className={`button ${tab === "content" ? "primary" : "quiet"}`} onClick={() => setTab("content")}>Содержимое</button>{assets.length > 0 && <button className={`button ${tab === "assets" ? "primary" : "quiet"}`} onClick={() => setTab("assets")}>Изображения · {assets.length}</button>}</nav>
      {error && <div className="form-error" role="alert">{error}</div>}
      {loading ? <div className="document-loading">Загружаем страницу…</div> : !error && detail && <>
        {tab === "content" && <><div className="document-facts"><span><small>Индекс</small><b>{detail.index.name}</b></span><span><small>Источник</small><b>{origins[detail.origin] || detail.origin}</b></span><span><small>Изменена в источнике</small><b>{integrationDate(detail.source_updated_at)}</b></span><span><small>Получена нами</small><b>{integrationDate(detail.fetched_at || detail.loaded_at)}</b></span>{detail.revision_updated_at && detail.revision_updated_at !== detail.source_updated_at && <span><small>Обновлена с учётом вложений</small><b>{integrationDate(detail.revision_updated_at)}</b></span>}</div><div className="document-source-line"><code>{detail.source_path}</code>{sourceLink && <a href={sourceLink} target="_blank" rel="noreferrer">Открыть источник ↗</a>}</div><section className="document-content"><div><span className="eyebrow">Актуальное содержимое</span><b>{(detail.content_chars ?? detail.content.length).toLocaleString("ru-RU")} символов</b></div><pre>{detail.content}</pre></section><details className="document-metadata"><summary>Метаданные и идентификаторы</summary><pre>{JSON.stringify({ document_id: detail.document_id, page_id: detail.page_id, source_id: detail.source_id, metadata: detail.metadata }, null, 2)}</pre></details></>}
        {tab === "assets" && <div className="history-assets"><p className="integration-muted">Извлечённый текст и описания изображений текущей страницы.</p>{assets.map(asset => <AssetPreview key={`${asset.id}:${asset.version}`} asset={asset} password={password} />)}</div>}
      </>}
    </div>
  </section></div>;
}
