"""User data isolation: a user can never see, change or delete another user's data, and
cannot tell whether another user's transcription ID exists (404, never 403)."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

BASE = "/api/v1/transcriptions"


@pytest.fixture
async def two_users(auth_headers_for: Any) -> tuple[dict[str, str], dict[str, str]]:
    alice = await auth_headers_for("alice@example.com")
    bob = await auth_headers_for("bob@example.com")
    return alice, bob


async def _create(client: AsyncClient, headers: dict[str, str], text: str) -> dict[str, Any]:
    response = await client.post(BASE, json={"title": text, "text": text}, headers=headers)
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    return body


async def test_list_only_shows_own_transcriptions(
    client: AsyncClient, two_users: tuple[dict[str, str], dict[str, str]]
) -> None:
    alice, bob = two_users
    await _create(client, alice, "alice secret plan")
    await _create(client, alice, "alice second note")
    await _create(client, bob, "bob note")

    alice_list = (await client.get(BASE, headers=alice)).json()
    bob_list = (await client.get(BASE, headers=bob)).json()
    assert alice_list["total"] == 2
    assert bob_list["total"] == 1
    assert [item["title"] for item in bob_list["items"]] == ["bob note"]


async def test_search_does_not_leak_other_users_data(
    client: AsyncClient, two_users: tuple[dict[str, str], dict[str, str]]
) -> None:
    alice, bob = two_users
    await _create(client, alice, "alice secret plan")
    response = await client.get(BASE, params={"q": "secret"}, headers=bob)
    assert response.json()["total"] == 0


async def test_cannot_read_update_or_delete_another_users_transcription(
    client: AsyncClient, two_users: tuple[dict[str, str], dict[str, str]]
) -> None:
    alice, bob = two_users
    created = await _create(client, alice, "alice private")
    url = f"{BASE}/{created['id']}"

    get_response = await client.get(url, headers=bob)
    patch_response = await client.patch(url, json={"text": "hacked"}, headers=bob)
    delete_response = await client.delete(url, headers=bob)

    for response in (get_response, patch_response, delete_response):
        assert response.status_code == 404
        assert response.json()["code"] == "transcription_not_found"

    # Alice's data is untouched.
    still_there = await client.get(url, headers=alice)
    assert still_there.status_code == 200
    assert still_there.json() == created


async def test_foreign_and_missing_ids_are_indistinguishable(
    client: AsyncClient, two_users: tuple[dict[str, str], dict[str, str]]
) -> None:
    alice, bob = two_users
    created = await _create(client, alice, "alice private")
    foreign = await client.get(f"{BASE}/{created['id']}", headers=bob)
    missing = await client.get(f"{BASE}/00000000-0000-0000-0000-000000000000", headers=bob)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


async def test_new_transcriptions_belong_to_the_caller(
    client: AsyncClient, two_users: tuple[dict[str, str], dict[str, str]]
) -> None:
    alice, bob = two_users
    created = await _create(client, bob, "bob's own")
    assert (await client.get(f"{BASE}/{created['id']}", headers=bob)).status_code == 200
    assert (await client.get(f"{BASE}/{created['id']}", headers=alice)).status_code == 404
