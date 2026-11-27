from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.enums import TEXT_MAX_LENGTH
from app.models.transcription import Transcription, TranscriptionSegment
from app.models.user import User

BASE = "/api/v1/transcriptions"


async def _create(client: AsyncClient, headers: dict[str, str], **payload: Any) -> dict[str, Any]:
    payload.setdefault("text", "hello world")
    response = await client.post(BASE, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------- Protected endpoints ----------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", BASE),
        ("GET", BASE),
        ("GET", f"{BASE}/{uuid.uuid4()}"),
        ("PATCH", f"{BASE}/{uuid.uuid4()}"),
        ("DELETE", f"{BASE}/{uuid.uuid4()}"),
    ],
)
async def test_endpoints_require_authentication(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path, json={"text": "x"})
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


# ---------- Create ----------


async def test_create_transcription(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    body = await _create(
        client,
        auth_headers,
        title="  Standup notes ",
        text="  aaj ki meeting cancel ho gayi  ",
        speech_mode="mixed",
        script_preference="roman",
    )
    assert body["title"] == "Standup notes"
    assert body["text"] == "aaj ki meeting cancel ho gayi"
    assert body["speech_mode"] == "mixed"
    assert body["script_preference"] == "roman"
    assert body["source"] == "manual"
    assert body["status"] == "completed"
    assert body["is_edited"] is False
    assert body["segments"] is None
    assert body["created_at"] == body["updated_at"]
    uuid.UUID(body["id"])


async def test_create_uses_defaults(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    body = await _create(client, auth_headers, text="only text")
    assert body["title"].startswith("Transcription - ")
    assert body["title"].endswith(" UTC")
    assert body["speech_mode"] == "auto"
    assert body["script_preference"] == "as_recognised"


async def test_create_preserves_unicode(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    text = "आज की meeting cancel हो गई 🙂"
    body = await _create(client, auth_headers, title="हिंदी", text=text)
    fetched = await client.get(f"{BASE}/{body['id']}", headers=auth_headers)
    assert fetched.json()["text"] == text
    assert fetched.json()["title"] == "हिंदी"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": "   "},
        {"text": "x" * (TEXT_MAX_LENGTH + 1)},
        {"text": "ok", "title": "   "},
        {"text": "ok", "title": "x" * 201},
        {"text": "ok", "speech_mode": "french"},
        {"text": "ok", "script_preference": "klingon"},
        {"text": "ok", "source": "live"},
        {"text": "ok", "user_id": str(uuid.uuid4())},
    ],
    ids=[
        "missing-text",
        "blank-text",
        "text-too-long",
        "blank-title",
        "title-too-long",
        "bad-speech-mode",
        "bad-script",
        "readonly-source",
        "foreign-user-id",
    ],
)
async def test_create_validation(
    client: AsyncClient, auth_headers: dict[str, str], payload: dict[str, Any]
) -> None:
    response = await client.post(BASE, json=payload, headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_create_accepts_max_length_text(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    body = await _create(client, auth_headers, text="y" * TEXT_MAX_LENGTH)
    assert len(body["text"]) == TEXT_MAX_LENGTH


# ---------- List ----------


async def test_list_empty(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.get(BASE, headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 20, "pages": 0}


async def test_list_newest_first_with_pagination(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    ids = [(await _create(client, auth_headers, title=f"T{i}"))["id"] for i in range(5)]

    page1 = (await client.get(BASE, params={"page_size": 2}, headers=auth_headers)).json()
    page3 = (
        await client.get(BASE, params={"page_size": 2, "page": 3}, headers=auth_headers)
    ).json()
    page4 = (
        await client.get(BASE, params={"page_size": 2, "page": 4}, headers=auth_headers)
    ).json()

    assert page1["total"] == 5
    assert page1["pages"] == 3
    assert [item["id"] for item in page1["items"]] == [ids[4], ids[3]]
    assert [item["id"] for item in page3["items"]] == [ids[0]]
    assert page4["items"] == []
    assert page4["total"] == 5


async def test_list_items_have_preview_not_full_text(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _create(client, auth_headers, text="word " * 100)
    await _create(client, auth_headers, text="short one")
    items = (await client.get(BASE, headers=auth_headers)).json()["items"]

    assert "text" not in items[0]
    assert items[0]["preview"] == "short one"
    assert items[1]["preview"].endswith("…")
    assert len(items[1]["preview"]) <= 201


async def test_list_search_title_and_text_case_insensitive(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _create(client, auth_headers, title="Project Alpha", text="kickoff")
    await _create(client, auth_headers, title="Groceries", text="buy ALPHA-numeric labels")
    await _create(client, auth_headers, title="Other", text="nothing here")

    response = await client.get(BASE, params={"q": "alpha"}, headers=auth_headers)
    assert response.json()["total"] == 2
    blank = await client.get(BASE, params={"q": "   "}, headers=auth_headers)
    assert blank.json()["total"] == 3


async def test_list_search_treats_wildcards_literally(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _create(client, auth_headers, text="we are 100% done")
    await _create(client, auth_headers, text="we are 100 done")
    await _create(client, auth_headers, text="file_name here")
    await _create(client, auth_headers, text="filename here")

    percent = (await client.get(BASE, params={"q": "100%"}, headers=auth_headers)).json()
    underscore = (await client.get(BASE, params={"q": "file_"}, headers=auth_headers)).json()
    assert [i["preview"] for i in percent["items"]] == ["we are 100% done"]
    assert [i["preview"] for i in underscore["items"]] == ["file_name here"]


async def test_list_filter_by_speech_mode(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _create(client, auth_headers, speech_mode="mixed")
    await _create(client, auth_headers, speech_mode="en")
    response = await client.get(BASE, params={"speech_mode": "mixed"}, headers=auth_headers)
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["speech_mode"] == "mixed"


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"q": "x" * 201}, {"status": "nope"}],
)
async def test_list_invalid_query(
    client: AsyncClient, auth_headers: dict[str, str], params: dict[str, Any]
) -> None:
    response = await client.get(BASE, params=params, headers=auth_headers)
    assert response.status_code == 422


# ---------- Get ----------


async def test_get_transcription(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    created = await _create(client, auth_headers, title="Detail", text="full text")
    response = await client.get(f"{BASE}/{created['id']}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == created


async def test_get_with_segments_in_sequence_order(
    client: AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await _create(client, auth_headers)
    transcription_id = uuid.UUID(created["id"])
    async with session_factory() as session:
        for seq in (2, 0, 1):  # inserted out of order on purpose
            session.add(
                TranscriptionSegment(
                    transcription_id=transcription_id,
                    seq=seq,
                    start_ms=seq * 1000,
                    end_ms=seq * 1000 + 900,
                    raw_text=f"segment {seq}",
                    language="hi",
                    avg_logprob=-0.2,
                )
            )
        await session.commit()

    with_segments = await client.get(
        f"{BASE}/{created['id']}", params={"include_segments": "true"}, headers=auth_headers
    )
    segments = with_segments.json()["segments"]
    assert [s["seq"] for s in segments] == [0, 1, 2]
    assert segments[1] == {
        "seq": 1,
        "start_ms": 1000,
        "end_ms": 1900,
        "raw_text": "segment 1",
        "language": "hi",
        "avg_logprob": -0.2,
    }

    without = await client.get(f"{BASE}/{created['id']}", headers=auth_headers)
    assert without.json()["segments"] is None


async def test_get_missing_returns_404(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.get(f"{BASE}/{uuid.uuid4()}", headers=auth_headers)
    assert response.status_code == 404
    assert response.json() == {
        "detail": "Transcription not found",
        "code": "transcription_not_found",
    }


async def test_get_invalid_uuid_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get(f"{BASE}/not-a-uuid", headers=auth_headers)
    assert response.status_code == 422


# ---------- Update ----------


async def test_update_title_only(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    created = await _create(client, auth_headers, title="Old", text="keep me")
    response = await client.patch(
        f"{BASE}/{created['id']}", json={"title": " New "}, headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "New"
    assert body["text"] == "keep me"
    assert body["is_edited"] is False
    assert datetime.fromisoformat(body["updated_at"]) > datetime.fromisoformat(
        created["updated_at"]
    )


async def test_update_text_marks_edited(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    created = await _create(client, auth_headers, title="Same", text="first draft")
    response = await client.patch(
        f"{BASE}/{created['id']}", json={"text": "corrected text"}, headers=auth_headers
    )
    body = response.json()
    assert response.status_code == 200
    assert body["text"] == "corrected text"
    assert body["title"] == "Same"
    assert body["is_edited"] is True

    persisted = (await client.get(f"{BASE}/{created['id']}", headers=auth_headers)).json()
    assert persisted == body


async def test_update_both_fields(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    created = await _create(client, auth_headers)
    response = await client.patch(
        f"{BASE}/{created['id']}", json={"title": "T", "text": "X"}, headers=auth_headers
    )
    assert (response.json()["title"], response.json()["text"]) == ("T", "X")


async def test_update_with_identical_text_is_not_an_edit(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await _create(client, auth_headers, text="unchanged")
    response = await client.patch(
        f"{BASE}/{created['id']}", json={"text": "unchanged"}, headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["is_edited"] is False
    assert response.json()["updated_at"] == created["updated_at"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": None},
        {"text": None},
        {"title": "  "},
        {"text": ""},
        {"status": "recording"},
        {"is_edited": False},
    ],
    ids=["empty", "null-title", "null-text", "blank-title", "empty-text", "status", "is-edited"],
)
async def test_update_validation(
    client: AsyncClient, auth_headers: dict[str, str], payload: dict[str, Any]
) -> None:
    created = await _create(client, auth_headers)
    response = await client.patch(f"{BASE}/{created['id']}", json=payload, headers=auth_headers)
    assert response.status_code == 422


async def test_update_missing_returns_404(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.patch(
        f"{BASE}/{uuid.uuid4()}", json={"title": "x"}, headers=auth_headers
    )
    assert response.status_code == 404


# ---------- Delete ----------


async def test_delete_transcription_and_its_segments(
    client: AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await _create(client, auth_headers)
    async with session_factory() as session:
        session.add(
            TranscriptionSegment(
                transcription_id=uuid.UUID(created["id"]),
                seq=0,
                start_ms=0,
                end_ms=500,
                raw_text="hi",
            )
        )
        await session.commit()

    response = await client.delete(f"{BASE}/{created['id']}", headers=auth_headers)
    assert response.status_code == 204
    assert response.content == b""

    assert (await client.get(f"{BASE}/{created['id']}", headers=auth_headers)).status_code == 404
    again = await client.delete(f"{BASE}/{created['id']}", headers=auth_headers)
    assert again.status_code == 404

    async with session_factory() as session:
        remaining = await session.scalar(select(func.count()).select_from(TranscriptionSegment))
    assert remaining == 0


async def test_deleting_user_cascades_to_transcriptions(
    client: AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create(client, auth_headers)
    async with session_factory() as session:
        user = (await session.execute(select(User))).scalar_one()
        await session.delete(user)
        await session.commit()
        remaining = await session.scalar(select(func.count()).select_from(Transcription))
    assert remaining == 0
