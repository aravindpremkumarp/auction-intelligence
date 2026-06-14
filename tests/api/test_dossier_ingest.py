"""Endpoint tests for dossier document ingest.

Repository, R2 storage, and the OCR+classify step are all faked, so these pin
the router contract: caps (size/type), the consent gate, ownership 404s, the
sync ingest happy path (status -> ready, checklist updates), graceful failure
(status -> failed but still 201), presigned-URL fetch, delete + per-user
isolation.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import auth_header


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> dict:
    from api.dossier import repository as repo
    from pipeline import dossier_ingest, storage

    state: dict = {
        # dossier_id -> {"owner": sub, "documents": {doc_id: rec}}
        "dossiers": {"dos-1": {"owner": "owner", "documents": {}}},
        "uploaded": [],   # (key, len(body))
        "deleted": [],    # list of key-lists
        "ingest_raises": False,
    }

    # ── repository fakes ──
    async def owns_dossier(sub, did):
        d = state["dossiers"].get(did)
        return d is not None and d["owner"] == sub

    async def add_document(sub, did, doc_id, *, filename, r2_key, content_type,
                           size_bytes, status, ocr_consent_at):
        d = state["dossiers"].get(did)
        if d is None or d["owner"] != sub:
            return False
        d["documents"][doc_id] = {
            "id": doc_id, "filename": filename, "r2_key": r2_key,
            "content_type": content_type, "size_bytes": size_bytes,
            "status": status, "doc_type": None, "category": None,
            "doc_type_confidence": None, "uploaded_at": "t0",
        }
        return True

    async def set_document_result(sub, did, doc_id, *, status, category, doc_type,
                                  confidence, reasoning, ocr_text, classified_at):
        d = state["dossiers"].get(did)
        if d is None or d["owner"] != sub or doc_id not in d["documents"]:
            return None
        rec = d["documents"][doc_id]
        rec.update(status=status, category=category, doc_type=doc_type,
                   doc_type_confidence=confidence)
        return {k: rec[k] for k in ("id", "filename", "doc_type", "category",
                                    "status", "doc_type_confidence", "uploaded_at")}

    async def get_document(sub, did, doc_id):
        d = state["dossiers"].get(did)
        if d is None or d["owner"] != sub or doc_id not in d["documents"]:
            return None
        return dict(d["documents"][doc_id])

    async def delete_document(sub, did, doc_id):
        d = state["dossiers"].get(did)
        if d is None or d["owner"] != sub or doc_id not in d["documents"]:
            return None
        return d["documents"].pop(doc_id)["r2_key"]

    async def get_dossier(sub, did):
        d = state["dossiers"].get(did)
        if d is None or d["owner"] != sub:
            return None
        return {
            "id": did, "title": "t", "created_at": "t0", "updated_at": "t0",
            "property": {"kind": "auction_property", "auction_id": "a-1"},
            "documents": [dict(r) for r in d["documents"].values()],
        }

    for name, fn in [
        ("owns_dossier", owns_dossier), ("add_document", add_document),
        ("set_document_result", set_document_result), ("get_document", get_document),
        ("delete_document", delete_document), ("get_dossier", get_dossier),
    ]:
        monkeypatch.setattr(repo, name, fn)

    # ── storage fakes ──
    def dossier_object_key(sub, did, doc_id, filename):
        return f"dossiers/{sub}/{did}/{doc_id}__{filename}"

    def upload_bytes_private(key, body, content_type=None):
        state["uploaded"].append((key, len(body)))
        return key

    def presigned_get_url(key, expires_in=300):
        return f"https://signed.example/{key}?ttl={expires_in}"

    def delete_private_objects(keys):
        state["deleted"].append(list(keys))

    monkeypatch.setattr(storage, "dossier_object_key", dossier_object_key)
    monkeypatch.setattr(storage, "upload_bytes_private", upload_bytes_private)
    monkeypatch.setattr(storage, "presigned_get_url", presigned_get_url)
    monkeypatch.setattr(storage, "delete_private_objects", delete_private_objects)

    # ── OCR + classify fake ──
    async def extract_and_classify(body, filename, content_type):
        if state["ingest_raises"]:
            raise dossier_ingest.IngestError("OCR produced no text", status_code=502)
        return {
            "markdown": "EC for survey 123 ...",
            "category": "D", "doc_type": "encumbrance_certificate",
            "confidence": 0.92, "reasoning": "lists registered transactions",
        }

    monkeypatch.setattr(dossier_ingest, "extract_and_classify", extract_and_classify)
    return state


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def _png(name: str = "deed.png") -> dict:
    return {"file": (name, b"\x89PNG\r\n\x1a\nfake-bytes", "image/png")}


def test_upload_requires_auth(fake: dict) -> None:
    client = _client()
    r = client.post("/dossiers/dos-1/documents", files=_png(), data={"consent": "true"})
    assert r.status_code == 401


def test_upload_requires_consent(fake: dict) -> None:
    client = _client()
    h = auth_header(sub="owner")
    r = client.post("/dossiers/dos-1/documents", files=_png(),
                    data={"consent": "false"}, headers=h)
    assert r.status_code == 400
    # Nothing was stored.
    assert fake["uploaded"] == []


def test_upload_rejects_unsupported_type(fake: dict) -> None:
    client = _client()
    h = auth_header(sub="owner")
    r = client.post(
        "/dossiers/dos-1/documents",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"consent": "true"}, headers=h,
    )
    assert r.status_code == 415
    assert fake["uploaded"] == []


def test_upload_rejects_too_large(fake: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline import dossier_ingest
    monkeypatch.setattr(dossier_ingest, "MAX_FILE_BYTES", 4)
    client = _client()
    h = auth_header(sub="owner")
    r = client.post("/dossiers/dos-1/documents",
                    files={"file": ("deed.png", b"way-too-big", "image/png")},
                    data={"consent": "true"}, headers=h)
    assert r.status_code == 413


def test_upload_unknown_dossier_404(fake: dict) -> None:
    client = _client()
    h = auth_header(sub="owner")
    r = client.post("/dossiers/nope/documents", files=_png(),
                    data={"consent": "true"}, headers=h)
    assert r.status_code == 404
    assert fake["uploaded"] == []  # ownership checked before storage


def test_upload_happy_path_classifies_and_updates_checklist(fake: dict) -> None:
    client = _client()
    h = auth_header(sub="owner")
    r = client.post("/dossiers/dos-1/documents", files=_png(),
                    data={"consent": "true"}, headers=h)
    assert r.status_code == 201
    body = r.json()

    # The new document is ready + classified.
    doc_id = body["uploaded_document_id"]
    docs = {d["id"]: d for d in body["documents"]}
    assert docs[doc_id]["status"] == "ready"
    assert docs[doc_id]["doc_type"] == "encumbrance_certificate"

    # Checklist reflects the EC now being present (1 of 10 must-haves).
    assert body["checklist"]["score"] == {"score": 10, "have": 1, "total": 10}
    # The original was stored privately.
    assert len(fake["uploaded"]) == 1


def test_upload_ocr_failure_records_failed_status(fake: dict) -> None:
    fake["ingest_raises"] = True
    client = _client()
    h = auth_header(sub="owner")
    r = client.post("/dossiers/dos-1/documents", files=_png(),
                    data={"consent": "true"}, headers=h)
    # Still 201 — the file is kept so the user can retry/delete.
    assert r.status_code == 201
    body = r.json()
    doc_id = body["uploaded_document_id"]
    docs = {d["id"]: d for d in body["documents"]}
    assert docs[doc_id]["status"] == "failed"
    # A failed classification adds nothing to the score.
    assert body["checklist"]["score"]["have"] == 0


def test_presigned_url_and_delete(fake: dict) -> None:
    client = _client()
    h = auth_header(sub="owner")
    up = client.post("/dossiers/dos-1/documents", files=_png(),
                     data={"consent": "true"}, headers=h).json()
    doc_id = up["uploaded_document_id"]

    url = client.get(f"/dossiers/dos-1/documents/{doc_id}/url", headers=h)
    assert url.status_code == 200
    assert url.json()["url"].startswith("https://signed.example/")
    assert url.json()["expires_in"] == 300

    d = client.delete(f"/dossiers/dos-1/documents/{doc_id}", headers=h)
    assert d.status_code == 204
    assert fake["deleted"]  # the object was cleaned up
    # Gone now.
    assert client.get(f"/dossiers/dos-1/documents/{doc_id}/url", headers=h).status_code == 404
    assert client.delete(f"/dossiers/dos-1/documents/{doc_id}", headers=h).status_code == 404


def test_documents_are_per_user(fake: dict) -> None:
    client = _client()
    owner = auth_header(sub="owner")
    intruder = auth_header(sub="intruder")
    doc_id = client.post("/dossiers/dos-1/documents", files=_png(),
                         data={"consent": "true"}, headers=owner).json()["uploaded_document_id"]

    # Intruder cannot upload to, read, or delete from the owner's dossier.
    assert client.post("/dossiers/dos-1/documents", files=_png(),
                       data={"consent": "true"}, headers=intruder).status_code == 404
    assert client.get(f"/dossiers/dos-1/documents/{doc_id}/url",
                      headers=intruder).status_code == 404
    assert client.delete(f"/dossiers/dos-1/documents/{doc_id}",
                         headers=intruder).status_code == 404
