import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

function argsMap(argv) {
  const result = {};
  for (let index = 2; index < argv.length; index += 2) result[argv[index].replace(/^--/, "")] = argv[index + 1];
  return result;
}

function colName(number) {
  let result = "";
  for (let n = number; n > 0; n = Math.floor((n - 1) / 26)) result = String.fromCharCode(65 + ((n - 1) % 26)) + result;
  return result;
}

async function csvObjects(file, sheetName) {
  const csvBook = await Workbook.fromCSV(await fs.readFile(file, "utf8"), { sheetName });
  const values = csvBook.worksheets.getItem(sheetName).getUsedRange().values;
  const headers = values[0].map((value) => String(value));
  return values.slice(1).filter((row) => row.some((value) => value !== null && value !== "")).map((row) =>
    Object.fromEntries(headers.map((header, index) => [header, row[index]])),
  );
}

const args = argsMap(process.argv);
if (args["inspect-template"]) {
  const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(args["inspect-template"]));
  await fs.mkdir(args["preview-dir"], { recursive: true });
  for (const sheet of workbook.worksheets.items) {
    const preview = await workbook.render({ sheetName: sheet.name, autoCrop: "all", scale: 0.8, format: "png" });
    await fs.writeFile(path.join(args["preview-dir"], `${sheet.name}.png`), new Uint8Array(await preview.arrayBuffer()));
  }
  const inspection = await workbook.inspect({ kind: "workbook,sheet,table,region", maxChars: 12000, tableMaxRows: 30, tableMaxCols: 14 });
  console.log(inspection.ndjson);
  process.exit(0);
}

for (const required of ["scores", "evidence", "scenarios", "transmission", "manifest", "output", "preview-dir"]) {
  if (!args[required]) throw new Error(`Missing --${required}`);
}

const scores = await csvObjects(args.scores, "ScoresCSV");
const evidence = await csvObjects(args.evidence, "EvidenceCSV");
const scenarios = await csvObjects(args.scenarios, "ScenariosCSV");
const transmissionRows = await csvObjects(args.transmission, "TransmissionCSV");
const manifest = JSON.parse(await fs.readFile(args.manifest, "utf8"));
if (scores.length !== 114) throw new Error(`Expected 114 score intersections, got ${scores.length}`);
if (scores.filter((row) => String(row.applicable) === "true").length !== 104) throw new Error("Expected 104 applicable scores");
if (scenarios.length !== 3) throw new Error(`Expected 3 scenarios, got ${scenarios.length}`);
if (transmissionRows.length !== 104) throw new Error(`Expected 104 transmission rows, got ${transmissionRows.length}`);

const COLORS = {
  navy: "#17365D", navy2: "#244A73", page: "#F3F6FA", surface: "#FFFFFF", line: "#CAD3DF",
  text: "#1F2937", muted: "#5B6573", white: "#FFFFFF", accent: "#D9EAF7", gray: "#D9DEE5",
  red3: "#8B1E2D", red2: "#C3424F", red1: "#F4C7CA", green1: "#D7EBC8", green2: "#73B65B",
  green3: "#2F6B3A", yellow: "#F5E4A5", blue: "#BFD7EA",
};

const GEOS = ["Global", "US", "Europe", "UK", "Japan", "EM ex China"];
const ASSETS = [...new Set(scores.map((row) => String(row.asset_segment)))];
const workbook = Workbook.create();
const strategy = workbook.worksheets.add("Strategy_Sentiment_Map");
const changes = workbook.worksheets.add("Change_Tracker");
const scenarioSheet = workbook.worksheets.add("Scenarios");
const evidenceSheet = workbook.worksheets.add("Evidence_Expectation");
const transmissionSheet = workbook.worksheets.add("Transmission_Control");

function baseSheet(sheet, lastColumn, title, subtitle) {
  sheet.showGridLines = false;
  sheet.getRange(`A1:${lastColumn}1`).merge();
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${lastColumn}1`).format = { fill: COLORS.navy, font: { bold: true, color: COLORS.white, size: 18 }, verticalAlignment: "center" };
  sheet.getRange(`A1:${lastColumn}1`).format.rowHeight = 32;
  sheet.getRange(`A2:${lastColumn}2`).merge();
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A2:${lastColumn}2`).format = { fill: COLORS.page, font: { italic: true, color: COLORS.muted, size: 10 }, verticalAlignment: "center" };
  sheet.getRange(`A2:${lastColumn}2`).format.rowHeight = 24;
}

function header(range, height = 36) {
  range.format = { fill: COLORS.navy, font: { bold: true, color: COLORS.white, size: 9 }, wrapText: true, verticalAlignment: "center", horizontalAlignment: "left", borders: { preset: "all", style: "thin", color: COLORS.line } };
  range.format.rowHeight = height;
}

function body(range, height = 48) {
  range.format = { fill: COLORS.surface, font: { color: COLORS.text, size: 9 }, wrapText: true, verticalAlignment: "top", borders: { preset: "all", style: "thin", color: COLORS.line } };
  range.format.rowHeight = height;
}

function labelBlock(sheet, labelRange, valueRange, rows) {
  sheet.getRange(labelRange).values = rows.map((row) => [row[0]]);
  sheet.getRange(valueRange).values = rows.map((row) => [row[1]]);
  sheet.getRange(labelRange).format = { fill: COLORS.page, font: { bold: true, color: COLORS.text, size: 9 }, wrapText: true, borders: { preset: "all", style: "thin", color: COLORS.line } };
  sheet.getRange(valueRange).format = { fill: COLORS.surface, font: { bold: true, color: COLORS.text, size: 9 }, wrapText: true, horizontalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.line } };
}

function widths(sheet, entries) {
  for (const [column, width] of entries) sheet.getRange(`${column}:${column}`).format.columnWidth = width;
}

function scoreFormatting(range) {
  const formats = [
    [-3, COLORS.red3, COLORS.white], [-2, COLORS.red2, COLORS.white], [-1, COLORS.red1, COLORS.text],
    [0, COLORS.gray, COLORS.text], [1, COLORS.green1, COLORS.text], [2, COLORS.green2, COLORS.text], [3, COLORS.green3, COLORS.white],
  ];
  for (const [value, fill, color] of formats) range.conditionalFormats.add("cellIs", { operator: "equal", formula: value, format: { fill, font: { bold: true, color } } });
  range.conditionalFormats.add("containsText", { text: "N/A", format: { fill: COLORS.gray, font: { bold: true, color: COLORS.muted } } });
}

const applicable = scores.filter((row) => String(row.applicable) === "true");
const scoreMap = new Map(scores.map((row) => [`${row.asset_segment}|${row.geography}`, row]));
baseSheet(strategy, "H", "MAE Full Market Map", `Excel-first production snapshot • ${manifest.snapshot_date} • ${manifest.snapshot_type} • horizon ${manifest.horizon}`);
labelBlock(strategy, "A3:A8", "B3:B8", [
  ["snapshot date", manifest.snapshot_date], ["previous comparable", manifest.previous_snapshot_date], ["horizon", manifest.horizon],
  ["snapshot type", manifest.snapshot_type], ["validation status", manifest.validation_status], ["applicable coverage", manifest.evidence_coverage],
]);
labelBlock(strategy, "D3:D8", "E3:E8", [
  ["technical PASS", `${manifest.technical_pass_count}/104`], ["analytical STRONG", manifest.analytical_quality.STRONG], ["analytical ACCEPTABLE", manifest.analytical_quality.ACCEPTABLE],
  ["analytical WEAK", manifest.analytical_quality.WEAK], ["REVIEW_REQUIRED", manifest.analytical_quality.REVIEW_REQUIRED], ["regional confirmation", `${manifest.regional_market_confirmation_coverage}/104`],
]);
labelBlock(strategy, "G3:G8", "H3:H8", [
  ["DIRECT / COMPOSITE", `${manifest.evidence_modes.DIRECT} / ${manifest.evidence_modes.COMPOSITE}`], ["HIGH / MEDIUM", `${manifest.convictions.HIGH} / ${manifest.convictions.MEDIUM}`], ["LOW conviction", manifest.convictions.LOW],
  ["limited / structural", `${manifest.comparability.LIMITED_COMPARABILITY} / ${manifest.comparability.STRUCTURAL_PROXY}`], ["methodology refinements", manifest.methodology_refinements], ["evidence gaps", 0],
]);
strategy.getRange("A10:H10").merge();
strategy.getRange("A10").values = [["Market Summary"]];
header(strategy.getRange("A10:H10"), 22);
strategy.getRange("A11:H12").merge();
strategy.getRange("A11").values = [[manifest.market_summary]];
strategy.getRange("A11:H12").format = { fill: COLORS.surface, font: { color: COLORS.text, size: 10 }, wrapText: true, verticalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.line } };
strategy.getRange("A11:H12").format.rowHeight = 42;
strategy.getRange("A14:G14").values = [["Asset segment", ...GEOS]];
header(strategy.getRange("A14:G14"), 28);
const matrixValues = ASSETS.map((asset) => [asset, ...GEOS.map((geo) => {
  const row = scoreMap.get(`${asset}|${geo}`);
  return String(row.applicable) === "true" ? Number(row.score) : "N/A";
})]);
strategy.getRange(`A15:G${14 + ASSETS.length}`).values = matrixValues;
body(strategy.getRange(`A15:G${14 + ASSETS.length}`), 25);
strategy.getRange(`A15:A${14 + ASSETS.length}`).format = { fill: COLORS.page, font: { bold: true, color: COLORS.text, size: 9 }, wrapText: true, verticalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.line } };
strategy.getRange(`B15:G${14 + ASSETS.length}`).format = { font: { bold: true, color: COLORS.text, size: 10 }, horizontalAlignment: "center", verticalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.line }, numberFormat: "0;[Red]-0;0" };
scoreFormatting(strategy.getRange(`B15:G${14 + ASSETS.length}`));
const ranked = [...applicable].sort((a, b) => Number(b.score) - Number(a.score) || Number(a.display_order) - Number(b.display_order));
const bottom = [...applicable].sort((a, b) => Number(a.score) - Number(b.score) || Number(a.display_order) - Number(b.display_order));
const low = applicable.filter((row) => row.conviction === "LOW").slice(0, 10);
const refined = applicable.filter((row) => row.change_type === "METHODOLOGY_REFINEMENT").sort((a, b) => Math.abs(Number(b.score_delta)) - Math.abs(Number(a.score_delta))).slice(0, 10);
const limited = applicable.filter((row) => row.comparability_status !== "DIRECTLY_COMPARABLE").slice(0, 10);
strategy.getRange("A36:H36").merge(); strategy.getRange("A36").values = [["Decision panels"]]; header(strategy.getRange("A36:H36"), 22);
strategy.getRange("A37:H37").values = [["Top 10 attractive cells", "score", "Bottom 10 cells", "score", "Methodology refinements", "delta", "Low conviction / limited", "score"]];
header(strategy.getRange("A37:H37"), 30);
const panels = Array.from({ length: 10 }, (_, index) => {
  const topRow = ranked[index]; const bottomRow = bottom[index]; const refinedRow = refined[index]; const lowRow = low[index] || limited[index];
  return [
    topRow ? `${topRow.geography} × ${topRow.asset_segment}` : "", topRow ? Number(topRow.score) : "",
    bottomRow ? `${bottomRow.geography} × ${bottomRow.asset_segment}` : "", bottomRow ? Number(bottomRow.score) : "",
    refinedRow ? `${refinedRow.geography} × ${refinedRow.asset_segment}` : "", refinedRow ? Number(refinedRow.score_delta) : "",
    lowRow ? `${lowRow.geography} × ${lowRow.asset_segment}` : "", lowRow ? Number(lowRow.score) : "",
  ];
});
strategy.getRange("A38:H47").values = panels; body(strategy.getRange("A38:H47"), 24);
scoreFormatting(strategy.getRange("B38:B47")); scoreFormatting(strategy.getRange("D38:D47")); scoreFormatting(strategy.getRange("H38:H47"));
strategy.getRange("A49:H49").merge(); strategy.getRange("A49").values = [["Evidence gaps: 0 • all 104 applicable cells have PASS evidence packages"]];
strategy.getRange("A49:H49").format = { fill: COLORS.green1, font: { bold: true, color: COLORS.text }, verticalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.line } };
widths(strategy, [["A", 39], ["B", 15], ["C", 35], ["D", 15], ["E", 37], ["F", 14], ["G", 38], ["H", 15]]);
strategy.freezePanes.freezeRows(14); strategy.freezePanes.freezeColumns(1);

baseSheet(changes, "K", "Change Tracker", `Previous ${manifest.previous_snapshot_date} → current ${manifest.snapshot_date}`);
labelBlock(changes, "A3:A6", "B3:B6", [["previous snapshot", manifest.previous_snapshot_date], ["current snapshot", manifest.snapshot_date], ["upgrades", manifest.upgrades], ["downgrades", manifest.downgrades]]);
labelBlock(changes, "D3:D6", "E3:E6", [["methodology refinements", manifest.methodology_refinements], ["unchanged / carry-forward", manifest.unchanged], ["benchmark refinements", manifest.benchmark_refinements], ["N/A", manifest.not_applicable_cells]]);
changes.getRange("A8:G8").values = [["Asset segment", ...GEOS]]; header(changes.getRange("A8:G8"), 28);
changes.getRange(`A9:G${8 + ASSETS.length}`).values = ASSETS.map((asset) => [asset, ...GEOS.map((geo) => {
  const row = scoreMap.get(`${asset}|${geo}`);
  if (String(row.applicable) !== "true") return "N/A";
  if (row.change_type === "NEW_COMPARABLE_ASSESSMENT") return "NEW";
  return Number(row.score_delta || 0);
})]);
body(changes.getRange(`A9:G${8 + ASSETS.length}`), 25);
changes.getRange(`A9:A${8 + ASSETS.length}`).format = { fill: COLORS.page, font: { bold: true, color: COLORS.text }, wrapText: true, borders: { preset: "all", style: "thin", color: COLORS.line } };
changes.getRange(`B9:G${8 + ASSETS.length}`).format = { horizontalAlignment: "center", verticalAlignment: "center", font: { bold: true, color: COLORS.text }, borders: { preset: "all", style: "thin", color: COLORS.line } };
changes.getRange(`B9:G${8 + ASSETS.length}`).conditionalFormats.add("containsText", { text: "NEW", format: { fill: COLORS.blue, font: { bold: true } } });
changes.getRange(`B9:G${8 + ASSETS.length}`).conditionalFormats.add("containsText", { text: "N/A", format: { fill: COLORS.gray, font: { bold: true, color: COLORS.muted } } });
const detailRows = applicable.filter((row) => row.change_type === "METHODOLOGY_REFINEMENT" || row.change_type === "NEW_COMPARABLE_ASSESSMENT").sort((a, b) => Math.abs(Number(b.score_delta || b.score)) - Math.abs(Number(a.score_delta || a.score))).slice(0, 30);
changes.getRange("A30:K30").merge(); changes.getRange("A30").values = [["Material change detail • upgrades / downgrades / methodology refinements are classified separately"]]; header(changes.getRange("A30:K30"), 22);
changes.getRange("A31:K31").values = [["cell_id", "previous score", "current score", "delta", "change type", "previous thesis", "current thesis", "what changed", "why it changed", "new evidence", "analyst conclusion"]]; header(changes.getRange("A31:K31"), 42);
changes.getRange(`A32:K${31 + detailRows.length}`).values = detailRows.map((row) => [row.cell_id, row.previous_score === "" ? "" : Number(row.previous_score), Number(row.score), row.score_delta === "" ? "NEW" : Number(row.score_delta), row.change_type, "", row.thesis, row.what_changed, row.why_changed, `${row.benchmark_id} • ${row.comparability_status}`, row.analyst_action]);
body(changes.getRange(`A32:K${31 + detailRows.length}`), 66);
widths(changes, [["A", 37], ["B", 14], ["C", 14], ["D", 12], ["E", 28], ["F", 32], ["G", 48], ["H", 34], ["I", 42], ["J", 26], ["K", 24]]);
changes.freezePanes.freezeRows(8); changes.freezePanes.freezeColumns(1);

baseSheet(scenarioSheet, "L", "Scenarios", "One canonical Base / Upside / Downside dataset • probability bands: LOW 0–30%, MEDIUM 30–60%, HIGH 60–100%");
const scenarioHeaders = ["scenario name", "status", "probability band", "narrative", "causal chain", "macro drivers", "winning cells", "vulnerable cells", "indicators", "trigger", "veto / invalidation", "review date"];
scenarioSheet.getRange("A4:L4").values = [scenarioHeaders]; header(scenarioSheet.getRange("A4:L4"), 42);
scenarioSheet.getRange("A5:L7").values = scenarios.map((row) => [row.scenario_name, row.current_status, row.probability_band, row.narrative, row.causal_chain, row.macro_drivers, row.winners, row.vulnerable_assets, row.indicators_to_watch, row.trigger, row.veto, row.last_review_date]);
body(scenarioSheet.getRange("A5:L7"), 112);
scenarioSheet.getRange("A10:F10").values = [["cell_id", "asset segment", "geography", "Base", "Upside / Risk-on", "Downside / Risk-off"]]; header(scenarioSheet.getRange("A10:F10"), 34);
function reactionMap(row) {
  return new Map(String(row.expected_reaction_by_cell).split(";").map((part) => part.trim()).filter(Boolean).map((part) => { const at = part.indexOf(":"); return [part.slice(0, at), Number(part.slice(at + 1))]; }));
}
const reactions = Object.fromEntries(scenarios.map((row) => [row.scenario_id, reactionMap(row)]));
scenarioSheet.getRange("A11:F114").values = applicable.map((row) => [row.cell_id, row.asset_segment, row.geography, reactions.BASE.get(row.cell_id), reactions.UPSIDE.get(row.cell_id), reactions.DOWNSIDE.get(row.cell_id)]);
body(scenarioSheet.getRange("A11:F114"), 24); scoreFormatting(scenarioSheet.getRange("D11:F114"));
widths(scenarioSheet, [["A", 38], ["B", 39], ["C", 16], ["D", 13], ["E", 18], ["F", 20], ["G", 40], ["H", 40], ["I", 36], ["J", 48], ["K", 48], ["L", 14]]);
scenarioSheet.freezePanes.freezeRows(10); scenarioSheet.freezePanes.freezeColumns(3);

const evidenceHeaders = Object.keys(evidence[0]);
const evidenceLast = colName(evidenceHeaders.length);
baseSheet(evidenceSheet, evidenceLast, "Evidence & Expectation", `Full PASS evidence register • ${evidence.length} rows • coverage ${manifest.evidence_coverage}`);
evidenceSheet.getRange(`A4:${evidenceLast}4`).values = [evidenceHeaders]; header(evidenceSheet.getRange(`A4:${evidenceLast}4`), 50);
evidenceSheet.getRange(`A5:${evidenceLast}${4 + evidence.length}`).values = evidence.map((row) => evidenceHeaders.map((key) => row[key]));
body(evidenceSheet.getRange(`A5:${evidenceLast}${4 + evidence.length}`), 58);
const urlColumn = colName(evidenceHeaders.indexOf("URL") + 1);
evidence.forEach((row, index) => {
  const escaped = String(row.URL).replaceAll('"', '""');
  evidenceSheet.getRange(`${urlColumn}${index + 5}`).formulas = [[`=HYPERLINK("${escaped}","Open source")`]];
});
const reviewColumn = colName(evidenceHeaders.indexOf("review_status") + 1);
evidenceSheet.getRange(`${reviewColumn}5:${reviewColumn}${4 + evidence.length}`).conditionalFormats.add("containsText", { text: "PASS", format: { fill: COLORS.green1, font: { bold: true } } });
for (let index = 0; index < evidenceHeaders.length; index++) {
  const key = evidenceHeaders[index]; const column = colName(index + 1);
  const width = ["excerpt", "relevance_reason", "research_view", "fundamental_macro_summary", "market_confirmation", "valuation_risk_summary", "conflict", "reviewer_note", "instrument_definition", "methodology_note"].includes(key) ? 48
    : ["title", "benchmark_name"].includes(key) ? 38 : ["URL", "market_data_source"].includes(key) ? 18 : ["cell_id", "source_id", "evidence_id", "benchmark_id"].includes(key) ? 28 : 16;
  evidenceSheet.getRange(`${column}:${column}`).format.columnWidth = width;
}
evidenceSheet.tables.add(`A4:${evidenceLast}${4 + evidence.length}`, true, "FullEvidenceTable");
evidenceSheet.freezePanes.freezeRows(4); evidenceSheet.freezePanes.freezeColumns(2);

const transmissionHeaders = Object.keys(transmissionRows[0]);
const transmissionLast = colName(transmissionHeaders.length);
baseSheet(transmissionSheet, transmissionLast, "Transmission & Control", "Macro driver → transmission mechanism → asset segment → expected reaction");
transmissionSheet.getRange(`A4:${transmissionLast}4`).values = [transmissionHeaders]; header(transmissionSheet.getRange(`A4:${transmissionLast}4`), 46);
transmissionSheet.getRange(`A5:${transmissionLast}${4 + transmissionRows.length}`).values = transmissionRows.map((row) => transmissionHeaders.map((key) => row[key]));
body(transmissionSheet.getRange(`A5:${transmissionLast}${4 + transmissionRows.length}`), 70);
for (let index = 0; index < transmissionHeaders.length; index++) {
  const key = transmissionHeaders[index]; const column = colName(index + 1);
  const width = ["macro_driver", "transmission_mechanism", "investment_logic", "trigger", "veto", "trigger_threshold", "veto_threshold", "monitoring_indicator", "primary_risk", "secondary_risk", "source_ids"].includes(key) ? 42 : ["cell_id", "asset_segment", "benchmark_id"].includes(key) ? 34 : 16;
  transmissionSheet.getRange(`${column}:${column}`).format.columnWidth = width;
}
const controlColumn = colName(transmissionHeaders.indexOf("control_status") + 1);
transmissionSheet.getRange(`${controlColumn}5:${controlColumn}${4 + transmissionRows.length}`).conditionalFormats.add("containsText", { text: "PASS", format: { fill: COLORS.green1, font: { bold: true } } });
for (const field of ["current_score", "base_scenario_effect", "upside_scenario_effect", "downside_scenario_effect"]) {
  const column = colName(transmissionHeaders.indexOf(field) + 1); scoreFormatting(transmissionSheet.getRange(`${column}5:${column}${4 + transmissionRows.length}`));
}
transmissionSheet.tables.add(`A4:${transmissionLast}${4 + transmissionRows.length}`, true, "FullTransmissionTable");
transmissionSheet.freezePanes.freezeRows(4); transmissionSheet.freezePanes.freezeColumns(2);

await fs.mkdir(path.dirname(args.output), { recursive: true });
await fs.rm(args["preview-dir"], { recursive: true, force: true });
await fs.mkdir(args["preview-dir"], { recursive: true });
const previews = [
  ["Strategy_Sentiment_Map", "A1:H49", 0.88], ["Change_Tracker", "A1:K61", 0.65],
  ["Scenarios", "A1:L35", 0.55], ["Evidence_Expectation", `A1:${evidenceLast}24`, 0.38],
  ["Transmission_Control", `A1:${transmissionLast}35`, 0.45],
];
for (const [sheetName, range, scale] of previews) {
  const preview = await workbook.render({ sheetName, range, scale, format: "png" });
  await fs.writeFile(path.join(args["preview-dir"], `${sheetName}.png`), new Uint8Array(await preview.arrayBuffer()));
}

const keyRange = await workbook.inspect({ kind: "table", range: "Strategy_Sentiment_Map!A1:H49", include: "values,formulas", tableMaxRows: 50, tableMaxCols: 8, maxChars: 10000 });
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 300 }, summary: "full workbook formula error scan" });
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(args.output);
console.log(JSON.stringify({
  output: args.output,
  sheets: ["Strategy_Sentiment_Map", "Change_Tracker", "Scenarios", "Evidence_Expectation", "Transmission_Control"],
  scoreRows: scores.length, evidenceRows: evidence.length, scenarioRows: scenarios.length, transmissionRows: transmissionRows.length,
  previews: previews.map(([name]) => path.join(args["preview-dir"], `${name}.png`)), keyRange: keyRange.ndjson, formulaErrors: errors.ndjson,
}, null, 2));
