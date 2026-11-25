# Prototype v1 scope

**Status date:** 23 November 2026

## Goal

Demonstrate a healthcare-focused analytics workflow that can accept different tabular data sources, infer their structure, profile data quality, create reversible cleaning copies, build descriptive visualizations, and answer a constrained set of aggregate questions.

## Demonstrated in the local prototype

1. First-owner organization setup and administrator login.
2. PostgreSQL-backed authentication and session persistence across a local server restart.
3. Upload and profiling of a hospital charges dataset with 163,065 rows and 12 columns.
4. Schema/type detection for provider, location, DRG, discharge and charge/payment fields.
5. Missingness, duplicate and IQR outlier-candidate review.
6. Approval of a whitespace-trimming transformation into a separate cleaned copy; row count stayed at 163,065 and the original remained available.
7. Re-analysis of the cleaned copy.
8. Aggregate city ranking for average covered charges, plus follow-up analysis of records versus distinct provider identifiers and descriptive DRG mix.

## Prototype constraints

- Single-user local-development analytics runtime with process-global workspace state.
- Upload processing relies on pandas/in-memory dataframes; no claim of arbitrary terabyte-scale processing.
- Local snapshots use the project's `instance/` storage path and need a persistent filesystem if relied on in a hosted environment.
- PostgreSQL currently supplies authentication/metadata foundations; it is not yet a general-purpose PostgreSQL source-data connector.
- Tenant-scoped database tables/RLS foundations do not by themselves make every runtime analytics operation tenant-isolated.
- Ask Your Data follow-up routing still has a known issue where a specific provider-count or DRG-mix follow-up can replay a previous methodology explanation.
- FHIR, HL7, general clinical XML, PDF and DOCX ingestion are not implemented as clinical/document adapters.
- No external security assessment, privacy certification, disaster-recovery test, or production load test has been completed.

## Prototype acceptance statement

Prototype v1 is suitable for a controlled demonstration using synthetic or properly de-identified data. It is not suitable for production hospital workflows or unrestricted public multi-user access.