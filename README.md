# ElectroTrack 2.0

A local-first Flask workspace for managing electricity consumers, meters, and cumulative readings — now with an animated 3D interface, interactive charts, live reading previews, and a full light/dark theme.

## Run locally

1. Activate the project's virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Start the application with `python app.py` and open `http://127.0.0.1:5001` (set `PORT` to override the port).

The app creates its SQLite database at `instance/electrotrack.db`; MySQL is not required. The session-signing key is kept in `instance/secret_key`.

## Interface

- **3D dashboard** — an animated energy core with orbiting rings and a 3D particle field, tilted glass cards, extruded isometric usage bars, a radial account-health gauge, and a live activity feed.
- **Interactive charts** — hover any chart to inspect exact values. Trend, comparison, and consumer-history charts are drawn on `<canvas>` and animate into view; each chart also has an accessible “view as a table” fallback.
- **Command palette** — press `⌘K` / `Ctrl+K` to search pages, consumers, and meters, or run actions such as recording a reading, adding a consumer, toggling the theme, and downloading a backup.
- **Theme** — dark by default, with a light theme that is remembered per browser.
- **Responsive layout** — the sidebar becomes a mobile drawer, cards reflow, and tables scroll horizontally.
- **Motion preferences** — all animation is disabled automatically when the OS requests reduced motion.
- **Keyboard shortcuts** — `⌘K` command palette, `R` record a reading, `C` add a consumer, `T` toggle theme, `Esc` close overlays.
- **Roles** — one administrator controls workspace settings and tariffs; operators manage consumers, meters, and readings; viewers can browse and download reports. Role checks are enforced by the server as well as the interface.
- **Toasts, live reading preview, CSV drag-and-drop, sortable/searchable consumer table, and print-friendly reports** are built in.

## Data model and calculations

- `consumers` stores consumer name, type, region, contact details, notes, and an optional tariff assignment.
- `meters` stores a unique meter code, its consumer, meter type, phase, and optional capacity (kW).
- `meter_readings` stores dated cumulative meter values and the calculated usage/validation result.
- Cumulative readings are measured in kWh and must be recorded in strictly increasing date order for each meter.
- The first cumulative reading is a baseline and produces no usage. Later usage is the current cumulative value minus the previous valid cumulative value.
- A lower cumulative value is saved as flagged for possible reset or rollover. The app does not guess a rollover modulus, so it does not calculate usage for that record.
- A calculated increase above the configurable reading-review threshold (default 600 kWh per reading interval) is held for manual approval. Pending records do not affect usage totals or analytics, and another reading for that meter cannot be entered until the pending item is reviewed.
- Approving an unusually large increase includes it in usage totals. Approving a possible reset accepts that reading as a new baseline without guessing usage across the reset. Rejected readings remain in the audit trail but are excluded from totals.
- Historical direct-usage entries from the earlier app version are retained and labeled as legacy. Demonstration records are labeled as sample data.
- The default high-usage threshold is 400 kWh and can be changed in Alerts.
- Consumption figures are analytical estimates from available readings; they do not replace official billing calculations.
- A local administrator login is created on first launch. Passwords are stored as hashes, and the session-signing key is kept in `instance/secret_key`.

## Reports and analysis

The Meter Readings page accepts CSV imports with `meter_code`, `reading_date`, and `cumulative_kwh` columns. It validates each row, skips invalid dates, unknown meters, future readings, and readings that are out of chronological order, and applies the same baseline/reset/high-usage review rules as manual entry. Import up to 1,000 rows per file; order each meter's readings from oldest to newest. A template can be downloaded from the page.

The Reports page provides CSV downloads for consumers, meter readings, monthly consumption, and reading validation, plus a downloadable SQLite database backup. Keep a copy of the backup outside the project folder. The Restore Backup form validates the database file and creates a timestamped safety copy of the current database before replacing it. After restoring, sign in using an account present in the restored backup.

Reports include reading and usage totals, a monthly usage preview, and a print-friendly view. Use the Analytics comparison selector to compare consumers or individual meters across the selected period. Consumer History can be filtered by meter and date range, and its interactive chart distinguishes sample and recorded usage.

The Meters page supports multiple meters per consumer, including meter type, phase, and capacity. A meter with saved readings cannot be deleted so its history remains linked. Analytics filters by period, consumer, region, consumer type, data source, and meter. The dashboard separates sample and recorded monthly totals, compares the current month with the previous one, projects month-end usage, and estimates carbon impact.

## Implementation notes

- Charts are rendered client-side in `static/js/app.js`; no charting library or network dependency is required (only Lucide icons and Google Fonts load from the web).
- Pandas and NumPy are used for period aggregation and calculation helpers.
- The single-page navigation uses hash routes with all sections rendered server-side, so data loads once per request and downloads/forms keep working without a JavaScript build step.
