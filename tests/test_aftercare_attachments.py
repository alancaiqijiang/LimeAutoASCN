"""HTTP-level coverage for maintenance attachment uploads.

The upload boundary is exercised through the real routes against a real (temporary)
aftercare database and media root, because the behaviour under test is the route
contract: what the staff member sees, and whether a rejected upload leaves behind a
maintenance record, an attachment row, or a stored media file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import aftercare_db
from app.aftercare_routes import create_staff_password
from app.main import app

VIN = "LGXCD6CD4P0123458"
PASSWORD = "LimeAuto-Route-Test-2026!"
SECRET = "attachment-test-secret"
EMAIL = "operator@example.test"

# Sniffable image headers: the store detects the type from the bytes, not the file name.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

EMPTY_PHOTO_ZH = "图片文件为空，请重新选择要上传的图片"
EMPTY_PHOTO_EN = "That photo file is empty. Choose the photo again."
TYPE_ERROR_ZH = "图片须为 JPEG、PNG 或 WEBP"
TOO_LARGE_ZH = "单张图片不能超过 10 MB"
RECORD_TOO_LARGE_ZH = "该维保单的图片合计不能超过 50 MB"
TOO_MANY_ZH = "该维保单的图片数量已达上限（20 张）"

RECORD_FIELDS = {
    "maintenance_date": "2026-09-08",
    "maintenance_type": "inspection",
    "summary": "PDI check",
    "service_provider": "Workshop",
    "source_reference": "job-1001",
    "note": "Record note",
}


class Staff:
    def __init__(self, client, vehicle_id, db_path, media_root):
        self.client = client
        self.vehicle_id = vehicle_id
        self.db_path = db_path
        self.media_root = media_root

    @property
    def csrf(self) -> str:
        token = self.client.cookies.get("limeauto_aftercare_csrf")
        assert token
        return token

    def create_url(self) -> str:
        return f"/ops/vehicles/{self.vehicle_id}/maintenance"

    def edit_url(self, record_id: int) -> str:
        return f"/ops/vehicles/{self.vehicle_id}/maintenance/{record_id}"

    def attachment_url(self, record_id: int, attachment_id: int) -> str:
        return (
            f"/ops/vehicles/{self.vehicle_id}/maintenance/{record_id}"
            f"/attachments/{attachment_id}"
        )

    def media_files(self) -> list:
        return [path for path in self.media_root.rglob("*") if path.is_file()]

    def db_state(self) -> tuple[int, int]:
        with aftercare_db.connect(self.db_path, hmac_secret=SECRET) as db:
            records = db.execute("SELECT COUNT(*) FROM maintenance_records").fetchone()[0]
            attachments = db.execute(
                "SELECT COUNT(*) FROM maintenance_attachments"
            ).fetchone()[0]
        return int(records), int(attachments)

    def attachments_of(self, record_id: int) -> list[dict]:
        with aftercare_db.connect(self.db_path, hmac_secret=SECRET) as db:
            return aftercare_db.list_maintenance_attachments(db, record_id)

    def record_ids(self) -> list[int]:
        with aftercare_db.connect(self.db_path, hmac_secret=SECRET) as db:
            return [
                int(row[0])
                for row in db.execute("SELECT id FROM maintenance_records ORDER BY id")
            ]


@pytest.fixture
def staff(tmp_path, monkeypatch) -> Staff:
    db_path = tmp_path / "aftercare.sqlite3"
    media_root = tmp_path / "media"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", SECRET)
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_MEDIA_ROOT", str(media_root))
    with aftercare_db.connect(db_path, hmac_secret=SECRET) as db:
        aftercare_db.create_staff_user(
            db, email=EMAIL, password_hash=create_staff_password(PASSWORD)
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(db, VIN, hmac_secret=SECRET)
    client = TestClient(app)
    login = client.post(
        "/ops/login",
        data={"email": EMAIL, "password": PASSWORD},
        follow_redirects=False,
    )
    assert login.status_code == 303, login.text
    assert client.cookies.get("limeauto_aftercare_csrf")
    return Staff(client, int(vehicle["id"]), db_path, media_root)


def _create(staff: Staff, *, files, extra: dict | None = None, lang: str | None = None):
    data = dict(RECORD_FIELDS)
    data["csrf_token"] = staff.csrf
    data.update(extra or {})
    url = staff.create_url() + (f"?lang={lang}" if lang else "")
    return staff.client.post(url, data=data, files=files, follow_redirects=False)


def _edit(staff: Staff, record_id: int, *, files, extra: dict | None = None):
    data = dict(RECORD_FIELDS)
    data["csrf_token"] = staff.csrf
    data.update(extra or {})
    return staff.client.post(
        staff.edit_url(record_id), data=data, files=files, follow_redirects=False
    )


def _browser_multipart(fields: dict[str, str], *, boundary: str = "----limeauto-boundary"):
    """A multipart body exactly as a browser builds it for an empty optional file input.

    ``httpx`` will not encode a zero-length filename part, so the empty selection has to
    be posted as raw bytes to exercise the route the way the form does.
    """
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="photos"; filename=""\r\n'
        "Content-Type: application/octet-stream\r\n\r\n\r\n"
    )
    return "".join(parts + [f"--{boundary}--\r\n"]).encode(), boundary


# --- upload boundary: empty selections, empty files, type and size limits -------------


def test_zero_byte_photo_with_filename_is_reported_and_leaves_no_orphans(staff):
    response = _create(staff, files=[("photos", ("broken.jpg", b"", "image/jpeg"))])

    assert response.status_code == 200
    assert EMPTY_PHOTO_ZH in response.text
    # The submitted values survive, so the staff member does not retype the record.
    assert "PDI check" in response.text
    assert staff.db_state() == (0, 0)
    assert staff.media_files() == []


def test_empty_filename_file_input_stays_usable(staff):
    """A form with an optional upload posts an empty part; it must not be an error."""
    body, boundary = _browser_multipart(
        {
            "maintenance_date": "2026-09-08",
            "maintenance_type": "inspection",
            "summary": "No photo",
            "csrf_token": staff.csrf,
        }
    )
    response = staff.client.post(
        staff.create_url(),
        content=body,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/ops/vehicles/{staff.vehicle_id}"
    assert staff.db_state() == (1, 0)
    assert staff.media_files() == []
    # The form itself is still reachable after the empty selection.
    form = staff.client.get(f"/ops/vehicles/{staff.vehicle_id}/maintenance/new")
    assert form.status_code == 200
    assert 'name="photos"' in form.text


def test_unsupported_photo_type_is_reported_and_leaves_no_orphans(staff):
    response = _create(staff, files=[("photos", ("scan.jpg", b"not an image at all", "image/jpeg"))])

    assert response.status_code == 200
    assert TYPE_ERROR_ZH in response.text
    assert staff.db_state() == (0, 0)
    assert staff.media_files() == []


def test_single_photo_over_ten_mb_is_reported_and_leaves_no_orphans(staff, monkeypatch):
    monkeypatch.setattr(aftercare_db, "ATTACHMENT_MAX_BYTES", 1024)

    response = _create(staff, files=[("photos", ("big.jpg", JPEG + b"\x00" * 4096, "image/jpeg"))])

    assert response.status_code == 200
    assert TOO_LARGE_ZH in response.text
    assert staff.db_state() == (0, 0)
    assert staff.media_files() == []


def test_record_total_capacity_rejects_the_whole_upload_without_orphans(staff, monkeypatch):
    monkeypatch.setattr(aftercare_db, "ATTACHMENT_MAX_RECORD_BYTES", 64)
    photo = JPEG + b"\x00" * 24  # Accepted alone, over the record budget together

    response = _create(
        staff,
        files=[
            ("photos", ("one.jpg", photo, "image/jpeg")),
            ("photos", ("two.jpg", photo, "image/jpeg")),
        ],
    )

    assert response.status_code == 200
    assert RECORD_TOO_LARGE_ZH in response.text
    assert staff.db_state() == (0, 0)
    assert staff.media_files() == []


def test_attachment_count_capacity_rejects_the_whole_upload_without_orphans(staff, monkeypatch):
    monkeypatch.setattr(aftercare_db, "ATTACHMENT_MAX_FILES", 1)

    response = _create(
        staff,
        files=[
            ("photos", ("one.jpg", JPEG, "image/jpeg")),
            ("photos", ("two.jpg", PNG, "image/png")),
        ],
    )

    assert response.status_code == 200
    assert TOO_MANY_ZH in response.text
    assert staff.db_state() == (0, 0)
    assert staff.media_files() == []


def test_edit_rejects_a_bad_photo_and_keeps_the_existing_attachment(staff):
    created = _create(staff, files=[("photos", ("keep.jpg", JPEG, "image/jpeg"))])
    assert created.status_code == 303
    record_id = staff.record_ids()[0]
    existing = staff.attachments_of(record_id)
    assert len(existing) == 1

    response = _edit(
        staff,
        record_id,
        files=[("photos", ("broken.png", b"", "image/png"))],
        extra={"summary": "Edited summary"},
    )

    assert response.status_code == 200
    assert EMPTY_PHOTO_ZH in response.text
    assert staff.db_state() == (1, 1)
    assert [item["id"] for item in staff.attachments_of(record_id)] == [existing[0]["id"]]
    with aftercare_db.connect(staff.db_path, hmac_secret=SECRET) as db:
        record = aftercare_db.maintenance_by_id(db, record_id)
    assert record["summary"] == "PDI check"
    assert len(staff.media_files()) == 1


# --- upload, read back, delete --------------------------------------------------------


def test_uploaded_photo_is_read_back_then_deleted(staff):
    response = _create(staff, files=[("photos", ("receipt.jpg", JPEG, "image/jpeg"))])
    assert response.status_code == 303
    record_id = staff.record_ids()[0]
    attachments = staff.attachments_of(record_id)
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment["original_filename"] == "receipt.jpg"
    assert attachment["size_bytes"] == len(JPEG)
    stored = staff.media_files()
    assert len(stored) == 1
    assert stored[0].read_bytes() == JPEG

    shown = staff.client.get(staff.attachment_url(record_id, int(attachment["id"])))
    assert shown.status_code == 200
    assert shown.content == JPEG
    assert shown.headers["content-type"] == "image/jpeg"

    detail = staff.client.get(staff.edit_url(record_id))
    assert detail.status_code == 200
    assert staff.attachment_url(record_id, int(attachment["id"])) in detail.text

    deleted = _edit(
        staff, record_id, files=[], extra={"delete_attachment_ids": str(attachment["id"])}
    )
    assert deleted.status_code == 303
    assert deleted.headers["location"] == f"/ops/vehicles/{staff.vehicle_id}"
    assert staff.db_state() == (1, 0)
    assert staff.media_files() == []
    assert staff.client.get(staff.attachment_url(record_id, int(attachment["id"]))).status_code == 404


def test_attachment_read_is_gated_and_scoped(staff):
    created = _create(staff, files=[("photos", ("gated.jpg", JPEG, "image/jpeg"))])
    assert created.status_code == 303
    record_id = staff.record_ids()[0]
    attachment_id = int(staff.attachments_of(record_id)[0]["id"])
    url = staff.attachment_url(record_id, attachment_id)

    anonymous = TestClient(app)
    gated = anonymous.get(url, follow_redirects=False)
    assert gated.status_code == 303
    assert gated.headers["location"].startswith("/ops/login?next=")

    missing = staff.client.get(staff.attachment_url(record_id, attachment_id + 1000))
    assert missing.status_code == 404
    wrong_record = staff.client.get(staff.attachment_url(record_id + 999, attachment_id))
    assert wrong_record.status_code == 404
    # The signed-in staff session must still read the file it just uploaded.
    assert staff.client.get(url, follow_redirects=False).status_code == 200


def test_delete_attachment_cannot_cross_records(staff):
    first = _create(staff, files=[("photos", ("first.jpg", JPEG, "image/jpeg"))])
    assert first.status_code == 303
    first_id = staff.record_ids()[0]
    stolen = int(staff.attachments_of(first_id)[0]["id"])

    second = _create(
        staff,
        files=[],
        extra={"maintenance_date": "2026-09-09", "summary": "Second record"},
    )
    assert second.status_code == 303
    second_id = staff.record_ids()[1]

    response = _edit(
        staff,
        second_id,
        files=[],
        extra={"summary": "Second record edited", "delete_attachment_ids": str(stolen)},
    )

    assert response.status_code == 303
    assert [item["id"] for item in staff.attachments_of(first_id)] == [stolen]
    assert len(staff.media_files()) == 1
    assert staff.client.get(staff.attachment_url(first_id, stolen)).status_code == 200


def test_attachment_read_is_scoped_to_its_own_vehicle(staff):
    created = _create(staff, files=[("photos", ("scoped.jpg", JPEG, "image/jpeg"))])
    assert created.status_code == 303
    record_id = staff.record_ids()[0]
    attachment_id = int(staff.attachments_of(record_id)[0]["id"])

    with aftercare_db.connect(staff.db_path, hmac_secret=SECRET) as db:
        other, _ = aftercare_db.create_or_get_vehicle(
            db, "LGXCD6CD4P0123459", hmac_secret=SECRET
        )
    other_url = (
        f"/ops/vehicles/{int(other['id'])}/maintenance/{record_id}"
        f"/attachments/{attachment_id}"
    )
    assert staff.client.get(other_url).status_code == 404
    # Still readable through its real owner, so the 404 is scoping and not a broken path.
    assert (
        staff.client.get(staff.attachment_url(record_id, attachment_id)).status_code == 200
    )


def test_unauthenticated_upload_is_gated_and_writes_nothing(staff):
    anonymous = TestClient(app)
    response = anonymous.post(
        staff.create_url(),
        data={**RECORD_FIELDS, "csrf_token": staff.csrf},
        files=[("photos", ("sneak.jpg", JPEG, "image/jpeg"))],
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/ops/login")
    assert staff.db_state() == (0, 0)
    assert staff.media_files() == []


def test_multiple_photos_are_all_stored_and_readable(staff):
    response = _create(
        staff,
        files=[
            ("photos", ("one.jpg", JPEG, "image/jpeg")),
            ("photos", ("two.png", PNG, "image/png")),
        ],
    )
    assert response.status_code == 303
    record_id = staff.record_ids()[0]
    attachments = staff.attachments_of(record_id)
    assert [item["original_filename"] for item in attachments] == ["one.jpg", "two.png"]
    assert [item["mime_type"] for item in attachments] == ["image/jpeg", "image/png"]
    stored = staff.media_files()
    assert sorted(path.suffix for path in stored) == [".jpg", ".png"]
    for item in attachments:
        read = staff.client.get(staff.attachment_url(record_id, int(item["id"])))
        assert read.status_code == 200


# --- localization ---------------------------------------------------------------------


def test_attachment_errors_follow_the_interface_language(staff):
    cases = [
        (EMPTY_PHOTO_ZH, EMPTY_PHOTO_EN, [("photos", ("broken.jpg", b"", "image/jpeg"))]),
        (
            TYPE_ERROR_ZH,
            "Photos must be JPEG, PNG, or WEBP",
            [("photos", ("scan.jpg", b"not an image at all", "image/jpeg"))],
        ),
    ]
    for zh_text, en_text, files in cases:
        zh = _create(staff, files=files, lang="zh")
        assert zh.status_code == 200
        assert zh_text in zh.text
        assert en_text not in zh.text

        en = _create(staff, files=files, extra={"summary": "English error"}, lang="en")
        assert en.status_code == 200
        assert en_text in en.text
        assert zh_text not in en.text

    assert staff.db_state() == (0, 0)
    assert staff.media_files() == []


def test_single_photo_size_error_is_localized(staff, monkeypatch):
    monkeypatch.setattr(aftercare_db, "ATTACHMENT_MAX_BYTES", 1024)
    files = [("photos", ("big.jpg", JPEG + b"\x00" * 4096, "image/jpeg"))]

    zh = _create(staff, files=files, lang="zh")
    assert zh.status_code == 200
    assert TOO_LARGE_ZH in zh.text

    en = _create(staff, files=files, extra={"summary": "English size error"}, lang="en")
    assert en.status_code == 200
    assert "Each photo must be 10 MB or smaller" in en.text
    assert TOO_LARGE_ZH not in en.text
    assert staff.db_state() == (0, 0)
    assert staff.media_files() == []
