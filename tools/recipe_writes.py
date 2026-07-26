"""Recipe write tools — ratings, comments, notes, create, update.

The read tools in ``recipes.py`` are safe by construction. These are not:
``update_recipe`` can lose data if an agent sends a partial ingredient
list, so the docstrings here are deliberately loud about append vs.
replace, and the one destructive path is gated behind ``confirm``.

``recipe_write_tools(user_client)`` returns a list of ``@tool`` callables,
built per-request so writes land as the signed-in user (Mealie's ratings
and comments are per-user, and it enforces household RBAC on the rest).
"""

from __future__ import annotations

import logging
from typing import Any

from strands import tool

from tools.mealie_client import MealieClient
from tools.recipes import recipe_url

logger = logging.getLogger(__name__)


def _ingredient(line: str) -> dict[str, Any]:
    """One free-text ingredient line as a Mealie ingredient object.

    We deliberately don't try to parse quantities/units/foods — Mealie's
    parser is a separate endpoint with its own failure modes, and a note
    round-trips exactly as typed."""
    line = line.strip()
    return {
        "quantity": 0,
        "unit": None,
        "food": None,
        "note": line,
        "display": line,
        "originalText": line,
        "title": None,
    }


def _step(text: str) -> dict[str, Any]:
    return {"title": "", "text": text.strip()}


def _ingredient_text(ing: dict[str, Any]) -> str:
    return ((ing.get("display") or ing.get("note")) or "").strip()


def _step_text(step: dict[str, Any]) -> str:
    return (step.get("text") or "").strip()


def _merge(
    existing: list[dict[str, Any]],
    lines: list[str],
    build: Any,
    text_of: Any,
) -> list[dict[str, Any]]:
    """Rebuild a list from plain text, KEEPING the original object
    wherever the text is unchanged.

    Rebuilding every entry would be quietly lossy: a parsed Mealie
    ingredient carries food/unit/quantity, a section header lives in
    ``title``, and steps carry ingredientReferences — none of which
    survive a round-trip through display text. Since the agent is told
    to send the complete list back after editing one line, rebuilding
    wholesale would downgrade the whole recipe on every typo fix. Only
    genuinely edited lines degrade to a free-text note."""
    out: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        old = existing[i] if i < len(existing) else None
        out.append(old if old is not None and text_of(old) == line else build(line))
    return out


def _structured_ingredient(parsed: dict[str, Any], line: str) -> dict[str, Any] | None:
    """Parser output as a writable ingredient, or None if it isn't safe.

    Mealie returns 500 when a PATCH carries a food or unit without an id
    (a create-shape), and the parser emits exactly that for anything it
    can't resolve against the instance's existing foods/units. So the id
    is the gate — NOT the confidence score, which happily reports 0.97
    on total nonsense."""
    ing = parsed.get("ingredient") or {}
    food = ing.get("food") or None
    unit = ing.get("unit") or None
    if not (food and food.get("id")):
        return None
    if unit and not unit.get("id"):
        return None
    return {
        "quantity": ing.get("quantity") or 0,
        "unit": unit,
        "food": food,
        "note": ing.get("note") or "",
        "originalText": line,
    }


def _reparse(
    client: MealieClient,
    merged: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    lines: list[str],
) -> list[dict[str, Any]]:
    """Try to re-derive food/unit/quantity for the entries we rebuilt.

    A rebuilt entry is plain text, which costs Mealie's shopping-list
    and scaling features. The parser can put the structure back. Entries
    that round-tripped unchanged are left strictly alone — they already
    hold the real thing. Best-effort: any failure keeps the plain notes
    rather than failing the edit."""
    idx = [i for i, m in enumerate(merged) if i >= len(existing) or m is not existing[i]]
    if not idx:
        return merged
    try:
        parsed = client.parse_ingredients([lines[i] for i in idx])
    except Exception:  # noqa: BLE001 — structure is a bonus, not the job
        logger.exception("ingredient re-parse failed; keeping plain notes")
        return merged
    out = list(merged)
    for slot, p in zip(idx, parsed, strict=False):
        if (structured := _structured_ingredient(p, lines[slot])) is not None:
            out[slot] = structured
    return out


def _clean(lines: list[str] | None) -> list[str]:
    return [s.strip() for s in (lines or []) if s and s.strip()]


def recipe_write_tools(user_client: MealieClient) -> list[Any]:
    """Build the recipe write tools bound to ``user_client``."""

    def _recipe_id(slug: str) -> str:
        rid = user_client.get_recipe(slug).get("id")
        if not rid:
            raise RuntimeError(f"recipe {slug!r} has no id")
        return rid

    @tool
    def rate_recipe(
        slug: str,
        rating: float | None = None,
        mark_favorite: bool | None = None,
        clear_rating: bool = False,
    ) -> str:
        """Set the CURRENT user's star rating and/or favorite flag on a recipe.

        Ratings in Mealie are per-user, not household-wide — this records
        what the signed-in person thought, which is what
        top_rated_recipes reads back. Use it right after someone says how
        a meal turned out.

        Rating and favorite move independently: passing only one leaves
        the other untouched.

        Args:
            slug: Recipe slug (e.g. 'bbq-chicken-sliders').
            rating: Stars, 0-5. Half stars are allowed (4.5).
            mark_favorite: True to favorite, False to un-favorite.
            clear_rating: True to wipe the star rating. Mealie has no
                delete-rating endpoint, so this sets 0 — which displays
                as unrated. Cannot be combined with `rating`.
        """
        if clear_rating and rating is not None:
            return "(error: pass either rating or clear_rating, not both)"
        if rating is None and mark_favorite is None and not clear_rating:
            return "(error: nothing to do — set rating, mark_favorite, or clear_rating)"
        if rating is not None and not (0 <= rating <= 5):
            return f"(error: rating must be between 0 and 5, got {rating})"

        new_rating = 0.0 if clear_rating else rating
        try:
            user_client.set_rating(slug, rating=new_rating, is_favorite=mark_favorite)
        except Exception as exc:  # noqa: BLE001
            logger.exception("rate_recipe failed for slug=%s", slug)
            return f"(rating error: {exc})"

        done: list[str] = []
        if clear_rating:
            done.append("cleared the star rating")
        elif rating is not None:
            done.append(f"rated {rating:g}/5 ⭐")
        if mark_favorite is True:
            done.append("marked as a favorite ❤️")
        elif mark_favorite is False:
            done.append("removed from favorites")
        return f"[{slug}]({recipe_url(slug)}) — {', '.join(done)}."

    @tool
    def comment_recipe(slug: str, text: str) -> str:
        """Add a comment to a recipe — a dated, per-user review that shows
        up in the recipe's comment thread in Mealie.

        Purely additive: this never touches the recipe itself or other
        people's comments. Use it for the verdict ("kids ate it, sauce
        needs more heat"). Use append_recipe_note instead for corrections
        that belong to the recipe rather than to a conversation.

        Args:
            slug: Recipe slug.
            text: The comment body.
        """
        if not text.strip():
            return "(error: comment text is empty)"
        try:
            user_client.add_comment(_recipe_id(slug), text.strip())
        except Exception as exc:  # noqa: BLE001
            logger.exception("comment_recipe failed for slug=%s", slug)
            return f"(comment error: {exc})"
        return f"Comment added to [{slug}]({recipe_url(slug)})."

    @tool
    def append_recipe_note(slug: str, title: str, text: str) -> str:
        """Append a note to a recipe's Notes section.

        APPENDS — existing notes are preserved. Notes are part of the
        recipe (everyone in the household sees them as recipe content),
        unlike comments, which read as conversation. Batch results,
        substitutions, and "next time do X" belong here.

        Args:
            slug: Recipe slug.
            title: Short heading for the note (e.g. '2026-07-26 batch').
            text: The note body.
        """
        if not text.strip():
            return "(error: note text is empty)"
        try:
            recipe = user_client.append_recipe_note(slug, title.strip(), text.strip())
        except Exception as exc:  # noqa: BLE001
            logger.exception("append_recipe_note failed for slug=%s", slug)
            return f"(note error: {exc})"
        count = len(recipe.get("notes") or [])
        return f"Note added to [{slug}]({recipe_url(slug)}) — {count} note(s) total."

    @tool
    def create_recipe(
        name: str,
        description: str = "",
        ingredients: list[str] | None = None,
        instructions: list[str] | None = None,
        recipe_yield: str = "",
        total_time: str = "",
        prep_time: str = "",
        cook_time: str = "",
        note: str = "",
        tags: list[str] | None = None,
        categories: list[str] | None = None,
    ) -> str:
        """Create a new recipe in Mealie and return its slug.

        Use this for recipes dictated from a card, a photo, or extracted
        from another recipe (e.g. promoting a side dish into its own
        entry). Confirm the ingredients and steps with the user before
        calling — a created recipe is visible to the whole household.

        Ingredients and instructions are plain text lines, one per
        ingredient/step, stored exactly as written.

        Args:
            name: Recipe title. Mealie derives the slug from it.
            description: One or two sentences about the dish.
            ingredients: Ingredient lines, e.g. ['2 cups flour', '1 tsp salt'].
            instructions: Step text, one string per step, in order.
            recipe_yield: Servings text, e.g. '4 servings' or '24 cookies'.
            total_time: e.g. '45 minutes'.
            prep_time: e.g. '15 minutes'.
            cook_time: e.g. '30 minutes'.
            note: Optional note to attach (recipe Notes section).
            tags: Tag NAMES. Tags that don't exist yet are created.
            categories: Category NAMES. Created if they don't exist.
        """
        if not name.strip():
            return "(error: recipe name is required)"

        try:
            slug = user_client.create_recipe(name.strip())
        except Exception as exc:  # noqa: BLE001
            logger.exception("create_recipe failed for name=%s", name)
            return f"(create error: {exc})"

        # Mealie's create endpoint takes a name and nothing else, so
        # everything the caller gave us lands in a follow-up patch. If
        # that half fails, say so loudly — an empty recipe now exists.
        fields: dict[str, Any] = {}
        if description.strip():
            fields["description"] = description.strip()
        if ings := _clean(ingredients):
            fields["recipeIngredient"] = _reparse(
                user_client, [_ingredient(i) for i in ings], [], ings
            )
        if steps := _clean(instructions):
            fields["recipeInstructions"] = [_step(s) for s in steps]
        if recipe_yield.strip():
            fields["recipeYield"] = recipe_yield.strip()
        if total_time.strip():
            fields["totalTime"] = total_time.strip()
        if prep_time.strip():
            fields["prepTime"] = prep_time.strip()
        if cook_time.strip():
            fields["cookTime"] = cook_time.strip()
        if note.strip():
            fields["notes"] = [{"title": "Notes", "text": note.strip()}]

        try:
            if tag_names := _clean(tags):
                fields["tags"] = user_client.resolve_organizers("tags", tag_names)
            if cat_names := _clean(categories):
                fields["recipeCategory"] = user_client.resolve_organizers("categories", cat_names)
            if fields:
                user_client.patch_recipe(slug, fields)
        except Exception as exc:  # noqa: BLE001
            logger.exception("create_recipe: fill-in patch failed for slug=%s", slug)
            return (
                f"Created [{name}]({recipe_url(slug)}) (`slug: {slug}`) but failed to "
                f"fill in the details: {exc}. The recipe exists and is empty — "
                f"retry with update_recipe."
            )
        return f"Created **[{name}]({recipe_url(slug)})** — `slug: {slug}`"

    @tool
    def update_recipe(
        slug: str,
        name: str | None = None,
        description: str | None = None,
        ingredients: list[str] | None = None,
        instructions: list[str] | None = None,
        recipe_yield: str | None = None,
        total_time: str | None = None,
        prep_time: str | None = None,
        cook_time: str | None = None,
        tags: list[str] | None = None,
        categories: list[str] | None = None,
        confirm: bool = False,
    ) -> str:
        """Correct fields on an existing recipe. Returns a before/after diff.

        Fields you don't pass are left alone. Fields you DO pass REPLACE
        what's there — ingredients, instructions, tags and categories
        especially: pass the complete list every time, never just the
        entries you're changing. Fetch the recipe with get_recipe first,
        edit the full list, send it back. Lines you leave exactly as they
        were keep their existing structure; only edited lines are
        rewritten.

        Do NOT use this to record how a batch turned out — that's
        append_recipe_note (which appends) or comment_recipe. This is for
        fixing what the recipe says: a wrong oven temp, a missing bake
        time, a typo.

        Args:
            slug: Recipe slug.
            name: New title. Renaming ALSO changes the slug — the reply
                reports the new one, and the old slug stops working.
            description: Replacement description.
            ingredients: COMPLETE replacement ingredient list.
            instructions: COMPLETE replacement list of step text.
            recipe_yield: e.g. '4 servings'.
            total_time: e.g. '45 minutes'.
            prep_time: e.g. '15 minutes'.
            cook_time: e.g. '30 minutes'.
            tags: COMPLETE replacement list of tag NAMES.
            categories: COMPLETE replacement list of category NAMES.
            confirm: Required (True) when any replacement list is SHORTER
                than the current one, since that drops entries. Ask the
                user before setting it.
        """
        try:
            before = user_client.get_recipe(slug)
        except Exception as exc:  # noqa: BLE001
            logger.exception("update_recipe: fetch failed for slug=%s", slug)
            return f"(fetch error: {exc})"

        fields: dict[str, Any] = {}
        diff: list[str] = []

        def _scalar(key: str, value: str | None) -> None:
            if value is None:
                return
            old = before.get(key) or ""
            if str(old) == value:
                return
            fields[key] = value
            diff.append(f"- {key}: {old or '(empty)'} → {value}")

        _scalar("name", name)
        _scalar("description", description)
        _scalar("recipeYield", recipe_yield)
        _scalar("totalTime", total_time)
        _scalar("prepTime", prep_time)
        _scalar("cookTime", cook_time)

        # Every list field REPLACES what's there, so a shorter list drops
        # entries. Collect every shrink first and refuse the whole call —
        # partial application would be worse than none.
        shrinking: list[str] = []
        for key, raw, build, text_of in (
            ("recipeIngredient", ingredients, _ingredient, _ingredient_text),
            ("recipeInstructions", instructions, _step, _step_text),
        ):
            if raw is None:
                continue
            incoming = _clean(raw)
            existing = before.get(key) or []
            if len(incoming) < len(existing):
                shrinking.append(f"{key} {len(existing)} → {len(incoming)}")
            merged = _merge(existing, incoming, build, text_of)
            if key == "recipeIngredient":
                merged = _reparse(user_client, merged, existing, incoming)
            fields[key] = merged
            rewritten = sum(
                1 for i, m in enumerate(merged) if i >= len(existing) or m is not existing[i]
            )
            diff.append(f"- {key}: {len(incoming)} item(s), {rewritten} changed")

        # Tags and categories replace wholesale too — tags=[] wipes them.
        for label, raw, existing_key in (
            ("tags", tags, "tags"),
            ("categories", categories, "recipeCategory"),
        ):
            if raw is None:
                continue
            old_n = len(before.get(existing_key) or [])
            if len(_clean(raw)) < old_n:
                shrinking.append(f"{label} {old_n} → {len(_clean(raw))}")

        if shrinking and not confirm:
            return (
                f"(refused: this would DROP entries from {slug} — "
                f"{'; '.join(shrinking)}. Send the complete list, or confirm "
                f"with the user and call again with confirm=True.)"
            )

        # Resolved after the guard so a refused call costs no writes.
        try:
            if tags is not None:
                fields["tags"] = user_client.resolve_organizers("tags", _clean(tags))
                diff.append(f"- tags: {', '.join(_clean(tags)) or '(none)'}")
            if categories is not None:
                fields["recipeCategory"] = user_client.resolve_organizers(
                    "categories", _clean(categories)
                )
                diff.append(f"- categories: {', '.join(_clean(categories)) or '(none)'}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("update_recipe: organizer resolution failed for slug=%s", slug)
            return f"(tag/category error: {exc})"

        if not fields:
            return f"No changes — [{slug}]({recipe_url(slug)}) already matches."

        try:
            updated = user_client.patch_recipe(slug, fields)
        except Exception as exc:  # noqa: BLE001
            logger.exception("update_recipe failed for slug=%s", slug)
            return f"(update error: {exc})"

        # Mealie re-slugs on rename, so the slug the caller passed may now
        # be dead. Report the new one or the agent's next call 404s.
        new_slug = updated.get("slug") or slug
        if new_slug != slug:
            diff.append(f"- slug: {slug} → {new_slug} (the old link no longer works)")
        return f"Updated **[{new_slug}]({recipe_url(new_slug)})**:\n" + "\n".join(diff)

    return [
        rate_recipe,
        comment_recipe,
        append_recipe_note,
        create_recipe,
        update_recipe,
    ]
