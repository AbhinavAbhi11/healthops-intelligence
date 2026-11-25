# Prototype v1 release checklist

## Repository hygiene

- [ ] `.env` and all `.env.*` secret files are excluded (except an intentionally redacted `.env.example`).
- [ ] `.venv/`, `instance/`, `data/`, uploaded datasets, local database files, backups and logs are excluded.
- [ ] No credentials/API keys/private keys are present in staged files or Git history.
- [ ] No dataset with unknown redistribution rights is committed.
- [ ] A license decision has been made before inviting reuse.

## Documentation and CI

- [ ] README describes current prototype scope and limitations.
- [ ] Architecture and security boundaries are documented.
- [ ] GitHub Actions test workflow passes.
- [ ] Known Ask Your Data follow-up routing limitation is disclosed.

## Hosted demo gate

- [ ] Hosted host/port support has been implemented and tested.
- [ ] Separate cloud PostgreSQL database and roles are configured.
- [ ] Migrations are applied to the cloud database.
- [ ] Secrets are configured outside Git.
- [ ] Only synthetic or properly de-identified data is used.
- [ ] Single-user/shared-workspace limitation is clearly disclosed.
- [ ] File persistence behaviour is explicitly configured and tested.