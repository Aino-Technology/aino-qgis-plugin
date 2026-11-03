# Aino for QGIS

The plugin uses QGIS's bundled `qgis.PyQt` and Qt Network. Sign in with your Aino account. Credentials stay in memory for the current plugin session. When a token expires, sign in again to continue the pending operation. Signing into another account clears the current project association and never replays another account's pending requests.

Tested on macOS with the official QGIS versions available on 2026-11-02:

| QGIS | Qt | PyQt | Python |
| --- | --- | --- | --- |
| 3.44.15 LTR | 5.15.19 | 5.15.10 | 3.12.11 |
| 4.2.3 stable | 6.11.1 | 6.11.0 | 3.12.11 |

The declared QGIS range is 3.44.15–4.2.3, bounded by the versions tested here. Windows and Linux have not been tested. Use the Python interpreter bundled with each QGIS runtime; do not install another Qt binding over QGIS. The tests initialize a temporary QGIS profile and communicate only with an injected localhost HTTP fixture:

```sh
QT_QPA_PLATFORM=offscreen /path/to/qgis-python -m pytest tests/test_api_client.py -q
QT_QPA_PLATFORM=offscreen /path/to/qgis-python -m pytest -q
```

`ApiClient.send(method, path, payload, callback)` accepts API-relative paths and calls `callback(ApiResponse)` on the Qt thread. A response exposes `status`, `data`, `error`, `ok`, and `message`. HTTP errors preserve server JSON. Transport errors have status zero. Request handles expose `abort()`, `waiting_for_login`, and `pending` (callback delivery is still outstanding); abort and client `close()` suppress callbacks without sending a server cancellation. A first 401 retains the exact serialized body (including the caller's request ID) until same-account authentication, then retries once. A second 401 completes with an authentication error. Callers must retain request IDs for transport recovery beyond that one authentication retry.

Create and use `Session` and `ApiClient` on the Qt thread. `Session.account_id` is the token's unverified subject, used only to separate local account state; the server authenticates every request. Listen to `account_changed(previous, current)` to clear account-scoped references. No password, token, project association, or operation journal is persisted by this transport. Credentials are never attached to signed file URLs: absolute URLs and redirects are rejected. File transfers require a separate transport using only the upload/export URL's supplied authorization.

The **Area and layers** tab prepares local analysis inputs. Choose the current map extent, selected polygon features in checked layers, or a point picked on the map with a radius in metres. Check source layers by their QGIS IDs (shown in tooltips); duplicate names remain separate inputs. Preparing inputs exports every feature in the provider/subset/edit-buffer view, regardless of feature selection. Selection determines only the polygon area. Unsupported style details are listed before the prepared-input signal is delivered.

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

Task8 tests include native QGIS image measurements, not a completed Aino browser comparison. The actual Aino Default/Groups component fixture and dependency-parity visual comparison remain part of the end-to-end Task12 gate.
