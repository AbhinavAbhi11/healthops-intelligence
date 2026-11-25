# Deployment readiness and hosting plan

**Current state:** Not deployment-ready as a public multi-user service. This document describes a restricted synthetic-data prototype deployment path only.

## Blocking items before a live app URL

1. **Host and port binding:** the current runtime uses the values loaded by `settings.py`. Render web services must listen on `0.0.0.0` and the port supplied in the `PORT` environment variable (default 10000). Add a narrowly scoped hosted-runtime configuration and tests before creating a Render service.
2. **Workspace isolation:** `SESSION` and `JOBS` are process-wide. Authentication does not yet bind every analytics operation to a per-user/per-organization workspace. Keep the hosted demo restricted to one operator and synthetic data until this is redesigned.
3. **Cloud PostgreSQL:** the laptop's local `healthops_validation_db` is not available to a cloud service. Provision a separate managed PostgreSQL database, create the required least-privilege roles, and apply migrations using the migrator role. Never expose the laptop database to the internet.
4. **Secrets:** configure app-role credentials, migrator credentials (only for a migration step), and the optional LLM key using the host's secret settings. Do not commit `.env`.
5. **Persistence:** uploads and local snapshots under `instance/` need persistent storage. Render Free web services have an ephemeral filesystem; local changes are lost when they restart, spin down or redeploy. Use an appropriate persistent disk/object store or make the hosted demo explicitly disposable and re-upload data for each session.
6. **Database retention:** do not rely on a temporary hosted database for durable user accounts. Render's Free Postgres database expires after 30 days. Keep a separate managed database with a retention policy and backups.
7. **Security boundary:** run only synthetic or properly de-identified data. Do not describe this prototype as HIPAA/GDPR compliant or as a production clinical system.

## Recommended prototype stack

- **Web service:** Render Web Service connected to the GitHub repository.
- **PostgreSQL:** a separate managed Postgres project. Neon currently offers a free prototype tier with monthly compute/storage/egress quotas and scale-to-zero; quota exhaustion suspends connections until the quota resets or the plan is upgraded. Review current limits before relying on it.
- **Repository CI:** GitHub Actions runs Python compilation and the deterministic unittest suite with the LLM disabled.

## Render-specific requirements

- Web service binds to `0.0.0.0` and reads `PORT` from the environment.
- Use `pip install -r requirements.txt` as the build command after confirming it installs on Linux/Python 3.13.
- Start command will be `python server.py` only after the host/port runtime patch is implemented and verified.
- Configure `/api/health` as the health-check path.
- Configure database and LLM values in Render's environment/secrets panel, never in the repository.
- Review the free-plan behaviour: services sleep after 15 minutes without inbound traffic; their filesystem is ephemeral; Free Postgres expires 30 days after creation. Free plans are for prototypes, not production.

## Pre-deploy checklist

- [ ] Repository contains source, tests, migrations and documentation only.
- [ ] `.env`, keys, local datasets, `instance/`, backups and `.venv/` are ignored and absent from Git history.
- [ ] CI passes on a clean GitHub runner.
- [ ] Hosted host/port support is implemented and tested.
- [ ] Hosted PostgreSQL roles and migrations are created in a separate cloud database.
- [ ] Owner account is created using a strong unique password.
- [ ] No real patient data is loaded.
- [ ] Workspace isolation and single-user demo limitations are clearly disclosed.
- [ ] Persistent storage or disposable-session behaviour is explicitly chosen.
- [ ] Health endpoint and login are tested on the deployed URL.

## Official platform references

- Render web services and port binding: https://render.com/docs/web-services
- Render free plan limitations: https://render.com/docs/free
- Render environment variables/secrets: https://render.com/docs/configure-environment-variables
- Neon free-plan limits: https://neon.com/docs/introduction/plans
- Neon connection strings: https://neon.com/docs/get-started/connect-neon