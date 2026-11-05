# Aino for QGIS

## Local package and release order

Build an installable archive without installing into any profile:

```sh
python3 tools/package_plugin.py /path/to/artifacts/aino-qgis-0.3.0.zip
```

The adjacent `.sha256` records the archive digest. After the backend release has been approved and applied, use QGIS **Plugins → Manage and Install Plugins → Install from ZIP** in the intended profile. The local validation process does not install into a user profile.

Deploy in this order: database migrations 168–170, compatible core and Temporal workers, compatible API gateway, then plugin 0.3.0. Keep the migration history and normal deployment checks for the target environment. The new plugin uses the QGIS integration routes by default, with no feature flag or legacy fallback; existing backend endpoints remain available to other clients. See [release notes](RELEASE_NOTES.md).

Save the QGIS project explicitly when you want its linked identity and imported layers to survive reopening. Hiding or closing the dock retains the active operation. Unloading the plugin stops local work and polling; it does not cancel server analysis. Reauthenticate with the same account to resume an interrupted operation. Keep the local operation journal and managed result files together with the project until recovery is no longer needed.

The plugin uses QGIS's bundled `qgis.PyQt` and Qt Network. Sign in with your Aino account. Credentials stay in memory for the current plugin session. When a token expires, sign in again to continue the pending operation. Signing into another account clears the current project association and never replays another account's pending requests.

Tested on macOS with the official QGIS versions available on 2026-11-02:

| QGIS | Qt | PyQt | Python |
| --- | --- | --- | --- |
| 3.44.15 LTR | 5.15.19 | 5.15.10 | 3.12.11 |
| 4.2.3 stable | 6.11.1 | 6.11.0 | 3.12.11 |

The declared QGIS range is 3.44.15–4.99. Native validation covers the exact macOS builds 3.44.15/Qt5 and 4.2.3/Qt6 listed above; other versions within the declared range have not been tested. The managed GeoPackage field-comment APIs require GDAL 3.7 or newer. Windows and Linux have not been tested. Use the Python interpreter bundled with each QGIS runtime; do not install another Qt binding over QGIS. The tests initialize a temporary QGIS profile and communicate only with an injected localhost HTTP fixture:

```sh
QT_QPA_PLATFORM=offscreen /path/to/qgis-python -m pytest tests/test_api_client.py -q
QT_QPA_PLATFORM=offscreen /path/to/qgis-python -m pytest -q
```

`ApiClient.send(method, path, payload, callback)` accepts API-relative paths and calls `callback(ApiResponse)` on the Qt thread. A response exposes `status`, `data`, `error`, `ok`, and `message`. HTTP errors preserve server JSON. Transport errors have status zero. Request handles expose `abort()`, `waiting_for_login`, and `pending` (callback delivery is still outstanding); abort and client `close()` suppress callbacks without sending a server cancellation. A first 401 retains the exact serialized body (including the caller's request ID) until same-account authentication, then retries once. A second 401 completes with an authentication error. Callers must retain request IDs for transport recovery beyond that one authentication retry.

Create and use `Session` and `ApiClient` on the Qt thread. `Session.account_id` is the token's unverified subject, used only to separate local account state; the server authenticates every request. Listen to `account_changed(previous, current)` to clear account-scoped references. No password, token, project association, or operation journal is persisted by this transport. Credentials are never attached to signed file URLs: absolute URLs and redirects are rejected. File transfers require a separate transport using only the upload/export URL's supplied authorization.

The **Area and layers** tab selects an optional manual area and explicit vector inputs. Use From your request, capture the current view explicitly, choose a polygon boundary layer, or pick a point with a radius in metres. Boundary selection and input checkboxes are independent. Duplicate layer names remain separate inputs. GO freezes selected inputs and exports every feature in their provider/subset/edit-buffer view; feature selection affects only the polygon boundary. Style simplifications remain visible as separate notices.

Capture APIs (QGIS main thread only):

```python
reference = RenderReference(canvas.mapSettings())
area = capture_area("extent", canvas, selected_layers)
job = SnapshotCapture(project, selected_ids, directory, reference=reference)
job.finished.connect(consume_completed_inputs)
job.failed.connect(show_error)
job.start()
```

Import these from `src.project_sync.styles`, `.area` and `.layers`. `capture_area(mode, canvas, selected_layers, center=None, radius_m=None)` returns the `QgisArea` JSON contract. Radius center coordinates use the canvas CRS; returned center and geometry use WGS84. Polygon holes and multipart geometry survive; overlapping selected polygons form a union. Transformed boundaries use 32 initial segments per source edge and adaptive midpoint refinement to 0.000001 degrees (depth limit 20, failing explicitly on nonconvergence). Radius boundaries use 720 geodesic segments. Dateline crossings become split polygons; polar caps are closed through the appropriate pole and normalized across the dateline, including a center at either pole. The supported radius domain is greater than zero and at most 10,000,000 metres; larger globe-spanning regions fail explicitly.

`RenderReference` copies actual map settings, DPI, resolution and scale. Its optional `zoom` argument is useful for a known target view; the default derives Web Mercator zoom at the map center with MapLibre's 512-pixel world convention. `convert_renderer(layer, reference.scale, reference=reference)` returns `(style, classification_metadata, warnings)`. The additional `reference` argument is required because scale alone does not determine pixel sizes or Aino's reference zoom. Simple circles match their outer diameter at that zoom; QGIS centered outlines are converted to Aino outside outlines. A single point multiplier cannot match both renderers at every zoom.

`SnapshotCapture` creates feature-source and renderer copies synchronously, then streams on the owning Qt thread in batches of at most 128 features or about 12 ms between features. No live QGIS object is sent to a worker. `progress(int)` reports feature count; `cancel()` removes incomplete/all job files while active. A single slow provider call or exceptionally large feature can exceed the cooperative time budget. The frozen QGIS edit-buffer view is verified for memory and GeoPackage; external database/file consistency follows the provider's iterator, not a promised transaction across all external sources. An unavailable source fails visibly. `capture_layers(project, selected_ids, directory, reference=reference)` is a synchronous convenience for tests/noninteractive capture, not the UI path.

Each finished layer dictionary contains these wire-ready values: `client_layer_id`, `name`, `visible`, `order`, `content_hash`, `style_hash`, `style`, `field_schema`. Local-only values are `local_path`, `schema_path`, `feature_count`, `warnings`, `provenance`. Upload the exact bytes of `local_path`, then set the server storage path as `file_path` in `QgisLayerInput`; never send a local path or local-only fields to that strict DTO. Files are closed and atomically renamed from `.partial` before handoff. All field schema is embedded in the GeoJSON foreign member `field_schema` and duplicated in the `.fields.json` sidecar. SHA-256 covers the exact uploaded bytes, including that schema. `style_hash` separately covers UTF-8 sorted-key compact JSON of the entire style dictionary. Recoloring changes only style identity when renderer membership stays the same.

Schema fields use the server vocabulary and retain exact case-sensitive names/order. `original_type` is a JSON string with `qgis_type`, `qgis_subtype`, `type_name`, `length`, `precision`; `serialization` is `json-native` or `iso8601-ms`. Native JSON structures, supported integers, nulls and Unicode are preserved; dates/times reflect current QGIS values, including its millisecond precision. Unsupported values fail explicitly. This cannot restore values already coerced by the source provider. Importers must use authoritative schema instead of relying on OGR inference for mixed JSON, all-null types or temporal-looking strings.

Categorized/graduated layers use `dataset_type=groups` and a free `aino_class[_N]` property. Its name avoids QGIS field collisions under Aino's `lower().upper()` normalization; underscores avoid nested-path aliases. Every feature gets the class ID selected by actual QGIS `symbolForFeature`, including a transparent `unrendered` class. A transparent explicit default group handles unknown classes. IDs hash renderer kind, source field, class index and typed value/range bounds, independently of colors and labels. Original typed category/range definitions, labels, enabled flags, captured reference context and simplification warnings are in `style.qgis.classification` / `style.qgis.warnings`. The extra schema field has `classification=true` and `original_type=aino-renderer-class-v1`. Original source fields are retained unchanged.

`OsmParserDialogMain.prepare_inputs(directory=None)` starts the cooperative path and emits `inputs_prepared(area, layers)` only after all files are complete. It cancels/discards on source, selection, style, view, account or project changes and on close/unload. Preview files belong to that UI preparation and survive only until its next invalidation. The synchronization controller must move completed files into its own operation staging directory before accepting the handoff for upload; then it owns their cleanup. Workers receive only completed files and JSON. Keep a separate local baseline fingerprint after result import: the server's protected `content_hash` identifies an immutable server version and is not the raw export/download checksum or local reserialized snapshot hash.

The local integration fixture renders all six geometry families in native QGIS and the unchanged Aino Default/Groups components using signed MVT from actual PostGIS ingestion. It uses matching 800×600 views, EPSG:3857, center 0/0, zoom 5, and DPR 1. Colors and classifications agree; raster edge coverage differs by up to two pixels. This is a controlled local comparison with the installed frontend dependency versions, not a claim of identical pixels or production dependency parity. See RELEASE_NOTES.md for validation limits.

GO owns capture, link, upload, snapshot readiness and analysis start as one durable request. It works with no input layers and no manual area. An empty snapshot is still bound to the request. Style simplifications are notices; there is no separate prepare/sync/start sequence. The lower-level capture and synchronization APIs below remain available for tests and explicit programmatic callers.

`SyncController(api_client, journal=None, uploader=None, parent=None)` exposes `prepare(project, layers, on_ready, context=None)`, `restore(project)`, `retry_link(project)`, `detach(project)`, `pause()` and `close()`. `ready(dict)` / `on_ready(snapshot)` only deliver a server-ready snapshot with `id` for analysis submission; `failed(str)`, `notice(str)`, `status(str)` and `relink_required(str)` are UI signals. MainTab forwards `snapshot_ready(dict)`. `context` is frozen JSON for the caller; MainTab currently persists the exact captured `area`. The analysis controller binds its prompt/start intent to this frozen context and snapshot. `restore` replays/polls immediately; it delivers readiness through the signal. An unresolved synchronization must be resumed before accepting a new capture. `pause`/close/unload stop local requests only.

Project custom property `aino/client_project_id` contains a UUID. Save As keeps it; **Detach and create a new link** replaces it explicitly, leaving the old journal intact. The plugin never saves the QGIS project automatically. A new or detached association UUID needs an explicit save even when the QGIS project already has a filename. The recovery notice stays until the current UUID is verified in the saved local QGS/QGZ metadata; it clears after a successful Save/Save As once archive writing has finished. Unrelated unsaved edits do not invalidate an already persisted UUID. Unreadable or nonlocal project metadata remains unverified. Save the project yourself to recover this UUID after process loss. Journals live under Qt AppDataLocation `aino/recovery/<SHA256(account_id)>/<client_project_id>/journal.json`, with atomic file replacement, file/directory fsync, and a version-1 envelope. Invalid journals fail visibly. Account IDs only scope local recovery; server authorization remains authoritative. 403/404 stop preparation and expose retry-existing-link and explicit detach actions. Switching account cancels local delivery and cannot read or replay the previous account's journal.

`RecoveryJournal.save(client_project_id, account_id, state)` replaces state; `load(...)` returns state or None. Controllers should instead use `update_sections(..., **sections)` on the Qt thread. Top-level section ownership is **sync** and **datasets** for synchronization, **analysis** for analysis, **imports** for result recovery; each update reloads and preserves other sections. `sync` owns operation_id, phase, frozen context, binding, link_intent, captured layers, manifest, snapshot_intent and acknowledged snapshot. `datasets` caches server-protected data identities for unchanged input reuse. A mutating API intent has request_id before dispatch; snapshot_intent freezes the exact complete body, including uploaded file paths. Upload mint/TUS attempt IDs are local journal records only, since these endpoints do not implement request-id deduplication. No credentials, upload endpoint, or opaque Location is persisted.

Normal UI capture uses app-data `previews/`, on the same filesystem as operation staging. Acceptance renames the completed files into the operation's persistent `staging/<operation_id>/` before returning; later UI invalidation only removes its former preview paths. Caller-supplied capture directories must be on that filesystem; cross-filesystem rename fails visibly without a large blocking copy. Staged files are verified off the Qt thread against the exact capture SHA-256 before transfer. Capture hashing remains independent of style hashing. Staging is retained for recovery; automatic cache eviction/orphan-storage reclamation is not provided by this client.

`UploadTransport.start(path, content_hash, issued, callback, before_request=None)` uses scoped `upload_token` only, validates the API-issued storage origin (HTTPS, or loopback HTTP for tests), refuses redirects and cross-origin/userinfo/fragment Locations, and never uses the API Bearer. Binary file verification/chunk reads run in file workers; QGIS objects and network callbacks remain on the Qt thread. `before_request(method, offset, length)` durably records a noncredential transport intent before dispatch. A lost PATCH is reconciled by HEAD while the in-memory token is valid, with validated offsets and bounded attempts. Expired tokens, lost create responses, or process loss require a fresh mint/path for unfinished upload. A durably completed file_path survives token loss. A lost snapshot response always replays its frozen request_id/body before considering other files. Temporary storage objects may be orphaned; only logical snapshot/dataset registration is idempotent.

Task11 imported-layer provenance contract: set JSON custom property **aino/output_provenance** on each imported QGIS layer after its local post-import baseline capture. Required keys are `account_id`, `client_project_id`, `project_id`, authoritative `dataset_id`, `version`, `content_hash`, `field_schema`, plus stable `part_id`, complete `part_ids`, `local_content_hash`, `local_style_hash`, `server_style`, `server_style_hash`. The local content hash is the Task8 exact capture-byte fingerprint, never the downloaded export checksum or the authoritative server content_hash. The style baseline is the corresponding Task8 capture style hash. Compute server_style_hash from canonical sorted-key compact JSON of the authoritative result style envelope. Preserve part identity separately from QGIS provider IDs/display names. A complete unchanged group references `output:<dataset_id>`; an incomplete selection, changed data or unrepresentable part presentation uploads only the selected captures. Multi-part reuse requires identical visibility/name, contiguous ordering, and unchanged per-part styles; otherwise it preserves each part separately. Single default-layer color changes reuse data identity. Unchanged imported presentation sends the authoritative server style, avoiding regenerated synthetic classification fields.

For a changed categorized presentation, Task11 may provide optional `class_mapping`: `{dataset_id, version, content_hash, local_field, server_field, classes}` tied to the same protected identity. `server_field` must exist in authoritative field_schema. `classes` maps every captured synthetic class ID (including any unrendered class used in the style) to the exact list of original server values represented by that class. Establish this mapping from original attributes and the post-import classification results, never by comparing labels/colors. Empty lists are allowed for classes with no represented source values. The synchronizer rewrites only style columns/values through this mapping, retaining server provenance with current capture metadata under `qgis.local_presentation`; it hashes the resulting envelope independently. Missing/incompatible mapping conservatively uploads captured data. Selection completeness and data-baseline checks still apply.

QGIS input and result `style` are explicit envelopes `{dataset_type, style, qgis}`. Core projects store separate dataset_type plus flat appearance (`groups: []` for plain styles) and qgis metadata, while immutable snapshots retain their original envelope/hash. Result envelopes preserve current default/groups mode; incompatible categories and unsupported gradient/heatmap modes explicitly fall back to base/default with warnings, including simplified point sizing. Task11 must honor those warnings and the envelope discriminator, not infer a renderer from stale groups/provenance. Nonempty pre-release cached results with untyped flat style are explicitly rejected; text-only results remain valid. This does not restart analysis or reconstruct historical appearance from current dataset presentation.

### Task 10 analysis lifecycle and Task 11 result handoff

GO creates one durable request intent, captures optional inputs and starts analysis after its snapshot is ready. Restoring that intent continues the same request; unrelated legacy snapshot readiness never creates a new analysis. The Analysis tab separates preparation, server steps, completeness and import status. Hiding the dock retains the controllers and active operation; unloading pauses local requests only. Cancel analysis is an explicit server action with confirmation.

`AnalysisController(api, journal=None, project=None, parent=None, scheduler=None)` owns
only the journal's `analysis` section through `update_sections`. Its public entry
points are `start(project_id, snapshot_id, prompt, area)`, `restore(task_id=None)`,
`submit_action(action)`, `cancel()`, `bind(project)`, `pause()` and `close()`.
`restore()` discovers the current account/project journal without creating an identity
for an untouched project; an explicit `task_id` can attach a known task when none is
already attached. `bind` clears local scope without altering the previous journal.

The section stores `start_intent` (the complete immutable request body and UUID),
`task_id`, `project_url`, `latest` (compact DTO), optional `action_intent` (complete
flat action body/UUID), and optional `results` (manifest and full report). Mutating
requests are persisted before sending; uncertain start/action responses retain their
exact request IDs and payloads. Resume retries these intents, never retargets an old
answer. A typed qgis_action_conflict discards only that rejected action intent and refreshes server state.
Other failures remain recoverable and do not create a failed analysis state.
A second analysis requires the previous server task to be terminal and no uncertain
action receipt. Revisions older than the displayed state are ignored.

`PollingController.watch(task_id)` immediately reads state, then schedules the next
request four seconds after delivery. It retains one pending request through login
recovery, backs off 4/8/16/32/60 seconds on failures, resets on success, continues
while awaiting input/confirmation, and stops at a terminal state. `stop()` aborts only
local delivery. Controller scope guards suppress callbacks after project/account
changes; reopening and same-account login resume the saved operation.

Question controls preserve item and option IDs, complete wording, descriptions,
single/multiple choices and permitted free text. Ordinary plan approval submits an
empty payload; screening Run/Edit submits actual question answers, with text required
for Edit. Source consent confirms all server tags, displaying persisted per-unit
`source_info` rates and server expiry. Missing rates are unavailable, never free;
these rates are informational, with no quantity, total estimate or price-lock promise.
A credits pause without `pending_action` has no synthetic consent button. Contact
options open the same `mailto:hello@aino.world` destination as Aino on user selection.
Frozen-area server question tools reject/regenerate drawing options; an old malformed
round is visibly unsupported in the plugin, never submitted as a fake drawing.

`AnalysisController.results_ready(dict)` and main tab `analysis_results_ready(dict)`
are Task 11's handoff. The manifest is already durably saved in `analysis.results`,
and is re-emitted on restore. Task 11 must make repeated deliveries idempotent using
its own `imports` journal section; it must not overwrite `analysis`, `sync` or
`datasets`. Current scope is available as controller `account`, `identity`, and
`state.start_intent.project_id`; the manifest contains `task_id` and authoritative
layer identities/versions/styles/schema. No geometry is embedded in polling and no
layer import/download is implemented by Task 10. Nonempty manifests show
“Download: results ready for layer import”; Task 11 owns actual download/import
progress. Empty manifests still fetch and display the full result report.

Reports are displayed as escaped/plain text, including the actual structured screening
narrative, criteria, candidates, risks, methodology and parcel-data limitations.
Recognized standalone widget markers are omitted from text display; rich web widgets
remain accessible through Open in Aino. The full server report is retained in the
journal regardless of display shape. Unknown reports fall back to an explicitly
labelled preview/link, without dumping arbitrary JSON or inventing findings.


#### Definitive rejection and uncertain delivery

The analysis journal now also records `start_dispatched` / `action_dispatched`.
New intents initially have a false marker; the controller durably sets it true before
HTTP. A restored true marker or a missing marker in an older journal means prior
delivery may have occurred. Such an intent retains its exact UUID/body across retries.

The QGIS start/action routes identify body validation before RPC dispatch with HTTP
400 and `{result: "bad_request", domain: "api_gateway", error_code:
"qgis_request_validation"}`. The controller may release that intent only when this
was its first observed dispatch. The same response after a lost reply, restart or
legacy journal cannot rule out an earlier acceptance, so it does not release the
intent. No decision depends on error-message prose or HTTP status alone.

For actions, the exact HTTP 403 `forbidden` / `billing` / `insufficient_credits` tuple
is definitive because core raises it after checking the locked action receipt.
The HTTP 409 `conflict` / `core` / `qgis_action_conflict` tuple also follows receipt
lookup. Arbitrary 400/403/409 responses, transport failures and wrong-domain codes
remain uncertain. Error text is only for display, with any appended debug traceback
excluded from the displayed/persisted rejection message.

A known rejection is atomically stored as `analysis.last_rejection` with operation,
original request, status/domain/code and readable message before its active intent is
removed locally. Rejected starts can be corrected with GO and a new UUID. Rejected
actions immediately resume polling and enable a corrected action or explicit Cancel;
cancellation remains available after restarting the plugin. The notice
survives dialog/controller reconstruction. A new explicit intent replaces the notice;
continuing polling never retargets the rejected answer.

### Persistent analysis results

Completed analysis manifests now start download and import automatically. The Analysis tab retains the full report and Aino link while showing download/import progress. **Retry saved result import** reuses the saved manifest; it does not start analysis or submit another paid operation. Text-only results complete without a download or a valid map canvas.

`ResultController(api, journal, project)` owns only the journal `imports` section. `start(manifest, project_id, reference)` scopes work to the signed-in account, current client-project UUID, server project and task; `pause()`/`close()` stop local delivery without cancelling analysis. Repeated result callbacks are intentional and safe. Each task records its manifest fingerprint, permanent storage descriptors and imported layer IDs. Completion is saved after adding layers; recovery also scans `aino/output_provenance` properties, so a crash after layer addition does not add duplicates. Existing local edits and styles stay intact. New versions of an already imported dataset receive a separate `(vN)` layer. Saving the QGIS project remains an explicit user action.

`ResultDownloader.download_results(task_id, manifest, target_dir, callback)` and the module convenience function of the same name (with `api=`) use authenticated `GET /project/dataset/export?dataset_id=...&expected_version=...`. Signed transfers use a dedicated Qt network manager without the API Bearer. Invalid/expired links can refresh once, always pinned to the manifest version; a version conflict stops. Export is the existing entitlement-gated, unmetered GET and has no invented request-ID or charging contract. Redirects, failed status, truncated bodies, size disagreement, invalid JSON and unsupported GeoJSON fail explicitly. Downloads stream to a temporary file, then worker-owned validation and atomic replacement put readable bytes in an account/project-scoped permanent directory. A failed replacement keeps a prior valid file. Cleanup is best effort: close/unlink errors cannot replace the original failure or suppress its single callback; an undeletable partial file is never accepted as a result. Server `content_hash` is never compared with wire SHA256.

Persistent layers use **native-GDAL-created GeoPackage**, with an explicit primitive schema, internal `_aino_rowid`, optional Z/M dimensions and physical fields `f_000`, `f_001`, etc. Original names are visible as aliases; exact case-distinct names, ordering and logical types live in versioned `aino/managed_output_v1` metadata and an adjacent `metadata.json`. JSON/heterogeneous/large-integer/classification values use explicit reversible JSON text; date/time/datetime use exact ISO text, preserving submilliseconds. JSON fields have a multiline editor and `(JSON)` alias. Native faithful primitives remain editable. No codec is inferred from a user string prefix. Ordinary added, renamed and deleted attributes are captured with the current logical schema and become new data snapshots when that schema/data changes. Original column comments carry a versioned managed-field marker validated against the immutable mapping, so native renames preserve JSON/temporal codecs through undo, rollback, commit and reopen. Added fields use normal QGIS snapshot serialization; deleted fields are omitted. Physical renames change the logical snapshot name, while aliases remain independently editable display labels. Malformed edited codecs, invalid/duplicate markers and ambiguous logical names fail explicitly instead of coercing or dropping values. Ordinary user source layers do not use these codecs.

The storage decision replaced the initial FlatGeobuf proposal after native tests demonstrated silent row loss when an XY Point was added to a PointZ file and EMPTY geometries were changed into NULL. GeoPackage preserves those cases and all six supported geometry types in XY/XYZ/XYZM. Mixed outputs split by exact geometry type/dimensionality; all parts retain the common dataset name, contiguous import order and stable `part_id`/`part_ids`. Original feature IDs (including explicit null), missing properties, null properties, empty geometries and genuine NULL geometries remain distinct. A managed-layer-only edit-buffer hook persists explicit missing-field→NULL edits as metadata attributes in the same commit; undo/redo/rollback follow the native edit buffer. This hook is attached to restored managed layers and does not modify ordinary inputs.

`import_result(project, result, local_path, reference=...)` returns QGIS layer IDs; its `result` additionally carries account/client-project/server-project/task scope. Without a supplied reference it uses the current QGIS canvas. The synchronous helper is for noninteractive callers. Interactive work uses worker-owned raw parsing/GDAL creation, then a cooperative Qt-thread `ImportJob` for QGIS renderer/capture/provenance operations. Scope changes, project clearing and unload suppress stale callbacks. Storage workers finish independently after cancellation and never operate on project/layer UI objects.

Each imported layer gets `aino/task_id`, `aino/dataset_id`, `aino/version` and full `aino/output_provenance` **before** project insertion. Provenance includes exact server identity/schema/style, `part_id`/`part_ids`, independent local content/style baselines and a proven synthetic-class→server-value `class_mapping` when supported. An atomic `output.provenance.json` preserves the original baseline even if edited backing data is later reimported after removing a layer. Managed recapture restores logical names/types/values, excludes storage fields, reserves original names before adding a synthetic classification field, and never replaces server identity with a local fingerprint. Complete unchanged split selections can reuse one protected `output:<dataset_id>`; partial or edited selections remain local snapshots and never expand to unselected siblings.

Renderer restoration honors the authoritative `dataset_type` envelope. Plain styles and supported categories/ranges restore fill/stroke/opacity/width/point size using the current render reference. Graduated metadata is checked against actual authoritative class values; stale range membership is shown as categories with a warning. Server fallback warnings remain visible. Legacy nonempty flat style envelopes fail clearly. Original source styles are never modified.

Current limits: internal storage metadata/FID fields must remain available. Original unmarked pre-release files remain readable when their field mapping is unambiguous; a renamed unmarked codec is rejected explicitly because its lineage cannot safely be inferred. No automatic migration or schema journal is used. Geometry edits should retain their layer's geometry family. Cross-family provider mutations are not promised lossless. GeoJSON empty coordinate arrays carry no Z/M tags, so no absent dimensional metadata is invented. Storage parses each complete GeoJSON in a worker (memory scales with that output), and permanent raw files/backing files are retained for recovery without automatic eviction. Native validation covers macOS QGIS 4.2.3/Qt6 and 3.44.15/Qt5. The local integration harness exercises explicit process kills at durable gateway, worker, and saved-project boundaries; this does not establish power-loss guarantees or arbitrary provider behavior. Windows and Linux remain unverified.

## Request workspace (0.3.0)

Open the Aino dock, enter a request such as “Parks in Berlin”, and press GO. An empty QGIS project is supported. Vector inputs are optional and must be selected explicitly. From your request resolves the area on the server; ambiguous places appear as questions in the same analysis. Area and layers also provides current view, selected polygons, and point/radius selection with a visible map preview.

The Analysis tab separates progress, questions, result completeness, and import status. Reference and unverified layers use separate Aino groups. Import retry reuses the existing analysis. Save the QGIS project to retain its Aino link and recovery journal.

A `0.3.0-test` archive is built from a committed revision in a separate staging directory, with its API URL set to `https://test-api.aino.world`. The source default remains `https://api.aino.world`. The test archive requires matching deployed core/gateway contracts; local verification does not deploy them.
