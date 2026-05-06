"""Unit tests for the server-side allergy guard.

The helper is the structural fallback for the prompt rule that
tells the agent to filter recommendations by declared allergies —
in production Gemini Flash Lite ignores the rule inconsistently,
so we drop dishes server-side before the response goes out.

Tests pin: case-insensitive substring match, accent stripping
(``maní`` matches ``mani``), short-allergen guard (1-2 char
strings skipped to avoid catastrophic over-filtering), empty-list
short-circuit, and the (kept, dropped) split.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.chat.tools._allergy_filter import (
    allergen_canonical_key,
    filter_dishes_by_allergies,
)


def _dish(name: str, description: str | None = None):
    import uuid

    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        description=description,
    )


class TestFilterDishesByAllergies:
    def test_empty_allergies_returns_input_untouched(self):
        d1 = _dish("Pasta")
        d2 = _dish("Risotto")
        kept, dropped = filter_dishes_by_allergies([d1, d2], [])
        assert kept == [d1, d2]
        assert dropped == []

    def test_drops_dish_when_name_mentions_allergen(self):
        d1 = _dish("Tarta de nueces")
        d2 = _dish("Risotto de hongos")
        kept, dropped = filter_dishes_by_allergies([d1, d2], ["nueces"])
        assert kept == [d2]
        assert len(dropped) == 1
        assert dropped[0]["name"] == "Tarta de nueces"
        assert "nueces" in dropped[0]["matched_allergens"]

    def test_drops_dish_when_description_mentions_allergen(self):
        d1 = _dish(
            "Malabi",
            description="Postre cremoso con coco, nueces y rosas",
        )
        d2 = _dish("Kanafeh", description="Masa filo con queso y almíbar")
        kept, dropped = filter_dishes_by_allergies([d1, d2], ["nueces"])
        assert kept == [d2]
        assert dropped[0]["name"] == "Malabi"

    def test_accent_insensitive_match(self):
        # User declared "mani" (no accent); dish description has
        # "maní" (with accent). The match must still fire.
        d1 = _dish("Salsa", description="Lleva maní tostado")
        kept, dropped = filter_dishes_by_allergies([d1], ["mani"])
        assert kept == []
        assert dropped[0]["matched_allergens"] == ["mani"]

    def test_short_allergens_skipped_to_avoid_overmatch(self):
        # If the DB has corrupted single-char items (e.g. ['m','a',
        # 'n','í'] from a Flash Lite serialisation slip), filtering
        # by 'a' would drop literally every dish. The helper guards
        # against this — entries shorter than the threshold are
        # ignored. The corruption itself is rejected upstream by
        # ``update_taste_profile`` now, but the filter stays
        # defensive in case stale rows survived.
        d1 = _dish("Pasta", description="Con tomate y albahaca")
        kept, dropped = filter_dishes_by_allergies([d1], ["a"])
        assert kept == [d1]
        assert dropped == []

    def test_plural_allergen_matches_singular_in_dish_name(self):
        # The exact prod incident: comensal declared "nueces"
        # (plural), the dish in DB is named "Malabi- Postre de Coco
        # Nuez Y Rosas" (singular). Naïve substring match misses
        # because "nueces" not in "nuez". The synonym index resolves
        # both forms to the same group, so the filter must drop.
        d = _dish("Malabi- Postre de Coco Nuez Y Rosas", description="")
        kept, dropped = filter_dishes_by_allergies([d], ["nueces"])
        assert kept == []
        assert dropped[0]["matched_allergens"] == ["nueces"]

    def test_singular_allergen_matches_plural_in_dish_text(self):
        # Symmetric case: user typed "nuez", dish description says
        # "con nueces tostadas".
        d = _dish("Tarta", description="Con nueces tostadas")
        kept, dropped = filter_dishes_by_allergies([d], ["nuez"])
        assert kept == []

    def test_synonym_group_crosslinguistic(self):
        # User declared "maní" (Rio de la Plata), reviewer wrote
        # "peanut" (English review imported).
        d = _dish("Salsa", description="Has peanut crumble on top")
        kept, dropped = filter_dishes_by_allergies([d], ["maní"])
        assert kept == []

    def test_multiple_allergens_each_matched_independently(self):
        d1 = _dish("Postre con nueces")
        d2 = _dish("Salsa con maní")
        d3 = _dish("Helado de chocolate")
        kept, dropped = filter_dishes_by_allergies(
            [d1, d2, d3], ["nueces", "maní"]
        )
        assert kept == [d3]
        assert len(dropped) == 2


class TestAllergenCanonicalKey:
    def test_singular_and_plural_collapse_to_same_key(self):
        # Production bug: comensal's profile ended up with both
        # ``"nuez"`` and ``"nueces"`` after two declarations because
        # the dedup compared raw lowercase strings. ``canonical_key``
        # is what unblocks dedup at the synonym-group level.
        assert allergen_canonical_key("nuez") == allergen_canonical_key(
            "nueces"
        )

    def test_synonym_group_crosslinguistic_same_key(self):
        assert allergen_canonical_key("maní") == allergen_canonical_key(
            "peanut"
        )
        assert allergen_canonical_key("leche") == allergen_canonical_key(
            "lactose"
        )

    def test_unrelated_allergens_have_different_keys(self):
        assert allergen_canonical_key("nuez") != allergen_canonical_key(
            "maní"
        )

    def test_out_of_index_term_falls_back_to_plural_stripped_form(self):
        # ``alcaucil`` isn't in the index but the Spanish plural
        # stripping should still fold ``alcauciles`` into the same
        # key, so two declarations don't duplicate.
        assert allergen_canonical_key("alcaucil") == allergen_canonical_key(
            "alcauciles"
        )

    def test_empty_string_returns_empty_key(self):
        assert allergen_canonical_key("") == ""
        assert allergen_canonical_key("   ") == ""
