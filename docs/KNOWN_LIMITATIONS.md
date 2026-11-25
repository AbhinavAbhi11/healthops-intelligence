# Known limitations

## Product and analytics

- The natural-language analyst supports a constrained set of descriptive operations. It is not an unrestricted BI copilot.
- Multi-turn follow-up routing is incomplete. A provider-count or DRG-mix follow-up may return a prior methodology answer instead of the requested narrow result.
- Provider IDs are identifiers; a distinct ID count should not automatically be described as a count of unique physical hospitals without source-dictionary confirmation.
- IQR flags are statistical candidates, not errors. Healthcare values can be naturally skewed and should not be deleted automatically.
- City-level charge averages are not case-mix-adjusted. Different DRG composition can affect the aggregate mean.
- Column type inference can classify numeric-looking identifiers (for example, ZIP codes) as numeric; identifier-aware typing needs further work.
- No general PostgreSQL/MySQL/SQL Server, REST, FHIR or HL7 analytics connector is available yet.
- FHIR/HL7, general clinical XML, PDF and DOCX ingestion require dedicated adapters.
- Pandas processing is bounded by the application's available memory. Do not claim arbitrary TB-scale capability.

## Operations and security

- The analytics workspace is process-global and single-user.
- Tenant metadata/RLS foundations do not yet prove tenant isolation across every analytics request.
- The current local persistence path is not automatically durable on free cloud filesystems.
- Hosted runtime port/host binding has not yet been adapted and verified for a provider such as Render.
- No independent penetration test, compliance assessment, load test, backup/restore exercise or incident-response exercise has been completed.