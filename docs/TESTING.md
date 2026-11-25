# Testing guide

## Local deterministic test run

Run from the repository root in the project virtual environment:

```powershell
$env:HEALTHOPS_ENV = "test"
$env:HEALTHOPS_LLM_ENABLED = "false"
python -m py_compile server.py settings.py persistence.py analytics_engine.py llm_analyst.py
python -m unittest discover -s tests -v
$testExitCode = $LASTEXITCODE
Remove-Item Env:HEALTHOPS_LLM_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:HEALTHOPS_ENV -ErrorAction SilentlyContinue
if ($testExitCode -ne 0) { throw "Tests failed." }
```

The LLM is disabled so tests do not depend on an external provider, account quota or API key.

## Test-data rules

- Use deterministic synthetic records only.
- Never include direct identifiers or real patient-level data in fixtures.
- Do not commit uploaded CSV/Excel files, local workspace snapshots, generated exports or screenshots containing secrets.

## Release rule

A green syntax check is not a substitute for the full unit suite. Run CI on the exact commit intended for GitHub and resolve failures before creating a public release tag.