from __future__ import annotations

import sqlite3

import pytest

from app import aftercare_db

VIN = "LGXCD6CD4P0123458"
VIN_WITH_SEPARATORS = "lgxcd6cd4p-0123 458"
SECRET = "test-secret"
SQLITE_INTEGER_MAX = 2**63 - 1


def opened_db(tmp_path):
    return aftercare_db.connect(tmp_path / "aftercare.sqlite3", hmac_secret=SECRET)


def test_empty_initialization_and_repeat_initialization(tmp_path):
    path = tmp_path / "aftercare.sqlite3"
    with aftercare_db.connect(path, hmac_secret=SECRET) as db:
        tables = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "staff_users",
            "vehicles",
            "maintenance_records",
            "vehicle_catalog_shortcuts",
            "audit_events",
        } <= tables

    with aftercare_db.connect(path, hmac_secret=SECRET) as db:
        assert db.execute("SELECT COUNT(*) AS count FROM vehicles").fetchone()["count"] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_vin_normalization_and_hmac_uniqueness(tmp_path):
    assert aftercare_db.normalize_vin(VIN_WITH_SEPARATORS) == VIN
    assert aftercare_db.vin_hmac(VIN_WITH_SEPARATORS, SECRET) == aftercare_db.vin_hmac(VIN, SECRET)
    with opened_db(tmp_path) as db:
        vehicle, created = aftercare_db.create_or_get_vehicle(
            db, VIN_WITH_SEPARATORS, brand="BYD", hmac_secret=SECRET
        )
        same_vehicle, created_again = aftercare_db.create_or_get_vehicle(
            db, VIN, brand="Changed", hmac_secret=SECRET
        )
        assert created is True
        assert created_again is False
        assert same_vehicle["id"] == vehicle["id"]
        assert same_vehicle["brand"] == "BYD"
        assert same_vehicle["vin_normalized"] == VIN
        assert same_vehicle["vin_hmac"] == aftercare_db.vin_hmac(VIN, SECRET)
        assert same_vehicle["vin_last4"] == VIN[-4:]
        assert aftercare_db.search_vehicles(db, VIN, hmac_secret=SECRET)[0]["id"] == vehicle["id"]
        assert aftercare_db.search_vehicles(db, "%", hmac_secret=SECRET) == []
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO vehicles(vin_normalized, vin_hmac, vin_last4, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (VIN, "different", VIN[-4:], "now", "now"),
            )


def test_maintenance_requires_existing_vehicle_and_keeps_ownership(tmp_path):
    with opened_db(tmp_path) as db:
        vehicle, _ = aftercare_db.create_or_get_vehicle(db, VIN, hmac_secret=SECRET)
        other_vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, "LSGCD6CD4P0123459", hmac_secret=SECRET
        )
        record = aftercare_db.create_maintenance_record(
            db,
            vehicle_id=vehicle["id"],
            maintenance_date="2026-08-01",
            maintenance_type="inspection",
            summary="PDI review",
            odometer_km=120,
        )
        assert record["vehicle_id"] == vehicle["id"]
        assert aftercare_db.maintenance_by_id(
            db, record["id"], vehicle_id=other_vehicle["id"]
        ) is None
        assert aftercare_db.list_maintenance_records(db, vehicle["id"])[0]["id"] == record["id"]
        with pytest.raises(ValueError, match="Vehicle does not exist"):
            aftercare_db.create_maintenance_record(
                db,
                vehicle_id=99999,
                maintenance_date="2026-08-01",
                maintenance_type="repair",
                summary="Not owned",
            )


def test_maintenance_date_requires_calendar_iso_date_without_future_policy(tmp_path):
    with opened_db(tmp_path) as db:
        vehicle, _ = aftercare_db.create_or_get_vehicle(db, VIN, hmac_secret=SECRET)
        future = aftercare_db.create_maintenance_record(
            db,
            vehicle_id=vehicle["id"],
            maintenance_date="2099-12-31",
            maintenance_type="inspection",
            summary="Future scheduled check",
        )
        assert future["maintenance_date"] == "2099-12-31"

        for invalid_date in ("", "2026/08/01", "20260801", "2026-02-30"):
            with pytest.raises(ValueError, match="Maintenance date"):
                aftercare_db.create_maintenance_record(
                    db,
                    vehicle_id=vehicle["id"],
                    maintenance_date=invalid_date,
                    maintenance_type="repair",
                    summary="Rejected date",
                )

        with pytest.raises(ValueError, match="Maintenance date"):
            aftercare_db.update_maintenance_record(
                db,
                future["id"],
                maintenance_date="2026-13-01",
                maintenance_type="inspection",
                summary="Should remain unchanged",
            )
        assert aftercare_db.maintenance_by_id(db, future["id"])["maintenance_date"] == "2099-12-31"


def test_maintenance_odometer_respects_sqlite_integer_boundary_on_create_and_update(tmp_path):
    with opened_db(tmp_path) as db:
        vehicle, _ = aftercare_db.create_or_get_vehicle(db, VIN, hmac_secret=SECRET)

        with pytest.raises(ValueError, match="Odometer cannot be negative"):
            aftercare_db.create_maintenance_record(
                db,
                vehicle_id=vehicle["id"],
                maintenance_date="2026-08-01",
                maintenance_type="inspection",
                summary="Rejected negative odometer",
                odometer_km=-1,
            )

        zero = aftercare_db.create_maintenance_record(
            db,
            vehicle_id=vehicle["id"],
            maintenance_date="2026-08-01",
            maintenance_type="inspection",
            summary="Zero odometer",
            odometer_km=0,
        )
        assert zero["odometer_km"] == 0

        record = aftercare_db.create_maintenance_record(
            db,
            vehicle_id=vehicle["id"],
            maintenance_date="2026-08-01",
            maintenance_type="inspection",
            summary="Maximum odometer",
            odometer_km=SQLITE_INTEGER_MAX,
        )
        assert record["odometer_km"] == SQLITE_INTEGER_MAX

        with pytest.raises(ValueError, match="Odometer exceeds SQLite INTEGER range"):
            aftercare_db.create_maintenance_record(
                db,
                vehicle_id=vehicle["id"],
                maintenance_date="2026-08-02",
                maintenance_type="inspection",
                summary="Too large odometer",
                odometer_km=SQLITE_INTEGER_MAX + 1,
            )

        updated = aftercare_db.update_maintenance_record(
            db,
            record["id"],
            maintenance_date="2026-08-03",
            maintenance_type="repair",
            summary="Updated maximum odometer",
            odometer_km=SQLITE_INTEGER_MAX,
        )
        assert updated is not None
        assert updated["odometer_km"] == SQLITE_INTEGER_MAX

        with pytest.raises(ValueError, match="Odometer cannot be negative"):
            aftercare_db.update_maintenance_record(
                db,
                record["id"],
                maintenance_date="2026-08-04",
                maintenance_type="repair",
                summary="Rejected negative update",
                odometer_km=-1,
            )

        with pytest.raises(ValueError, match="Odometer exceeds SQLite INTEGER range"):
            aftercare_db.update_maintenance_record(
                db,
                record["id"],
                maintenance_date="2026-08-05",
                maintenance_type="repair",
                summary="Too large update",
                odometer_km=SQLITE_INTEGER_MAX + 1,
            )

        unchanged = aftercare_db.maintenance_by_id(db, record["id"])
        assert unchanged["odometer_km"] == SQLITE_INTEGER_MAX


def test_shortcut_replacement_preserves_history_and_one_current(tmp_path):
    with opened_db(tmp_path) as db:
        staff = aftercare_db.create_staff_user(
            db, email="operator@limeauto.test", password_hash="hash"
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(db, VIN, hmac_secret=SECRET)
        first = aftercare_db.set_catalog_shortcut(
            db,
            vehicle_id=vehicle["id"],
            series_code="HYE",
            model_code="HYEE-PZ02",
            confirmation_note="Manual review 1",
            created_by=staff["id"],
        )
        second = aftercare_db.set_catalog_shortcut(
            db,
            vehicle_id=vehicle["id"],
            series_code="EUH",
            model_code="EUHD-PZ01",
            confirmation_note="Manual review 2",
            created_by=staff["id"],
        )
        assert first["is_current"] == 1
        assert second["is_current"] == 1
        history = aftercare_db.list_catalog_shortcuts(db, vehicle["id"])
        assert len(history) == 2
        assert sum(item["is_current"] for item in history) == 1
        current = aftercare_db.current_catalog_shortcut(db, vehicle["id"])
        assert current is not None
        assert current["id"] == second["id"]
        assert db.execute(
            "SELECT COUNT(*) AS count FROM vehicle_catalog_shortcuts WHERE vehicle_id = ? AND is_current = 1",
            (vehicle["id"],),
        ).fetchone()["count"] == 1


def test_audit_insertion_is_structured(tmp_path):
    with opened_db(tmp_path) as db:
        staff = aftercare_db.create_staff_user(
            db, email="admin@limeauto.test", password_hash="hash", role="admin"
        )
        event = aftercare_db.append_audit_event(
            db,
            actor_user_id=staff["id"],
            action="vehicle_created",
            target_type="vehicle",
            target_id="42",
            metadata={"vin_last4": VIN[-4:], "created": True},
        )
        assert event["actor_user_id"] == staff["id"]
        assert event["action"] == "vehicle_created"
        assert "vin_last4" in event["metadata_json"]
        assert VIN not in event["metadata_json"]
        assert aftercare_db.list_audit_events(db)[0]["id"] == event["id"]


def test_vehicle_listing_and_search_support_offset_pagination(tmp_path):
    with opened_db(tmp_path) as db:
        for number in range(125):
            aftercare_db.create_or_get_vehicle(
                db,
                f"LGXCD6CD4P{number:07d}",
                brand="BYD",
                model_label=f"Model {number}",
                hmac_secret=SECRET,
            )

        first_page = aftercare_db.list_vehicles(db, limit=101, offset=0)
        second_page = aftercare_db.list_vehicles(db, limit=101, offset=100)
        assert len(first_page) == 101
        assert len(second_page) == 25
        assert first_page[-1]["id"] == second_page[0]["id"]
        assert second_page[-1]["vin_last4"] == "0000"

        search_page = aftercare_db.search_vehicles(db, "BYD", limit=101, offset=100)
        assert len(search_page) == 25
        assert search_page[-1]["vin_last4"] == "0000"

        with pytest.raises(ValueError, match="Invalid vehicle offset"):
            aftercare_db.search_vehicles(db, "BYD", offset=-1)


def test_overview_filters_keep_unrecorded_rows_until_a_column_is_constrained(tmp_path):
    with opened_db(tmp_path) as db:
        hyundai, _ = aftercare_db.create_or_get_vehicle(
            db,
            "LGXCD6CD4P0000001",
            brand="Hyundai",
            hmac_secret=SECRET,
        )
        recorded, _ = aftercare_db.create_or_get_vehicle(
            db,
            "LGXCD6CD4P0000002",
            brand="BYD",
            model_label="Seal",
            model_year=2024,
            color="White",
            delivery_date="2026-01-02",
            hmac_secret=SECRET,
        )

        blank = aftercare_db.normalize_overview_filters()
        assert blank["brand"] == ""
        all_rows = aftercare_db.list_overview_vehicles(db, blank, limit=10, offset=0)
        assert {row["id"] for row in all_rows} == {hyundai["id"], recorded["id"]}

        hyundai_only = aftercare_db.list_overview_vehicles(
            db,
            aftercare_db.normalize_overview_filters(brand="hyundai"),
            limit=10,
            offset=0,
        )
        assert [row["id"] for row in hyundai_only] == [hyundai["id"]]
        assert hyundai_only[0]["model_label"] is None

        year_only = aftercare_db.list_overview_vehicles(
            db,
            aftercare_db.normalize_overview_filters(model_year="2024"),
            limit=10,
            offset=0,
        )
        assert [row["id"] for row in year_only] == [recorded["id"]]

        missing = aftercare_db.list_overview_vehicles(
            db,
            aftercare_db.normalize_overview_filters(brand="Hyundai", model_year="2024"),
            limit=10,
            offset=0,
        )
        assert missing == []

        with pytest.raises(ValueError, match="Invalid model year filter"):
            aftercare_db.normalize_overview_filters(model_year="18")
        with pytest.raises(ValueError):
            aftercare_db.normalize_overview_filters(delivery_date="2026-13-01")


def test_maintenance_text_fields_are_bounded(tmp_path):
    with opened_db(tmp_path) as db:
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, VIN, brand="BYD", hmac_secret=SECRET
        )
        db.commit()
        base = {
            "vehicle_id": vehicle["id"],
            "maintenance_date": "2026-09-08",
            "maintenance_type": "repair",
        }

        # Fields that used to accept unbounded input are now capped.
        for field, limit, offset in (
            ("summary", aftercare_db.MAINTENANCE_SUMMARY_LIMIT, 1),
            ("service_provider", aftercare_db.MAINTENANCE_PROVIDER_LIMIT, 1),
            ("source_reference", aftercare_db.MAINTENANCE_REFERENCE_LIMIT, 1),
            ("note", aftercare_db.MAINTENANCE_NOTE_LIMIT, 1),
        ):
            payload = dict(base, **{"summary": "ok", field: "x" * (limit + offset)})
            with pytest.raises(ValueError, match="Text field is too long"):
                aftercare_db.create_maintenance_record(db, **payload)

        record = aftercare_db.create_maintenance_record(
            db,
            **dict(base, summary="y" * aftercare_db.MAINTENANCE_SUMMARY_LIMIT),
        )
        assert len(record["summary"]) == aftercare_db.MAINTENANCE_SUMMARY_LIMIT

        with pytest.raises(ValueError, match="Text field is too long"):
            aftercare_db.update_maintenance_record(
                db,
                record["id"],
                maintenance_date=base["maintenance_date"],
                maintenance_type=base["maintenance_type"],
                summary="z" * (aftercare_db.MAINTENANCE_SUMMARY_LIMIT + 1),
            )

        # A rejected update must not have shortened or replaced the stored row.
        assert db.execute(
            "SELECT length(summary) FROM maintenance_records WHERE id = ?",
            (record["id"],),
        ).fetchone()[0] == aftercare_db.MAINTENANCE_SUMMARY_LIMIT
