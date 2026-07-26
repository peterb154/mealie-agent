"""Recipe write tools against a mock Mealie (httpx.MockTransport).

No network, no Postgres. The point is the outbound HTTP: which endpoint,
which body, and — for the two tools that can lose data — whether the
request is sent at all.

Run:  uv run --with-requirements requirements.txt --with pytest -m pytest tests/
"""

import copy
import json

import httpx
import pytest

from tools.mealie_client import MealieClient
from tools.recipe_writes import _ingredient, _ingredient_text as _text, _step, recipe_write_tools

BASE = "http://mealie.test"

RECIPE = {
    "id": "r-1",
    "slug": "brownies",
    "name": "Outrageous Brownies",
    "description": "Fudgy.",
    "totalTime": None,
    "recipeYield": "24 brownies",
    "notes": [{"title": "old note", "text": "keep me"}],
    "tags": [{"id": "t-1", "name": "Dessert", "slug": "dessert"}],
    "recipeIngredient": [
        # Parsed, and inside a section — everything a plain-text
        # round-trip would silently throw away.
        {
            "display": "2 cups flour",
            "note": "",
            "quantity": 2,
            "unit": {"name": "cup"},
            "food": {"name": "flour"},
            "title": "Dry ingredients",
        },
        {"display": "cocoa", "note": "cocoa"},
        {"display": "KOHER salt", "note": "KOHER salt"},
    ],
    "recipeInstructions": [
        {"id": "s-1", "title": "Prep", "text": "mix", "ingredientReferences": [{"referenceId": "x"}]},
        {"id": "s-2", "title": "", "text": "bake", "ingredientReferences": []},
    ],
}

EXISTING_TAG = {"id": "t-1", "name": "Dessert", "slug": "dessert"}


class MockMealie:
    """Just enough Mealie to exercise the write paths. Records every
    request so tests can assert on method/URL/body."""

    def __init__(self):
        self.recipe = copy.deepcopy(RECIPE)
        self.seen: list[tuple[str, str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, str(request.url), body))

        if path == "/api/users/self":
            return httpx.Response(200, json={"id": "u-1", "email": "brian@example.com"})
        if path == "/api/users/u-1/ratings/brownies":
            return httpx.Response(200, json={})
        if path == "/api/comments":
            return httpx.Response(201, json={"id": "c-1", **(body or {})})
        if path == "/api/recipes" and request.method == "POST":
            return httpx.Response(201, json="coleslaw")
        if path == "/api/recipes/brownies":
            if request.method == "PATCH":
                self.recipe.update(body or {})
                if "name" in (body or {}):
                    # Mealie re-slugs on rename (RepositoryRecipes.update).
                    self.recipe["slug"] = body["name"].lower().replace(" ", "-")
            return httpx.Response(200, json=self.recipe)
        if path == "/api/recipes/coleslaw":
            return httpx.Response(200, json={"id": "r-2", "slug": "coleslaw"})
        if path.startswith("/api/organizers/"):
            if request.method == "POST":
                name = (body or {})["name"]
                return httpx.Response(201, json={"id": "t-new", "name": name, "slug": name.lower()})
            if "/slug/" in path:
                wanted = path.rsplit("/", 1)[-1]
                if wanted == EXISTING_TAG["slug"]:
                    return httpx.Response(200, json=EXISTING_TAG)
                return httpx.Response(404, json={"detail": "not found"})
            # Deliberately unhelpful: exercises the search fallback only
            # when the slug lookup misses.
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, json={"detail": f"unmocked: {request.method} {path}"})

    def calls(self, method: str, needle: str) -> list[dict | None]:
        return [b for m, url, b in self.seen if m == method and needle in url]


@pytest.fixture
def mealie():
    return MockMealie()


@pytest.fixture
def tools(mealie):
    client = MealieClient(BASE, "tok")
    # Swap in the mock transport; everything else about the client is real.
    client._client = httpx.Client(
        base_url=BASE,
        transport=httpx.MockTransport(mealie),
        headers={"Authorization": "Bearer tok"},
    )
    # The factory returns strands @tool objects; unwrap to plain callables.
    return {t.__wrapped__.__name__: t.__wrapped__ for t in recipe_write_tools(client)}


# --- rate_recipe ------------------------------------------------------------


def test_rate_recipe_posts_to_the_users_own_rating_path(tools, mealie):
    out = tools["rate_recipe"]("brownies", rating=4.5)
    assert "4.5/5" in out
    # /users/{id}/, not /users/self/ — Mealie has no self alias for writes.
    assert mealie.calls("POST", "/api/users/u-1/ratings/brownies") == [{"rating": 4.5}]


def test_favorite_only_leaves_rating_out_of_the_body(tools, mealie):
    """Mealie merges on non-null fields, so an absent key preserves the
    existing rating. Sending rating=None would be the same, but sending
    rating=0 would silently wipe it."""
    tools["rate_recipe"]("brownies", mark_favorite=True)
    assert mealie.calls("POST", "/ratings/")[-1] == {"isFavorite": True}


def test_clear_rating_sends_zero(tools, mealie):
    tools["rate_recipe"]("brownies", clear_rating=True)
    assert mealie.calls("POST", "/ratings/")[-1] == {"rating": 0.0}


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        ({"rating": 3, "clear_rating": True}, "not both"),
        ({}, "nothing to do"),
        ({"rating": 9}, "between 0 and 5"),
    ],
)
def test_rate_recipe_rejects_bad_input_without_calling_mealie(tools, mealie, kwargs, needle):
    out = tools["rate_recipe"]("brownies", **kwargs)
    assert needle in out
    assert mealie.calls("POST", "/ratings/") == []


# --- comment_recipe ---------------------------------------------------------


def test_comment_recipe_resolves_slug_to_id(tools, mealie):
    out = tools["comment_recipe"]("brownies", "  kids loved it  ")
    assert "Comment added" in out
    assert mealie.calls("POST", "/api/comments") == [
        {"recipeId": "r-1", "text": "kids loved it"}
    ]


def test_empty_comment_is_refused(tools, mealie):
    assert "empty" in tools["comment_recipe"]("brownies", "   ")
    assert mealie.calls("POST", "/api/comments") == []


# --- append_recipe_note -----------------------------------------------------


def test_append_recipe_note_keeps_existing_notes(tools, mealie):
    out = tools["append_recipe_note"]("brownies", "2026-07-26 batch", "used dutch cocoa")
    assert "2 note(s)" in out
    patched = mealie.calls("PATCH", "/api/recipes/brownies")[-1]
    assert patched == {
        "notes": [
            {"title": "old note", "text": "keep me"},
            {"title": "2026-07-26 batch", "text": "used dutch cocoa"},
        ]
    }
    # A note append must not touch anything else on the recipe.
    assert set(patched) == {"notes"}


# --- create_recipe ----------------------------------------------------------


def test_create_recipe_is_two_phase_and_resolves_tags(tools, mealie):
    out = tools["create_recipe"](
        "Coleslaw",
        description="From the deli burger.",
        ingredients=["1 head cabbage", "  ", "1/2 cup mayo"],
        instructions=["Shred cabbage.", "Toss."],
        recipe_yield="6 servings",
        note="Extracted from deli-burger.",
        tags=["Dessert", "Side"],
    )
    assert "slug: coleslaw" in out
    assert mealie.calls("POST", "/api/recipes")[-1] == {"name": "Coleslaw"}

    fields = mealie.calls("PATCH", "/api/recipes/coleslaw")[-1]
    assert [i["note"] for i in fields["recipeIngredient"]] == ["1 head cabbage", "1/2 cup mayo"]
    assert [s["text"] for s in fields["recipeInstructions"]] == ["Shred cabbage.", "Toss."]
    assert fields["recipeYield"] == "6 servings"
    assert fields["notes"] == [{"title": "Notes", "text": "Extracted from deli-burger."}]
    # Existing tag reused by id; unknown tag created.
    assert fields["tags"] == [EXISTING_TAG, {"id": "t-new", "name": "Side", "slug": "side"}]


def test_create_recipe_reports_a_half_created_recipe(tools, mealie):
    """If the fill-in patch fails the recipe still exists — say so rather
    than reporting a clean failure the user would retry."""

    def boom(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(500, json={"detail": "nope"})
        return mealie(request)

    client = MealieClient(BASE, "tok")
    client._client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(boom))
    fresh = {t.__wrapped__.__name__: t.__wrapped__ for t in recipe_write_tools(client)}
    out = fresh["create_recipe"]("Coleslaw", description="x")
    assert "exists and is empty" in out


# --- update_recipe ----------------------------------------------------------


def test_update_recipe_sends_only_changed_fields(tools, mealie):
    out = tools["update_recipe"]("brownies", total_time="45 minutes")
    assert mealie.calls("PATCH", "/api/recipes/brownies")[-1] == {"totalTime": "45 minutes"}
    assert "totalTime" in out


def test_rename_reports_the_new_slug(tools, mealie):
    """Mealie re-slugs on rename. Returning the old slug hands the user a
    dead link and breaks the agent's next call."""
    out = tools["update_recipe"]("brownies", name="Fudgy Brownies")
    assert "fudgy-brownies" in out
    assert "old link no longer works" in out


def test_update_recipe_noop_sends_nothing(tools, mealie):
    out = tools["update_recipe"]("brownies", recipe_yield="24 brownies")
    assert "No changes" in out
    assert mealie.calls("PATCH", "/api/recipes/brownies") == []


def test_shorter_ingredient_list_is_refused_without_confirm(tools, mealie):
    out = tools["update_recipe"]("brownies", ingredients=["2 cups flour", "cocoa"])
    assert "refused" in out and "recipeIngredient 3 → 2" in out
    assert mealie.calls("PATCH", "/api/recipes/brownies") == []


def test_shorter_ingredient_list_goes_through_with_confirm(tools, mealie):
    tools["update_recipe"]("brownies", ingredients=["2 cups flour", "cocoa"], confirm=True)
    fields = mealie.calls("PATCH", "/api/recipes/brownies")[-1]
    assert [_text(i) for i in fields["recipeIngredient"]] == ["2 cups flour", "cocoa"]


def test_typo_fix_preserves_structure_of_untouched_lines(tools, mealie):
    """The issue #9 case: fix one word, send the full list back.

    Rebuilding every entry from its display string would flatten parsed
    ingredients into free text and drop section headers — and the count
    is unchanged, so the confirm guard would never catch it."""
    tools["update_recipe"](
        "brownies", ingredients=["2 cups flour", "cocoa", "kosher salt"]
    )
    ings = mealie.calls("PATCH", "/api/recipes/brownies")[-1]["recipeIngredient"]
    assert [_text(i) for i in ings] == ["2 cups flour", "cocoa", "kosher salt"]
    # Untouched line keeps everything a text round-trip would have lost.
    assert ings[0]["food"] == {"name": "flour"}
    assert ings[0]["quantity"] == 2
    assert ings[0]["title"] == "Dry ingredients"
    # Only the edited line is rebuilt as a plain note.
    assert ings[2] == _ingredient("kosher salt")


def test_edited_step_does_not_flatten_the_others(tools, mealie):
    tools["update_recipe"]("brownies", instructions=["mix", "bake at 350"])
    steps = mealie.calls("PATCH", "/api/recipes/brownies")[-1]["recipeInstructions"]
    assert steps[0]["title"] == "Prep"
    assert steps[0]["ingredientReferences"] == [{"referenceId": "x"}]
    assert steps[1] == _step("bake at 350")


def test_dropping_tags_needs_confirm_too(tools, mealie):
    """tags=[] wipes every tag. Same class of loss as a shorter
    ingredient list, so the same gate applies."""
    out = tools["update_recipe"]("brownies", tags=[])
    assert "refused" in out and "tags 1 → 0" in out
    assert mealie.calls("PATCH", "/api/recipes/brownies") == []

    tools["update_recipe"]("brownies", tags=[], confirm=True)
    assert mealie.calls("PATCH", "/api/recipes/brownies")[-1] == {"tags": []}


def test_existing_tag_is_found_by_slug_not_by_search(tools, mealie):
    """Search is lexical and paginated; an exact-name miss would create a
    duplicate tag. The mock's search always returns nothing, so a pass
    here proves the slug lookup is what resolved it."""
    tools["update_recipe"]("brownies", tags=["Dessert"], confirm=True)
    fields = mealie.calls("PATCH", "/api/recipes/brownies")[-1]
    assert fields["tags"] == [EXISTING_TAG]
    assert mealie.calls("POST", "/api/organizers/") == []
