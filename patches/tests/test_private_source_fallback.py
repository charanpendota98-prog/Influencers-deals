"""Private Telegram invite labels must never silently disable ingestion."""
from __future__ import annotations

import asyncio
import logging
import urllib.request
from types import SimpleNamespace
from unittest.mock import AsyncMock

from influencer_hub import config, puller


def _dialog(
    peer_id: int,
    title: str,
    *,
    username: str = "",
    creator: bool = False,
    kind: str = "group",
    latest_text: str = "latest deal",
):
    entity = SimpleNamespace(
        id=peer_id,
        title=title,
        username=username,
        megagroup=kind == "group",
        broadcast=kind == "channel",
        creator=creator,
    )
    latest = SimpleNamespace(id=1, message=latest_text, text=latest_text)
    return SimpleNamespace(
        id=-1000000000 - peer_id,
        entity=entity,
        name=title,
        username=username,
        is_user=False,
        is_bot=False,
        is_group=kind == "group",
        is_channel=kind == "channel",
        message=latest,
    )


def _private_entry(name: str = "Priority Source", suffix: str = "one") -> dict[str, str]:
    return {
        "name": name,
        "spec": f"https://t.me/+private-invite-{suffix}",
        "kind": "production",
    }


def _clear_fallback_warning_cache():
    puller._WARNED_PRIVATE_FALLBACKS.clear()


def test_named_unmatched_private_invites_fall_back_to_all_236_joined_dialogs(monkeypatch, caplog):
    dialogs = [_dialog(index, f"Joined Deal Group {index}") for index in range(236)]
    entries = [_private_entry("Priority Source", str(index)) for index in range(3)]
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))
    _clear_fallback_warning_cache()
    caplog.set_level(logging.WARNING, logger="influencer_hub.puller")

    selection = puller._source_selection(dialogs, entries)

    assert len(selection["selected"]) == 236
    assert selection["selection_mode"] == "joined_dialog_fallback"
    assert selection["fallback_reason"] == "private_invite_label_unmatched"
    assert len(selection["unmatched_private_invites"]) == 3
    assert "No joined dialog matched 3 private invite source label(s)" in caplog.text
    assert "236 eligible group/channel(s)" in caplog.text
    assert "private-invite-one" not in caplog.text  # never leak invite hashes


def test_private_invite_fallback_excludes_configured_output_dialogs(monkeypatch):
    dialogs = [
        _dialog(101, "Source group"),
        _dialog(102, "Our broadcast", username="our_broadcast"),
    ]
    monkeypatch.setattr(
        puller,
        "_excluded_output_keys",
        lambda: ({"our_broadcast"}, {"-1000000102", "102"}),
    )

    selected = puller._joined_source_dialogs(dialogs, [_private_entry()])

    assert [dialog.name for dialog in selected] == ["Source group"]


def test_private_invite_fallback_excludes_our_own_channels_unless_explicitly_selected(monkeypatch):
    dialogs = [
        _dialog(201, "Community deals"),
        _dialog(202, "Our own channel", creator=True, kind="channel"),
    ]
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))

    selected = puller._joined_source_dialogs(dialogs, [_private_entry()])
    explicitly_selected = puller._joined_source_dialogs(
        dialogs,
        [{"name": "Our own channel", "spec": "https://t.me/+own-invite", "kind": "production"}],
    )

    assert [dialog.name for dialog in selected] == ["Community deals"]
    # Explicitly configuring an owned dialog as a source remains an intentional opt-in.
    assert [dialog.name for dialog in explicitly_selected] == ["Our own channel"]


def test_exact_private_dialog_title_matches_only_that_joined_dialog(monkeypatch, caplog):
    dialogs = [_dialog(301, "Priority Source"), _dialog(302, "Unrelated deals")]
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))
    _clear_fallback_warning_cache()
    caplog.set_level(logging.WARNING, logger="influencer_hub.puller")

    selection = puller._source_selection(dialogs, [_private_entry("priority-source")])

    assert [dialog.name for dialog in selection["selected"]] == ["Priority Source"]
    assert selection["selection_mode"] == "configured_selectors"
    assert selection["matched_selectors"] == 1
    assert not caplog.text


def test_public_selector_mismatch_does_not_trigger_broad_fallback(monkeypatch):
    dialogs = [_dialog(401, "Joined but not configured")]
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))

    selection = puller._source_selection(
        dialogs,
        [{"name": "Missing public source", "spec": "@not_joined_source", "kind": "production"}],
    )

    assert selection["selected"] == []
    assert selection["selection_mode"] == "configured_selectors"
    assert not selection["unmatched_private_invites"]


def test_fallback_warning_is_emitted_once_per_source_configuration(monkeypatch, caplog):
    dialogs = [_dialog(501, "Joined deal group")]
    entries = [_private_entry()]
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))
    _clear_fallback_warning_cache()
    caplog.set_level(logging.WARNING, logger="influencer_hub.puller")

    puller._source_selection(dialogs, entries)
    first_warning_count = caplog.text.count("No joined dialog matched")
    puller._source_selection(dialogs, entries)
    second_warning_count = caplog.text.count("No joined dialog matched")

    assert first_warning_count == 1
    assert second_warning_count == first_warning_count


def test_source_inspection_reports_fallback_without_invite_or_history_requests(monkeypatch):
    dialogs = [_dialog(601, "Joined one"), _dialog(602, "Joined two", kind="channel")]
    calls = []

    class JoinedDialogsOnlyClient:
        def is_connected(self):
            return True

        async def is_user_authorized(self):
            return True

        async def iter_dialogs(self):
            calls.append("iter_dialogs")
            for dialog in dialogs:
                yield dialog

    monkeypatch.setattr(puller.telegram_ops, "_client", lambda: JoinedDialogsOnlyClient())
    monkeypatch.setattr(puller, "_source_entries", lambda use_dummy=False: [_private_entry()])
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))
    _clear_fallback_warning_cache()

    report = asyncio.run(puller.inspect_source_selection())

    assert report["ok"] is True
    assert report["authorized"] is True
    assert report["configured_sources"] == 1
    assert report["joined_group_channels"] == 2
    assert report["eligible_joined_dialogs"] == 2
    assert report["selected_sources"] == 2
    assert report["unresolved_private_invites"] == 1
    assert report["selection_mode"] == "joined_dialog_fallback"
    assert report["fallback_reason"] == "private_invite_label_unmatched"
    assert calls == ["iter_dialogs"]


def test_worker_puller_reads_messages_from_fallback_dialogs(monkeypatch):
    dialogs = [_dialog(701, "Joined deals A"), _dialog(702, "Joined deals B")]
    read_entities = []

    class FakeClient:
        def is_connected(self):
            return True

        async def iter_dialogs(self):
            for dialog in dialogs:
                yield dialog

        async def iter_messages(self, entity, limit=None, **_kwargs):
            read_entities.append(entity)
            yield SimpleNamespace(id=1, message=f"Deal from {entity.title}", text="")

    monkeypatch.setattr(puller.telegram_ops, "_client", lambda: FakeClient())
    monkeypatch.setattr(puller, "_source_entries", lambda use_dummy=False: [_private_entry()])
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))
    monkeypatch.setattr(puller.db, "get_worker_offset", lambda _key: 0)
    monkeypatch.setattr(config, "TELEGRAM_SOURCE_REQUEST_SPACING", 0)
    _clear_fallback_warning_cache()

    records = asyncio.run(puller.pull_new_deals(limit=10))

    assert [record["source"] for record in records] == ["Joined deals A", "Joined deals B"]
    assert [record["text"] for record in records] == ["Deal from Joined deals A", "Deal from Joined deals B"]
    assert read_entities == [dialog.entity for dialog in dialogs]


def test_dialog_summaries_are_secret_free_and_capped(monkeypatch):
    dialogs = [
        _dialog(800 + index, f"Joined Group {index}", username=f"joined_{index}")
        for index in range(60)
    ]

    class FakeClient:
        def is_connected(self):
            return True

        async def is_user_authorized(self):
            return True

        async def iter_dialogs(self):
            for dialog in dialogs:
                yield dialog

    monkeypatch.setattr(puller.telegram_ops, "_client", lambda: FakeClient())
    monkeypatch.setattr(puller, "_source_entries", lambda use_dummy=False: [_private_entry()])
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))
    _clear_fallback_warning_cache()
    report = asyncio.run(puller.inspect_source_selection(include_dialog_names=True))

    assert report["selected_sources"] == 60
    assert len(report["selected_dialogs"]) == 50
    assert report["selected_dialogs_truncated"] == 10
    first = report["selected_dialogs"][0]
    assert first["name"] == "Joined Group 0"
    assert first["username"] == "joined_0"
    assert first["kind"] == "group"
    assert "t.me" not in str(first)  # never surface an invite


def test_cli_doctor_can_report_private_invite_fallback_without_leaking_invites(monkeypatch, capsys):
    from influencer_hub import cli

    class HealthyResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    report = {
        "ok": True,
        "authorized": True,
        "configured_sources": 3,
        "selected_sources": 236,
        "unresolved_private_invites": 3,
        "selection_mode": "joined_dialog_fallback",
        "fallback_reason": "private_invite_label_unmatched",
    }
    monkeypatch.setattr(config, "EARNKARO_API_KEY", "test-key")
    monkeypatch.setattr(config, "EARNKARO_PUBLISHER_ID", "test-publisher")
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "test-api-hash")
    monkeypatch.setattr(config, "TELEGRAM_SESSION", "test-session")
    monkeypatch.setattr(config, "WA_HUB_URL", "http://wa-hub.test")
    monkeypatch.setattr(puller, "inspect_source_selection", AsyncMock(return_value=report))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: HealthyResponse())
    args = cli.build_parser().parse_args(["doctor", "--telegram-sources"])

    result = cli._cmd_doctor(args)

    output = capsys.readouterr().out
    assert result == 0
    assert "Telegram source selection (236 selected / 3 configured)" in output
    assert "3 private invite label(s) did not match" in output
    assert "did not check or join invites" in output
    assert "test-invite" not in output
