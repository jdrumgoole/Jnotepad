# Jnotepad

A note-taking web app for keeping track of notes, links and images.

- **Capture:** one box for everything. Type a note, paste a URL (a bare URL becomes a link
  item), or paste / drag / pick images. Add tags if you want, then press Ctrl+Enter.
- **Find:** search across title, text, URL and tags, and filter by kind (note / link / image)
  or by tag.
- **Auth:** username + password through [regstack](https://github.com/jdrumgoole/regstack):
  Argon2 hashing, JWT with revocation on logout and on password change, and per-account
  lockout after repeated failures.
- **Storage:** [SecantusDB](https://github.com/jdrumgoole/SecantusDB), a MongoDB wire-protocol
  server on WiredTiger, embedded in-process by default. The app talks to it with plain
  `pymongo`.

## Run it

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/jnotepad                 # http://127.0.0.1:8000
```

Data goes to `./data/`: `data/secantus/` holds the database files, and `data/jwt_secret` is
generated on first run with mode 0600. Back up that directory.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `JNOTEPAD_DATA_DIR` | `./data` | Database store and generated JWT secret |
| `JNOTEPAD_MONGODB_URL` | unset | Use an external MongoDB-wire server instead of the embedded one |
| `JNOTEPAD_DATABASE` | `jnotepad` | Database name |
| `JNOTEPAD_JWT_SECRET` | generated | Set explicitly if you don't want it on disk |
| `JNOTEPAD_JWT_TTL_SECONDS` | 604800 | Login lifetime (7 days) |
| `JNOTEPAD_ALLOW_REGISTRATION` | `true` | Turn off after creating your account(s) |
| `JNOTEPAD_MAX_IMAGE_BYTES` | 8 MiB | Per-image limit (images are stored inline, so this stays < 16 MiB) |

### Using the Rust SecantusDB server instead

The embedded server is SecantusDB's pure-Python reference implementation, which is fine for a
personal notebook. For the faster Rust server:

```bash
cargo install secantus-mdb --version <current beta>
secantusd-rs --port 27017 --storage-path ./data/secantus-rs
JNOTEPAD_MONGODB_URL=mongodb://127.0.0.1:27017 .venv/bin/jnotepad
```

Python and Rust servers use the same on-disk format, but only one process can hold a
directory at a time (WiredTiger takes an exclusive lock).

## Tests

```bash
.venv/bin/pytest        # runs against a real embedded SecantusDB in a temp dir
.venv/bin/ruff check .
```

## Design notes / known trade-offs

- **Usernames on top of an email-only library.** regstack identifies users by email, so
  `joe` is stored as `joe@users.jnotepad.internal`. `.internal` is an ICANN-reserved
  private TLD, so these addresses can never deliver mail. The public `/api/session/*`
  endpoints forward to regstack's JSON router, which is mounted on a **private** in-process
  ASGI app. That keeps regstack's email routes (change-email, forgot-password, ...) off the
  public surface. A consequence: **there is no password reset.** If you forget your password,
  you need to edit the user document in the database. Email verification, password reset and
  account deletion are disabled in the regstack config.
- **The token lives in `localStorage`** because regstack only supports bearer tokens, not
  cookies. Any XSS would expose it. Mitigations: all user content is rendered with
  `textContent`; links are restricted to `http(s)`; images are type-sniffed from their magic
  bytes, SVG is refused, and they are served with `nosniff`; and a strict CSP (`script-src
  'self'`, no inline script) is set.
- **Images are stored inline in BSON** (`images` collection), not in GridFS. This is simple and
  fine for screenshots and photos, but it is why there is a per-image size cap.
- **Search is an escaped, case-insensitive regex scan** over the user's own items. It is fine
  for thousands of items, but it isn't full-text search and won't scale to very large
  collections.
- **Bind address is 127.0.0.1.** There is no TLS. Put it behind a reverse proxy with HTTPS if
  you expose it beyond localhost. There is also no per-IP rate limiting (regstack supports it
  via `slowapi` if needed).
- SecantusDB is beta software (pinned to `0.7.0b3`), and regstack is pinned to `0.9.3`
  because the integration relies on its route and response shapes.
