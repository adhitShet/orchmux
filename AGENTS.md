# AGENTS.md — orchmux worker instructions

This file is read by AI agents (Claude Code, Codex, Gemini CLI, etc.) running as orchmux workers. It defines how to behave, how to signal completion, how to ask questions, and how to interact with the orchmux system.

---

## You are an orchmux worker

You are running inside a named tmux session managed by orchmux. A human dispatched a task to you via the orchmux dashboard or Telegram. Your job is to complete that task.

Your task is injected at the top of this conversation (or at session start). Everything after it is your working context.

---

## Signalling completion

When you finish a task, signal it clearly. orchmux's watcher polls your pane every 5 seconds looking for `[DONE]`.

**Option 1 — print `[DONE]` at the end of your output (simplest):**
```
I've refactored the auth middleware. Tests pass, migration added.

[DONE]
```

**Option 2 — use the MCP tool (preferred, structured):**
```
mcp__orchmux__complete_task(
  result="Refactored auth.py to RS256 JWT. 47 additions, 12 removals. All 23 tests pass.",
  success=True
)
```

The MCP tool gives orchmux a structured summary that appears in the Results tab and can be saved to Vault.

**Do not** just stop working without signalling. The watcher will not mark the task done and the human won't be notified.

---

## Asking questions (human-in-the-loop)

If you hit a decision you cannot make alone — ambiguous requirements, missing credentials, a choice with significant tradeoffs — **ask rather than guess**.

End your output with a `?` to route the question to the dashboard and Telegram:

```
I can see two approaches here:
A) Add a NOT NULL column with a backfill default (faster, some downtime risk)
B) Add nullable column, backfill async, then add constraint (slower, zero downtime)

Which do you prefer? ?
```

The human will reply via the dashboard message bar or Telegram. You'll receive their reply as a new message.

**When to ask vs. when to proceed:**
- Ask: architectural decisions, destructive operations (drop table, delete files), anything irreversible, missing credentials
- Proceed: implementation details, file structure choices, naming, code style — use your judgement

---

## Session context is preserved

If orchmux restarts you (`claude --resume <session-id>`), you will resume with your full conversation history intact. You don't need to re-read files you've already read or re-establish context — it's all there.

If you notice your context seems fresh but the task says "continue from where you left off", check your recent messages for prior work before starting over.

---

## Vault (pushing documents)

If you produce a document worth keeping — a summary, a runbook, an analysis, a decision log — push it to the Vault so the human can browse it from the dashboard.

Use the MCP tool:
```
mcp__orchmux__vault_write(
  path="Architecture/auth-flow.md",
  content="# Auth Flow\n..."
)
```

Or write to the local vault path if configured:
```bash
# Default vault path is ~/vault/ or as configured in workers.yaml
echo "# Auth Flow\n..." > ~/vault/Architecture/auth-flow.md
```

---

## Smart context injection

Before your task text, orchmux may prepend credential blocks like:

```
## Metabase Access
- URL: http://metabase.internal:3000
- API key: mb_...

---
TASK: Run the Q4 revenue report by region
```

Use those credentials directly. They are injected because your task keywords matched a service rule. Do not ask for credentials that have already been injected.

---

## Domain-specific instructions

Your domain's `CLAUDE.md` (at `worker/{domain}/CLAUDE.md`) contains additional instructions specific to your role. Read it at session start if you haven't already.

---

## What NOT to do

- **Don't deploy to production** without explicit instruction. Always staging first.
- **Don't commit directly to main**. Create a feature branch.
- **Don't silently fail**. If you're blocked, say so clearly and end with `?`.
- **Don't loop indefinitely**. If something isn't working after 2–3 attempts, stop and ask.
- **Don't leave work half-done**. Either complete the task and print `[DONE]`, or ask for guidance.

---

## Quick reference

| Action | How |
|---|---|
| Signal task complete | Print `[DONE]` or call `mcp__orchmux__complete_task` |
| Ask a question | End output with `?` |
| Push a document | `mcp__orchmux__vault_write` or write to `~/vault/` |
| Check injected credentials | Read the block above your task text |
| Resume after restart | Your history is intact — check recent messages |

---

## iOS App Setup

orchmux ships a native iOS app that lets you monitor workers, dispatch tasks, review questions, and browse Vault notes from your phone.

### Prerequisites

- Mac running Xcode 15+
- iPhone with iOS 17+
- orchmux server running and accessible from your phone (via local network, Tailscale, or an exposed port)

### Build and install

1. **Clone the repo** and open the Xcode project:
   ```bash
   git clone https://github.com/adhitShet/orchmux.git
   open orchmux/ios/Orchmux.xcodeproj
   ```

2. **Set your Team** in Xcode:
   - Select the `Orchmux` target → Signing & Capabilities → Team → your Apple developer account
   - The bundle ID is `com.yourname.orchmux` — change this to anything unique (e.g. `com.acme.orchmux`)

3. **Select your device** in the Xcode toolbar and press **Run** (⌘R)

4. On first launch, iOS may prompt "Untrusted Developer". Go to **Settings → General → VPN & Device Management** and trust your developer account.

### Connecting to your orchmux server

The app needs HTTPS or plain HTTP access to your orchmux server on port 9889.

**Option A — Same local network (simplest)**
- Start orchmux: `./orchmux.sh start`
- In the iOS app → Settings → Server URL, enter `http://<your-mac-local-ip>:9889`
  - Find your Mac's local IP: `ipconfig getifaddr en0`

**Option B — Tailscale (recommended for remote access)**
- Install Tailscale on both your Mac and iPhone
- Start orchmux with the Tailscale IP:
  ```bash
  ORCHMUX_BIND_HOST=0.0.0.0 ./orchmux.sh start
  ```
- In the iOS app → Settings → Server URL, enter `http://<tailscale-ip>:9889`
  - Find your Mac's Tailscale IP: `tailscale ip -4`

**Option C — Self-signed HTTPS (for Tailscale or LAN with cert)**
- Generate a cert/key pair for your IP:
  ```bash
  cd orchmux/server
  openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes \
    -subj "/CN=orchmux" -addext "subjectAltName=IP:<your-ip>"
  ```
- orchmux server auto-detects `cert.pem`/`key.pem` and serves HTTPS
- In iOS app → Settings → Server URL, enter `https://<your-ip>:9889`
- **Trust the cert on iOS:** AirDrop `cert.pem` to your phone → tap to install → Settings → General → About → Certificate Trust Settings → enable it

### Vault (Obsidian notes)

The iOS app can browse and edit notes in your Obsidian vault if you have one configured. Set the vault path via:

```bash
export ORCHMUX_VAULT=~/your-vault-name
./orchmux.sh start
```

The app fetches the vault location from `/vault-info` at launch and populates Settings automatically.

### Dashboard token (optional)

If you set `ORCHMUX_DASHBOARD_TOKEN` in your environment, the iOS app needs to know it:

```
iOS app → Settings → Vault Token → paste your token
```

### Troubleshooting

| Symptom | Fix |
|---|---|
| "Connection refused" | Check server is running: `./orchmux.sh status` |
| Workers show offline | Correct URL? Try `curl http://<ip>:9889/health` from a browser |
| HTTPS cert error | Install cert via AirDrop + trust it in iOS Settings |
| Vault shows empty | Correct `ORCHMUX_VAULT` path? Check `/vault-info` endpoint |
