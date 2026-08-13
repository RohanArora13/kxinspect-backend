"""Store durability: restart, corruption, version gates, event ring and reset safety."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from app.core.clock import ManualClock, parse_instant
from app.core.config import EVENT_RING_CAPACITY, REFERENCE_NOW, RUNTIME_MARKER_UUID
from app.db.models import ChangeType, EventType, ResourceType, parse_cursor
from app.db.store import (
    CorruptRuntimeStateError,
    DraftEvent,
    IncompatibleRuntimeStateError,
    JsonStore,
    MutationOutcome,
    RuntimeLockedError,
    entity_list,
    find_entity,
)
from app.services.event_bus import replayable_events
from scripts.reset_runtime import UnsafeTargetError, reset

from tests.conftest import make_settings


def open_store(tmp_path: Path, **overrides: object) -> JsonStore:
    settings = make_settings(tmp_path, **overrides)
    store = JsonStore(settings, ManualClock(parse_instant(REFERENCE_NOW)))
    store.open()
    return store


def bump(entity_id: str) -> DraftEvent:
    return DraftEvent(
        type=EventType.CHARGE_UPDATED,
        entity_id=entity_id,
        resource_type=ResourceType.CHARGE,
        change_type=ChangeType.UPDATED,
    )


class TestLifecycle:
    def test_first_boot_seeds_and_persists(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            assert store.store_revision == 1
            assert (tmp_path / "runtime" / "state.json").is_file()
            assert (tmp_path / "runtime" / ".kxinspect-runtime").read_text().strip() == (RUNTIME_MARKER_UUID)
        finally:
            store.close()

    def test_second_boot_reuses_state_and_keeps_the_epoch(self, tmp_path: Path) -> None:
        first = open_store(tmp_path)
        epoch = first.state_epoch
        asyncio.run(first.commit(lambda snap: MutationOutcome(value=None, events=[bump("CHG-001")])))
        revision = first.store_revision
        first.close()

        second = open_store(tmp_path)
        try:
            assert second.state_epoch == epoch
            assert second.store_revision == revision
        finally:
            second.close()

    def test_second_boot_backfills_legacy_cost_breakdowns_without_reseed(self, tmp_path: Path) -> None:
        first = open_store(tmp_path)
        epoch = first.state_epoch
        first.close()
        path = tmp_path / "runtime" / "state.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for charge in payload["entities"]["charges"]:
            charge.pop("costBreakdown")
        path.write_text(json.dumps(payload), encoding="utf-8")

        second = open_store(tmp_path)
        try:
            restored = json.loads(path.read_text(encoding="utf-8"))
            assert second.state_epoch == epoch
            assert all(charge["costBreakdown"] for charge in restored["entities"]["charges"])
            wardrobe = next(charge for charge in restored["entities"]["charges"] if charge["id"] == "CHG-001")
            assert wardrobe["costBreakdown"][0]["label"] == "Replacement hinge set"
        finally:
            second.close()

    def test_a_second_process_cannot_open_the_same_runtime(self, tmp_path: Path) -> None:
        first = open_store(tmp_path)
        try:
            with pytest.raises(RuntimeLockedError):
                open_store(tmp_path)
        finally:
            first.close()

    def test_corrupt_state_is_refused_rather_than_reseeded(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.close()
        (tmp_path / "runtime" / "state.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(CorruptRuntimeStateError):
            open_store(tmp_path)

    def test_seed_version_mismatch_preserves_state_and_refuses_service(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.close()
        path = tmp_path / "runtime" / "state.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["seedVersion"] = "99"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(IncompatibleRuntimeStateError, match="reset_runtime"):
            open_store(tmp_path)
        assert json.loads(path.read_text(encoding="utf-8"))["seedVersion"] == "99"

    def test_schema_mismatch_refuses_startup(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.close()
        path = tmp_path / "runtime" / "state.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schemaVersion"] = "2"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(IncompatibleRuntimeStateError):
            open_store(tmp_path)

    def test_state_referencing_a_missing_attachment_is_refused(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.close()
        path = tmp_path / "runtime" / "state.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["entities"]["charges"][0]["photos"].append(
            {
                "id": "missing",
                "url": "/api/v1/attachments/att_missing",
                "thumbnailUrl": "/api/v1/attachments/att_missing",
                "mediaType": "image/png",
                "width": 1,
                "height": 1,
                "altKey": "x",
                "sortOrder": 9,
            }
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(CorruptRuntimeStateError, match="att_missing"):
            open_store(tmp_path)


class TestCommit:
    def test_a_raising_mutator_leaves_state_untouched(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            before = asyncio.run(store.snapshot())

            def explode(_: object) -> MutationOutcome[None]:
                raise RuntimeError("boom")

            with pytest.raises(RuntimeError, match="boom"):
                asyncio.run(store.commit(explode))
            assert asyncio.run(store.snapshot()) == before
        finally:
            store.close()

    def test_mutations_are_isolated_until_they_commit(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:

            def rename_then_fail(snapshot: object) -> MutationOutcome[None]:
                entity_list(snapshot, "charges")[0]["itemName"] = "MUTATED"  # type: ignore[arg-type]
                raise RuntimeError("late failure")

            with pytest.raises(RuntimeError):
                asyncio.run(store.commit(rename_then_fail))
            live = asyncio.run(store.snapshot())
            assert find_entity(live, "charges", "CHG-001")["itemName"] == "Wardrobe"  # type: ignore[index]
        finally:
            store.close()

    def test_persist_false_does_not_advance_the_revision(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            revision = store.store_revision
            asyncio.run(store.commit(lambda _: MutationOutcome(value=1, persist=False)))
            assert store.store_revision == revision
        finally:
            store.close()

    def test_deferred_error_is_raised_after_the_state_is_durable(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:

            def reconcile_then_conflict(snapshot: object) -> MutationOutcome[None]:
                entity_list(snapshot, "charges")[0]["status"] = "accepted"  # type: ignore[arg-type]
                return MutationOutcome(
                    value=None,
                    events=[bump("CHG-001")],
                    deferred_error=RuntimeError("409"),
                )

            with pytest.raises(RuntimeError, match="409"):
                asyncio.run(store.commit(reconcile_then_conflict))
            persisted = json.loads((tmp_path / "runtime" / "state.json").read_text("utf-8"))
            assert persisted["entities"]["charges"][0]["status"] == "accepted"
        finally:
            store.close()

    def test_events_are_numbered_within_one_revision(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            result = asyncio.run(
                store.commit(lambda _: MutationOutcome(value=None, events=[bump("A"), bump("B")]))
            )
            ids = [event["id"] for event in result.events]
            parsed = [parse_cursor(item) for item in ids]
            assert [item[2] for item in parsed] == [1, 2]  # type: ignore[index]
            assert {item[1] for item in parsed} == {store.store_revision}  # type: ignore[index]
        finally:
            store.close()


class TestEventRing:
    def test_ring_is_bounded_and_records_what_it_evicted(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            for index in range(EVENT_RING_CAPACITY + 5):
                asyncio.run(
                    store.commit(
                        lambda _, index=index: MutationOutcome(value=None, events=[bump(f"CHG-{index}")])
                    )
                )
            snapshot = asyncio.run(store.snapshot())
            assert len(snapshot["events"]) == EVENT_RING_CAPACITY
            evicted = parse_cursor(snapshot["streamEvictedThrough"])
            assert evicted is not None and evicted[1] > 0
        finally:
            store.close()

    def test_baseline_cursor_is_servable_while_nothing_was_evicted(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            asyncio.run(store.commit(lambda _: MutationOutcome(value=None, events=[bump("A")])))
            snapshot = asyncio.run(store.snapshot())
            replay, servable = replayable_events(
                events=snapshot["events"],
                stream_epoch=snapshot["streamEpoch"],
                evicted_through=snapshot["streamEvictedThrough"],
                cursor=f"{snapshot['streamEpoch']}:0:0",
            )
            assert servable
            assert len(replay) == 1
        finally:
            store.close()

    def test_evicted_cursor_is_not_servable(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            for index in range(EVENT_RING_CAPACITY + 3):
                asyncio.run(
                    store.commit(
                        lambda _, index=index: MutationOutcome(value=None, events=[bump(f"CHG-{index}")])
                    )
                )
            snapshot = asyncio.run(store.snapshot())
            _, servable = replayable_events(
                events=snapshot["events"],
                stream_epoch=snapshot["streamEpoch"],
                evicted_through=snapshot["streamEvictedThrough"],
                cursor=f"{snapshot['streamEpoch']}:0:0",
            )
            assert not servable
        finally:
            store.close()

    @pytest.mark.parametrize("cursor", ["not-a-cursor", "epoch:one:two", "other-epoch:1:1"])
    def test_unparseable_or_foreign_cursors_are_not_servable(self, tmp_path: Path, cursor: str) -> None:
        store = open_store(tmp_path)
        try:
            snapshot = asyncio.run(store.snapshot())
            _, servable = replayable_events(
                events=snapshot["events"],
                stream_epoch=snapshot["streamEpoch"],
                evicted_through=snapshot["streamEvictedThrough"],
                cursor=cursor,
            )
            assert not servable
        finally:
            store.close()


class TestReset:
    def test_reset_rotates_both_epochs_and_clears_the_ledger(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        try:
            asyncio.run(store.commit(lambda _: MutationOutcome(value=None, events=[bump("A")])))
            before_state = store.state_epoch
            before_stream = store.stream_epoch
            fresh = asyncio.run(store.reset())
            assert fresh["stateEpoch"] != before_state
            assert fresh["streamEpoch"] != before_stream
            assert fresh["events"] == []
            assert fresh["idempotency"] == {}
            assert fresh["storeRevision"] == 1
        finally:
            store.close()


class TestResetScriptSafety:
    def test_relative_paths_are_refused(self) -> None:
        with pytest.raises(UnsafeTargetError, match="absolute"):
            reset(Path("runtime"))

    def test_missing_marker_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "victim").mkdir()
        with pytest.raises(UnsafeTargetError, match="marker"):
            reset(tmp_path / "victim")

    def test_wrong_marker_uuid_is_refused(self, tmp_path: Path) -> None:
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / ".kxinspect-runtime").write_text("11111111-2222-4333-8444-555555555555", encoding="utf-8")
        with pytest.raises(UnsafeTargetError, match="marker uuid"):
            reset(victim)

    def test_symlinked_target_is_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (real / ".kxinspect-runtime").write_text(RUNTIME_MARKER_UUID, encoding="utf-8")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(UnsafeTargetError, match="symlink"):
            reset(link)

    def test_home_directory_is_refused(self) -> None:
        with pytest.raises(UnsafeTargetError):
            reset(Path.home())

    def test_valid_runtime_root_removes_only_named_children(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.close()
        runtime = (tmp_path / "runtime").resolve()
        (runtime / "keep-me.txt").write_text("user file", encoding="utf-8")
        removed = reset(runtime)
        assert "state.json" in removed
        assert not (runtime / "state.json").exists()
        assert (runtime / "keep-me.txt").is_file()
