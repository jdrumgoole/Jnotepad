# Jnotepad

A note-taking web app for keeping track of notes, links and images.

- **Capture:** one box for everything. Type a note, paste a URL (a bare URL becomes a link
  item), or paste / drag / pick images. Add tags if you want, then press Ctrl+Enter.
- **Find:** search across title, text, URL and tags, and filter by kind (note / link / image)
  or by tag.
- **Auth:** username + password through [regstack](https://github.com/jdrumgoole/regstack):
  Argon2 hashing, JWT with revocation on logout and on password change, and per-account
  lockout after repeated failures.
- **Storage:** [SecantusDB](https://github.com/jdrumgoole/SecantusDB)'s Rust server
  (`secantusd-rs`), a MongoDB wire-protocol server on WiredTiger. The app starts it as a child
  process on a loopback ephemeral port and stops it cleanly on exit. The app talks to it with
  plain `pymongo`.

## Run it

You need the `secantusd-rs` binary. It's a Rust crate, not a Python package. It needs
rustc 1.98 or newer:

```bash
rustup update stable
cargo install secantus-mdb --version 0.5.3-beta.196 --locked   # installs ~/.cargo/bin/secantusd-rs
```

Then:

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/jnotepad                 # http://127.0.0.1:8000
```

The app looks for `secantusd-rs` on `$PATH`, then in `~/.cargo/bin`. If it can't find it, it
refuses to start and prints the install command. It does not silently fall back to the
slower Python server.

Data goes to `./data/`: `data/secantus/` holds the WiredTiger files, and `data/jwt_secret` is
generated on first run with mode 0600. Back up that directory.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `JNOTEPAD_DATA_DIR` | `./data` | Database store and generated JWT secret |
| `JNOTEPAD_ENGINE` | `rust` | `rust` (managed `secantusd-rs`) or `python` (in-process reference server, `pip install -e ".[python-server]"`) |
| `JNOTEPAD_SECANTUSD_BIN` | auto | Path to `secantusd-rs` |
| `JNOTEPAD_SECANTUS_CACHE_SIZE` | `256M` | WiredTiger cache (the server's own default is 4G) |
| `JNOTEPAD_MONGODB_URL` | unset | Connect to a server you run yourself; nothing is started |
| `JNOTEPAD_DATABASE` | `jnotepad` | Database name |
| `JNOTEPAD_JWT_SECRET` | generated | Set explicitly if you don't want it on disk |
| `JNOTEPAD_JWT_TTL_SECONDS` | 604800 | Login lifetime (7 days) |
| `JNOTEPAD_ALLOW_REGISTRATION` | `true` | Turn off after creating your account(s) |
| `JNOTEPAD_MAX_IMAGE_BYTES` | 8 MiB | Per-image limit (images are stored inline, so this stays < 16 MiB) |

### How the database server is managed

- The app starts `secantusd-rs --host 127.0.0.1 --port 0 ...`, reads the real port from its
  `listening on` line, and passes that URI to regstack and to the app's own client.
- On shutdown it sends SIGTERM, which makes the server checkpoint and close WiredTiger. If the
  server hasn't exited after 30s, it is killed.
- On Linux, the child is bound to the app with `PR_SET_PDEATHSIG`. If the app is SIGKILLed,
  the server still gets SIGTERM and doesn't hang around holding the data-dir lock. Tested: no
  orphan, and the data was intact on restart.
- The Python and Rust servers share the on-disk format. A store written by the Python server
  opened in the Rust one with users, items and images intact, so `JNOTEPAD_ENGINE` can be
  switched on an existing data dir. Only one server can hold a directory at a time (WiredTiger
  takes an exclusive lock). A second instance fails to start with a clear error.
- Want to run the server yourself, e.g. under systemd? Start `secantusd-rs` and set
  `JNOTEPAD_MONGODB_URL`.

## Tests

```bash
.venv/bin/pytest                          # real secantusd-rs per run, temp data dir
JNOTEPAD_ENGINE=python .venv/bin/pytest   # same suite on the Python server
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
- SecantusDB is beta software. The app is tested against `secantusd-rs 0.5.3-beta.196` and
  logs a warning if it finds a different version. It can't pin the binary the way pip pins a
  package, so it is up to you which version is installed. regstack is pinned to `0.9.3`
  because the integration relies on its route and response shapes.
