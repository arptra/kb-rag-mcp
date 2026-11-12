# Confluence synchronization

The dashboard connects a Confluence root page to an explicitly selected RAG index. A
source never falls back to the default index. Connection settings and credentials are
managed through `/admin/api/confluence`; ordinary readers cannot change credentials.

## Configuration and authentication

Set `KB_CONFLUENCE_STATE_DIR` to a persistent directory outside the disposable embedding
cache. The directory holds SQLite state, encrypted credentials, original page bodies,
immutable normalized revisions, attachments and image extraction results. Back up this
directory together with `KB_HISTORY_DIR`. Its `credentials.key` is necessary to decrypt
the saved tokens and must be included in a protected backup. The key and database are
created with owner-only file permissions; source responses never return a token.

A connection requires a name, an existing `index_id`, a Confluence base URL, a root page
URL and token. Data Center personal access tokens use Bearer authentication. Cloud API
tokens using Basic authentication additionally require the account email in `username`.
The adapter detects `*.atlassian.net` as Cloud; choose `deployment=cloud` or
`deployment=data_center` explicitly for other deployments. Root links containing
`/pages/<id>`, `pageId=<id>`, numeric IDs and legacy `/display/SPACE/Title` are supported.
Cloud email is required even when the deployment is detected automatically; a Cloud
API token is never silently treated as a Data Center Bearer token.

The connection test reads root metadata without saving settings or content. Creating a
source through the dashboard/API immediately queues its first synchronization. This
sync discovers the root and descendants using paginated APIs, then saves their stable
IDs, parent relationships, versions and modification dates. Each later run checks the
tree and attachment versions again. Unchanged pages do not download the body or trigger
an index build. New attachment versions create a new snapshot even if the page version
has not changed. Text or image changes publish only to the source's selected index.
Cloud traversal follows paginated `direct-children` recursively through pages, folders,
databases, whiteboards and embeds, avoiding the depth limit of the descendants API.
Only page content is indexed. Cloud attachment downloads use the supported REST
attachment download endpoint with an explicit version; Data Center attachment lists
request `expand=version` to detect image-only updates.

One Confluence service token imports a shared corpus. Confluence per-person permissions
are not automatically copied into this application's users. Connect a subtree whose
content is intended for this KB's audience. A confirmed page-level access denial hides
that page's current content and history until access is confirmed again; the protected
archive is retained. An incomplete traversal or network error never marks absent pages
as deleted. Leaving the configured subtree removes a page from current results while
retaining its archive.

Disconnecting a source stops its schedule and removes the connection from the active
list. Existing indexed documents, history and authenticated image evidence remain
available. An attachment from a page with confirmed access loss stays hidden even after
the source is disconnected.

## Scheduling and recovery

Each source has a schedule:

```json
{
  "kind": "daily",
  "interval_minutes": 60,
  "time": "03:00",
  "weekday": 0,
  "timezone": "Europe/Moscow"
}
```

`kind` is `manual`, `interval`, `daily` or `weekly`; weekday zero means Monday. The server
stores timestamps in UTC and evaluates daily/weekly wall times in the chosen IANA
timezone. Pausing stops scheduled checks; a manual sync remains available. The backend
must be running for scheduled work. A missed check is queued once after startup.

Manual and scheduled requests share a durable queue and duplicate active requests for
one source resolve to the same job. Only one scheduler owns a state directory. Interrupted
jobs resume after restart, reusing saved page/attachment revisions and indexing
checkpoints. Canceling a job retains completed checkpoints. The scheduler and embedding
worker propagate cancellation while waiting for an index lock, downloading, extracting
images and indexing.

An index lock coordinates source materialization and manual builds. Successful
synchronization runs a build before reporting publication complete. A publish failure
keeps the source dirty so the next run retries publication without downloading unchanged
pages again. The serving index is replaced only after its new generation is validated.

## History and images

`source_updated_at` is the page's modification date from Confluence. The separate
`snapshot_updated_at` is the latest page/attachment modification date for the snapshot;
if the API omits an attachment date, its first observed timestamp is used. These are
distinct from fetching and indexing dates. A normal search uses current revisions.
Historical tools can select a revision or date without mixing later image changes into
an earlier snapshot.

Enable `include_history` to backfill page versions still available through Confluence.
Historical page text is fetched with an explicit version. Current attachments are never
silently substituted into an older page version: backfilled versions say that their
historical attachment association is unavailable. Attachments and image evidence
observed by this service remain bound to their exact archived snapshot.
Data Center version enumeration uses `/rest/experimental/content/{id}/version`, as
described by [Atlassian](https://support.atlassian.com/confluence/kb/how-to-retrieve-all-previous-versions-of-content-using-the-rest-api/).
This is an experimental endpoint and availability must be checked on the target server.
If it is unavailable, disable backfilling existing history; snapshots observed by this
service still retain their history.

Image analysis uses the configured GigaCode runner, with one downloaded image in an
isolated workspace, read-only tools, cancellation and a locally validated JSON result.
It extracts visible text, descriptions, tables, diagram nodes and edges, and uncertainty.
No Confluence credential enters the prompt. Image bytes plus extractor/model identity
key the cache; source/version provenance is added by the server. Inline SVG diagrams are
also archived and passed to extraction. Unsupported or unreadable images remain visible
as warnings with their originals; an extraction failure does not discard page text.

Original attachments of other types are archived. Plugin-specific macros without an API
text/image representation cannot be reconstructed reliably; their original storage body
is retained. Image interpretation is evidence with explicit uncertainty, not a guarantee
that every diagram relationship is recognized correctly.

Downloads are bounded in size and time. API pagination is restricted to the configured
origin. Cloud's signed `*.media.atlassian.com` downloads use a separate client without
Confluence Authorization headers or cookies. Arbitrary cross-origin redirects are
rejected. Remote filenames never become managed filesystem paths.

## Operations and API

Jobs persist each stage (`discovery`, `fetch`, `attachments`, `images`, `normalization`,
`history`, `checkpoint`, `indexing`, `publication`) with UTC timestamps, counters,
cache hits, warnings and errors. Log reads use a cursor and bounded page size rather
than repeatedly loading an entire growing log. Tokens and Authorization headers are
redacted from persisted messages.

Main endpoints:

- `GET /admin/api/confluence` — source/job summary and extractor availability.
- `POST /admin/api/confluence/test` — read-only root connection check.
- `GET|POST /admin/api/confluence/sources` — list/create connections; creation also queues
  the initial synchronization.
- `PATCH /admin/api/confluence/sources/{id}` — update configuration or token.
- `POST /admin/api/confluence/sources/{id}/sync` — manual synchronization.
- `POST /admin/api/confluence/sources/{id}/pause` — pause/resume schedule.
- `POST /admin/api/confluence/sources/{id}/delete` — disconnect while retaining indexed content
  and archived evidence.
- `GET /admin/api/confluence/sources/{id}/pages?offset=0&limit=100&query=` — bounded saved
  page tree state with title/ID filtering; returns `pages`, `total`, `offset`, `limit`
  and `has_more` (maximum page size 200).
- `GET /admin/api/confluence/jobs/{id}` — job status.
- `GET /admin/api/confluence/jobs/{id}/log?cursor=0&limit=200` — incremental journal.
- `POST /admin/api/confluence/jobs/{id}/cancel` — cancel while retaining checkpoints.
- `GET /admin/api/confluence/assets/{id}?source_id=...&version=...` — authenticated
  access to an attachment belonging to the requested source/version.

Offline verification:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q -p no:cacheprovider tests/test_confluence.py
```

These tests mock Confluence and GigaCode. A real deployment still needs a successful
connection test and an initial run against its own Confluence permissions and installed
GigaCode image-reading capability.
