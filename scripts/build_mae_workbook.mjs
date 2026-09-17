import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

function argsMap(argv) {
  const result = {};
  for (let index = 2; index < argv.length; index += 2) result[argv[index].replace(/^--/, "")] = argv[index + 1];
  return result;
}

async function csvObjects(file, sheetName) {
  const book = await Workbook.fromCSV(await fs.readFile(file, "utf8"), { sheetName });
  const values = book.worksheets.getItem(sheetName).getUsedRange().values;
  return values.slice(1).map((row) => Object.fromEntries(values[0].map((header, index) => [header, row[index]])));
}

const args = argsMap(process.argv);
for (const required of ["scores", "evidence", "scenarios", "manifest", "output", "preview-dir"]) {
  if (!args[required]) throw new Error(`Missing --${required}`);
}

const scores = await csvObjects(args.scores, "ScoresCSV");
const evidence = await csvObjects(args.evidence, "EvidenceCSV");
const scenarios = await csvObjects(args.scenarios, "ScenariosCSV");
const manifest = JSON.parse(await fs.readFile(args.manifest, "utf8"));
const displaySnapshotType = manifest.snapshot_status === "TEST_ONLY" ? "TEST_ONLY" : manifest.snapshot_type;
if (scores.length !== 10) throw new Error(`Expected 10 scores, got ${scores.length}`);
if (scenarios.length !== 3) throw new Error(`Expected 3 scenarios, got ${scenarios.length}`);

const COLORS = {
  ink: "#292823",
  page: "#E8E7DD",
  surface: "#F5F3EA",
  accent: "#C6E79A",
  positive: "#A9D889",
  negative: "#C9675A",
  neutral: "#D8D4C8",
  warning: "#F0CE78",
  line: "#D7D3C6",
  muted: "#68665D",
  white: "#FFFFFF",
};

const workbook = Workbook.create();
const strategy = workbook.worksheets.add("Strategy_Sentiment_Map");
const changes = workbook.worksheets.add("Change_Tracker");
const scenarioSheet = workbook.worksheets.add("Scenarios");
const evidenceSheet = workbook.worksheets.add("Evidence_Expectation");
const transmission = workbook.worksheets.add("Transmission_Control");

function d(value) {
  return value ? new Date(`${value}T00:00:00Z`) : null;
}

function baseSheet(sheet, lastColumn, title, subtitle) {
  sheet.showGridLines = false;
  sheet.getRange(`A1:${lastColumn}1`).merge();
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: COLORS.ink,
    font: { bold: true, color: COLORS.white, size: 18 },
    verticalAlignment: "center",
  };
  sheet.getRange(`A2:${lastColumn}2`).merge();
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A2:${lastColumn}2`).format = {
    fill: COLORS.page,
    font: { italic: true, color: COLORS.muted, size: 10 },
    verticalAlignment: "center",
  };
  sheet.getRange(`A1:${lastColumn}1`).format.rowHeight = 30;
  sheet.getRange(`A2:${lastColumn}2`).format.rowHeight = 24;
}

function header(range) {
  range.format = {
    fill: COLORS.ink,
    font: { bold: true, color: COLORS.white, size: 9 },
    wrapText: true,
    verticalAlignment: "center",
    horizontalAlignment: "left",
    borders: { preset: "all", style: "thin", color: COLORS.line },
  };
  range.format.rowHeight = 40;
}

function body(range, rowHeight = 66) {
  range.format = {
    fill: COLORS.surface,
    font: { color: COLORS.ink, size: 9 },
    wrapText: true,
    verticalAlignment: "top",
    borders: { preset: "all", style: "thin", color: COLORS.line },
  };
  range.format.rowHeight = rowHeight;
}

function widths(sheet, mapping) {
  for (const [column, width] of mapping) sheet.getRange(`${column}:${column}`).format.columnWidth = width;
}

function addScoreFormatting(range) {
  range.conditionalFormats.add("cellIs", { operator: "greaterThan", formula: 0, format: { fill: COLORS.positive, font: { bold: true } } });
  range.conditionalFormats.add("cellIs", { operator: "equal", formula: 0, format: { fill: COLORS.neutral, font: { bold: true } } });
  range.conditionalFormats.add("cellIs", { operator: "lessThan", formula: 0, format: { fill: COLORS.negative, font: { bold: true, color: COLORS.white } } });
}

function addDeltaFormatting(range) {
  range.conditionalFormats.add("cellIs", { operator: "greaterThan", formula: 0, format: { fill: COLORS.positive, font: { bold: true } } });
  range.conditionalFormats.add("cellIs", { operator: "lessThan", formula: 0, format: { fill: COLORS.negative, font: { bold: true, color: COLORS.white } } });
  range.conditionalFormats.add("cellIs", { operator: "equal", formula: 0, format: { fill: COLORS.neutral } });
}

function primaryEvidence(cellId) {
  const rows = evidence.filter((row) => row.cell_id === cellId && row.review_status === "PASS");
  const research = rows.filter((row) => !String(row.provider).includes("FRED"));
  const candidates = research.length ? research : rows;
  return candidates.sort((a, b) => String(b.publication_date).localeCompare(String(a.publication_date)))[0] || {};
}

baseSheet(
  strategy,
  "N",
  "Strategy & Sentiment Map",
  `Excel-first MAE snapshot • ${manifest.snapshot_date} • ${displaySnapshotType} • horizon ${manifest.horizon}`,
);
strategy.getRange("A3:B7").values = [
  ["snapshot date", d(manifest.snapshot_date)],
  ["snapshot type", displaySnapshotType],
  ["horizon", manifest.horizon],
  ["current market regime", manifest.current_market_regime],
  ["previous comparable snapshot", d(manifest.previous_snapshot_date)],
];
strategy.getRange("A3:A7").format = { fill: COLORS.page, font: { bold: true, color: COLORS.ink }, borders: { preset: "all", style: "thin", color: COLORS.line } };
strategy.getRange("B3:B7").format = { fill: COLORS.surface, font: { color: COLORS.ink }, wrapText: true, borders: { preset: "all", style: "thin", color: COLORS.line } };
strategy.getRange("B3").format.numberFormat = "yyyy-mm-dd";
strategy.getRange("B7").format.numberFormat = "yyyy-mm-dd";
strategy.getRange("D3:D7").values = [["upgrades"], ["downgrades"], ["unchanged"], ["evidence coverage"], ["scenario count"]];
strategy.getRange("D3:D7").format = { fill: COLORS.page, font: { bold: true, color: COLORS.ink }, borders: { preset: "all", style: "thin", color: COLORS.line } };
strategy.getRange("E3").formulas = [["=COUNTIF(K19:K28,\">0\")"]];
strategy.getRange("E4").formulas = [["=COUNTIF(K19:K28,\"<0\")"]];
strategy.getRange("E5").formulas = [["=COUNTIF(K19:K28,\"=0\")"]];
strategy.getRange("E6").formulas = [["=COUNTA(G19:G28)&\"/10\""]];
strategy.getRange("E7").formulas = [["=COUNTA('Scenarios'!A5:A7)"]];
strategy.getRange("E3:E7").format = { fill: COLORS.accent, font: { bold: true, color: COLORS.ink }, horizontalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.line } };
strategy.getRange("A9:N9").merge();
strategy.getRange("A9").values = [["Market summary"]];
strategy.getRange("A9:N9").format = { fill: COLORS.ink, font: { bold: true, color: COLORS.white }, verticalAlignment: "center" };
manifest.market_summary_bullets.slice(0, 5).forEach((text, index) => {
  const row = 10 + index;
  strategy.getRange(`A${row}:N${row}`).merge();
  strategy.getRange(`A${row}`).values = [[`• ${text}`]];
  strategy.getRange(`A${row}:N${row}`).format = { fill: index % 2 ? COLORS.surface : COLORS.page, font: { color: COLORS.ink, size: 10 }, wrapText: true, verticalAlignment: "center" };
  strategy.getRange(`A${row}:N${row}`).format.rowHeight = 24;
});
strategy.getRange("A15:G16").merge();
strategy.getRange("A15").values = [[`Leading positions: ${manifest.leading_positions.join("; ")}`]];
strategy.getRange("H15:N16").merge();
strategy.getRange("H15").values = [[`Most vulnerable: ${manifest.vulnerable_positions.join("; ")}`]];
strategy.getRange("A15:G16").format = { fill: COLORS.positive, font: { bold: true, color: COLORS.ink }, wrapText: true, verticalAlignment: "center" };
strategy.getRange("H15:N16").format = { fill: COLORS.negative, font: { bold: true, color: COLORS.white }, wrapText: true, verticalAlignment: "center" };

const strategyHeaders = ["region", "asset", "score", "direction", "current thesis", "main driver", "evidence status", "source provider", "short supporting excerpt", "previous score", "score delta", "change status", "affected scenario", "last review date"];
strategy.getRange("A18:N18").values = [strategyHeaders];
header(strategy.getRange("A18:N18"));
strategy.getRange("A19:N28").values = scores.map((row) => {
  const source = primaryEvidence(row.cell_id);
  return [
    row.geography,
    row.segment,
    Number(row.score),
    row.view,
    row.current_thesis,
    row.current_driver,
    row.evidence_status_code,
    source.provider || row.source_1_provider,
    source.excerpt || row.source_1_excerpt,
    Number(row.previous_score_numeric),
    null,
    row.change_status,
    row.affected_scenario,
    d(row.last_review_date),
  ];
});
body(strategy.getRange("A19:N28"), 86);
strategy.getRange("K19").formulas = [["=C19-J19"]];
strategy.getRange("K19:K28").fillDown();
strategy.getRange("C19:C28").format.numberFormat = "0";
strategy.getRange("J19:K28").format.numberFormat = "0;[Red]-0;-";
strategy.getRange("N19:N28").format.numberFormat = "yyyy-mm-dd";
addScoreFormatting(strategy.getRange("C19:C28"));
addDeltaFormatting(strategy.getRange("K19:K28"));
strategy.getRange("L19:L28").conditionalFormats.add("containsText", { text: "CARRY_FORWARD", format: { fill: COLORS.warning, font: { bold: true } } });
widths(strategy, [["A", 12], ["B", 30], ["C", 8], ["D", 21], ["E", 46], ["F", 28], ["G", 16], ["H", 22], ["I", 45], ["J", 12], ["K", 11], ["L", 23], ["M", 29], ["N", 14]]);
strategy.freezePanes.freezeRows(18);
strategy.freezePanes.freezeColumns(3);

baseSheet(changes, "O", "Change Tracker", `Previous ${manifest.previous_snapshot_date} → current ${manifest.snapshot_date}`);
const changeHeaders = ["cell_id", "asset", "previous snapshot date", "current snapshot date", "previous score", "current score", "score delta", "previous thesis", "current thesis", "previous driver", "current driver", "what changed", "why it changed", "new evidence", "change status"];
changes.getRange("A4:O4").values = [changeHeaders];
header(changes.getRange("A4:O4"));
changes.getRange("A5:O14").values = scores.map((row) => [
  row.cell_id,
  row.segment,
  d(row.previous_snapshot_date),
  d(row.snapshot_date),
  Number(row.previous_score_numeric),
  null,
  null,
  row.previous_thesis,
  row.current_thesis,
  row.previous_driver,
  row.current_driver,
  row.what_changed,
  row.why_changed,
  row.new_evidence,
  row.change_status,
]);
body(changes.getRange("A5:O14"), 88);
scores.forEach((_, index) => {
  const row = index + 5;
  changes.getRange(`F${row}`).formulas = [[`='Strategy_Sentiment_Map'!C${index + 19}`]];
  changes.getRange(`G${row}`).formulas = [[`=F${row}-E${row}`]];
});
changes.getRange("C5:D14").format.numberFormat = "yyyy-mm-dd";
changes.getRange("E5:G14").format.numberFormat = "0;[Red]-0;-";
addScoreFormatting(changes.getRange("F5:F14"));
addDeltaFormatting(changes.getRange("G5:G14"));
changes.getRange("O5:O14").conditionalFormats.add("containsText", { text: "CARRY_FORWARD", format: { fill: COLORS.warning, font: { bold: true } } });
widths(changes, [["A", 30], ["B", 31], ["C", 15], ["D", 15], ["E", 11], ["F", 11], ["G", 10], ["H", 42], ["I", 42], ["J", 27], ["K", 27], ["L", 34], ["M", 40], ["N", 42], ["O", 24]]);
changes.freezePanes.freezeRows(4);
changes.freezePanes.freezeColumns(2);

baseSheet(scenarioSheet, "N", "Scenarios", "Canonical Base, Upside / Risk-on and Downside / Risk-off dataset");
const scenarioHeaders = ["scenario name", "current status", "short narrative", "causal chain", "macro drivers", "winners", "vulnerable assets", "indicators to watch", "trigger", "veto / invalidation", "affected MAE cells", "expected reaction by affected cell", "last review date", "snapshot date"];
scenarioSheet.getRange("A4:N4").values = [scenarioHeaders];
header(scenarioSheet.getRange("A4:N4"));
scenarioSheet.getRange("A5:N7").values = scenarios.map((row) => [
  row.scenario_name,
  row.current_status,
  row.narrative,
  row.causal_chain,
  row.macro_drivers,
  row.winners,
  row.vulnerable_assets,
  row.indicators_to_watch,
  row.trigger,
  row.veto,
  row.affected_cells,
  row.expected_reaction_by_cell,
  d(row.last_review_date),
  d(row.snapshot_date),
]);
body(scenarioSheet.getRange("A5:N7"), 135);
scenarioSheet.getRange("A5:A7").format = { font: { bold: true, color: COLORS.ink, size: 11 }, horizontalAlignment: "center", verticalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.line } };
scenarioSheet.getRange("A5").format.fill = COLORS.accent;
scenarioSheet.getRange("A6").format.fill = COLORS.positive;
scenarioSheet.getRange("A7").format.fill = COLORS.negative;
scenarioSheet.getRange("M5:N7").format.numberFormat = "yyyy-mm-dd";
widths(scenarioSheet, [["A", 20], ["B", 16], ["C", 42], ["D", 52], ["E", 32], ["F", 34], ["G", 34], ["H", 38], ["I", 42], ["J", 42], ["K", 46], ["L", 58], ["M", 14], ["N", 14]]);
scenarioSheet.freezePanes.freezeRows(4);

baseSheet(evidenceSheet, "S", "Evidence & Expectation Check", `PASS evidence register • coverage ${manifest.evidence_coverage}`);
const evidenceHeaders = ["source_id", "cell_id", "asset", "provider", "title", "publication_date", "url", "source class", "excerpt", "relevance reason", "research view", "related indicator", "actual value", "expected or reference value", "market confirmation", "conflict", "evidence status", "review status", "carry-forward flag"];
evidenceSheet.getRange("A4:S4").values = [evidenceHeaders];
header(evidenceSheet.getRange("A4:S4"));
const evidenceEnd = evidence.length + 4;
evidenceSheet.getRange(`A5:S${evidenceEnd}`).values = evidence.map((row) => [
  row.source_id,
  row.cell_id,
  row.asset,
  row.provider,
  row.title,
  d(row.publication_date),
  row.URL,
  row.source_class,
  row.excerpt,
  row.relevance_reason,
  row.research_view,
  row.related_indicator,
  row.actual_value,
  row.expected_or_reference_value,
  row.market_confirmation,
  row.conflict,
  row.evidence_status,
  row.review_status,
  row.carry_forward_flag,
]);
body(evidenceSheet.getRange(`A5:S${evidenceEnd}`), 78);
evidenceSheet.getRange(`F5:F${evidenceEnd}`).format.numberFormat = "yyyy-mm-dd";
evidenceSheet.getRange(`R5:R${evidenceEnd}`).conditionalFormats.add("containsText", { text: "PASS", format: { fill: COLORS.positive, font: { bold: true } } });
evidenceSheet.getRange(`R5:R${evidenceEnd}`).conditionalFormats.add("containsText", { text: "MANUAL_REVIEW", format: { fill: COLORS.warning, font: { bold: true } } });
evidenceSheet.getRange(`R5:R${evidenceEnd}`).conditionalFormats.add("containsText", { text: "REJECTED", format: { fill: COLORS.negative, font: { bold: true, color: COLORS.white } } });
widths(evidenceSheet, [["A", 24], ["B", 30], ["C", 30], ["D", 23], ["E", 48], ["F", 14], ["G", 60], ["H", 22], ["I", 50], ["J", 42], ["K", 46], ["L", 28], ["M", 45], ["N", 34], ["O", 45], ["P", 38], ["Q", 18], ["R", 18], ["S", 18]]);
evidenceSheet.freezePanes.freezeRows(4);
evidenceSheet.freezePanes.freezeColumns(3);

baseSheet(transmission, "M", "Transmission & Control Layer", "Macro driver → transmission mechanism → asset → expected market reaction");
const transmissionHeaders = ["cell_id", "macro driver", "transmission mechanism", "asset", "expected direction", "investment logic", "trigger", "veto", "primary risk", "next review date", "scenario connection", "evidence status", "analyst action"];
transmission.getRange("A4:M4").values = [transmissionHeaders];
header(transmission.getRange("A4:M4"));
transmission.getRange("A5:M14").values = scores.map((row) => {
  const linked = scenarios.filter((scenario) => String(row.affected_scenario).includes(String(scenario.scenario_name)));
  const selected = linked.length ? linked : [scenarios[0]];
  const macroDriver = String(row.current_driver || "").split(";")[0];
  return [
    row.cell_id,
    macroDriver,
    row.transmission_mechanism,
    row.segment,
    row.view,
    `${macroDriver} → ${row.transmission_mechanism} → ${row.segment} → ${row.view}`,
    selected.map((scenario) => scenario.trigger).join(" | "),
    selected.map((scenario) => scenario.veto).join(" | "),
    row.primary_risk,
    d(row.next_review_date),
    row.affected_scenario,
    row.evidence_status_code,
    row.analyst_action,
  ];
});
body(transmission.getRange("A5:M14"), 94);
transmission.getRange("J5:J14").format.numberFormat = "yyyy-mm-dd";
widths(transmission, [["A", 31], ["B", 28], ["C", 45], ["D", 32], ["E", 23], ["F", 58], ["G", 48], ["H", 48], ["I", 36], ["J", 14], ["K", 30], ["L", 18], ["M", 38]]);
transmission.freezePanes.freezeRows(4);
transmission.freezePanes.freezeColumns(2);

await fs.mkdir(path.dirname(args.output), { recursive: true });
await fs.rm(args["preview-dir"], { recursive: true, force: true });
await fs.mkdir(args["preview-dir"], { recursive: true });
const sheetNames = ["Strategy_Sentiment_Map", "Change_Tracker", "Scenarios", "Evidence_Expectation", "Transmission_Control"];
for (const sheetName of sheetNames) {
  const preview = await workbook.render({ sheetName, autoCrop: "all", scale: 0.72, format: "png" });
  await fs.writeFile(path.join(args["preview-dir"], `${sheetName}.png`), new Uint8Array(await preview.arrayBuffer()));
}

const keyRange = await workbook.inspect({
  kind: "table",
  range: "Strategy_Sentiment_Map!A1:N28",
  include: "values,formulas",
  tableMaxRows: 28,
  tableMaxCols: 14,
  maxChars: 4500,
});
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(args.output);
console.log(JSON.stringify({
  output: args.output,
  sheets: sheetNames,
  scoreRows: scores.length,
  evidenceRows: evidence.length,
  scenarioRows: scenarios.length,
  previews: sheetNames.map((name) => path.join(args["preview-dir"], `${name}.png`)),
  keyRange: keyRange.ndjson,
  formulaErrors: errors.ndjson,
}, null, 2));
