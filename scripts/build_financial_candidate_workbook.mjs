import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

function argsMap(argv) {
  const result = {};
  for (let index = 2; index < argv.length; index += 2) {
    result[argv[index].replace(/^--/, "")] = argv[index + 1];
  }
  return result;
}

const args = argsMap(process.argv);
for (const required of ["input-dir", "output", "preview-dir", "qa-output"]) {
  if (!args[required]) throw new Error(`Missing --${required}`);
}

const inputDir = args["input-dir"];
const report = JSON.parse(await fs.readFile(path.join(inputDir, "financial_validation_report.json"), "utf8"));
const matrix = JSON.parse(await fs.readFile(path.join(inputDir, "mae_matrix.json"), "utf8")).records;
const evidence = JSON.parse(await fs.readFile(path.join(inputDir, "eligible_evidence.json"), "utf8")).records;
const shiftSignals = JSON.parse(await fs.readFile(path.join(inputDir, "shift_signals.json"), "utf8"));
const whatChanged = JSON.parse(await fs.readFile(path.join(inputDir, "what_changed.json"), "utf8"));
const scenarios = JSON.parse(await fs.readFile(path.join(inputDir, "scenarios.json"), "utf8"));
const transmissions = JSON.parse(await fs.readFile(path.join(inputDir, "transmission_paths.json"), "utf8"));
const evidenceSources = evidence.flatMap((item) => item.sources || []);
const sourceTaxonomy = [
  ["Research documents", new Set(evidence.map((item) => item.research_view_id).filter(Boolean)).size],
  ["Data providers", new Set(evidenceSources.filter((source) => source.role === "DATA_PROVIDER").map((source) => source.institution).filter(Boolean)).size],
  ["Benchmarks", new Set(evidenceSources.filter((source) => source.role === "BENCHMARK").map((source) => source.source_id).filter(Boolean)).size],
  ["Evidence items", evidence.length],
  ["Approved direct evidence", evidence.filter((item) => item.evidence_mode === "DIRECT").length],
  ["Composite inputs", matrix.filter((cell) => cell.trace.evidence_mode === "COMPOSITE").length],
  ["Carry-forward lineage", matrix.filter((cell) => cell.score_status === "CARRY_FORWARD").length],
];

if (matrix.length !== 114) throw new Error(`Expected 114 canonical cells, got ${matrix.length}`);
if (report.reconciliation.geometry_rows !== 19 || report.reconciliation.geometry_regions !== 6) {
  throw new Error("Candidate geometry does not reconcile to 19 x 6");
}

const COLORS = {
  navy: "#142638",
  navy2: "#203A52",
  paper: "#F4F1E9",
  surface: "#FBFAF6",
  ink: "#1F2B33",
  muted: "#62717B",
  line: "#D6DCE0",
  active: "#B8DDA8",
  carry: "#F0D78A",
  insufficient: "#E7E2D7",
  rejected: "#D88E83",
  notApplicable: "#C8CDD1",
  white: "#FFFFFF",
  link: "#008000",
  danger: "#A1302A",
};

const workbook = Workbook.create();
const release = workbook.worksheets.add("Release Status");
const audit = workbook.worksheets.add("Cell Audit");
const evidenceSheet = workbook.worksheets.add("Evidence Lineage");
const matrixSheet = workbook.worksheets.add("MAE Matrix");
const derived = workbook.worksheets.add("Derived Outputs");
const checks = workbook.worksheets.add("Checks");

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
  sheet.getRange(`A2:${lastColumn}2`).format.rowHeight = 28;
}

function header(range) {
  range.format = {
    fill: COLORS.navy2,
    font: { bold: true, color: COLORS.white, size: 9 },
    wrapText: true,
    verticalAlignment: "center",
    horizontalAlignment: "left",
    borders: { preset: "all", style: "thin", color: COLORS.line },
  };
  range.format.rowHeight = 34;
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
  range.conditionalFormats.add("containsText", { text: "CARRY_FORWARD", format: { fill: COLORS.carry, font: { bold: true } } });
  range.conditionalFormats.add("containsText", { text: "INSUFFICIENT_DATA", format: { fill: COLORS.insufficient } });
  range.conditionalFormats.add("containsText", { text: "REJECTED", format: { fill: COLORS.rejected, font: { bold: true } } });
  range.conditionalFormats.add("containsText", { text: "NOT_APPLICABLE", format: { fill: COLORS.notApplicable } });
}

title(
  release,
  "H",
  "MAE Financial Candidate — Release Status",
  `${report.snapshot_date} • candidate ${report.candidate_id} • business hash ${report.business_data_hash}`,
);
release.getRange("A4:B10").values = [
  ["Technical status", report.governance.technical_status],
  ["Financial status", report.governance.financial_status],
  ["Analyst review", report.governance.analyst_review_status],
  ["Release status", report.governance.release_status],
  ["Human approval created", report.governance.human_approval_created],
  ["Rulebook", report.strict_contract.rulebook_version],
  ["Review business hash", report.review_business_hash],
];
release.getRange("A4:A10").format = { fill: COLORS.paper, font: { bold: true, color: COLORS.ink }, borders: { preset: "all", style: "thin", color: COLORS.line } };
release.getRange("B4:B10").format = { fill: COLORS.surface, font: { color: COLORS.ink }, wrapText: true, borders: { preset: "all", style: "thin", color: COLORS.line } };
release.getRange("D4:D10").values = [
  ["Geometry cells"], ["Applicable"], ["Not applicable"], ["Numeric scores"], ["Null scores"], ["Eligible evidence"], ["Approved sources"],
];
release.getRange("E4").formulas = [["=COUNTA('Cell Audit'!A5:A118)"]];
release.getRange("E5").formulas = [["=COUNTIF('Cell Audit'!D5:D118,\"APPLICABLE\")"]];
release.getRange("E6").formulas = [["=COUNTIF('Cell Audit'!E5:E118,\"NOT_APPLICABLE\")"]];
release.getRange("E7").formulas = [["=COUNT('Cell Audit'!F5:F118)"]];
release.getRange("E8").formulas = [["=E4-E7"]];
release.getRange("E9").values = [[report.reconciliation.eligible_evidence_count]];
release.getRange("E10").values = [[report.reconciliation.approved_source_count]];
release.getRange("D4:D10").format = { fill: COLORS.paper, font: { bold: true, color: COLORS.ink }, borders: { preset: "all", style: "thin", color: COLORS.line } };
release.getRange("E4:E10").format = { fill: COLORS.active, font: { bold: true, color: COLORS.link }, horizontalAlignment: "right", borders: { preset: "all", style: "thin", color: COLORS.line }, numberFormat: "#,##0" };
release.getRange("A12:H13").merge();
release.getRange("A12").values = [["CANDIDATE READY FOR HUMAN REVIEW — NOT FINANCIALLY APPROVED. Engineering validation cannot create analyst approval; release remains QUARANTINED."]];
release.getRange("A12:H13").format = { fill: COLORS.rejected, font: { bold: true, color: COLORS.white, size: 12 }, wrapText: true, verticalAlignment: "center", horizontalAlignment: "center" };
release.getRange("A15:E15").values = [["Score status", "Count (formula)", "Expected", "Difference", "Check"]];
header(release.getRange("A15:E15"));
const statusNames = ["ACTIVE", "CARRY_FORWARD", "INSUFFICIENT_DATA", "NOT_APPLICABLE", "REJECTED"];
release.getRange("A16:A20").values = statusNames.map((name) => [name]);
release.getRange("B16").formulas = [["=COUNTIF('Cell Audit'!$E$5:$E$118,A16)"]];
release.getRange("B16:B20").fillDown();
release.getRange("C16:C20").values = statusNames.map((name) => [report.reconciliation.status_counts[name]]);
release.getRange("D16").formulas = [["=B16-C16"]];
release.getRange("D16:D20").fillDown();
release.getRange("E16").formulas = [["=IF(D16=0,\"OK\",\"FAIL\")"]];
release.getRange("E16:E20").fillDown();
body(release.getRange("A16:E20"), 24);
statusFormatting(release.getRange("A16:A20"));
release.getRange("E16:E20").conditionalFormats.add("containsText", { text: "OK", format: { fill: COLORS.active, font: { bold: true } } });
release.getRange("E16:E20").conditionalFormats.add("containsText", { text: "FAIL", format: { fill: COLORS.rejected, font: { bold: true, color: COLORS.white } } });
release.getRange("A23:B23").values = [["Source / lineage category", "Count"]];
header(release.getRange("A23:B23"));
release.getRange("A24:B30").values = sourceTaxonomy;
body(release.getRange("A24:B30"), 24);
release.getRange("B24:B30").format = { fill: COLORS.surface, font: { bold: true, color: COLORS.link }, horizontalAlignment: "right", numberFormat: "#,##0", borders: { preset: "all", style: "thin", color: COLORS.line } };
widths(release, [["A", 30], ["B", 28], ["C", 16], ["D", 14], ["E", 14], ["F", 13], ["G", 13], ["H", 13]]);
release.freezePanes.freezeRows(2);

title(audit, "O", "Cell Financial Audit", "One deterministic status for every canonical 19 × 6 position; blank score means null, never zero.");
const auditHeaders = ["cell id", "canonical row", "region", "applicability", "score status", "score", "direction", "evidence mode", "confidence", "approved sources", "review state", "limitation reason", "rejection reason", "cell business hash", "explicit neutral"];
audit.getRange("A4:O4").values = [auditHeaders];
header(audit.getRange("A4:O4"));
audit.getRange("A5:O118").values = matrix.map((cell) => [
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
body(audit.getRange("A5:O118"), 34);
audit.getRange("F5:F118").format.numberFormat = "0;[Red]-0;-";
audit.getRange("J5:J118").format.numberFormat = "#,##0";
statusFormatting(audit.getRange("E5:E118"));
audit.getRange("F5:F118").conditionalFormats.add("cellIs", { operator: "greaterThan", formula: 0, format: { fill: COLORS.active, font: { bold: true } } });
audit.getRange("F5:F118").conditionalFormats.add("cellIs", { operator: "lessThan", formula: 0, format: { fill: COLORS.rejected, font: { bold: true, color: COLORS.white } } });
widths(audit, [["A", 22], ["B", 43], ["C", 14], ["D", 17], ["E", 22], ["F", 9], ["G", 12], ["H", 15], ["I", 12], ["J", 14], ["K", 16], ["L", 55], ["M", 55], ["N", 34], ["O", 15]]);
audit.freezePanes.freezeRows(4);
audit.freezePanes.freezeColumns(2);

title(evidenceSheet, "Q", "Evidence Lineage", "Only financially admitted evidence appears here; source governance fields remain auditable.");
const evidenceHeaders = ["research view", "cell id", "publication", "exact quote", "direction", "score", "confidence", "mode", "score cap", "source institution", "source role", "source tier", "lifecycle", "source URL", "retrieved at", "content hash", "review decision"];
evidenceSheet.getRange("A4:Q4").values = [evidenceHeaders];
header(evidenceSheet.getRange("A4:Q4"));
const evidenceRows = evidence.flatMap((item) => item.sources.map((source) => [
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
body(evidenceSheet.getRange(`A5:Q${Math.max(5, 4 + evidenceRows.length)}`), 82);
evidenceSheet.getRange(`F5:F${Math.max(5, 4 + evidenceRows.length)}`).format.numberFormat = "0;[Red]-0;-";
evidenceSheet.getRange(`I5:I${Math.max(5, 4 + evidenceRows.length)}`).format.numberFormat = "0";
evidenceSheet.getRange(`O5:O${Math.max(5, 4 + evidenceRows.length)}`).format.numberFormat = "yyyy-mm-dd hh:mm";
widths(evidenceSheet, [["A", 34], ["B", 21], ["C", 48], ["D", 70], ["E", 12], ["F", 9], ["G", 12], ["H", 13], ["I", 10], ["J", 24], ["K", 16], ["L", 23], ["M", 13], ["N", 62], ["O", 20], ["P", 34], ["Q", 16]]);
evidenceSheet.freezePanes.freezeRows(4);
evidenceSheet.freezePanes.freezeColumns(2);

title(matrixSheet, "O", "Sparse MAE Matrix", "Scores remain blank for null statuses. Status matrix makes every absence explicit.");
const regions = ["Global", "US", "Europe", "UK", "Japan", "EM ex China"];
const rowKeys = [...new Set(matrix.map((cell) => cell.template_row_key))];
matrixSheet.getRange("A4:G4").values = [["Canonical row", ...regions]];
matrixSheet.getRange("I4:O4").values = [["Canonical row", ...regions]];
header(matrixSheet.getRange("A4:G4"));
header(matrixSheet.getRange("I4:O4"));
matrixSheet.getRange("A5:A23").values = rowKeys.map((row) => [row]);
matrixSheet.getRange("I5:I23").values = rowKeys.map((row) => [row]);
for (let rowIndex = 0; rowIndex < 19; rowIndex += 1) {
  for (let regionIndex = 0; regionIndex < 6; regionIndex += 1) {
    const auditRow = 5 + rowIndex * 6 + regionIndex;
    const scoreCell = matrixSheet.getCell(4 + rowIndex, 1 + regionIndex);
    scoreCell.formulas = [[`=IF('Cell Audit'!F${auditRow}=\"\",\"\",'Cell Audit'!F${auditRow})`]];
    const statusCell = matrixSheet.getCell(4 + rowIndex, 9 + regionIndex);
    statusCell.formulas = [[`='Cell Audit'!E${auditRow}`]];
  }
}
body(matrixSheet.getRange("A5:G23"), 30);
body(matrixSheet.getRange("I5:O23"), 30);
matrixSheet.getRange("B5:G23").format = { font: { bold: true, color: COLORS.link }, horizontalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.line }, numberFormat: "0;[Red]-0;-" };
matrixSheet.getRange("J5:O23").format.font = { color: COLORS.link, size: 8 };
statusFormatting(matrixSheet.getRange("J5:O23"));
widths(matrixSheet, [["A", 43], ["B", 12], ["C", 12], ["D", 12], ["E", 12], ["F", 12], ["G", 14], ["H", 3], ["I", 43], ["J", 18], ["K", 18], ["L", 18], ["M", 18], ["N", 18], ["O", 20]]);
matrixSheet.freezePanes.freezeRows(4);
matrixSheet.freezePanes.freezeColumns(1);

title(derived, "H", "Derived Outputs", "No page-filling scenarios: unsupported outputs remain explicitly empty/insufficient.");
derived.getRange("A4:D4").values = [["Layer", "Status", "Record count", "Reason / limitation"]];
header(derived.getRange("A4:D4"));
const derivedRows = [
  ["What Changed", whatChanged.status, whatChanged.records.length, whatChanged.records.length ? "Only admitted cells are included." : "No admitted change records."],
  ["Shift Signal", shiftSignals.status, shiftSignals.records.length, shiftSignals.reason],
  ["Scenarios", scenarios.status, scenarios.records.length, scenarios.reason],
  ["Transmission paths", transmissions.status, transmissions.records.length, transmissions.reason],
];
derived.getRange("A5:D8").values = derivedRows;
body(derived.getRange("A5:D8"), 62);
derived.getRange("C5:C8").format.numberFormat = "#,##0";
derived.getRange("A10:H11").merge();
derived.getRange("A10").values = [["No rejected, downgraded, or unresolved decision contributes to score, signal, scenario, change, or matrix approval."]];
derived.getRange("A10:H11").format = { fill: COLORS.paper, font: { bold: true, color: COLORS.ink }, wrapText: true, verticalAlignment: "center" };
widths(derived, [["A", 28], ["B", 24], ["C", 16], ["D", 82], ["E", 12], ["F", 12], ["G", 12], ["H", 12]]);

title(checks, "G", "Workbook Checks", "Formula-driven tie-outs against the JSON/CSV candidate contract.");
checks.getRange("A4:G4").values = [["Check", "Actual", "Expected", "Difference", "Tolerance", "Status", "Notes"]];
header(checks.getRange("A4:G4"));
const checkLabels = [
  ["Geometry cells", null, 114, null, 0, null, "Every canonical cell is present once."],
  ["Applicable cells", null, 104, null, 0, null, "Template applicability is preserved."],
  ["Not applicable cells", null, 10, null, 0, null, "Gold/other precious metals retain N/A geometry."],
  ["Numeric scores", null, report.reconciliation.numeric_score_count, null, 0, null, "Only ACTIVE/CARRY_FORWARD may be numeric."],
  ["Status total", null, 114, null, 0, null, "Five statuses reconcile to geometry."],
  ["Explicit-neutral zeros", null, report.reconciliation.explicit_neutral_zero_count, null, 0, null, "Zero requires explicit neutral evidence."],
  ["Approved sources", null, report.reconciliation.approved_source_count, null, 0, null, "Eligible evidence source count."],
];
checks.getRange("A5:G11").values = checkLabels;
checks.getRange("B5").formulas = [["=COUNTA('Cell Audit'!A5:A118)"]];
checks.getRange("B6").formulas = [["=COUNTIF('Cell Audit'!D5:D118,\"APPLICABLE\")"]];
checks.getRange("B7").formulas = [["=COUNTIF('Cell Audit'!E5:E118,\"NOT_APPLICABLE\")"]];
checks.getRange("B8").formulas = [["=COUNT('Cell Audit'!F5:F118)"]];
checks.getRange("B9").formulas = [["=SUM('Release Status'!B16:B20)"]];
checks.getRange("B10").formulas = [["=COUNTIFS('Cell Audit'!F5:F118,0,'Cell Audit'!O5:O118,TRUE)"]];
checks.getRange("B11").formulas = [["='Release Status'!E10"]];
checks.getRange("D5").formulas = [["=B5-C5"]];
checks.getRange("D5:D11").fillDown();
checks.getRange("F5").formulas = [["=IF(ABS(D5)<=E5,\"OK\",\"FAIL\")"]];
checks.getRange("F5:F11").fillDown();
body(checks.getRange("A5:G11"), 34);
checks.getRange("B5:F11").format.numberFormat = "#,##0;[Red](#,##0);-";
checks.getRange("B5:B11").format.font = { color: COLORS.link };
checks.getRange("F5:F11").conditionalFormats.add("containsText", { text: "OK", format: { fill: COLORS.active, font: { bold: true } } });
checks.getRange("F5:F11").conditionalFormats.add("containsText", { text: "FAIL", format: { fill: COLORS.rejected, font: { bold: true, color: COLORS.white } } });
checks.getRange("A13:C13").values = [["Overall model status", null, null]];
checks.getRange("B13").formulas = [["=IF(COUNTIF(F5:F11,\"FAIL\")=0,\"OK\",\"FAIL\")"]];
checks.getRange("A13:B13").format = { fill: COLORS.navy2, font: { bold: true, color: COLORS.white, size: 12 }, borders: { preset: "outside", style: "medium", color: COLORS.navy } };
checks.getRange("B13").conditionalFormats.add("containsText", { text: "OK", format: { fill: COLORS.active, font: { bold: true, color: COLORS.ink } } });
checks.getRange("B13").conditionalFormats.add("containsText", { text: "FAIL", format: { fill: COLORS.rejected, font: { bold: true, color: COLORS.white } } });
widths(checks, [["A", 30], ["B", 14], ["C", 14], ["D", 14], ["E", 12], ["F", 14], ["G", 58]]);
checks.freezePanes.freezeRows(4);

await fs.mkdir(path.dirname(args.output), { recursive: true });
await fs.rm(args["preview-dir"], { recursive: true, force: true });
await fs.mkdir(args["preview-dir"], { recursive: true });
const renderSpecs = [
  ["Release Status", "A1:H30", 1.0],
  ["Cell Audit", "A1:O30", 0.65],
  ["Evidence Lineage", "A1:Q6", 0.65],
  ["MAE Matrix", "A1:O23", 0.72],
  ["Derived Outputs", "A1:H11", 0.9],
  ["Checks", "A1:G13", 1.0],
];
for (const [sheetName, range, scale] of renderSpecs) {
  const preview = await workbook.render({ sheetName, range, scale, format: "png" });
  await fs.writeFile(path.join(args["preview-dir"], `${sheetName.replaceAll(" ", "_")}.png`), new Uint8Array(await preview.arrayBuffer()));
}

const keyRelease = await workbook.inspect({
  kind: "table",
  range: "Release Status!A1:H30",
  include: "values,formulas",
  tableMaxRows: 30,
  tableMaxCols: 8,
  maxChars: 5000,
});
const keyChecks = await workbook.inspect({
  kind: "table",
  range: "Checks!A1:G13",
  include: "values,formulas",
  tableMaxRows: 13,
  tableMaxCols: 7,
  maxChars: 5000,
});
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
const errorTokens = ["#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A"];
const formulaErrorCount = errorTokens.filter((token) => errors.ndjson.includes(token)).length;
const checkStatuses = checks.getRange("F5:F11").values.flat();
const checkFailureCount = checkStatuses.filter((value) => value !== "OK").length;

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(args.output);
const qa = {
  status: formulaErrorCount === 0 && checkFailureCount === 0 ? "PASSED" : "FAILED",
  formula_error_count: formulaErrorCount,
  check_failure_count: checkFailureCount,
  overall_model_status: checkFailureCount === 0 ? "OK" : "FAIL",
  sheet_count: renderSpecs.length,
  rendered_sheets: renderSpecs.map(([sheetName]) => sheetName),
  rendered_previews: renderSpecs.map(([sheetName]) => path.join(args["preview-dir"], `${sheetName.replaceAll(" ", "_")}.png`)),
  reconciliation: report.reconciliation,
  release_inspection: keyRelease.ndjson,
  checks_inspection: keyChecks.ndjson,
  formula_error_scan: errors.ndjson,
};
await fs.writeFile(args["qa-output"], `${JSON.stringify(qa, null, 2)}\n`, "utf8");
console.log(JSON.stringify({
  output: args.output,
  status: qa.status,
  formulaErrorCount,
  checkFailureCount,
  sheets: qa.rendered_sheets,
  previews: qa.rendered_previews,
  geometryCells: matrix.length,
  evidenceRows: evidenceRows.length,
}, null, 2));
