"""Unit tests for `notify_admins_user_created` fanout logic.

No DB: a fake session captures the rows the helper would add and
`_load_admin_users` is monkeypatched so we exercise the pure branching
(fanout, skip-self, no-admins, text format/truncation) fast and offline.
"""

import uuid

import pytest

from app.services import admin_notification_service as svc


class _FakeSession:
    """Captures `db.add(...)` calls; the helper does no other DB I/O once
    `_load_admin_users` is stubbed."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)


class _FakeUser:
    def __init__(
        self,
        *,
        id: uuid.UUID | None = None,
        handle: str | None = None,
        display_name: str = "Display Name",
    ) -> None:
        self.id = id or uuid.uuid4()
        self.handle = handle
        self.display_name = display_name


def _patch_admins(monkeypatch, admins: list[_FakeUser]) -> None:
    async def _fake_load(_db):
        return admins

    monkeypatch.setattr(svc, "_load_admin_users", _fake_load)


@pytest.mark.asyncio
async def test_fanout_one_notification_per_admin(monkeypatch):
    admin_a, admin_b = _FakeUser(), _FakeUser()
    _patch_admins(monkeypatch, [admin_a, admin_b])
    db = _FakeSession()
    new_user = _FakeUser(handle="nuevo")

    await svc.notify_admins_user_created(db, new_user)

    assert len(db.added) == 2
    recipients = {n.recipient_user_id for n in db.added}
    assert recipients == {admin_a.id, admin_b.id}
    for n in db.added:
        assert n.kind == "user_created"
        # Actor y target apuntan al usuario nuevo → el click abre su perfil.
        assert n.actor_user_id == new_user.id
        assert n.target_user_id == new_user.id


@pytest.mark.asyncio
async def test_skips_self_when_admin_is_the_new_user(monkeypatch):
    shared_id = uuid.uuid4()
    self_admin = _FakeUser(id=shared_id)
    other_admin = _FakeUser()
    _patch_admins(monkeypatch, [self_admin, other_admin])
    db = _FakeSession()
    new_user = _FakeUser(id=shared_id, handle="adminself")

    await svc.notify_admins_user_created(db, new_user)

    assert len(db.added) == 1
    assert db.added[0].recipient_user_id == other_admin.id


@pytest.mark.asyncio
async def test_no_admins_no_inserts(monkeypatch):
    _patch_admins(monkeypatch, [])
    db = _FakeSession()

    await svc.notify_admins_user_created(db, _FakeUser(handle="x"))

    assert db.added == []


@pytest.mark.asyncio
async def test_text_prefers_handle_then_display_name(monkeypatch):
    _patch_admins(monkeypatch, [_FakeUser()])

    db = _FakeSession()
    await svc.notify_admins_user_created(db, _FakeUser(handle="mariap"))
    assert db.added[0].text == "Nuevo usuario registrado: @mariap"

    db = _FakeSession()
    await svc.notify_admins_user_created(
        db, _FakeUser(handle=None, display_name="María Pérez")
    )
    assert db.added[0].text == "Nuevo usuario registrado: @María Pérez"


@pytest.mark.asyncio
async def test_text_truncated_to_500_chars(monkeypatch):
    _patch_admins(monkeypatch, [_FakeUser()])
    db = _FakeSession()

    await svc.notify_admins_user_created(db, _FakeUser(handle="h" * 600))

    text = db.added[0].text
    # Truncado a [:497] + "…" → 498 chars, dentro del cap de 500 de la
    # columna `notifications.text` (mismo patrón que category_pending).
    assert len(text) <= 500
    assert len(text) == 498
    assert text.endswith("…")
