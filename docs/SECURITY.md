# Account credential security and recovery (October 2026)

## Scope
Bilibili and Douyin Cookie JSON files and the account pool are now saved as
authenticated Fernet ciphertext with atomic file replacement. The first read
of a valid old plaintext JSON file migrates its contents without changing its
in-memory structure. On POSIX, the credential directory is restricted to 0700
and new key/ciphertext files are restricted to 0600.

The default key lives in `data/plugin_data/astrbot_plugin_videos_analysis/accounts/.credentials.key`.
**Back up this key together with the encrypted files**: losing it makes
encrypted sessions unrecoverable and requires fresh logins. Never commit or
share the key. For stronger secret separation, provide
`VA_CREDENTIAL_KEY` as a 44-character URL-safe base64 Fernet key through a
protected service secret/environment setting, **before the first migration**.
Generate one with Python:
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
Once chosen, keep the same key configured across restarts. Changing from the
file-based key to an environment key requires an explicit decrypt/re-encrypt
migration (not included here); simply changing it will fail closed.

Invalid/corrupt encrypted files, malformed legacy JSON and incorrect keys are
never silently overwritten. They raise an error (or show a login-unavailable
state) rather than replacing the original credentials.

**Limitations**: This is at-rest defense against accidental plaintext leakage,
not against a host compromise: a process that can read both ciphertext and the
same-directory key can decrypt. Playwright browser profiles, historical backups,
AstrBot's provider settings and any other plugin's secrets are NOT encrypted by
this change. Keep plugin_data and browser profile backups private, limit host
access and configure secure backups separately.

## Execution recovery
An approval creates a durable `accounts/creator/executions.json` journal entry
**before** the external publishing/commenting operation begins. If an operation
is interrupted before its result is recorded, it becomes `uncertain` after a
restart and is **not retried automatically**. The `/va_pending` command
and the debugging WebUI API display these records separately from approvals
that can still be executed.

The journal intentionally does not guarantee exactly-once execution across
unreliable external APIs. A human operator must reconcile uncertain records
against the platform's actual works, comments or profile before repeating an
action. Keep the journal, published.jsonl and pending.json in backups.
There is no automatic reconciliation or delete/reset command in this release.
The journal has a bounded history of 200 completed actions plus all unresolved
actions. Legacy approvals created before this update remain supported.

## Testing
`python -m pytest -q tests/test_security_hardening.py tests/test_creator_ops.py tests/test_redact.py`
`python -m compileall -q services utils handlers main.py douyin_login.py`

The supplied tests cover plaintext migration, restricted permissions, loss of
decryption key, corrupted legacy data, cancellation during an external
operation and refusal to begin if durable logging fails. Platform integration,
browser storage, and real account recovery are not part of the offline tests.


## Bilibili download and creator workflow hardening

- QR images are one-time login credentials. The QR payload/base64 and rendered QR matrix are not logged; temporary local QR images use restrictive file permissions and are deleted after the login attempt.
- Cookie validity uses `/x/web-interface/nav`, keyed by a SHA-256 digest of the cookie header (the raw cookie is never used as a cache key). Positive results live for six hours; explicit invalid results for one minute. Network failures are not cached as credential expiry and do not trigger account rotation.
- Bilibili API calls share a bounded `aiohttp` session that is closed by the plugin lifecycle. A download pipeline is serialized, and the parsed view/playurl result is passed through to the downloader rather than requesting playurl twice.
- Explicit anti-abuse responses `-352`, `-412`, HTTP 412 and HTTP 429 open a persistent circuit until the next China-local day. The plugin falls back to the anonymous path and notifies configured administrators once. A generic `-403` is not treated as rate limiting because it may be content/permission-specific. If the circuit file cannot be written, the current process still fails closed.
- This does **not** impersonate a different browser TLS fingerprint, bypass CAPTCHA, rotate accounts to evade a platform block, or retry a blocked endpoint. The existing refresh-token, buvid, bili_ticket and WBI support are retained; they are not new bypass guarantees.
- Subtitle-first transcription already existed and is retained. Video understanding summaries are now stored in a TTL-backed SQLite cache (default seven days, configurable through `analysis_cache_ttl_days`); the previous `video_context.json` is imported once.

## LLM-generated public text

External comments, usernames, titles and descriptions are passed as JSON data and explicitly treated as untrusted content, not instructions. Generated public replies/comments must satisfy a strict JSON schema and text validation before an approval request is created. The existing administrator confirmation and write quota remain mandatory; model output never authorizes publication by itself.
