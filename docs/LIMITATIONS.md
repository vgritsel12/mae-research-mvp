# Public-release limitations

The core application source is present. Private assets required for full reproduction are deliberately missing: MAE workbook/baseline, curated scores/scenarios, research source configurations, historical series, evidence snapshots, SQLite database and published release bundles.

An initial broader test selection produced 32 passes and 29 failures. The failures exposed missing private/configuration/fixture dependencies in that selection; they do not establish that all source-level integration workflows pass. Those dependent tests are not included in the public test command. The final nine public tests are a deliberately narrow, reproducible subset, not the full original suite.

Six UI routes boot without uncaught exceptions, but five expose unavailable-data messages. No data-dependent scripts should be assumed runnable from this package alone. The two included JSON files are the original historical-analog methodology configuration and research-lifecycle configuration; they contain no observations.

No missing application functions have been recreated. The optional provider is tested with mocks only. Production readiness, security completeness, financial accuracy and deployment are not established by this package.
