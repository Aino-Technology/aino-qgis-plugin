import io
import plistlib
import subprocess
from types import SimpleNamespace
import zipfile
from uuid import uuid4

import pytest
from qgis.core import QgsProject

from src.login import login_tab
from src.login.login_tab import LoginDialog
from src.project_sync.recovery import _stored_identity, _xml_identity


load_replacements = LoginDialog.load_macos_text_replacements


def test_macos_replacements_read_plist_without_process(tmp_path, monkeypatch):
    path = tmp_path / ".GlobalPreferences.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "NSUserDictionaryReplacementItems": [
                    {"on": True, "replace": "@@", "with": "person@example.invalid"},
                    {"on": False, "replace": "off", "with": "disabled"},
                    "invalid",
                ]
            }
        )
    )
    monkeypatch.setattr(login_tab, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(login_tab.os.path, "expanduser", lambda _: str(path))

    def forbidden(*args, **kwargs):
        raise AssertionError("Reading preferences must not start a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert load_replacements() == {"@@": "person@example.invalid"}
    path.write_bytes(b"invalid plist")
    assert load_replacements() == {}
    path.unlink()
    assert load_replacements() == {}


def xml(identity, declaration=""):
    return (
        f'<?xml version="1.0"?>{declaration}<qgis><properties><aino>'
        f"<client_project_id>{identity}</client_project_id></aino></properties></qgis>"
    ).encode()


@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE qgis [<!ENTITY value "{}">]>',
        '<!DOCTYPE qgis [<!ENTITY value SYSTEM "file:///nonexistent/aino-secret">]>',
        '<!DOCTYPE qgis [<!ENTITY % value SYSTEM "https://example.invalid/aino-secret">%value;]>',
    ],
)
@pytest.mark.parametrize("extension", ["qgs", "qgz"])
def test_saved_identity_rejects_entities(qgis_app, tmp_path, declaration, extension):
    identity = str(uuid4())
    data = xml(identity, declaration.format(identity))
    path = tmp_path / f"project.{extension}"
    if extension == "qgz":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("project.qgs", data)
    else:
        path.write_bytes(data)
    project = QgsProject()
    project.setFileName(str(path))
    assert _stored_identity(project) is None


def test_native_xml_preserves_identity_doctype_unicode_and_chunk_boundaries():
    identity = str(uuid4())
    data = xml(identity, '<!DOCTYPE qgis PUBLIC "http://mrcc.com/qgis.dtd" "SYSTEM">')
    data = data.replace(
        b"<properties>", "<title>Карта &amp; данные</title><properties>".encode()
    )

    class Chunks(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            return super().read(min(size, 7))

    assert _xml_identity(Chunks(data)) == identity


def test_native_xml_rejects_expanding_entity_value():
    data = xml(
        "&c;",
        '<!DOCTYPE qgis [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;">]>',
    )
    with pytest.raises(ValueError):
        _xml_identity(io.BytesIO(data))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_native_xml_rejects_entity_encoded_as_saved_uuid(encoding):
    identity = str(uuid4())
    data = xml("&value;", f'<!DOCTYPE qgis [<!ENTITY value "{identity}">]>')
    if encoding == "utf-16":
        data = data.decode("utf-8").encode("utf-16")
    with pytest.raises(ValueError):
        _xml_identity(io.BytesIO(data))
