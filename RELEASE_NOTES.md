# Aino QGIS 0.3.0 — request workspace

GO now starts from an empty project, using the request to resolve its area and asking location questions within the same task when needed. Selected vector inputs and manual boundaries are frozen at submission. The persistent dock has request, area/layers, and analysis tabs, native map tools, CRS-aware previews, accessible question forms, readable progress, and independent completeness/import messages.

Results distinguish requested outputs, reference layers, and unverified legacy layers. Recovery retains request/action identities, immutable snapshots, and saved layer imports. The behavior is enabled by default. Compatibility remains QGIS 3.44.15–4.99, with GDAL 3.7+.

The staging-only test ZIP uses `https://test-api.aino.world`; production source defaults remain unchanged. See the accompanying verification report and SHA receipt for exact native runtimes, committed sources, executed local scenarios, and remaining limits. Backend deployment, live paid-provider acceptance, and Windows/Linux acceptance are separate steps.

Local verification: complete native suites passed 423 tests per runtime with two evidence-dependent skips; a final focused run passed 66 tests per runtime, including both completed evidence tests and the capacity-notice regression, with no skips. Real local gateway/RPC/Temporal/PostGIS/native-QGIS scenarios cover empty GO, same-task ambiguity, manual screening, unavailable source plus H3, partial output, capacity guidance and process recovery. Core QGIS-focused tests passed 276, the final pending-ingestion regression file passed 32, and gateway tests passed 115. Full Core collection remains blocked by two unrelated existing imports; this is not a full Core pytest pass.

# Aino QGIS 0.2.0 — local release candidate

The plugin links a QGIS project to an Aino project, uploads immutable file-based vector snapshots, starts durable multistep analysis, presents questions and confirmations, polls compact status, and imports final layers into editable managed GeoPackages. Repeated prompts reuse the project. Changed inputs create new versions; unchanged complete previous outputs can be referenced without reuploading. Text-only results retain their full report.

The new flow is enabled by default. It preserves ordinary local source layers and renderers. Users review style simplifications before upload. Raster/WMS sources are not exported as vectors. Signed exports are pinned to a dataset version; the current server policy does not expire these links or add an export meter charge. Retry refreshes only export delivery, not analysis.

Release order: migrations 168–170 → core and workers → gateway → plugin. Do not publish the plugin ahead of compatible backend handlers. Rollback requires coordinated service/schema compatibility assessment and retaining journals/result storage; this release does not add a switch to the old flow. Publication, deployment, and installation in a user profile are separate actions.

Validation: complete native unit suites on macOS QGIS 4.2.3/Qt6 and 3.44.15/Qt5 (233 passed each; artifact-consumption test separately enabled by the integration evidence path); 102 gateway route/contract/export/schema tests; 209 core QGIS and affected SQL tests. The core SQL-to-GeoJSON converter now retains explicit null property keys. All-null properties may still lack generated typed SQL columns under existing inference; nullable scalar extraction from raw PROPERTIES is supported. This does not claim automatic JSONB decoding of arbitrary SQL result values.

Actual browser/native comparison uses unchanged Aino Default/Groups, real signed MVT, and actual available_properties metadata. Fixture view: center 0/0, EPSG:3857, zoom5, 800×600, DPR1. Colors agree to one RGB unit in sampled interiors; raster edges can differ by up to two pixels. Installed frontend dependencies differ from declared versions, so production dependency parity remains unverified.

The local storage/auth/planner/metadata-description adapters are deterministic fixtures. The HTTP/RabbitMQ/core transaction/reconciler/Temporal/PostGIS/QGIS paths and Go tile/export handlers are real. A 30,000-feature, 10.54 MB input was captured cooperatively below the fixture's actual 20 MB plan limit; this is not an unlimited-file or constant-memory promise. Result parsing retains a complete GeoJSON in a worker, so memory scales with the file. External paid-provider/source acceptance and Windows/Linux smoke remain outstanding and require their own authorized environment. No live paid request, publication, or deployment was performed.

## Final review corrections

Structured screening reports, including candidate locations, now persist before result notification and pass through reconstructed analysis controllers to result import. Only the schema-owned `analysis.results.report` subtree is report data; session/transport credentials, API keys, upload tokens and opaque upload locations remain rejected elsewhere in the journal. Sync and import sections retain their independent ownership.

The matching core fixes preserve archived input tables during both snapshot and ordinary web metadata synchronization and apply the supported presentation of explicitly selected protected outputs without changing their data identity or historical manifests. Omitted saved outputs remain unchanged.

The final plugin source passed 267 native tests on each macOS stable/LTR runtime; one artifact-consumption integration test is intentionally skipped in each ordinary unit run. The earlier fullchain and manual appearance evidence remain bounded baseline evidence. Reusable browser receipts are render smoke and do not assert color/classification agreement. Real-provider/source acceptance, Windows/Linux, production frontend dependency parity and full historical migration rehearsal remain open.


Rebased on upstream main `174d3c8` (2026-11-03): retains the redesigned login/main UI, SVG welcome banner, direct login and signup, username text replacements, visible account email and sign-out, and resource/deploy fixes. These controls use the asynchronous session and multistep controllers; sign-out stops local callbacks and keeps account-scoped recovery files. Analysis quotas are informational; submission/confirmation remains server-authoritative. The legacy single-parser and inline bulk-upload flows remain replaced. Native regression coverage includes login/signup ownership, synthetic text replacements, limits/response shapes, account switching and preserved recovery journals. QGIS metadata now declares 3.44.15–4.99 by explicit user choice; exact native validation remains macOS 3.44.15/Qt5 and 4.2.3/Qt6, with GDAL 3.7+ required by managed field-comment APIs.


Upstream login-error presentation is retained: styled plain-text Warning/Critical dialogs with OK acknowledgement, shown asynchronously and owned by the login window. Closing login or unloading the plugin closes the popup and pending callbacks; repeated errors reuse one popup. Long server errors remain complete in the selectable Details pane. The former inline-label substitution was corrected after the scoped upstream-preservation review.
