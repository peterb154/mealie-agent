"""Pull Mealie recipes into the local pgvector mirror.

Run modes:

- ``python scripts/sync_recipes.py`` — incremental since last sync.
- ``python scripts/sync_recipes.py --full`` — re-embed everything.

Idempotent: uses ``INSERT ... ON CONFLICT DO UPDATE`` keyed on
``mealie_recipe_id``, so re-runs just refresh rows whose embedding
is stale.

Also prunes: rows whose recipe no longer exists in Mealie are deleted,
because upsert alone would leave them in the vector index forever and
``search_recipes`` would keep returning slugs that 404. Pass
``--no-prune`` to skip, ``--force-prune`` to allow a prune that would
remove more than 10% of the mirror (the guard exists because a
truncated Mealie drain looks identical to a mass deletion).

Works from inside the agent container:
    docker compose exec agent python /app/scripts/sync_recipes.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC
from pathlib import Path

import psycopg

# Make `tools.*` importable when run as a top-level script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strands_pg._pool import resolve_dsn  # noqa: E402
from tools.embedding import embed  # noqa: E402
from tools.mealie_client import MealieClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_recipes")


def _snippet_for(r: dict) -> str:
    """Text we actually embed. Name, tags, description, ingredient names,
    and a rating hint so semantic queries like 'favorite' or 'top rated'
    have something to latch onto."""
    parts: list[str] = [r.get("name") or ""]
    if desc := r.get("description"):
        parts.append(desc)
    tags = [t.get("name", "") for t in (r.get("tags") or [])]
    if tags:
        parts.append("tags: " + ", ".join(tags))
    cats = [c.get("name", "") for c in (r.get("recipeCategory") or [])]
    if cats:
        parts.append("categories: " + ", ".join(cats))
    ings = [
        (i.get("food") or {}).get("name") or i.get("note") or ""
        for i in (r.get("recipeIngredient") or [])
    ]
    ings = [i for i in ings if i]
    if ings:
        parts.append("ingredients: " + ", ".join(ings))
    rating = r.get("rating")
    if isinstance(rating, (int, float)) and rating > 0:
        # Plain-english descriptor helps embeddings; the numeric column
        # handles structured filters/sorts.
        label = "favorite" if rating >= 4.5 else "highly rated" if rating >= 4 else "rated"
        parts.append(f"{label} ({rating}/5)")
    return "\n".join(p for p in parts if p)[:2000]


def _upsert(cur: psycopg.Cursor, row: dict) -> None:
    cur.execute(
        """
        INSERT INTO recipe_embeddings
            (mealie_recipe_id, slug, name, snippet, embedding, rating,
             source_updated_at, synced_at)
        VALUES (%(id)s, %(slug)s, %(name)s, %(snippet)s, %(embedding)s,
                %(rating)s, %(updated)s, now())
        ON CONFLICT (mealie_recipe_id) DO UPDATE SET
            slug = EXCLUDED.slug,
            name = EXCLUDED.name,
            snippet = EXCLUDED.snippet,
            embedding = EXCLUDED.embedding,
            rating = EXCLUDED.rating,
            source_updated_at = EXCLUDED.source_updated_at,
            synced_at = now()
        """,
        row,
    )


def _live_recipe_ids(mc: MealieClient) -> set[str]:
    """Every recipe id currently in Mealie.

    Id-only drain — no detail fetch, no embedding — so it stays cheap
    enough to run on every sync, including incremental ones.

    Raises rather than returning a partial set: a truncated drain is
    indistinguishable from a mass deletion, and the caller uses this to
    decide what to delete.
    """
    ids: set[str] = set()
    page = 1
    while True:
        body = mc.list_recipes(page=page, per_page=100)
        items = body.get("items") or []
        ids |= {i["id"] for i in items if i.get("id")}
        total_pages = body.get("total_pages") or body.get("totalPages") or 1
        if page >= total_pages or not items:
            break
        page += 1
    return ids


def _prune(
    conn: psycopg.Connection,
    live_ids: set[str],
    *,
    max_delete_ratio: float = 0.10,
    force: bool = False,
) -> int:
    """Drop mirror rows for recipes that no longer exist in Mealie.

    The sync is otherwise upsert-only, so a deleted recipe would sit in
    the vector index forever and ``search_recipes`` would keep offering
    a slug that ``get_recipe`` can't resolve.

    Guarded by a ratio check: an incomplete drain looks exactly like a
    bulk deletion, and acting on one would wipe the mirror. A real bulk
    delete just needs --force-prune.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT mealie_recipe_id, slug FROM recipe_embeddings")
        rows = cur.fetchall()
    if not rows:
        return 0
    orphans = [(str(rid), slug) for rid, slug in rows if str(rid) not in live_ids]
    if not orphans:
        log.info("prune: %d mirror rows, all still live", len(rows))
        return 0

    ratio = len(orphans) / len(rows)
    if ratio > max_delete_ratio and not force:
        log.error(
            "prune: REFUSING to delete %d/%d rows (%.1f%%) — over the %.0f%% guard. "
            "That usually means the Mealie drain was incomplete, not that %d "
            "recipes were deleted. Re-run with --force-prune if it really was.",
            len(orphans), len(rows), ratio * 100, max_delete_ratio * 100, len(orphans),
        )
        return 0

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM recipe_embeddings WHERE mealie_recipe_id = ANY(%s)",
            ([rid for rid, _ in orphans],),
        )
    conn.commit()
    for _, slug in orphans[:20]:
        log.info("prune: removed %s", slug)
    if len(orphans) > 20:
        log.info("prune: ... and %d more", len(orphans) - 20)
    log.info("prune: removed %d orphaned row(s)", len(orphans))
    return len(orphans)


def _drain_recipes(mc: MealieClient, updated_after: str | None) -> list[dict]:
    """Walk Mealie pagination until we run out of items."""
    all_items: list[dict] = []
    page = 1
    while True:
        body = mc.list_recipes(page=page, per_page=100, updated_after=updated_after)
        items = body.get("items") or []
        all_items.extend(items)
        total_pages = body.get("total_pages") or body.get("totalPages") or 1
        log.info("fetched page %d/%d  (+%d, total=%d)", page, total_pages, len(items), len(all_items))
        if page >= total_pages or not items:
            break
        page += 1
    return all_items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true", help="Re-embed everything, not just new/updated.")
    ap.add_argument(
        "--batch-sleep",
        type=float,
        default=0.1,
        help="Seconds to sleep between recipes (throttle Bedrock).",
    )
    ap.add_argument(
        "--no-prune",
        action="store_true",
        help="Skip removing mirror rows for recipes deleted in Mealie.",
    )
    ap.add_argument(
        "--force-prune",
        action="store_true",
        help="Prune even when it would remove >10%% of the mirror.",
    )
    args = ap.parse_args()

    dsn = resolve_dsn()
    log.info("connecting to %s", dsn.split("@")[-1])

    with MealieClient.from_env() as mc, psycopg.connect(dsn) as conn:
        # Pull the service account's ratings up-front. Mealie stores ratings
        # per-user (/api/users/self/ratings); the recipe-level `rating`
        # field on /api/recipes/{id} is always null. Map by recipeId so the
        # per-recipe loop can look them up in O(1).
        ratings_by_id: dict[str, float] = {}
        for row in mc.self_ratings():
            rid, val = row.get("recipeId"), row.get("rating")
            if rid and isinstance(val, (int, float)) and val > 0:
                ratings_by_id[rid] = float(val)
        log.info("loaded %d user ratings", len(ratings_by_id))

        # Figure out how much to sync.
        updated_after: str | None = None
        if not args.full:
            with conn.cursor() as cur:
                cur.execute("SELECT max(source_updated_at) FROM recipe_embeddings")
                row = cur.fetchone()
            if row and row[0]:
                updated_after = row[0].astimezone(UTC).isoformat()
                log.info("incremental sync since %s", updated_after)
            else:
                log.info("no prior sync found — doing full pull")

        list_summaries = _drain_recipes(mc, updated_after)
        log.info("mealie reports %d recipes to process", len(list_summaries))

        done = 0
        started = time.time()
        for summary in list_summaries:
            slug = summary.get("slug")
            if not slug:
                continue
            # The list endpoint doesn't always return tags/ingredients; fetch the
            # full document so the embedding snippet is high-quality.
            try:
                full = mc.get_recipe(slug)
            except Exception:  # noqa: BLE001
                log.exception("get_recipe %s failed — skipping", slug)
                continue
            # Patch the recipe's rating from the user-rating map before
            # snippet generation so the 'favorite'/'highly rated' token
            # makes it into the embedding.
            rid = full.get("id")
            if rid and rid in ratings_by_id:
                full["rating"] = ratings_by_id[rid]
            snippet = _snippet_for(full)
            try:
                vec = embed(snippet)
            except Exception:  # noqa: BLE001
                log.exception("embed %s failed — skipping", slug)
                continue
            row = {
                "id": full.get("id"),
                "slug": slug,
                "name": full.get("name") or slug,
                "snippet": snippet,
                "embedding": vec,
                "rating": full.get("rating"),
                "updated": full.get("dateUpdated"),
            }
            with conn.cursor() as cur:
                _upsert(cur, row)
            done += 1
            if done % 25 == 0:
                conn.commit()
                rate = done / max(1, time.time() - started)
                log.info("committed %d/%d  (%.1f recipes/s)", done, len(list_summaries), rate)
            time.sleep(args.batch_sleep)
        conn.commit()
        log.info("sync complete — %d recipes embedded", done)

        # Prune last: embedding work is the expensive part, and a prune
        # failure shouldn't cost the sync that already succeeded.
        if args.no_prune:
            log.info("prune: skipped (--no-prune)")
        else:
            try:
                live = _live_recipe_ids(mc)
                log.info("prune: mealie reports %d live recipes", len(live))
                _prune(conn, live, force=args.force_prune)
            except Exception:  # noqa: BLE001 — never fail a good sync over this
                log.exception("prune failed — mirror may contain deleted recipes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
