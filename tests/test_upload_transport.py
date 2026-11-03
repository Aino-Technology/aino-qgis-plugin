import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from src.project_sync.upload_transport import UploadTransport


def issued(origin, **overrides):
    return {
        "upload_url": origin + "/storage/v1/upload/resumable",
        "protocol": "tus",
        "upload_token": "scoped-storage-test-token",
        "file_path": "17/new.geojson",
        "bucket": "heatmap-dataset",
        "max_size_bytes": 10000,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        **overrides,
    }


@pytest.fixture
def transfer(tmp_path, qgis_app):
    path = tmp_path / "data.geojson"
    path.write_bytes(b"abcdefghij")
    uploader = UploadTransport(chunk_size=4)
    outcomes = []
    yield uploader, path, outcomes
    uploader.abort()
    uploader.deleteLater()
    qgis_app.processEvents()


def begin(transfer, local_api, wait_for, **overrides):
    uploader, path, outcomes = transfer
    uploader.start(
        str(path),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        issued(local_api.origin, **overrides),
        outcomes.append,
    )
    wait_for(lambda: bool(local_api.requests) or bool(outcomes))


def test_tus_lost_patch_head_reconciles_exact_bytes(transfer, local_api, wait_for):
    begin(transfer, local_api, wait_for)
    _, _, outcomes = transfer
    create = local_api.requests[0]
    assert create["method"] == "POST"
    assert create["headers"]["Authorization"] == "Bearer scoped-storage-test-token"
    assert create["headers"]["Upload-Length"] == "10"
    assert "objectName MTcvbmV3Lmdlb2pzb24=" in create["headers"]["Upload-Metadata"]
    local_api.respond(0, 201, headers={"Location": "/session/opaque-secret"}, raw=b"")
    wait_for(lambda: len(local_api.requests) == 2)
    assert local_api.requests[1]["body"] == b"abcd"
    local_api.requests[1]["socket"].abort()
    wait_for(lambda: len(local_api.requests) == 3)
    assert local_api.requests[2]["method"] == "HEAD"
    local_api.respond(
        2, 200, headers={"Upload-Offset": "4", "Upload-Length": "10"}, raw=b""
    )
    wait_for(lambda: len(local_api.requests) == 4)
    assert local_api.requests[3]["body"] == b"efgh"
    local_api.respond(3, 204, headers={"Upload-Offset": "8"}, raw=b"")
    wait_for(lambda: len(local_api.requests) == 5)
    assert local_api.requests[4]["body"] == b"ij"
    local_api.requests[4]["socket"].abort()
    wait_for(lambda: len(local_api.requests) == 6)
    local_api.respond(5, 200, headers={"Upload-Offset": "10"}, raw=b"")
    wait_for(lambda: bool(outcomes))
    assert outcomes == [""]
    assert all(
        item["headers"]["Authorization"] == "Bearer scoped-storage-test-token"
        for item in local_api.requests
    )


@pytest.mark.parametrize(
    "location",
    [
        "https://other.invalid/session",
        "//other.invalid/session",
        "http://user:pass@127.0.0.1/session",
        "/session#secret",
    ],
)
def test_unsafe_location_refused_without_credential_forwarding(
    transfer, local_api, wait_for, location
):
    begin(transfer, local_api, wait_for)
    local_api.respond(0, 201, headers={"Location": location}, raw=b"")
    wait_for(lambda: bool(transfer[2]))
    assert transfer[2][0]
    assert len(local_api.requests) == 1


@pytest.mark.parametrize("offset", ["", "-1", "5", "1000", "NaN", "1.0"])
def test_invalid_patch_offset_refused(transfer, local_api, wait_for, offset):
    begin(transfer, local_api, wait_for)
    local_api.respond(0, 201, headers={"Location": "/session"}, raw=b"")
    wait_for(lambda: len(local_api.requests) == 2)
    local_api.respond(1, 204, headers={"Upload-Offset": offset}, raw=b"")
    wait_for(lambda: bool(transfer[2]))
    assert "offset" in transfer[2][0]


def test_expired_token_and_hash_mismatch_send_nothing(transfer, local_api, wait_for):
    begin(transfer, local_api, wait_for, expires_at="2000-01-01T00:00:00Z")
    assert "expired" in transfer[2][0]
    assert not local_api.requests
    uploader, path, outcomes = transfer
    outcomes.clear()
    uploader.start(str(path), "0" * 64, issued(local_api.origin), outcomes.append)
    wait_for(lambda: bool(outcomes))
    assert "changed" in outcomes[0]
    assert not local_api.requests


def test_redirect_refused_and_abort_suppresses_callback(transfer, local_api, wait_for):
    begin(transfer, local_api, wait_for)
    local_api.respond(0, 307, headers={"Location": "https://other.invalid"}, raw=b"")
    wait_for(lambda: bool(transfer[2]))
    assert "redirect" in transfer[2][0]
    uploader, path, outcomes = transfer
    outcomes.clear()
    uploader.start(
        str(path),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        issued(local_api.origin),
        outcomes.append,
    )
    wait_for(lambda: len(local_api.requests) == 2)
    uploader.abort()
    local_api.respond(1, 201, headers={"Location": "/session"}, raw=b"")
    assert not outcomes


class ScopedTus:
    def __init__(self, api):
        import base64
        import hmac
        import json

        self.api = api
        self.objects = {}
        self.sessions = {}
        self.drop_create = False
        self.drop_patch = False
        self.violations = []
        self.clock = datetime.now(timezone.utc).timestamp()
        original = api.read

        def read(connection, buffer):
            count = len(api.requests)
            original(connection, buffer)
            if len(api.requests) == count:
                return
            request = api.requests[-1]
            index = len(api.requests) - 1
            try:
                credential = request["headers"]["Authorization"].removeprefix("Bearer ")
                head, body, signature = credential.split(".")
                expected = (
                    base64.urlsafe_b64encode(
                        hmac.digest(
                            b"local-fixture-key", (head + "." + body).encode(), "sha256"
                        )
                    )
                    .rstrip(b"=")
                    .decode()
                )
                assert hmac.compare_digest(expected, signature)
                claims = json.loads(
                    base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
                )
                assert claims["exp"] > self.clock
                path = claims["upload_path"]
                if request["method"] == "POST":
                    metadata = dict(
                        value.split(" ", 1)
                        for value in request["headers"]["Upload-Metadata"].split(",")
                    )
                    assert base64.b64decode(metadata["objectName"]).decode() == path
                    assert (
                        base64.b64decode(metadata["bucketName"]).decode()
                        == "heatmap-dataset"
                    )
                    location = f"/session/{len(self.sessions)}"
                    self.sessions[location] = path
                    self.objects[path] = bytearray()
                    if self.drop_create:
                        self.drop_create = False
                        connection.abort()
                    else:
                        api.respond(index, 201, headers={"Location": location}, raw=b"")
                else:
                    assert self.sessions[request["path"]] == path
                    data = self.objects[path]
                    if request["method"] == "PATCH":
                        assert int(request["headers"]["Upload-Offset"]) == len(data)
                        data.extend(request["body"])
                        if self.drop_patch:
                            self.drop_patch = False
                            connection.abort()
                            return
                    api.respond(
                        index,
                        204 if request["method"] == "PATCH" else 200,
                        headers={"Upload-Offset": str(len(data))},
                        raw=b"",
                    )
            except (AssertionError, KeyError, ValueError) as error:
                self.violations.append(str(error))
                api.respond(index, 403, raw=b"")

        api.read = read

    def authorization(self, path):
        import base64
        import hmac
        import json

        def encode(value):
            return (
                base64.urlsafe_b64encode(json.dumps(value).encode())
                .rstrip(b"=")
                .decode()
            )

        head = encode({"alg": "HS256", "typ": "JWT"})
        body = encode(
            {"sub": "local-test", "upload_path": path, "exp": self.clock + 900}
        )
        signature = (
            base64.urlsafe_b64encode(
                hmac.digest(b"local-fixture-key", f"{head}.{body}".encode(), "sha256")
            )
            .rstrip(b"=")
            .decode()
        )
        return issued(
            self.api.origin, file_path=path, upload_token=f"{head}.{body}.{signature}"
        )


def test_scoped_jwt_tus_persistent_offsets_and_lost_create_restart(
    transfer, local_api, wait_for
):
    fixture = ScopedTus(local_api)
    uploader, path, outcomes = transfer
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    fixture.drop_create = True
    uploader.start(
        str(path), digest, fixture.authorization("17/orphan.geojson"), outcomes.append
    )
    wait_for(lambda: bool(outcomes))
    assert outcomes[0]
    outcomes.clear()
    fixture.drop_patch = True
    uploader.start(
        str(path),
        digest,
        fixture.authorization("17/restarted.geojson"),
        outcomes.append,
    )
    wait_for(lambda: bool(outcomes))
    assert outcomes == [""]
    assert bytes(fixture.objects["17/restarted.geojson"]) == path.read_bytes()
    assert bytes(fixture.objects["17/orphan.geojson"]) == b""
    assert not fixture.violations
    assert any(request["method"] == "HEAD" for request in local_api.requests)


def test_storage_expiry_does_not_substitute_api_credentials(
    transfer, local_api, wait_for
):
    fixture = ScopedTus(local_api)
    uploader, path, outcomes = transfer
    authorization = fixture.authorization("17/expired.geojson")
    fixture.clock += 901
    uploader.start(
        str(path),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        authorization,
        outcomes.append,
    )
    wait_for(lambda: bool(outcomes))
    assert "403" in outcomes[0]
    assert len(local_api.requests) == 1
    assert fixture.violations


def test_failed_durable_intent_prevents_storage_mutation(transfer, local_api, wait_for):
    uploader, path, outcomes = transfer

    def cannot_save(*args):
        raise OSError("Disk unavailable")

    uploader.start(
        str(path),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        issued(local_api.origin),
        outcomes.append,
        before_request=cannot_save,
    )
    wait_for(lambda: bool(outcomes))
    assert outcomes == ["Disk unavailable"]
    assert not local_api.requests
