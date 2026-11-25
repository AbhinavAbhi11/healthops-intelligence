# Security and data-use policy

## Intended use

HealthOps Intelligence is an experimental software prototype for synthetic or properly de-identified healthcare and hospital-operations datasets. It is not a medical device, clinical decision-support system, or certified healthcare data-processing service.

## Never upload

- Directly identifying patient data (names, MRNs, addresses, contact details, dates of birth, or similar identifiers).
- Protected health information or any identifiable health record.
- Hospital production exports, credentials, private keys, access tokens, or confidential documents.

## Secrets

- Keep `.env`, database credentials, API keys, session secrets, certificates, and private keys out of Git.
- Use hosting-provider secret/environment-variable settings for hosted prototypes.
- Do not paste secrets into issues, pull requests, screenshots, or chat logs.
- If a secret is committed or shared, revoke/rotate it immediately; deleting the file in a later commit does not remove it from Git history.

## Current technical boundary

The application has a PostgreSQL authentication/session foundation and tenant-oriented database schema/RLS work. However, the analytics workspace is still process-global and the HTTP runtime is a single-user prototype. Do not treat the database foundation as proof that all API paths, uploaded datasets, caches, reports, and background jobs are tenant-isolated.

## AI analyst

The constrained planner is designed to receive schema metadata and a redacted question, then have the application validate and execute the analytical plan locally. Do not rely on this as a substitute for a privacy review. Use synthetic/de-identified data and keep any external AI-provider key in a secret store.

## Reporting a vulnerability

Do not publish exploit details or secrets in a public issue. Contact the repository maintainer privately once a contact address has been selected.