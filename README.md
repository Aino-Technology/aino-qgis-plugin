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
