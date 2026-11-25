# Prototype demo guide

## Recommended demo flow

1. Log in using the prototype administrator account.
2. Open **Data ingestion** and upload a synthetic or properly de-identified CSV/XLSX dataset.
3. Review detected tables, row counts, fields and inferred types under **Tables & schema**.
4. Open **Data quality** and review missingness, duplicates and IQR anomaly candidates. Treat outliers as review flags, not automatic errors.
5. Open **Data cleaning**. Select a conservative transformation, preview it, and approve only after checking the source and effect. Confirm that the original dataset remains available.
6. Open **Visualizations** and inspect descriptive trends.
7. Open **Ask Your Data** and try the sample prompts below.
8. Generate a Markdown report and inspect its sources and limitations.

## Sample Ask Your Data prompts

- How many records and fields are loaded?
- Which fields have the most missing values?
- Which provider cities have the highest average covered charges?
- How many distinct provider identifiers are represented in each selected city? Do not display identifier values.
- Compare DRG category mix across the selected cities. Explain how case mix can affect unadjusted average charges.
- Which numeric fields have IQR outlier candidates, and why are these not automatically errors?

## Interpretation reminders

- `n` in a grouped average is the number of valid contributing rows for that field/group, not automatically the number of distinct hospitals.
- Distinct provider identifiers are not proof of unique physical hospitals unless the source data dictionary confirms that mapping.
- An unadjusted average covered charge is not a like-for-like price comparison. Different DRG composition can change the average.
- Average covered charges, average total payments and average Medicare payments are different measures.
- Do not infer causation, clinical quality, or a pricing conclusion from descriptive averages alone.