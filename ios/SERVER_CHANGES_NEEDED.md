# Server Changes Needed — orchmux server.py

**Target server:** `server.py` (FastAPI, port 9889)

**Why:** the iOS app needs to stop hardcoding vault metadata and needs per-worker notes/todos alongside the existing global `/todos` feed.

---

## 1. `GET /vault-info`

Returns the vault location the server is reading from, so clients don't have to guess.

**Read** (in this order):
- `ORCHMUX_VAULT` env var → full path to vault root
- fallback to `Path.home() / "obsidian-vault"` (matches existing `/session-notes` default)

**Response shape:**
```json
{
  "vault_name": "obsidian-vault",
  "vault_path": "/Users/adi/obsidian-vault",
  "notes_path": "AI-Systems/Claude-Logs/Sessions"
}
```

- `vault_name` = last path component of the vault root (this is what Obsidian's app uses in its vault switcher)
- `notes_path` = relative path inside the vault where session notes live (same path `/session-notes/:session` already reads from)

No auth required.

---

## 2. Extend `/todos` with per-session attribution

Currently `/todos` is a flat global list of `{id, text, done}`. Add an **optional** `session` field so todos can belong to a worker *or* be global (no session).

**Schema change — add `session: Optional[str] = None` to each todo record.**

### `GET /todos` (no params)
Unchanged — returns **all** todos, each now including `session` field (nullable):
```json
[
  {"id": 1777009788604, "text": "...", "done": false, "session": null},
  {"id": 1777015000000, "text": "...", "done": false, "session": "rex"}
]
```

### `GET /todos?session=<name>`
Returns only todos matching that session:
```json
[{"id": 1777015000000, "text": "...", "done": false, "session": "rex"}]
```

### `GET /todos?session=` (empty string)
Returns only GLOBAL todos (session is null).

### `POST /todos`
Body now accepts optional `session`:
```json
{"text": "refactor the queue", "done": false, "session": "rex"}
```
Returns `{"ok": true, "id": <new_id>}` (include the id so clients can update/delete without a refetch).

### `PATCH /todos/{id}`
Partial update. Body can include any of `text`, `done`, `session`. Returns `{"ok": true}`. 404 if id missing.

### `DELETE /todos/{id}`
Returns `{"ok": true}`. 404 if id missing.

---

## Persistence

Keep using whatever file/store `/todos` currently uses. Just add `session` to each record. Migrate existing records by giving them `session: null`.

## Backwards compatibility

- Existing POSTs without `session` keep working (stored with `session: null`)
- Existing GETs without filter keep returning all todos

---

## Acceptance tests (from another machine)

```bash
B=http://localhost:9889

# 1. Vault info
curl -ksS $B/vault-info | jq
# expect: {"vault_name": "...", "vault_path": "...", "notes_path": "..."}

# 2. Create global + per-session todos
curl -ksS -X POST $B/todos -H 'Content-Type: application/json' \
  -d '{"text":"global note","done":false}'
curl -ksS -X POST $B/todos -H 'Content-Type: application/json' \
  -d '{"text":"note for rex","done":false,"session":"rex"}'

# 3. Filtered reads
curl -ksS "$B/todos?session=rex"   | jq   # → only rex todos
curl -ksS "$B/todos?session="       | jq   # → only global todos
curl -ksS "$B/todos"                | jq   # → all todos

# 4. Mark done + delete
ID=$(curl -ksS $B/todos | jq '.[0].id')
curl -ksS -X PATCH "$B/todos/$ID" -H 'Content-Type: application/json' -d '{"done":true}'
curl -ksS -X DELETE "$B/todos/$ID"
```

---

## Scope boundaries

- **Don't touch** `/session-notes/:session` — that's the markdown files feed, separate from todos
- **Don't rename** `/todos` or change existing response field names
- **No new auth/tokens** — match the current `/todos` behavior
- **Keep the diff small** — two additions + one schema field

Reply with the diff and the output of the acceptance tests when done.
