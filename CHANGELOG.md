# Changelog

## Foundation pass — 2026-10-29

- Split the original HTML's large inline stylesheet and JavaScript into `static/css/app.css` and `static/js/app.js`.
- Consolidated the styling into externally served assets, refined the dark charcoal/plum palette, added clearer typography and surface hierarchy, improved focus states and responsive styling, and reduced decorative motion.
- Added a guarded static asset route with path-traversal prevention and same-origin script policy.
- Added request `Content-Length` validation for malformed, negative, oversized, and incomplete bodies.
- Corrected aggregation-intent parsing so the word `Total` inside a selected field name does not override an explicit request for an average.
- Fixed the denominator in category-frequency recommendations for multi-table analyses.
- Standardised analytical number formatting in the assistant/report output and dashboard, avoiding long floating-point artifacts.
- Aligned frontend upload validation with the backend's supported extensions and runtime-configured size limits, including the combined request cap.
- Added 24 deterministic tests, including an HTTP upload → background analysis → chatbot regression flow.
- Added a project architecture/productionization plan and updated the README to match what is actually supported.

This foundation pass is not a production-readiness certification. Authentication, user/organisation isolation, durable storage and audit history, scalable query execution, general-purpose LLM planning, additional database/API connectors, and broader report formats remain open engineering work. See `docs/PRODUCTIONIZATION_PLAN.md`.
