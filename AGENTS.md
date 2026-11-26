# AGENTS.md

Claude Code plugin: list and read a Google Drive folder through OAuth, read-only.
Human setup guide: `README.md` (Traditional Chinese). Design and trade-offs: `docs/ARCHITECTURE.md`.

## Try the plugin

The OAuth backend is already deployed (`https://gdrive-reader-auth.adsfaaron.workers.dev`) and the plugin points to it.
Trying the plugin needs no Google Cloud or Cloudflare credentials. Deploy your own copy (README section 三) only if asked to.

1. Check that `uv --version` works. The plugin starts its MCP server with uv.
2. In Claude Code:
   ```text
   /plugin marketplace add ADSFAaron/claude-gdrive-reader
   /plugin install gdrive-reader@gdrive-reader-marketplace
   /reload-plugins
   ```
   `/mcp` should list `plugin:gdrive-reader:gdrive` as connected. The first start downloads dependencies (10–20 s).
3. Ask Claude to list a folder and paste the Drive folder link. If no account is connected, Claude calls `connect_google`, which opens a browser.
4. **A human must finish the Google sign-in in that browser, on the same machine. An agent cannot do this step.**
   - Sign in with the Google account the folder was shared with.
   - "Google hasn't verified this app" is expected (restricted scope, unverified app): choose Advanced, then continue.
5. Ask Claude to read a PDF and an image from the listing.
   - PDFs return their text layer. Scanned PDFs report that they have no text layer.
   - Images return metadata plus a note that image content is not interpreted. This is by design.

MCP tools: `connect_google`, `auth_status`, `disconnect_google`, `list_folder`, `read_file`.

## Code map

| Concern | File |
| --- | --- |
| MCP tool definitions | `plugin/src/gdrive_reader/server.py` |
| OAuth: PKCE, loopback listener, `state` check | `plugin/src/gdrive_reader/oauth.py` |
| Token refresh and revocation | `plugin/src/gdrive_reader/auth.py` |
| Token file (mode 0600) | `plugin/src/gdrive_reader/token_store.py` |
| Drive API calls, retries, error messages | `plugin/src/gdrive_reader/drive.py` |
| Per-type reading (PDF, image, text, Google Docs) | `plugin/src/gdrive_reader/reader.py` |
| Backend: consent redirect, code exchange; holds the client secret | `backend/src/worker.py` |

## Tests

```bash
cd plugin  && uv run pytest && uv run ruff check .
cd backend && uv run pytest && uv run ruff check .
```

The automated tests mock Google. They do not replace the sign-in and reading steps above.

## Rules for changes

- Never commit secrets: `backend/.dev.vars`, `client_secret*.json`, `token.json`.
- The MCP server uses stdout for the protocol. Log to stderr only.
