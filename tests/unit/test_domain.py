"""Pure lifecycle, deadline, namespace and canonicalisation rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.core.canonical_json import CanonicalJsonError, canonical_json, canonical_sha256
from app.core.clock import ManualClock, format_instant, parse_instant
from app.core.graphemes import grapheme_clusters, grapheme_length
from app.domain.charge_state import (
    Actor,
    ChargeEvent,
    ChargeStatus,
    InvalidTransition,
    TransitionAccepted,
    actor_for,
    appears_in_history,
    appears_in_open,
    is_terminal,
    state_vectors,
    transition,
)
from app.domain.deadline import deadline_for, is_expired, seconds_until
from app.domain.namespace import (
    NamespaceError,
    database_name,
    fixture_namespace,
    normalize_base_url,
    remote_namespace,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestChargeStateMachine:
    def test_every_status_event_pair_has_a_defined_outcome(self) -> None:
        vectors = state_vectors()
        assert len(vectors) == len(ChargeStatus) * len(ChargeEvent) == 30
        assert all(vector["outcome"] in {"accepted", "invalidTransition"} for vector in vectors)

    @pytest.mark.parametrize(
        ("status", "event", "expected", "origin"),
        [
            (ChargeStatus.OUTSTANDING, ChargeEvent.ACCEPT, ChargeStatus.ACCEPTED, "student"),
            (ChargeStatus.OUTSTANDING, ChargeEvent.CONTEST, ChargeStatus.CONTESTED, None),
            (
                ChargeStatus.OUTSTANDING,
                ChargeEvent.DEADLINE_ELAPSED,
                ChargeStatus.ACCEPTED,
                "deadline",
            ),
            (ChargeStatus.ACCEPTED, ChargeEvent.PAY, ChargeStatus.PAID, None),
            (
                ChargeStatus.CONTESTED,
                ChargeEvent.OPERATOR_UPHOLD,
                ChargeStatus.ACCEPTED,
                "operator",
            ),
            (
                ChargeStatus.CONTESTED,
                ChargeEvent.OPERATOR_DISMISS,
                ChargeStatus.RESOLVED,
                None,
            ),
        ],
    )
    def test_legal_transitions(
        self,
        status: ChargeStatus,
        event: ChargeEvent,
        expected: ChargeStatus,
        origin: str | None,
    ) -> None:
        result = transition(status, event)
        assert isinstance(result, TransitionAccepted)
        assert result.to_status is expected
        assert (result.acceptance_origin.value if result.acceptance_origin else None) == origin

    @pytest.mark.parametrize("status", [ChargeStatus.PAID, ChargeStatus.RESOLVED])
    @pytest.mark.parametrize("event", list(ChargeEvent))
    def test_terminal_statuses_reject_every_event(self, status: ChargeStatus, event: ChargeEvent) -> None:
        result = transition(status, event)
        assert isinstance(result, InvalidTransition)
        assert is_terminal(status)

    def test_invalid_pairs_never_silently_no_op(self) -> None:
        result = transition(ChargeStatus.ACCEPTED, ChargeEvent.CONTEST)
        assert isinstance(result, InvalidTransition)
        assert result.from_status is ChargeStatus.ACCEPTED
        assert result.event is ChargeEvent.CONTEST

    def test_actor_permissions_are_partitioned(self) -> None:
        assert actor_for(ChargeEvent.ACCEPT) is Actor.STUDENT
        assert actor_for(ChargeEvent.DEADLINE_ELAPSED) is Actor.SYSTEM
        assert actor_for(ChargeEvent.OPERATOR_DISMISS) is Actor.OPERATOR

    def test_accepted_appears_in_both_hub_tabs(self) -> None:
        assert appears_in_open(ChargeStatus.ACCEPTED)
        assert appears_in_history(ChargeStatus.ACCEPTED)
        assert not appears_in_open(ChargeStatus.RESOLVED)
        assert not appears_in_history(ChargeStatus.OUTSTANDING)


class TestDeadlinePolicy:
    def test_deadline_is_raised_at_plus_grace_days(self) -> None:
        raised = parse_instant("2026-07-15T09:00:00Z")
        assert format_instant(deadline_for(raised, 30)) == "2026-08-14T09:00:00Z"

    def test_zero_grace_is_immediate(self) -> None:
        raised = parse_instant("2026-07-15T09:00:00Z")
        assert deadline_for(raised, 0) == raised

    def test_negative_grace_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            deadline_for(parse_instant("2026-07-15T09:00:00Z"), -1)

    def test_expiry_includes_the_boundary_instant(self) -> None:
        deadline = parse_instant("2026-08-01T12:00:00Z")
        assert not is_expired(deadline - timedelta(seconds=1), deadline)
        assert is_expired(deadline, deadline)
        assert is_expired(deadline + timedelta(seconds=1), deadline)

    def test_leap_day_and_month_rollover(self) -> None:
        raised = parse_instant("2028-01-30T23:30:00Z")
        assert format_instant(deadline_for(raised, 30)) == "2028-02-29T23:30:00Z"

    def test_seconds_until_never_goes_negative(self) -> None:
        deadline = parse_instant("2026-08-01T12:00:00Z")
        assert seconds_until(deadline - timedelta(minutes=5), deadline) == 300.0
        assert seconds_until(deadline + timedelta(minutes=5), deadline) == 0.0


class TestInstantFormatting:
    def test_round_trip(self) -> None:
        assert format_instant(parse_instant("2026-08-01T12:00:00Z")) == "2026-08-01T12:00:00Z"

    def test_naive_datetimes_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive datetimes"):
            format_instant(datetime(2026, 8, 1, 12, 0, 0))

    def test_offsets_other_than_z_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="'Z'"):
            parse_instant("2026-08-01T12:00:00+01:00")

    def test_milliseconds_are_preserved(self) -> None:
        moment = datetime(2026, 8, 1, 12, 0, 0, 123000, tzinfo=UTC)
        assert format_instant(moment) == "2026-08-01T12:00:00.123Z"

    def test_manual_clock_cannot_move_backwards(self) -> None:
        clock = ManualClock(parse_instant("2026-08-01T12:00:00Z"))
        with pytest.raises(ValueError, match="backwards"):
            clock.advance(timedelta(seconds=-1))


class TestCanonicalJson:
    def test_members_are_sorted_and_whitespace_free(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_nested_arrays_keep_order(self) -> None:
        assert canonical_json({"x": [3, 1, 2]}) == b'{"x":[3,1,2]}'

    def test_control_characters_use_short_escapes(self) -> None:
        assert canonical_json("a\nb\x01") == b'"a\\nb\\u0001"'

    def test_unicode_is_not_escaped(self) -> None:
        assert canonical_json("bücher") == '"bücher"'.encode()

    def test_floats_are_refused(self) -> None:
        with pytest.raises(CanonicalJsonError, match="floating point"):
            canonical_json({"amount": 25.0})

    def test_unsafe_integers_are_refused(self) -> None:
        with pytest.raises(CanonicalJsonError, match="safe range"):
            canonical_json(2**53)

    def test_digest_is_order_independent(self) -> None:
        assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})


class TestGraphemes:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", 0),
            ("abcdefghij", 10),
            ("é", 1),  # e + combining acute
            ("👍🏽", 1),  # emoji + skin-tone modifier
            ("👨‍👩‍👧", 1),  # ZWJ family
            ("🇬🇧", 1),  # regional indicator pair
            ("🇬🇧🇫🇷", 2),
            ("\r\n", 1),
            ("한", 1),
        ],
    )
    def test_cluster_counts(self, text: str, expected: int) -> None:
        assert grapheme_length(text) == expected

    def test_clusters_reassemble_into_the_original(self) -> None:
        text = "Damage é 👍🏽 was 🇬🇧 pre-existing"
        assert "".join(grapheme_clusters(text)) == text

    def test_contest_boundary_lengths(self) -> None:
        assert grapheme_length("a" * 9) == 9
        assert grapheme_length("a" * 10) == 10
        assert grapheme_length("e\u0301" * 2000) == 2000


class TestNamespace:
    def test_fixture_namespace_is_stable(self) -> None:
        assert fixture_namespace("1") == "fixture:v1"

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("http://127.0.0.1:8000", "http://127.0.0.1:8000/"),
            ("HTTP://127.0.0.1:8000", "http://127.0.0.1:8000"),
            ("https://api.example.com", "https://API.Example.com:443/"),
            ("https://api.example.com/kx/v1", "https://api.example.com/kx/v1/"),
            ("https://bücher.example", "https://xn--bcher-kva.example"),
        ],
    )
    def test_equivalent_urls_share_a_namespace(self, left: str, right: str) -> None:
        assert remote_namespace(base_url=left, api_major="1", schema_major="1") == remote_namespace(
            base_url=right, api_major="1", schema_major="1"
        )

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("http://127.0.0.1:8000", "http://127.0.0.1:8001"),
            ("https://a.example", "https://b.example"),
            ("https://api.example.com", "https://api.example.com/kx"),
            ("http://localhost", "https://localhost"),
        ],
    )
    def test_distinct_origins_do_not_collide(self, left: str, right: str) -> None:
        assert remote_namespace(base_url=left, api_major="1", schema_major="1") != remote_namespace(
            base_url=right, api_major="1", schema_major="1"
        )

    def test_default_ports_are_inserted(self) -> None:
        assert normalize_base_url("http://localhost").as_text() == "http://localhost:80"
        assert normalize_base_url("https://localhost").as_text() == "https://localhost:443"

    def test_ipv6_literal_keeps_brackets(self) -> None:
        assert normalize_base_url("http://[::1]:8000").as_text() == "http://[::1]:8000"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "ftp://example.com",
            "https://example.com?x=1",
            "https://example.com#f",
            "https://user:pass@example.com",
            "https://",
        ],
    )
    def test_unusable_urls_are_rejected(self, raw: str) -> None:
        with pytest.raises(NamespaceError):
            normalize_base_url(raw)

    def test_api_and_schema_major_participate_in_the_hash(self) -> None:
        base = "https://api.example.com"
        assert remote_namespace(base_url=base, api_major="1", schema_major="1") != remote_namespace(
            base_url=base, api_major="2", schema_major="1"
        )

    def test_database_name_is_derived_from_the_namespace(self) -> None:
        name = database_name("fixture:v1")
        assert name.startswith("kxinspections_")
        assert len(name) == len("kxinspections_") + 64


class TestExportedVectorsMatchTheImplementation:
    """The bundle the frontend replays must equal what this code actually does."""

    def test_state_vectors_file_matches_generated(self, tmp_path: Path) -> None:
        from scripts.export_fixtures import export

        manifest = export(tmp_path / "bundle")
        assert manifest["schemaVersion"] == "1"
        emitted = json.loads(
            (tmp_path / "bundle" / "vectors" / "charge_state_vectors.json").read_text("utf-8")
        )
        assert emitted == state_vectors()
