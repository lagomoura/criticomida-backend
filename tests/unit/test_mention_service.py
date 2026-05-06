"""Unit tests para ``extract_handles`` — la regex es la frontera entre 'texto
con mención' y 'texto que no'. Anti-email es lo más sutil y lo cubrimos
explícitamente."""

from app.services.mention_service import extract_handles


def test_empty_and_none_return_empty_list():
    assert extract_handles("") == []
    assert extract_handles(None) == []  # type: ignore[arg-type]


def test_single_mention():
    assert extract_handles("hola @bob") == ["bob"]


def test_mention_at_start():
    assert extract_handles("@alice probaste el sushi?") == ["alice"]


def test_does_not_match_email():
    # Boundary izquierdo previene match en `foo@bar.com`. Si el regex matcheara
    # `@bar`, mandaríamos notif a un usuario aleatorio cada vez que alguien
    # incluya su email en un comentario.
    assert extract_handles("escribime a foo@bar.com") == []


def test_dedup_is_case_insensitive():
    # Mantiene el casing original de la PRIMERA aparición (UX: si el usuario
    # eligió @Bob, mostramos eso, no @bob).
    assert extract_handles("@Bob @bob @BOB") == ["Bob"]


def test_max_length_30_chars():
    # 30 chars OK, 31 corta al char 30.
    long_handle = "a" * 30
    assert extract_handles(f"@{long_handle}") == [long_handle]
    too_long = "b" * 31
    assert extract_handles(f"@{too_long}") == [too_long[:30]]


def test_stops_at_invalid_char():
    assert extract_handles("@bob! qué onda") == ["bob"]
    assert extract_handles("@bob.com") == ["bob"]
    assert extract_handles("@maria-jose") == ["maria"]


def test_underscore_is_valid():
    assert extract_handles("@maria_jose") == ["maria_jose"]


def test_double_at_does_not_match_inner_handle():
    # `@@bob` → el primer `@` es lookbehind del segundo `@`, así que `bob`
    # SÍ matchea (el lookbehind es `[A-Za-z0-9_]`, no incluye `@`).
    assert extract_handles("@@bob") == ["bob"]


def test_multiple_distinct_mentions_in_order():
    assert extract_handles("hola @alice y @bob") == ["alice", "bob"]


def test_lone_at_does_not_crash():
    assert extract_handles("@") == []
    assert extract_handles("solo @ sin handle") == []
