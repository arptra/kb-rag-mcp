# Confluence synchronization

The dashboard connects a Confluence root page to an explicitly selected RAG index. A
source never falls back to the default index. Connection settings and credentials are
managed through `/admin/api/confluence`; ordinary readers cannot change credentials.

## Configuration and authentication

Set `KB_CONFLUENCE_STATE_DIR` to a persistent directory outside the disposable embedding
cache. The directory holds SQLite state, encrypted credentials, attachments and image
extraction results. Current page text is materialized in the selected index's knowledge
directory. Back up both directories. The state directory's `credentials.key` decrypts
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
Explicit `deployment=data_center` always sends Bearer authentication, including for
older saved sources that still contain a Cloud email. The dashboard omits that email
in Data Center requests. Existing `auto` connections with a username retain Basic
authentication for compatibility; choose Data Center explicitly when using a PAT.

When connecting another root page, the dashboard reuses saved credentials for the same
Confluence server. A selector lists existing connections and their account names;
choosing one fills the server and authentication settings without returning its token
to the browser. The form automatically selects a matching saved connection and checks
access to the new root before saving. Administrators can instead enter a new token.
An authentication failure prompts for another saved connection or new credentials;
network, TLS, rate-limit and page-permission errors do not invalidate saved credentials.

The administrative `GET /admin/api/confluence/credentials` endpoint returns only safe
connection metadata. Pass `credential_source_id` instead of `token` to connection
tests, source creation or updates. Reuse copies encrypted credentials into the target
source, along with the donor's deployment and username; the target index, root and
schedule remain independent. Updating or disconnecting one source does not rotate or
remove credentials from other sources. Reuse is restricted to the same scheme, host,
port and Confluence API base path (Cloud's implicit `/wiki` is recognized). Changing
servers requires a matching saved connection or a new token, including when editing
an existing source. Existing connections are available without a migration.

The connection test reads root metadata without saving settings or content. Creating a
source through the dashboard/API immediately queues its first synchronization. This
sync discovers the root and descendants using paginated APIs, then saves their stable
IDs, parent relationships, versions and modification dates. Each later run checks the
tree and attachment versions again. Unchanged pages do not download the body or trigger
an index build. New attachment versions refresh the current document even if the page
version has not changed. Text or image changes publish only to the source's selected index.
Cloud traversal follows paginated `direct-children` recursively through pages, folders,
databases, whiteboards and embeds, avoiding the depth limit of the descendants API.
Data Center likewise follows each page's paginated `/child/page` collection. Both
deployments traverse every nesting level using a queue, without a depth cutoff or
Python recursion. The separate `max_pages` safety limit bounds the total number of
discovered items (50,000 by default); reaching it reports an incomplete tree rather
than a successful full import. A page that did not change is still checked for new
children during every synchronization.
Data Center pagination preserves the server's query options when switching from a
next link to an offset. Repeated child listings that make no progress are reported
and retried as an incomplete branch instead of looping indefinitely.

The source journal records each checked parent (`page_id`), children returned,
new branches, completed branches and branches still unresolved. `depth_limit: null`
means no depth limit; `page_limit` is the total-item safety limit. After updating the
backend, use **Синхронизировать** on an existing source to rescan its entire tree. Previously
downloaded unchanged pages reuse their saved content. A branch that fails after
retries remains in the failure report, and its old descendants are not removed.

Only page content is indexed. Cloud attachment downloads use the supported REST
attachment download endpoint with an explicit version; Data Center attachment lists
request `expand=version` to detect image-only updates.

One Confluence service token imports a shared corpus. Confluence per-person permissions
are not automatically copied into this application's users. Connect a subtree whose
content is intended for this KB's audience. A confirmed page-level access denial hides
that page's content until access is confirmed again. An incomplete traversal or network
error never marks absent pages as deleted. Leaving the configured subtree removes a
page from current results. Archives created by earlier releases are left on disk but
are not used by search or agent tools.

Disconnecting a source stops its schedule and removes the connection from the active
list. Existing indexed documents and authenticated image evidence remain
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

Manual and scheduled requests share a durable queue. Duplicate active requests for the
same source and operation resolve to the same job; different operations run separately.
Only one scheduler owns a state directory. Interrupted
jobs resume after restart, reusing saved current pages, attachments and indexing
checkpoints. Canceling a job retains completed checkpoints. The scheduler and embedding
worker propagate cancellation while waiting for an index lock, downloading, extracting
images and indexing.

Failures loading an individual page or discovering its children do not stop the rest
of the synchronization. Failed items enter a durable retry queue. After each initial
discovery or page-loading pass, the worker makes up to two additional passes over
failed items with a delay between passes; HTTP-level retries and rate-limit waits
still apply within each attempt.
The queue and attempt counts survive a backend restart. Canceling a job remains
available while waiting or retrying.

The dashboard shows the retry round, pending items and recovered pages. Its
**Ошибки загрузки** report lists the remaining page IDs, titles, links, failed stage,
attempt count and sanitized error, 50 items at a time. A discovery failure is shown
as an unreadable branch: its descendants may not have been discovered yet. The report
refreshes while the selected job runs and remains available after completion. A job
that still has failed pages or branches finishes with warnings and publishes the
successfully loaded pages. Previously saved current pages are retained on transient errors;
confirmed access denial retains the existing access-loss behavior. Incomplete discovery
never treats unseen pages as deleted. Run **Синхронизировать** on the source to start another
synchronization after resolving a persistent error.

Global failures, such as an unavailable state database or index publication failure,
still fail the job rather than presenting an incomplete publication as successful.
These errors appear in the job status and journal, separately from page failures.
Index publication also receives up to two additional attempts. Unexpected exceptions
include their type and code locations, without exception payloads, page text or tokens.

An index lock coordinates source materialization and manual builds. Successful
synchronization runs a build before reporting publication complete. A publish failure
keeps the source dirty so the next run retries publication without downloading unchanged
pages again. The serving index is replaced only after its new generation is validated.

The index build still reads and chunks the selected index's current documents and
publishes a complete generation. Cached vectors are reused without rewriting them;
only missing text embeddings are calculated and checkpointed. Existing legacy vector
links receive an index automatically, and vector updates preserve their parent rows.
No re-download or deletion of existing state is needed for this optimization.

During indexing, the job and backend log expose `index_stage`, `provider`, `completed`,
`total`, `reused`, `computed` and `elapsed_seconds` when available. For embedding batches,
`reused + computed = completed`; duplicated text in one batch is computed once.
With `provider=hash`, embeddings are calculated locally; this avoids neural-model
inference but still requires tokenization, hashing, reading and publishing the index.
Repeated progress events are emitted at most twice a second; stage transitions and
final counters are delivered immediately. This avoids a database commit for every
document on fast builds while preserving progress reporting and worker cancellation.

## Current pages and images

`source_updated_at` is the page's modification date from Confluence. The separate
`snapshot_updated_at` is the latest page/attachment modification date for the current
document; if the API omits an attachment date, its first observed timestamp is used. These are
distinct from fetching and indexing dates and remain available as document metadata.
Synchronization always loads only the current Confluence page. It does not list or fetch
historical versions and does not create page archives or original-body snapshots.
The legacy `include_history` configuration field is accepted for compatibility but
always normalized to `false`, including when reading older saved connections.
Existing page archive files are not deleted during an upgrade; they are not used for
search or agent responses. MCP and A2A retrieve current documents only.

Image analysis uses the configured GigaCode runner, with one downloaded image in an
isolated workspace, read-only tools, cancellation and a locally validated JSON result.
It extracts visible text, descriptions, tables, diagram nodes and edges, and uncertainty.
No Confluence credential enters the prompt. Image bytes plus extractor/model identity
key the cache; source/version provenance is added by the server. Inline SVG diagrams are
also archived and passed to extraction. Unsupported or unreadable images remain visible
as warnings with their originals; an extraction failure does not discard page text.

Administrators can use **Распознать изображения** on a source card to process images
in its already imported current pages. This creates a separate durable operation in
the same journal, with progress, cancellation, retries and browser-login links. It
checks the current page and attachment versions but does not rediscover the subtree;
run **Синхронизировать** first to import newly added pages. Recognized descriptions are
published to the source's existing index. Successful cached results are reused, and
an unchanged repeat does not rerun GigaCode or rebuild the index.

Manual recognition does not enable automatic recognition or change the source's
schedule. Saved successful image descriptions remain available during later ordinary
syncs, including when automatic recognition is disabled. The cache can reuse identical
image bytes across attachment versions, including an unchanged inline SVG on an edited
page; changed image bytes do not inherit the previous description. Source cards separate primary
actions from settings, scheduling and cleanup actions; readers can view pages and
journals, while management actions remain administrator-only.

Only successful (`ok`) image results are reused from the extraction cache. A previous
`partial`, `unsupported` or `unreadable` result is analyzed again on retry, including
results saved by older versions. Missing/corrupt cache entries are regenerated.
Known image signatures select the temporary file extension; renaming an unknown
binary file to PNG is not used as a substitute for conversion. Image filenames are
also recognized when Confluence reports the generic `application/octet-stream` type.

The operation journal distinguishes download problems from image-analysis problems:

- `attachment_html_response`: the image download endpoint returned HTML (often an
  SSO/login response), even if its HTTP status was 200; it was not sent to GigaCode.
- `image_input_empty`, `image_input_html`, `image_input_format`: the local input is
  empty, contains HTML or has an unrecognized format; analysis did not start.
- `image_timeout`: waiting for browser login or image analysis exceeded the CLI limit.
- `image_invalid_result`: GigaCode's JSON, schema or reported input file was invalid.
- `image_runner_error`: the CLI failed without a valid result; this does not establish
  that the image format itself is unsupported.
- `image_partial`, `image_unreadable`, `image_unsupported`: GigaCode returned this
  status. Check original quality, format, and the configured CLI/model's image tools;
  the status alone does not prove a file cannot be processed by another configuration.
- `image_extractor_unconfigured`: no GigaCode image extractor is configured.

Entries include page/attachment IDs and safe failure codes; analysis-start events also
record input size and extension. Raw CLI exception text, model explanations, credentials
and attachment download URLs are not copied into these diagnostic messages.

When GigaCode CLI requests browser authentication, the Confluence dashboard shows
**GigaCode ожидает вход через браузер** above the sources and in the running job.
Sign into the dashboard as an administrator, then use **Открыть страницу входа** or
**Скопировать ссылку**. Complete the login in the browser; the existing CLI process
continues image analysis automatically. There is no need to queue a second synchronization.
The full URL is also available in the administrator's operation journal under
`details.authentication_url`. Readers see the waiting state but not the login URL.
The normal backend log records the job ID and the request for browser authentication,
without copying the one-time URL or CLI output into the shared server log.

The waiting banner clears when analysis resumes, or the attempt ends, fails or is
cancelled. Restarting the backend discards the old pending login action and resumes
the saved synchronization; if the new CLI process requires login it supplies a new
URL. A link in an old journal entry belongs to that old attempt. The existing
`KB_GIGACODE_AUTH_TIMEOUT_SECONDS` setting controls the configured login waiting allowance
(600 seconds by default); analysis has its own timeout. Browser login must use the flow
supported by the installed CLI; forwarding a URL does not forward a callback listening
on the backend's localhost to the administrator's computer.

Original attachments of other types are archived. Plugin-specific macros without an API
text/image representation cannot be reconstructed reliably; the normalized document
marks macros without a readable representation. Image interpretation is evidence with
explicit uncertainty, not a guarantee that every diagram relationship is recognized correctly.

Downloads are bounded in size and time. API pagination is restricted to the configured
origin. Cloud's signed `*.media.atlassian.com` downloads use a separate client without
Confluence Authorization headers or cookies. Arbitrary cross-origin redirects are
rejected. Remote filenames never become managed filesystem paths.

## Operations and API

Jobs persist each stage (`discovery`, `fetch`, `attachments`, `images`, `normalization`,
`checkpoint`, `indexing`, `publication`) with UTC timestamps, counters,
cache hits, warnings and errors. Log reads use a cursor and bounded page size rather
than repeatedly loading an entire growing log. Tokens and Authorization headers are
redacted from persisted messages.

The administrator can clear a source's synchronization journal with **Очистить журнал**
in its row. This removes log entries across that source's jobs, including entries
already written by running jobs. It preserves source configuration, schedules,
documents, attachments, job progress and retry/failure reports. Running jobs continue
and new entries appear normally; other sources' journals are unaffected.

Main endpoints:

- `GET /admin/api/confluence` — source/job summary and extractor availability.
- `GET /admin/api/confluence/credentials` — administrator-only saved credential choices;
  metadata only, no tokens or encrypted credentials.
- `POST /admin/api/confluence/test` — read-only root connection check.
- `GET|POST /admin/api/confluence/sources` — list/create connections; creation also queues
  the initial synchronization.
- `POST /admin/api/confluence/sources/{id}/images/analyze` — queue manual recognition for
  already imported current pages. Jobs retain `type=confluence` and expose
  `operation=analyze_images`; existing synchronization jobs use `operation=sync`.
  A duplicate active operation returns its job; a conflicting operation returns 409.
- `PATCH /admin/api/confluence/sources/{id}` — update configuration or token.
- `POST /admin/api/confluence/sources/{id}/sync` — manual synchronization.
- `POST /admin/api/confluence/sources/{id}/pause` — pause/resume schedule.
- `POST /admin/api/confluence/sources/{id}/logs/clear` — administrator-only journal
  cleanup for all jobs of this source, without cancelling synchronization.
- `POST /admin/api/confluence/sources/{id}/delete` — disconnect while retaining indexed content
  and archived evidence.
- `GET /admin/api/confluence/sources/{id}/pages?offset=0&limit=100&query=` — bounded saved
  page tree state with title/ID filtering; returns `pages`, `total`, `offset`, `limit`
  and `has_more` (maximum page size 200).
- `GET /admin/api/confluence/jobs/{id}` — job status.
- `GET /admin/api/confluence/jobs/{id}/log?cursor=0&limit=200` — incremental journal.
- `GET /admin/api/confluence/jobs/{id}/failures?offset=0&limit=50` — paginated durable
  retry/failure report with reader authorization; returns `items`, `total`, `offset`,
  `limit` and `has_more` (maximum page size 200). Each item includes `page_id`, `title`,
  `url`, `stage`, `task_kind` (`page` or `discovery`), `attempts`, `error`, `error_type`,
  `status_code` and `status` (`retry` or `failed`). Recovered items leave this report.
- `POST /admin/api/confluence/jobs/{id}/cancel` — cancel while retaining checkpoints.
- `GET /admin/api/confluence/assets/{id}?source_id=...&version=...` — authenticated
  access to an attachment belonging to the requested source/version.

## Connection troubleshooting

Run `./scripts/start-mcp-http.sh logs` and repeat **Check access** in the dashboard.
The connection check logs its start and outcome in `.cache/kb/runtime/mcp-http.log`;
each API request logs a trace/request ID, endpoint (without query), attempt, response
status and elapsed time. Retries log their reason and wait duration. Client startup
reports the authentication mode, whether CA/proxy environment settings are present,
timeouts and retry limit, without exposing their secret values.

The synchronization journal is also mirrored to the server log with job ID and stage:
page discovery/checks, attachment downloads/cache, image analysis/cache, normalization,
saved checkpoints, indexing progress, publication, cancellation and completion. All of
this uses `KB_LOG_LEVEL=INFO` (the default); DEBUG is not needed. Network failures have
separate categories. Raw HTTPX/httpcore transport dumps are suppressed only within
Confluence requests because they can contain signed URLs or authentication headers;
other integrations' logs are unaffected. Document bodies and raw GigaCode output are
not mirrored to the server log. Synchronization errors remain in the UI job journal.

To collect the latest diagnostic fragment after reproducing a failure:

```bash
tail -n 300 .cache/kb/runtime/mcp-http.log
```

`Confluence network request failed` means the transport failed, not that Confluence
rejected the token. The UI now includes a category and explanation, such as
`tls_certificate`, `dns`, `proxy`, `connect_timeout` or `read_timeout`.
Connection-test JSON adds `error_code` for classified failures and `upstream_status`
for a Confluence HTTP error. The endpoint keeps HTTP 409 for upstream failures;
HTTP 401 with `Administrator authentication required` belongs to this app's admin login.
An upstream `Authentication failed` now includes HTTP 401, the actual `auth=basic`
or `auth=bearer` mode, deployment and request ID, plus a mode-specific troubleshooting
hint. Its `error_code` is `authentication`. A 401 can originate from Confluence or a
gateway; it does not prove the token itself is invalid. The response body and raw
authentication headers are never copied into diagnostics.

Compare curl from the **backend host/container and service environment**, using the
same destination. A successful curl from a laptop does not establish that the backend
has the required DNS, route, VPN or proxy. `curl -k` disables certificate verification;
the application verifies HTTPS certificates. For a corporate certificate authority,
obtain a trusted PEM CA bundle and export it for the backend process before restarting:

```bash
export SSL_CERT_FILE=/absolute/path/to/corporate-ca-bundle.pem
./scripts/start-mcp-http.sh restart
./scripts/start-mcp-http.sh logs
```

Use a bundle containing all needed trusted CAs, including public CAs if Cloud attachment
downloads use public Atlassian media hosts. This replaces HTTPX's default CA bundle.
For systemd, configure the variable in the service's environment and restart that service.
The file is a **trusted CA bundle for outbound connections**, not this application's
self-signed browser/server certificate or a private key.

HTTPX also uses exported `SSL_CERT_DIR`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and
`NO_PROXY`. The launcher does not source the project `.env`: placing these non-`KB_`
variables there alone does not export them to HTTPX. It does not read curl's `.curlrc`,
`CURL_CA_BUNDLE` or `REQUESTS_CA_BUNDLE`. See the official
[HTTPX environment settings](https://www.python-httpx.org/environment_variables/).

Offline verification:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/pytest -q -p no:cacheprovider tests/test_confluence.py
```

These tests mock Confluence and GigaCode. A real deployment still needs a successful
connection test and an initial run against its own Confluence permissions and installed
GigaCode image-reading capability.
