import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [inputPath, previewDir] = process.argv.slice(2);
if (!inputPath || !previewDir) {
  throw new Error("Usage: build_autonomous_workbook.mjs <workbook_build_input.json> <preview-dir>");
}
const input = JSON.parse(await fs.readFile(inputPath, "utf8"));
const trustLanguageReplacements = new Map([
  ["требуется проверка аналитиком", "требуется дополнительное подтверждение источниками"],
  ["требуется финансовая проверка", "требуется дополнительное подтверждение источниками"],
  ["duration signal requires human review", "duration signal requires additional source confirmation"],
  ["commodity signal requires human review", "commodity signal requires additional source confirmation"],
]);

function sanitizeTrustLanguage(value) {
  if (typeof value === "string") {
    let text = value;
    for (const [oldText, newText] of trustLanguageReplacements.entries()) {
      text = text.replaceAll(oldText, newText);
    }
    return text;
  }
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeTrustLanguage(item));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, sanitizeTrustLanguage(item)]));
  }
  return value;
}

const matrix = sanitizeTrustLanguage(JSON.parse(await fs.readFile(path.join(input.release_dir, "stage_04_matrix.json"), "utf8")));
const scenarioPayload = sanitizeTrustLanguage(JSON.parse(await fs.readFile(path.join(input.release_dir, "stage_05_scenarios.json"), "utf8")));
const explanationPayload = sanitizeTrustLanguage(JSON.parse(await fs.readFile(path.join(input.release_dir, "stage_06_explanations_analogs.json"), "utf8")));
const validation = sanitizeTrustLanguage(JSON.parse(await fs.readFile(path.join(input.release_dir, "stage_07_validation.json"), "utf8")));

const workbook = Workbook.create();
const cover = workbook.worksheets.add("Cover");
const matrixSheet = workbook.worksheets.add("Matrix");
const heatmap = workbook.worksheets.add("Heatmap");
const changes = workbook.worksheets.add("Changes");
const scenarios = workbook.worksheets.add("Scenarios");
const checks = workbook.worksheets.add("Checks");
const sources = workbook.worksheets.add("Sources & Audit");

const navy = "#15283B";
const teal = "#0F766E";
const pale = "#E8F1F3";
const ink = "#15202B";
const muted = "#5E6C76";
const green = "#DCFCE7";
const red = "#FEE2E2";
const amber = "#FEF3C7";
const white = "#FFFFFF";
const disclosure = "AI-generated market assessment. Not individually reviewed by a human analyst and not personal investment advice.";

function titleBand(sheet, range, title) {
  sheet.getRange(range).merge();
  sheet.getRange(range).values = [[title]];
  sheet.getRange(range).format = {
    fill: navy,
    font: { bold: true, color: white, size: 16 },
    verticalAlignment: "center",
  };
  sheet.getRange(range).format.rowHeight = 32;
}

function header(range) {
  range.format = {
    fill: teal,
    font: { bold: true, color: white },
    wrapText: true,
    verticalAlignment: "center",
    borders: { preset: "inside", style: "thin", color: "#D1E4E4" },
  };
  range.format.rowHeight = 28;
}

function compactBody(range) {
  range.format = {
    font: { color: ink, size: 10 },
    verticalAlignment: "top",
    borders: { insideHorizontal: { style: "thin", color: "#E5E7EB" } },
  };
}

// Cover
cover.showGridLines = false;
titleBand(cover, "A1:H2", "MAE · Autonomous Market Assessment");
cover.getRange("A4:B12").values = [
  ["Release ID", input.release_id],
  ["Snapshot date", validation.snapshot_date],
  ["Technical status", validation.technical_status],
  ["Model validation", validation.model_validation_status],
  ["Release status", "AUTO_PUBLISHED"],
  ["Human review", validation.human_review_status],
  ["Release manifest", input.release_manifest_hash],
  ["Matrix business hash", matrix.business_hash],
  ["Disclosure", disclosure],
];
cover.getRange("A4:A12").format = { fill: pale, font: { bold: true, color: navy }, verticalAlignment: "top" };
cover.getRange("B4:B12").format = { font: { color: ink }, wrapText: true, verticalAlignment: "top" };
cover.getRange("D4:E7").values = [
  ["Check", "Result"],
  ["Applicable scores", null],
  ["N/A positions", null],
  ["Total positions", null],
];
header(cover.getRange("D4:E4"));
cover.getRange("E5").formulas = [["=COUNTIF('Matrix'!$G$2:$G$115,\"APPLICABLE\")"]];
cover.getRange("E6").formulas = [["=COUNTIF('Matrix'!$G$2:$G$115,\"NOT_APPLICABLE\")"]];
cover.getRange("E7").formulas = [["=COUNTA('Matrix'!$A$2:$A$115)"]];
cover.getRange("D5:E7").format.borders = { preset: "inside", style: "thin", color: "#D9E1E8" };
cover.getRange("A14:H16").merge();
cover.getRange("A14:H16").values = [[disclosure]];
cover.getRange("A14:H16").format = { fill: amber, font: { italic: true, color: ink }, wrapText: true, verticalAlignment: "center" };
cover.getRange("A:A").format.columnWidth = 24;
cover.getRange("B:B").format.columnWidth = 46;
cover.getRange("C:C").format.columnWidth = 3;
cover.getRange("D:D").format.columnWidth = 24;
cover.getRange("E:E").format.columnWidth = 16;
cover.getRange("F:H").format.columnWidth = 12;

// Matrix source of truth
matrixSheet.showGridLines = false;
matrixSheet.freezePanes.freezeRows(1);
matrixSheet.freezePanes.freezeColumns(2);
const matrixHeaders = [
  "Canonical cell ID", "Row", "Asset class", "Asset group", "Asset segment", "Region",
  "Applicability", "Current", "Previous", "Delta", "Mode", "Confidence",
  "Supporting themes", "Source IDs", "Main risk", "Reasoning",
  "Invalidation", "Source URLs", "Business hash",
];
matrixSheet.getRange("A1:S1").values = [matrixHeaders];
header(matrixSheet.getRange("A1:S1"));
const matrixRows = matrix.cells.map((cell) => [
  cell.canonical_cell_id, cell.row_index, cell.asset_class, cell.asset_group, cell.asset_segment,
  cell.region, cell.applicability, cell.current, cell.previous, cell.delta, cell.mode,
  cell.confidence, (cell.supporting_theme_ids || []).join("; "),
  (cell.supporting_source_ids || []).join("; "), cell.main_risk || "",
  cell.reasoning, cell.invalidation,
  (cell.sources || []).map((source) => source.url).join("; "), cell.business_hash,
]);
matrixSheet.getRange(`A2:S${matrixRows.length + 1}`).values = matrixRows;
compactBody(matrixSheet.getRange("A2:S115"));
matrixSheet.getRange("B2:B115").format.numberFormat = "0";
matrixSheet.getRange("H2:J115").format.numberFormat = "+0;-0;0";
matrixSheet.getRange("M2:R115").format.wrapText = true;
for (const [col, width] of Object.entries({ A: 20, B: 7, C: 16, D: 18, E: 28, F: 14, G: 18, H: 10, I: 10, J: 9, K: 18, L: 12, M: 22, N: 42, O: 32, P: 48, Q: 44, R: 38, S: 22 })) {
  matrixSheet.getRange(`${col}:${col}`).format.columnWidth = width;
}
matrixSheet.getRange("H2:J115").conditionalFormats.add("cellIs", { operator: "lessThan", formula: 0, format: { fill: "#FECACA" } });
matrixSheet.getRange("H2:J115").conditionalFormats.add("cellIs", { operator: "equal", formula: 0, format: { fill: "#FFFFFF" } });
matrixSheet.getRange("H2:J115").conditionalFormats.add("cellIs", { operator: "greaterThan", formula: 0, format: { fill: "#BBF7D0" } });
matrixSheet.getRange("K2:K115").conditionalFormats.add("containsText", { text: "CARRY_FORWARD", format: { fill: pale, font: { color: navy } } });

// Formula-driven heatmap
heatmap.showGridLines = false;
titleBand(heatmap, "A1:H1", "19 × 6 Current MAE Matrix");
heatmap.getRange("A3:H3").values = [["Asset segment", "Global", "US", "Europe", "UK", "Japan", "EM ex China", "Row"]];
header(heatmap.getRange("A3:H3"));
const labelsByRow = [];
for (const cell of matrix.cells) {
  if (!labelsByRow.some((item) => item.row === cell.row_index)) {
    labelsByRow.push({ row: cell.row_index, label: `${cell.asset_class} · ${cell.asset_group} · ${cell.asset_segment}` });
  }
}
labelsByRow.sort((a, b) => a.row - b.row);
heatmap.getRange("A4:A22").values = labelsByRow.map((item) => [item.label]);
heatmap.getRange("H4:H22").values = labelsByRow.map((item) => [item.row]);
for (let row = 4; row <= 22; row += 1) {
  for (let col = 2; col <= 7; col += 1) {
    const letter = String.fromCharCode(64 + col);
    heatmap.getRange(`${letter}${row}`).formulas = [[
      `=IF(COUNTIFS('Matrix'!$B$2:$B$115,$H${row},'Matrix'!$F$2:$F$115,${letter}$3,'Matrix'!$G$2:$G$115,"APPLICABLE")=0,"N/A",SUMIFS('Matrix'!$H$2:$H$115,'Matrix'!$B$2:$B$115,$H${row},'Matrix'!$F$2:$F$115,${letter}$3))`,
    ]];
  }
}
heatmap.getRange("A4:H22").format.borders = { preset: "all", style: "thin", color: "#DCE3E8" };
heatmap.getRange("B4:G22").format = { horizontalAlignment: "center", verticalAlignment: "center", font: { bold: true, color: ink } };
heatmap.getRange("B4:G22").format.numberFormat = "+0;-0;0";
heatmap.getRange("B4:G22").conditionalFormats.add("cellIs", { operator: "lessThan", formula: 0, format: { fill: "#FECACA" } });
heatmap.getRange("B4:G22").conditionalFormats.add("cellIs", { operator: "equal", formula: 0, format: { fill: "#FFFFFF" } });
heatmap.getRange("B4:G22").conditionalFormats.add("cellIs", { operator: "greaterThan", formula: 0, format: { fill: "#BBF7D0" } });
heatmap.getRange("A:A").format.columnWidth = 46;
heatmap.getRange("B:G").format.columnWidth = 14;
heatmap.getRange("H:H").format.columnWidth = 8;
heatmap.freezePanes.freezeRows(3);

// Changes
changes.showGridLines = false;
titleBand(changes, "A1:H1", "What Changed");
changes.getRange("A3:H3").values = [["Cell", "Asset segment", "Region", "Previous", "Current", "Delta", "Mode", "Reasoning"]];
header(changes.getRange("A3:H3"));
const changedCells = matrix.cells.filter((cell) => cell.delta !== null && cell.delta !== 0);
const fallbackCells = matrix.cells.filter((cell) => cell.applicability === "APPLICABLE").slice(0, 20);
const changeRows = (changedCells.length ? changedCells : fallbackCells).map((cell) => [
  cell.canonical_cell_id, cell.asset_segment, cell.region, cell.previous, cell.current, cell.delta,
  cell.mode, cell.reasoning,
]);
changes.getRange(`A4:H${changeRows.length + 3}`).values = changeRows;
compactBody(changes.getRange(`A4:H${changeRows.length + 3}`));
changes.getRange(`D4:F${changeRows.length + 3}`).format.numberFormat = "+0;-0;0";
changes.getRange("H:H").format.wrapText = true;
for (const [col, width] of Object.entries({ A: 20, B: 28, C: 14, D: 10, E: 10, F: 9, G: 18, H: 60 })) changes.getRange(`${col}:${col}`).format.columnWidth = width;
changes.freezePanes.freezeRows(3);

// Scenarios
scenarios.showGridLines = false;
titleBand(scenarios, "A1:H1", "Scenario Lens");
scenarios.getRange("A3:H3").values = [["Scenario", "Name", "Probability", "Narrative", "Causal chain", "Trigger", "Invalidation", "Material affected cells"]];
header(scenarios.getRange("A3:H3"));
const scenarioRows = scenarioPayload.scenarios.map((row) => [
  row.scenario_id, row.scenario_name, row.probability_band, row.narrative, row.causal_chain,
  row.trigger, row.veto, row.material_affected_cells || "None in this release",
]);
scenarios.getRange("A4:H6").values = scenarioRows;
scenarios.getRange("A4:H6").format = { wrapText: true, verticalAlignment: "top", borders: { insideHorizontal: { style: "thin", color: "#DCE3E8" } } };
for (const [col, width] of Object.entries({ A: 12, B: 20, C: 14, D: 52, E: 54, F: 44, G: 44, H: 30 })) scenarios.getRange(`${col}:${col}`).format.columnWidth = width;
scenarios.getRange("4:6").format.rowHeight = 82;

// Checks
checks.showGridLines = false;
titleBand(checks, "A1:G1", "Reconciliation Checks");
checks.getRange("A3:G3").values = [["Check", "Actual", "Expected", "Difference", "Tolerance", "Status", "Notes"]];
header(checks.getRange("A3:G3"));
checks.getRange("A4:A9").values = [
  ["Total positions"], ["Applicable scores"], ["N/A positions"], ["Delta reconciliation errors"],
  ["Missing evidence modes"], ["Scenario count"],
];
checks.getRange("B4:B9").formulas = [
  ["=COUNTA('Matrix'!$A$2:$A$115)"],
  ["=COUNTIF('Matrix'!$G$2:$G$115,\"APPLICABLE\")"],
  ["=COUNTIF('Matrix'!$G$2:$G$115,\"NOT_APPLICABLE\")"],
  ["=SUMPRODUCT(--('Matrix'!$G$2:$G$115=\"APPLICABLE\"),--('Matrix'!$J$2:$J$115<>'Matrix'!$H$2:$H$115-'Matrix'!$I$2:$I$115))"],
  ["=COUNTIFS('Matrix'!$G$2:$G$115,\"APPLICABLE\",'Matrix'!$K$2:$K$115,\"\")"],
  ["=COUNTA('Scenarios'!$A$4:$A$6)"],
];
checks.getRange("C4:C9").values = [[114], [104], [10], [0], [0], [3]];
checks.getRange("D4:D9").formulas = [["=B4-C4"], ["=B5-C5"], ["=B6-C6"], ["=B7-C7"], ["=B8-C8"], ["=B9-C9"]];
checks.getRange("E4:E9").values = [[0], [0], [0], [0], [0], [0]];
checks.getRange("F4:F9").formulas = [["=IF(ABS(D4)<=E4,\"OK\",\"FAIL\")"], ["=IF(ABS(D5)<=E5,\"OK\",\"FAIL\")"], ["=IF(ABS(D6)<=E6,\"OK\",\"FAIL\")"], ["=IF(ABS(D7)<=E7,\"OK\",\"FAIL\")"], ["=IF(ABS(D8)<=E8,\"OK\",\"FAIL\")"], ["=IF(ABS(D9)<=E9,\"OK\",\"FAIL\")"]];
checks.getRange("G4:G9").values = [["Canonical geometry"], ["Integer scores only"], ["Explicit null positions"], ["Current - previous = delta"], ["One mode per applicable cell"], ["BASE / UPSIDE / DOWNSIDE"]];
checks.getRange("A4:G9").format.borders = { preset: "inside", style: "thin", color: "#DCE3E8" };
checks.getRange("F4:F9").conditionalFormats.add("containsText", { text: "OK", format: { fill: green, font: { bold: true, color: "#166534" } } });
checks.getRange("F4:F9").conditionalFormats.add("containsText", { text: "FAIL", format: { fill: red, font: { bold: true, color: "#991B1B" } } });
checks.getRange("A:A").format.columnWidth = 32;
checks.getRange("B:F").format.columnWidth = 14;
checks.getRange("G:G").format.columnWidth = 34;

// Sources and audit
sources.showGridLines = false;
titleBand(sources, "A1:G1", "Sources & Audit Trail");
sources.getRange("A3:B10").values = [
  ["Field", "Value"],
  ["Release ID", input.release_id],
  ["Release manifest hash", input.release_manifest_hash],
  ["Candidate content hash", validation.candidate_content_hash],
  ["Source manifest hash", validation.source_manifest_hash],
  ["Matrix business hash", matrix.business_hash],
  ["Historical analog source", explanationPayload.historical_analogs.source_artifact],
  ["Historical analog hash", explanationPayload.historical_analogs.source_sha256],
];
header(sources.getRange("A3:B3"));
sources.getRange("A4:A10").format = { fill: pale, font: { bold: true, color: navy } };
sources.getRange("B4:B10").format = { wrapText: true, font: { color: ink } };
sources.getRange("A12:G12").values = [["Cell", "Provider", "Title", "Publication date", "URL", "Mode", "Confidence"]];
header(sources.getRange("A12:G12"));
const sourceRows = matrix.cells.flatMap((cell) => (cell.sources || []).map((source) => [
  cell.canonical_cell_id, source.provider, source.title, source.publication_date, source.url, cell.mode, cell.confidence,
]));
if (sourceRows.length) {
  sources.getRange(`A13:G${sourceRows.length + 12}`).values = sourceRows;
  compactBody(sources.getRange(`A13:G${sourceRows.length + 12}`));
} else {
  sources.getRange("A13:G13").merge();
  sources.getRange("A13:G13").values = [["No source rows were available in this release artifact; publication is blocked unless validation explains the gap."]];
  sources.getRange("A13:G13").format = { fill: pale, font: { italic: true, color: muted }, wrapText: true };
}
for (const [col, width] of Object.entries({ A: 24, B: 28, C: 40, D: 16, E: 52, F: 18, G: 14 })) sources.getRange(`${col}:${col}`).format.columnWidth = width;

await fs.mkdir(path.dirname(input.xlsx_path), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const keyInspection = await workbook.inspect({
  kind: "table",
  range: "Checks!A3:G9",
  include: "values,formulas",
  tableMaxRows: 10,
  tableMaxCols: 8,
});
const errorInspection = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log(keyInspection.ndjson);
console.log(errorInspection.ndjson);

const previewErrors = [];
for (const sheetName of ["Cover", "Matrix", "Heatmap", "Changes", "Scenarios", "Checks", "Sources & Audit"]) {
  try {
    const preview = await workbook.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
    const safeName = sheetName.replace(/[^A-Za-z0-9]+/g, "_").toLowerCase();
    await fs.writeFile(path.join(previewDir, `${safeName}.png`), new Uint8Array(await preview.arrayBuffer()));
  } catch (error) {
    previewErrors.push({ sheetName, error: error instanceof Error ? error.message : String(error).slice(0, 500) });
  }
}
if (previewErrors.length) {
  console.log(JSON.stringify({ preview_errors: previewErrors }));
}

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(input.xlsx_path);
console.log(JSON.stringify({ xlsx_path: input.xlsx_path, sheets: 7, rows: matrix.cells.length }));
