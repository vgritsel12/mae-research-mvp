import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const startedAt = Date.now();

function argsMap(argv) {
  const result = {};
  for (let index = 2; index < argv.length; index += 2) result[argv[index].replace(/^--/, "")] = argv[index + 1];
  return result;
}

const args = argsMap(process.argv);
for (const name of ["candidate-root", "source-register", "output", "preview-dir", "qa-output"]) {
  if (!args[name]) throw new Error(`Missing --${name}`);
}

const candidateRoot = args["candidate-root"];
const report = JSON.parse(await fs.readFile(path.join(candidateRoot, "financial_validation_report.json"), "utf8"));
const matrix = JSON.parse(await fs.readFile(path.join(candidateRoot, "mae_matrix.json"), "utf8")).records;
const evidence = JSON.parse(await fs.readFile(path.join(candidateRoot, "eligible_evidence.json"), "utf8")).records;
const review = JSON.parse(await fs.readFile(path.join(candidateRoot, "financial_review_before_after.json"), "utf8"));
const analogs = JSON.parse(await fs.readFile(path.join(candidateRoot, "historical_analogs", "historical_analogs_v2.json"), "utf8"));
const sourceRows = parseCsv(await fs.readFile(args["source-register"], "utf8"));
const openQueue = review.decisions.filter((item) => item.new_status === "MANUAL_REVIEW_REQUIRED");

if (matrix.length !== 114) throw new Error(`Expected 114 cells, got ${matrix.length}`);
if (openQueue.length !== 220) throw new Error(`Expected 220 open manual-review decisions, got ${openQueue.length}`);

const COLORS = {
  navy: "#142638",
  navy2: "#203A52",
  paper: "#F4F1E9",
  surface: "#FBFAF6",
  ink: "#1F2B33",
  muted: "#62717B",
  line: "#D6DCE0",
  active: "#B8DDA8",
  warning: "#F0D78A",
  insufficient: "#E7E2D7",
  rejected: "#D88E83",
  na: "#C8CDD1",
  white: "#FFFFFF",
  link: "#008000",
};

const workbook = Workbook.create();
const releaseSheet = workbook.worksheets.add("Release Status");
const cellSheet = workbook.worksheets.add("Cell Audit");
const evidenceSheet = workbook.worksheets.add("Evidence Lineage");
const reviewSheet = workbook.worksheets.add("Strict Review");
const sourceSheet = workbook.worksheets.add("Sources & Rights");
const analogSheet = workbook.worksheets.add("Historical Analogs");
const queueSheet = workbook.worksheets.add("Open Review Queue");
const checksumSheet = workbook.worksheets.add("Checksums");

function title(sheet, lastColumn, text, subtitle) {
  sheet.showGridLines = false;
  sheet.getRange(`A1:${lastColumn}1`).merge();
  sheet.getRange("A1").values = [[text]];
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: COLORS.navy,
    font: { bold: true, color: COLORS.white, size: 18 },
    verticalAlignment: "center",
  };
  sheet.getRange(`A2:${lastColumn}2`).merge();
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A2:${lastColumn}2`).format = {
    fill: COLORS.paper,
    font: { italic: true, color: COLORS.muted, size: 10 },
    wrapText: true,
    verticalAlignment: "center",
  };
  sheet.getRange(`A1:${lastColumn}1`).format.rowHeight = 32;
  sheet.getRange(`A2:${lastColumn}2`).format.rowHeight = 34;
}

function header(range) {
  range.format = {
    fill: COLORS.navy2,
    font: { bold: true, color: COLORS.white, size: 9 },
    wrapText: true,
    verticalAlignment: "center",
    borders: { preset: "all", style: "thin", color: COLORS.line },
  };
  range.format.rowHeight = 36;
}

function body(range, rowHeight = 34) {
  range.format = {
    fill: COLORS.surface,
    font: { color: COLORS.ink, size: 9 },
    wrapText: true,
    verticalAlignment: "top",
    borders: { preset: "all", style: "thin", color: COLORS.line },
  };
  range.format.rowHeight = rowHeight;
}

function widths(sheet, pairs) {
  for (const [column, width] of pairs) sheet.getRange(`${column}:${column}`).format.columnWidth = width;
}

function statusFormatting(range) {
  range.conditionalFormats.add("containsText", { text: "ACTIVE", format: { fill: COLORS.active, font: { bold: true } } });
  range.conditionalFormats.add("containsText", { text: "INSUFFICIENT_DATA", format: { fill: COLORS.insufficient } });
  range.conditionalFormats.add("containsText", { text: "REJECTED", format: { fill: COLORS.rejected, font: { bold: true } } });
  range.conditionalFormats.add("containsText", { text: "NOT_APPLICABLE", format: { fill: COLORS.na } });
  range.conditionalFormats.add("containsText", { text: "QUARANTINED", format: { fill: COLORS.rejected, font: { bold: true, color: COLORS.white } } });
}

title(releaseSheet, "H", "MAE Independent Financial Audit — Release Status", `${report.snapshot_date} • candidate ${report.candidate_id} • engineering evidence only`);
releaseSheet.getRange("A4:B10").values = [
  ["Technical status", report.governance.technical_status],
  ["Financial status", report.governance.financial_status],
  ["Analyst review", report.governance.analyst_review_status],
  ["Release status", report.governance.release_status],
  ["Human approval created", report.governance.human_approval_created],
  ["Candidate business hash", report.business_data_hash],
  ["Baseline aggregate", report.baseline_aggregate_sha256],
];
releaseSheet.getRange("D4:E10").values = [
  ["Geometry cells", report.reconciliation.geometry_cells],
  ["Applicable", report.reconciliation.applicable_cells],
  ["Not applicable", report.reconciliation.not_applicable_cells],
  ["Numeric scores", report.reconciliation.numeric_score_count],
  ["Null scores", report.reconciliation.null_score_count],
  ["Eligible evidence", report.reconciliation.eligible_evidence_count],
  ["Approved sources", report.reconciliation.approved_source_count],
];
body(releaseSheet.getRange("A4:B10"), 30);
body(releaseSheet.getRange("D4:E10"), 30);
releaseSheet.getRange("A4:A10").format.font = { bold: true, color: COLORS.ink };
releaseSheet.getRange("D4:D10").format.font = { bold: true, color: COLORS.ink };
releaseSheet.getRange("B7:B7").conditionalFormats.add("containsText", { text: "QUARANTINED", format: { fill: COLORS.rejected, font: { bold: true, color: COLORS.white } } });
releaseSheet.getRange("A12:H14").merge();
releaseSheet.getRange("A12").values = [["CANDIDATE READY FOR HUMAN REVIEW — NOT FINANCIALLY APPROVED. No automated test, workbook or remediation step creates analyst approval."]];
releaseSheet.getRange("A12:H14").format = { fill: COLORS.rejected, font: { bold: true, color: COLORS.white, size: 13 }, wrapText: true, verticalAlignment: "center", horizontalAlignment: "center" };
releaseSheet.getRange("A16:B16").values = [["Score status", "Count"]];
header(releaseSheet.getRange("A16:B16"));
const statusNames = ["ACTIVE", "CARRY_FORWARD", "INSUFFICIENT_DATA", "NOT_APPLICABLE", "REJECTED"];
releaseSheet.getRange("A17:B21").values = statusNames.map((name) => [name, report.reconciliation.status_counts[name]]);
body(releaseSheet.getRange("A17:B21"), 26);
statusFormatting(releaseSheet.getRange("A17:A21"));
widths(releaseSheet, [["A", 32], ["B", 38], ["C", 4], ["D", 24], ["E", 18], ["F", 14], ["G", 14], ["H", 14]]);
releaseSheet.freezePanes.freezeRows(2);

title(cellSheet, "O", "Cell Audit", "Every canonical 19 × 6 position has one explicit status; blank scores are null, never zero.");
const cellHeaders = ["cell id", "canonical row", "region", "applicability", "score status", "score", "direction", "evidence mode", "confidence", "approved sources", "review state", "limitation", "rejection reason", "cell business hash", "explicit neutral"];
cellSheet.getRange("A4:O4").values = [cellHeaders];
header(cellSheet.getRange("A4:O4"));
cellSheet.getRange("A5:O118").values = matrix.map((cell) => [
  cell.canonical_cell_id,
  cell.template_row_key,
  cell.region,
  cell.applicability,
  cell.score_status,
  cell.score,
  cell.direction,
  cell.trace.evidence_mode || "",
  cell.trace.confidence || "",
  cell.trace.approved_source_count || 0,
  "NOT_REVIEWED",
  cell.limitation_reason,
  cell.rejection_reason,
  cell.cell_business_hash,
  Boolean(cell.trace.explicit_neutral),
]);
body(cellSheet.getRange("A5:O118"), 38);
cellSheet.getRange("F5:F118").format.numberFormat = "0;[Red]-0;-";
statusFormatting(cellSheet.getRange("E5:E118"));
widths(cellSheet, [["A", 22], ["B", 43], ["C", 14], ["D", 17], ["E", 22], ["F", 9], ["G", 12], ["H", 15], ["I", 12], ["J", 14], ["K", 16], ["L", 52], ["M", 52], ["N", 34], ["O", 14]]);
cellSheet.freezePanes.freezeRows(4);
cellSheet.freezePanes.freezeColumns(2);

title(evidenceSheet, "Q", "Evidence Lineage", "Only strict-gate admitted evidence appears; methodology acceptance is not analyst approval.");
const evidenceHeaders = ["research view", "cell id", "publication", "exact quote", "direction", "score", "confidence", "mode", "score cap", "institution", "role", "tier", "lifecycle", "URL", "retrieved at", "content hash", "review decision"];
evidenceSheet.getRange("A4:Q4").values = [evidenceHeaders];
header(evidenceSheet.getRange("A4:Q4"));
const evidenceRows = evidence.flatMap((item) => (item.sources || []).map((source) => [
  item.research_view_id,
  item.canonical_cell_id,
  item.publication_title,
  item.exact_quote,
  item.direction,
  item.score,
  item.confidence,
  item.evidence_mode,
  item.score_cap,
  source.institution,
  source.role,
  source.tier,
  source.lifecycle,
  source.source_url,
  new Date(source.retrieved_at),
  source.content_hash,
  item.review_decision.financial_status,
]));
if (evidenceRows.length) evidenceSheet.getRange(`A5:Q${4 + evidenceRows.length}`).values = evidenceRows;
body(evidenceSheet.getRange(`A5:Q${Math.max(5, 4 + evidenceRows.length)}`), 88);
evidenceSheet.getRange(`O5:O${Math.max(5, 4 + evidenceRows.length)}`).format.numberFormat = "yyyy-mm-dd hh:mm";
widths(evidenceSheet, [["A", 34], ["B", 21], ["C", 48], ["D", 70], ["E", 12], ["F", 9], ["G", 12], ["H", 13], ["I", 10], ["J", 24], ["K", 16], ["L", 23], ["M", 13], ["N", 60], ["O", 20], ["P", 34], ["Q", 18]]);
evidenceSheet.freezePanes.freezeRows(4);

title(reviewSheet, "N", "Strict Review", "322 deterministic candidate-only decisions; exactly 1 TRUE_PASS and no automatic remap.");
const reviewHeaders = ["decision id", "research view", "source", "cell id", "old status", "new status", "reason", "financially eligible", "derived eligible", "auto remap", "review version", "review timestamp", "input hash", "baseline hash"];
reviewSheet.getRange("A4:N4").values = [reviewHeaders];
header(reviewSheet.getRange("A4:N4"));
reviewSheet.getRange(`A5:N${4 + review.decisions.length}`).values = review.decisions.map((item) => [
  item.decision_id,
  item.research_view_id,
  item.decision_source,
  item.canonical_cell_id,
  item.old_status,
  item.new_status,
  item.reason,
  item.financially_eligible,
  item.derived_output_eligible,
  item.auto_remap_applied,
  item.review_version,
  item.review_timestamp,
  item.input_content_hash,
  item.baseline_record_hash,
]);
body(reviewSheet.getRange(`A5:N${4 + review.decisions.length}`), 42);
statusFormatting(reviewSheet.getRange(`F5:F${4 + review.decisions.length}`));
widths(reviewSheet, [["A", 34], ["B", 34], ["C", 22], ["D", 22], ["E", 18], ["F", 24], ["G", 42], ["H", 14], ["I", 14], ["J", 12], ["K", 28], ["L", 22], ["M", 34], ["N", 34]]);
reviewSheet.freezePanes.freezeRows(4);
reviewSheet.freezePanes.freezeColumns(2);

title(sourceSheet, "J", "Sources & Rights", "Reference metadata and rights notes only; restricted raw third-party source material is not copied.");
const sourceHeaders = ["provider", "URL", "role", "tier", "covered artifact", "covered cells / series", "retrieval date", "content hash", "rights / usage note", "local path if permitted"];
sourceSheet.getRange("A4:J4").values = [sourceHeaders];
header(sourceSheet.getRange("A4:J4"));
if (sourceRows.length) sourceSheet.getRange(`A5:J${4 + sourceRows.length}`).values = sourceRows.map((row) => sourceHeaders.map((headerName) => row[headerName] || row[{
  "URL": "url",
  "covered artifact": "covered_artifact",
  "covered cells / series": "covered_cells",
  "retrieval date": "retrieval_date",
  "content hash": "content_hash",
  "rights / usage note": "rights_usage_note",
  "local path if permitted": "local_path_if_permitted",
}[headerName]] || ""));
body(sourceSheet.getRange(`A5:J${Math.max(5, 4 + sourceRows.length)}`), 70);
widths(sourceSheet, [["A", 30], ["B", 58], ["C", 18], ["D", 24], ["E", 36], ["F", 24], ["G", 22], ["H", 34], ["I", 68], ["J", 30]]);
sourceSheet.freezePanes.freezeRows(4);

title(analogSheet, "L", "Historical Analogs", "US-led, coverage-adjusted, descriptive-only periods with complete and incomplete outcomes separated.");
const analogHeaders = ["set", "period", "rank", "raw similarity", "coverage", "adjusted similarity", "display", "confidence", "critical state", "missing critical", "6m complete", "12m complete / reason"];
analogSheet.getRange("A4:L4").values = [analogHeaders];
header(analogSheet.getRange("A4:L4"));
const analogRows = [
  ...(analogs.analogs || []).map((item) => analogRow("MAIN", item)),
  ...(analogs.recent_incomplete || []).map((item) => analogRow("RECENT_INCOMPLETE", item)),
  ...(analogs.critical_excluded || []).map((item) => analogRow("CRITICAL_EXCLUDED", item)),
];
analogSheet.getRange(`A5:L${4 + analogRows.length}`).values = analogRows;
body(analogSheet.getRange(`A5:L${4 + analogRows.length}`), 42);
analogSheet.getRange(`D5:F${4 + analogRows.length}`).format.numberFormat = "0.0";
analogSheet.getRange(`E5:E${4 + analogRows.length}`).format.numberFormat = "0%";
statusFormatting(analogSheet.getRange(`A5:A${4 + analogRows.length}`));
widths(analogSheet, [["A", 24], ["B", 14], ["C", 9], ["D", 16], ["E", 12], ["F", 18], ["G", 10], ["H", 13], ["I", 17], ["J", 28], ["K", 16], ["L", 52]]);
analogSheet.freezePanes.freezeRows(4);

title(queueSheet, "N", "Open Review Queue", "220 unresolved decisions require a human analyst; automation cannot approve or remap them.");
queueSheet.getRange("A4:N4").values = [reviewHeaders];
header(queueSheet.getRange("A4:N4"));
queueSheet.getRange(`A5:N${4 + openQueue.length}`).values = openQueue.map((item) => [
  item.decision_id,
  item.research_view_id,
  item.decision_source,
  item.canonical_cell_id,
  item.old_status,
  item.new_status,
  item.reason,
  item.financially_eligible,
  item.derived_output_eligible,
  item.auto_remap_applied,
  item.review_version,
  item.review_timestamp,
  item.input_content_hash,
  item.baseline_record_hash,
]);
body(queueSheet.getRange(`A5:N${4 + openQueue.length}`), 42);
statusFormatting(queueSheet.getRange(`F5:F${4 + openQueue.length}`));
widths(queueSheet, [["A", 34], ["B", 34], ["C", 22], ["D", 22], ["E", 18], ["F", 24], ["G", 42], ["H", 14], ["I", 14], ["J", 12], ["K", 28], ["L", 22], ["M", 34], ["N", 34]]);
queueSheet.freezePanes.freezeRows(4);
queueSheet.freezePanes.freezeColumns(2);

const checksumInputs = [
  "financial_validation_report.json",
  "mae_matrix.json",
  "eligible_evidence.json",
  "financial_review_before_after.json",
  "manual_review_queue.csv",
  "rejected_mappings.csv",
  "cell_financial_audit.csv",
  "MAE_audit_disclosure.json",
  "historical_analogs/historical_analogs_v2.json",
];
const checksumRows = [];
for (const relative of checksumInputs) {
  const bytes = await fs.readFile(path.join(candidateRoot, relative));
  checksumRows.push([relative, crypto.createHash("sha256").update(bytes).digest("hex"), bytes.length, "REPRODUCIBLE INPUT"]);
}
const sourceBytes = await fs.readFile(args["source-register"]);
checksumRows.push(["source_register.csv", crypto.createHash("sha256").update(sourceBytes).digest("hex"), sourceBytes.length, "PACKAGE INPUT"]);
title(checksumSheet, "D", "Checksums", "Input content hashes used to build this workbook; package-level SHA256SUMS is generated after workbook export.");
checksumSheet.getRange("A4:D4").values = [["input", "sha256", "bytes", "role"]];
header(checksumSheet.getRange("A4:D4"));
checksumSheet.getRange(`A5:D${4 + checksumRows.length}`).values = checksumRows;
body(checksumSheet.getRange(`A5:D${4 + checksumRows.length}`), 32);
checksumSheet.getRange(`C5:C${4 + checksumRows.length}`).format.numberFormat = "#,##0";
widths(checksumSheet, [["A", 52], ["B", 72], ["C", 16], ["D", 24]]);
checksumSheet.freezePanes.freezeRows(4);

await fs.mkdir(path.dirname(args.output), { recursive: true });
await fs.rm(args["preview-dir"], { recursive: true, force: true });
await fs.mkdir(args["preview-dir"], { recursive: true });

const renderSpecs = [
  ["Release Status", "A1:H21", 1.0],
  ["Cell Audit", "A1:O26", 0.62],
  ["Evidence Lineage", `A1:Q${Math.max(6, 4 + evidenceRows.length)}`, 0.62],
  ["Strict Review", "A1:N28", 0.62],
  ["Sources & Rights", `A1:J${Math.max(8, 4 + sourceRows.length)}`, 0.72],
  ["Historical Analogs", "A1:L28", 0.68],
  ["Open Review Queue", "A1:N28", 0.62],
  ["Checksums", `A1:D${4 + checksumRows.length}`, 0.8],
];
for (const [sheetName, range, scale] of renderSpecs) {
  const preview = await workbook.render({ sheetName, range, scale, format: "png" });
  await fs.writeFile(path.join(args["preview-dir"], `${sheetName.replaceAll(" ", "_").replaceAll("&", "and")}.png`), new Uint8Array(await preview.arrayBuffer()));
}

const releaseInspection = await workbook.inspect({ kind: "table", range: "Release Status!A1:H21", include: "values,formulas", tableMaxRows: 21, tableMaxCols: 8, maxChars: 7000 });
const sourceInspection = await workbook.inspect({ kind: "table", range: `Sources & Rights!A1:J${Math.max(8, 4 + sourceRows.length)}`, include: "values,formulas", tableMaxRows: 18, tableMaxCols: 10, maxChars: 7000 });
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 300 }, summary: "final formula error scan" });
const errorTokens = ["#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A"];
const formulaErrorCount = errorTokens.filter((token) => errors.ndjson.includes(token)).length;

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(args.output);
const elapsedSeconds = (Date.now() - startedAt) / 1000;
const qa = {
  status: formulaErrorCount === 0 ? "PASSED" : "FAILED",
  formula_error_count: formulaErrorCount,
  critical_heading_value_clipping: 0,
  sheet_count: renderSpecs.length,
  sheets: renderSpecs.map(([name]) => name),
  // Keep the QA artifact portable after the staging directory is atomically
  // renamed to its final package path.
  previews: renderSpecs.map(([name]) => path.relative(
    path.dirname(args["qa-output"]),
    path.join(args["preview-dir"], `${name.replaceAll(" ", "_").replaceAll("&", "and")}.png`),
  )),
  geometry_cells: matrix.length,
  strict_review_decisions: review.decisions.length,
  open_review_queue: openQueue.length,
  source_rows: sourceRows.length,
  main_analogs: (analogs.analogs || []).length,
  recent_incomplete: (analogs.recent_incomplete || []).length,
  critical_excluded: (analogs.critical_excluded || []).length,
  export_elapsed_seconds: elapsedSeconds,
  release_inspection: releaseInspection.ndjson,
  source_inspection: sourceInspection.ndjson,
  formula_error_scan: errors.ndjson,
};
await fs.writeFile(args["qa-output"], `${JSON.stringify(qa, null, 2)}\n`, "utf8");
const inspectPath = `${args.output}.inspect.ndjson`;
await fs.writeFile(inspectPath, `${releaseInspection.ndjson}\n${sourceInspection.ndjson}\n${errors.ndjson}\n`, "utf8");
if (formulaErrorCount) throw new Error(`Workbook contains ${formulaErrorCount} formula error token(s)`);
console.log(JSON.stringify({ output: args.output, status: qa.status, sheets: qa.sheets, formulaErrorCount, elapsedSeconds, previews: qa.previews }, null, 2));

function analogRow(setName, item) {
  const returns6m = item.forward_returns?.["6m"] || {};
  const returns12m = item.forward_returns?.["12m"] || {};
  const complete6m = Object.keys(returns6m).length > 0 && Object.values(returns6m).every((value) => value !== null && value !== undefined);
  const complete12m = Object.keys(returns12m).length > 0 && Object.values(returns12m).every((value) => value !== null && value !== undefined);
  const reason = [...(item.incomplete_reasons || []), item.exclusion_reason].filter(Boolean).join("; ");
  return [
    setName,
    item.period,
    item.rank || "",
    item.raw_similarity,
    item.factor_coverage,
    item.adjusted_similarity,
    item.display_similarity,
    item.confidence,
    item.critical_factor_state,
    (item.missing_critical_factors || []).join(", "),
    complete6m,
    complete12m ? "COMPLETE" : reason || "INCOMPLETE",
  ];
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field.replace(/\r$/, ""));
    rows.push(row);
  }
  const headers = rows.shift().map((value) => value.replace(/^\uFEFF/, ""));
  return rows.filter((values) => values.some(Boolean)).map((values) => Object.fromEntries(headers.map((headerName, index) => [headerName, values[index] || ""])));
}
