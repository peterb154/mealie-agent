# mealie-agent — Chef Rex

Per-user meal-planning assistant for the Petersons family Mealie library.
Lives alongside Mealie at `recipes.epetersons.com` and is reachable at
[`mealie-agent.epetersons.com`](https://mealie-agent.epetersons.com).
Built on [strands-pg](https://github.com/peterb154/strands-pgsql-agent-framework).

## What he does

- **Recipe discovery** over 5,400+ recipes — semantic search for mood
  queries ("quick chicken dinner"), lexical search for proper nouns
  ("Mary Jean's funeral meatballs"), cookbook-aware (cookbooks are
  saved tag filters in Mealie).
- **Favorites + ratings** — pulls the signed-in user's personal
  ratings from `/api/users/self/ratings` and surfaces them as ⭐ 4-5
  hits when planning.
- **Meal planning** — sees recent history so he doesn't repeat what
  you just cooked, alternates household vs. personal preferences if
  that's your pattern, defaults to dinner unless told otherwise.
- **Shopping lists** — walks the meal plan, extracts ingredients from
  every scheduled recipe, consolidates across meals, scales by
  headcount (chicken by total weight etc.), drops pantry staples,
  attaches each item's source recipe(s) and a grocery-store search
  link. Per-user store preference captured during onboarding.
- **Memory** — multi-scope via strands-pg's `memory_tools(namespaces=...)`.
  Personal (`user:<email>`) and household (`household:<id>`) are
  namespaced separately so allergies vs. "we do veggie Tuesdays" don't
  bleed together.
- **Onboarding** — first time a user talks to Rex, `recall_personal`
  returns nothing so he runs a single short questionnaire (diet, cook
  time, household, preferred store) and saves the answers before
  diving into the actual request.

## Deployment topology

```
┌─────────────────────────────────────────────────────────────┐
│ recipes.epetersons.com  (Mealie, CT 100, 192.168.0.6)      │
│   ├── NPM injects <script src=mealie-agent/static/shim.js>  │
│   │   (proxy_host/9.conf has a patched Accept-Encoding to   │
│   │    let sub_filter work — see local_network/npm/)        │
│   └── shim renders a 🧑‍🍳 "Chat with Chef Rex" pill in the  │
│       corner of every page. Click → open a new tab with     │
│       the user's JWT handed off via URL fragment.           │
│                                                             │
│ mealie-agent.epetersons.com  (this agent, CT 114,           │
│                               192.168.0.24)                 │
│   ├── FastAPI `/chat` + `/chat/stream` (SSE)                │
│   ├── auth_verifier introspects the fragment-handoff JWT    │
│   │   against Mealie's /api/users/self                      │
│   ├── Local Postgres mirror of 5,400+ recipes (pgvector)    │
│   └── systemd timer: incremental recipe sync every 10 min   │
└─────────────────────────────────────────────────────────────┘
```

Public DNS: both subdomains CNAME to `home.epetersons.com`. NPM
fronts everything; Cloudflare is not in this path.

## Key design decisions

- **`cache_agents=False`** — user JWTs rotate every 48h and contexts
  differ per request, so we rebuild the agent each turn. Construction
  is cheap; Bedrock is the expensive part.
- **Session_id comes from the verifier, not the request body** —
  prevents spoofing.
- **Ratings are per-user** — the recipe-level `rating` field on
  Mealie's `/api/recipes/{id}` is always null; real data lives under
  `/api/users/self/ratings`. `top_rated_recipes` joins that with the
  recipe detail endpoint.
- **Prompts on disk win on deploy** — `PgPromptStore.seed_from_dir`
  respects file mtime; live API edits persist until the next deploy.
- **`GIT_SHA` build-arg, not runtime** — `deploy.sh` exports the short
  commit SHA before `docker compose up -d --build`. `/api/health`
  reports it so n8n's deploy workflow can verify landings.

## Tool registry (for the record)

| Tool | Source | What it does |
|---|---|---|
| `current_time` | strands-agents-tools | Anchors "today" / "tonight" etc. |
| `search_recipes` | tools/recipes.py | Semantic search (local pgvector) |
| `search_recipes_text` | tools/recipes.py | Mealie lexical search + tag/cookbook filter |
| `top_rated_recipes` | tools/recipes.py | User's ⭐ 4+ / favorites |
| `list_cookbooks` | tools/recipes.py | Mealie cookbooks (saved filters) |
| `get_recipe` | tools/recipes.py | Full recipe detail + URL |
| `list_meal_plan` | tools/mealplan.py | Upcoming scheduled meals |
| `meal_plan_history` | tools/mealplan.py | Recent cooked meals (anti-repeat) |
| `list_ingredients_for_meal_plan` | tools/mealplan.py | Flat ingredient dump for shopping-list build |
| `add_to_meal_plan` | tools/mealplan.py | Schedule a recipe; defaults to dinner |
| `delete_meal_plan_entry` | tools/mealplan.py | Fix duplicates |
| `list_shopping_lists` | tools/shopping.py | Which lists exist |
| `show_shopping_list` | tools/shopping.py | What's on a list |
| `add_to_shopping_list` | tools/shopping.py | Single item |
| `bulk_add_to_shopping_list` | tools/shopping.py | Many items, with grocery-search links |
| `check_shopping_item` | tools/shopping.py | Mark bought / unbought |
| `delete_shopping_item` | tools/shopping.py | Remove one |
| `clear_shopping_list` | tools/shopping.py | Wipe list (all or checked-only) |
| `remember_personal` / `recall_personal` | strands_pg.memory_tools | Per-user facts |
| `remember_household` / `recall_household` | strands_pg.memory_tools | Shared household rules |

## Deploying changes

Push to `main` — n8n receives the webhook and calls
`POST /api/deploy` on this agent. That writes `/opt/mealie-agent/.deploy-trigger`;
the host's systemd `mealie-agent-deploy.path` unit fires
`.service` which runs `deploy.sh` with `GIT_SHA` exported. A
`docker compose up -d --build` rebuilds the image and restarts the
container. Verify with `curl https://mealie-agent.epetersons.com/api/health`
— the `commit` field should match the short SHA you pushed.

Prompt-only changes (no code): push the `.md` file and redeploy. The
on-disk file wins because its mtime is newer than the DB row.

## Upgrading the strands-pg framework

```bash
bash <(curl -sSL https://raw.githubusercontent.com/peterb154/strands-pgsql-agent-framework/main/install.sh) \
    . --refresh --ref v0.7.0
```

`--refresh` only touches `strands_pg/` and the framework-numbered
migrations (`001-099*.sql`). Your `app.py`, `tools/`, `prompts/`,
`Dockerfile` are left alone.

## Troubleshooting

```bash
# Agent container logs
ssh root@192.168.0.24 'docker logs --tail 100 mealie-agent-agent-1'

# Incremental sync timer
ssh root@192.168.0.24 'systemctl list-timers mealie-agent-sync --no-pager'
ssh root@192.168.0.24 'journalctl -u mealie-agent-sync.service --since "1 hour ago"'

# Full recipe resync (takes ~22 min for 5k+ recipes)
ssh root@192.168.0.24 'nohup docker compose -f /opt/mealie-agent/docker-compose.yml \
  exec -T agent python /app/scripts/sync_recipes.py --full \
  > /opt/mealie-agent/sync.log 2>&1 &'
```

NPM shim-injection gotcha (if the floating pill disappears): see
[`local_network/npm/`](https://github.com/peterb154/local_network/tree/main/npm).
The `--refresh` flag on upstream won't restore this — it's an NPM-side
patch, run the idempotent script there.

## License

MIT, same as the framework.
