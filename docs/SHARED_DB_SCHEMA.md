# Shared Database Schema — `invorto-db` Submodule Approach

## Why

Both `invorto-ui` and `invorto-voice-ai` share the same Supabase Postgres instance.
Currently migrations are split across two repos with a circular FK dependency:

```
Supabase migrations ──REFERENCES──► assistants    (created by voice-ai 001)
voice-ai migration 011 ─REFERENCES─► organizations (created by Supabase baseline)
```

Neither set of migrations can run first without the other already being applied.
A new environment setup has no documented, reliable order.

The fix: a standalone `invorto-db` repo that owns **all** SQL migrations.
Both service repos consume it as a git submodule, pinned to a specific version tag.
This is the same pattern as JOOQ shared schema modules in Java — one versioned
schema artifact, multiple consumers.

---

## Repository: `invorto-db`

### Structure

```
invorto-db/
├── migrations/
│   │
│   │   ── Supabase / org layer ──────────────────────────────────────────
│   ├── 0001_baseline_orgs_users.sql          # organizations, org_users, global_admins,
│   │                                         # org_bots, campaigns, call_queue etc.
│   │                                         # (from: supabase 20260101000000_baseline.sql)
│   │
│   │   ── Voice-AI backend tables ──────────────────────────────────────
│   ├── 0002_assistants_phone_numbers_calls.sql  # assistants, phone_numbers, calls
│   │                                            # (from: voice-ai 001_initial_schema.sql)
│   │
│   │   ── Cross-reference FKs (safe now — both tables exist above) ─────
│   ├── 0003_add_org_id_to_backend_tables.sql    # assistants/phone_numbers/calls.org_id → organizations
│   │                                            # (from: voice-ai 011)
│   ├── 0004_switch_bot_fks_to_assistants.sql    # campaigns/org_bot_assignments.bot_id → assistants
│   │                                            # (from: supabase 20260223120000)
│   │
│   │   ── Incremental changes in chronological order ───────────────────
│   ├── 0005_add_twilio_credentials.sql          # (voice-ai 002)
│   ├── 0006_seed_data.sql                       # (voice-ai 003)
│   ├── 0007_add_transcriber_settings.sql        # (voice-ai 004)
│   ├── 0008_add_jambonz_credentials.sql         # (voice-ai 005)
│   ├── 0009_add_jambonz_trunk_name.sql          # (voice-ai 006)
│   ├── 0010_add_voice_model.sql                 # (voice-ai 007)
│   ├── 0011_add_mcube_support.sql               # (voice-ai 008)
│   ├── 0012_add_recording_url.sql               # (voice-ai 009)
│   ├── 0013_add_vad_settings.sql                # (voice-ai 010)
│   ├── 0014_add_call_source.sql                 # (supabase 20260218120000)
│   ├── 0015_add_bot_id_to_call_queue.sql        # (supabase 20260223130000)
│   ├── 0016_create_org_api_keys.sql             # (supabase 20260316000100)
│   ├── 0017_add_max_api_keys.sql                # (supabase 20260316000200)
│   ├── 0018_create_org_api_key_audit_logs.sql   # (supabase 20260316000300)
│   ├── 0019_grant_api_key_permissions.sql       # (supabase 20260316000400)
│   └── 0020_add_max_active_api_keys.sql         # (supabase 20260316000500)
│
├── migrate.py          # standalone migration runner (psycopg2, no framework deps)
├── CHANGELOG.md        # one entry per version tag — what changed and why
└── README.md
```

### Key design decisions

**Naming:** `NNNN_description.sql` — simple incrementing integer prefix.
No timestamps needed since ordering is explicit and there is only one source.

**Cross-repo FKs resolved:** By placing both `organizations` (0001) and `assistants`
(0002) in the same file sequence, migration 0003 can safely add the cross FKs.
The circular dependency is eliminated by controlling order within one repo.

**Single tracking table:** `migration_history` — one row per applied migration.
Supabase CLI is replaced as the migration tool for schema changes (see below).
Supabase CLI is still used for local dev spin-up, Edge Functions, and RLS
policy editing but not for applying production schema migrations.

---

## `migrate.py` — the shared runner

Lives in `invorto-db/`. Both service repos invoke it via the submodule path.
It is a self-contained Python script with no dependencies beyond `psycopg2`.

```python
# invorto-db/migrate.py
#!/usr/bin/env python3
"""
Invorto shared schema migration runner.
Applies all *.sql files in ./migrations/ in filename order.
Tracks applied migrations in the migration_history table.

Usage:
    python migrate.py                    # apply pending migrations
    python migrate.py --status           # show applied / pending
    python migrate.py --dry-run          # print SQL without applying
    python migrate.py --migrations-dir   # override migrations directory path
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")
DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def get_connection():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def ensure_migration_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS migration_history (
                id          SERIAL PRIMARY KEY,
                migration_name VARCHAR(255) NOT NULL UNIQUE,
                applied_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()


def get_applied(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT migration_name FROM migration_history")
        return {r[0] for r in cur.fetchall()}


def get_pending(migrations_dir: Path, applied: set) -> list[Path]:
    return [
        f for f in sorted(migrations_dir.glob("*.sql"))
        if f.stem not in applied
    ]


def run_migration(conn, path: Path, dry_run: bool) -> bool:
    sql = path.read_text()
    if dry_run:
        print(f"\n-- DRY RUN: {path.name} --\n{sql}")
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO migration_history (migration_name) VALUES (%s)",
                (path.stem,)
            )
        conn.commit()
        print(f"  ✓  {path.name}")
        return True
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  ✗  {path.name}: {e}", file=sys.stderr)
        return False


def cmd_migrate(migrations_dir: Path, dry_run: bool):
    conn = get_connection()
    ensure_migration_table(conn)
    pending = get_pending(migrations_dir, get_applied(conn))
    if not pending:
        print("✓ All migrations are up to date.")
        return
    print(f"Applying {len(pending)} migration(s)...")
    for path in pending:
        if not run_migration(conn, path, dry_run):
            sys.exit(1)
    conn.close()


def cmd_status(migrations_dir: Path):
    conn = get_connection()
    ensure_migration_table(conn)
    applied = get_applied(conn)
    pending = get_pending(migrations_dir, applied)
    print(f"\nApplied  ({len(applied)}):")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT migration_name, applied_at FROM migration_history ORDER BY id")
        for row in cur.fetchall():
            print(f"  ✓  {row['migration_name']}  ({row['applied_at'].date()})")
    if pending:
        print(f"\nPending  ({len(pending)}):")
        for p in pending:
            print(f"  ○  {p.name}")
    else:
        print("\n  (none pending)")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status",   action="store_true")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--migrations-dir", type=Path, default=DEFAULT_MIGRATIONS_DIR)
    args = parser.parse_args()

    if args.status:
        cmd_status(args.migrations_dir)
    else:
        cmd_migrate(args.migrations_dir, args.dry_run)
```

---

## Adding the submodule to each repo

Run once per repo (you do this, not in CI):

```bash
# In invorto-voice-ai
git submodule add git@bitbucket.org:invorto/invorto-db.git db
git submodule update --init
git add .gitmodules db
git commit -m "chore: add invorto-db as shared schema submodule"

# In invorto-ui
git submodule add git@bitbucket.org:invorto/invorto-db.git db
git submodule update --init
git add .gitmodules db
git commit -m "chore: add invorto-db as shared schema submodule"
```

After this, `db/` in each repo is a pointer to a specific commit in `invorto-db`.
Running `git submodule update --init` in a fresh clone hydrates it.

---

## `invorto-voice-ai` changes

### Makefile

```makefile
# Before
migrate:
    cd migrations && python migrate.py

# After
migrate:
    python db/migrate.py

migrate-status:
    python db/migrate.py --status

migrate-dry-run:
    python db/migrate.py --dry-run
```

### Remove old migrations folder

```bash
# After confirming all SQL is consolidated into invorto-db:
git rm -r migrations/
git commit -m "chore: remove local migrations — now managed via invorto-db submodule"
```

Keep `migrations/migrate.py` deleted too — `db/migrate.py` replaces it.

### CI (bitbucket-pipelines.yml)

```yaml
# Add submodule init step before any migrate step
- step:
    name: Run DB migrations
    script:
      - git submodule update --init --recursive
      - python db/migrate.py
```

---

## `invorto-ui` changes

### Replace `supabase db push` for schema migrations

`supabase db push` is replaced by `python db/migrate.py` for applying schema changes
to a real database. The Supabase CLI is kept only for:

- `supabase start` — local dev postgres + studio
- `supabase gen types typescript` — regenerating TS types after schema changes
- Edge Functions deployment

```bash
# Before — applied Supabase-only migrations
supabase db push

# After — applies all migrations (org + backend tables) in correct order
python db/migrate.py
```

### Local dev with Supabase CLI

For local development, the Supabase CLI spins up a local Postgres.
You still need to seed it with the full schema. Two options:

**Option A — symlink (cleanest):**
```bash
# Remove existing migrations folder, replace with symlink to submodule
rm -rf supabase/migrations
ln -s ../db/migrations supabase/migrations
# supabase start will now use invorto-db migrations automatically
```

**Option B — script (safer if you don't want to change the supabase/ folder):**
```bash
# package.json script
"db:migrate": "python db/migrate.py",
"db:migrate:status": "python db/migrate.py --status",
"db:types": "supabase gen types typescript --project-id $SUPABASE_PROJECT_ID > src/types/supabase.ts"
```

### CI (Bitbucket / GitHub Actions)

```yaml
- step:
    name: Apply DB migrations
    script:
      - git submodule update --init --recursive
      - pip install psycopg2-binary
      - python db/migrate.py
```

---

## Version pinning workflow

This is the JOOQ equivalent of bumping a library version.

### Making a schema change

```bash
# 1. Work in invorto-db repo
cd invorto-db
# Add new migration file
echo "ALTER TABLE assistants ADD COLUMN ..." > migrations/0021_add_xyz.sql
git add migrations/0021_add_xyz.sql
git commit -m "feat: add xyz to assistants"
git tag v1.3.0
git push origin main --tags

# 2. Bump the pin in invorto-voice-ai
cd ../invorto-voice-ai
cd db && git fetch && git checkout v1.3.0 && cd ..
git add db
git commit -m "chore(db): bump schema to v1.3.0 — add xyz to assistants"

# 3. Bump the pin in invorto-ui (if the change affects the frontend)
cd ../invorto-ui
cd db && git checkout v1.3.0 && cd ..
git add db
git commit -m "chore(db): bump schema to v1.3.0"
```

### New environment setup (after this is in place)

```bash
# 1. Clone either service repo
git clone git@bitbucket.org:invorto/invorto-voice-ai.git
cd invorto-voice-ai

# 2. Hydrate the submodule
git submodule update --init

# 3. Set DATABASE_URL to your Supabase Postgres
export DATABASE_URL=postgresql://postgres:<pw>@db.<ref>.supabase.co:5432/postgres

# 4. Run migrations — all tables, correct order, one command
make migrate
#  Applying 20 migration(s)...
#    ✓  0001_baseline_orgs_users.sql
#    ✓  0002_assistants_phone_numbers_calls.sql
#    ...
#    ✓  0020_add_max_active_api_keys.sql
#  Done.

# That's it. No separate supabase db push needed.
```

---

## Migration to this approach (one-time steps)

1. **Create `invorto-db` repo** — empty, main branch
2. **Consolidate migrations** — merge and renumber all SQL from both repos in the
   order shown in the structure above, resolving the circular FK by ordering
   `organizations` before `assistants` before the cross-reference migration
3. **Tag `v1.0.0`** on the consolidated state
4. **Add submodule** to both service repos pointing at `v1.0.0`
5. **Handle existing databases** — existing envs already have all tables applied.
   Seed `migration_history` with all 20 migration names so the runner skips them:
   ```sql
   INSERT INTO migration_history (migration_name) VALUES
     ('0001_baseline_orgs_users'),
     ('0002_assistants_phone_numbers_calls'),
     -- ... all 20
     ('0020_add_max_active_api_keys')
   ON CONFLICT DO NOTHING;
   ```
6. **Remove old migration folders** from both service repos
7. **Update CI** in both repos to add `git submodule update --init` step

---

## Trade-offs

| | Submodule approach | Current split |
|---|---|---|
| New env setup | 1 command | Undocumented order, 2 toolchains |
| Schema change | PR in invorto-db + pin bump in service repos | PR in whichever repo "owns" it |
| Circular FK | Eliminated (controlled ordering) | Exists, blocks clean setup |
| Voice-ai standalone | ✓ (just needs DATABASE_URL, any Postgres) | ✗ (needs Supabase specifically) |
| Supabase local dev | Symlink or script sync | Native `supabase db push` |
| Team overhead | Slightly more (extra repo, pin bumps) | Less upfront, more confusion later |
