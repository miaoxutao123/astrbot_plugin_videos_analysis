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
