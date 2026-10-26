"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { test } = require("node:test");

const extensionRoot = path.resolve(__dirname, "..");
const staticRoot = path.resolve(extensionRoot, "..", "src", "aiworkhub", "dashboard_static");
const read = (...parts) => fs.readFileSync(path.join(...parts), "utf8");

const SURFACES = [
  {
    name: "VS Code webview",
    html: read(extensionRoot, "extension.js"),
    script: read(extensionRoot, "media", "app.js"),
    css: read(extensionRoot, "media", "app.css"),
    activityWidth: "96px",
    signalWidth: "86px",
  },
  {
    name: "browser dashboard",
    html: read(staticRoot, "index.html"),
    script: read(staticRoot, "dashboard.js"),
    css: read(staticRoot, "dashboard.css"),
    activityWidth: "105px",
    signalWidth: "92px",
  },
];

const COLUMNS = ["Status", "Task", "Topic", "Runner", "Model", "Effort", "Activity", "Signal"];
const EFFORT_INDEX = COLUMNS.indexOf("Effort");
const EM_DASH = "—";
const NO_EFFORT_TITLE = "No reasoning effort recorded (editor bridge or older run)";

const BASE_TASK = {
  task_id: "T-1",
  status: "processing",
  topic: "dashboard-effort-column",
  runner: "claude_sonnet-5",
  model: "claude-sonnet-5",
  updated_at: "2026-07-21T00:00:00+00:00",
};

const EFFORT_CASES = [
  {
    name: "applied effort shows the key with profile and status",
    fields: { effort: "max", effort_profile: "maximum", effort_status: "applied" },
    text: "max",
    className: "cell-primary",
    title: "profile maximum, status applied",
  },
  {
    name: "not-applied effort shows default with the real profile and status",
    fields: { effort: "default", effort_profile: "maximum", effort_status: "capability_ceiling" },
    text: "default",
    className: "cell-primary",
    title: "profile maximum, status capability_ceiling",
  },
  {
    name: "effort without detail falls back to its own name as the tooltip",
    fields: { effort: "high" },
    text: "high",
    className: "cell-primary",
    title: "high",
  },
  {
    name: "a lone profile is the whole tooltip detail",
    fields: { effort: "high", effort_profile: "balanced" },
    text: "high",
    className: "cell-primary",
    title: "profile balanced",
  },
  {
    name: "padded effort is trimmed",
    fields: { effort: "  xhigh  ", effort_status: "applied" },
    text: "xhigh",
    className: "cell-primary",
    title: "status applied",
  },
  {
    name: "no effort keys render an em dash",
    fields: {},
    text: EM_DASH,
    className: "cell-secondary",
    title: NO_EFFORT_TITLE,
  },
  {
    name: "null effort renders an em dash",
    fields: { effort: null, effort_profile: "maximum" },
    text: EM_DASH,
    className: "cell-secondary",
    title: NO_EFFORT_TITLE,
  },
  {
    name: "blank effort renders an em dash",
    fields: { effort: "   " },
    text: EM_DASH,
    className: "cell-secondary",
    title: NO_EFFORT_TITLE,
  },
  {
    name: "numeric effort renders an em dash",
    fields: { effort: 5 },
    text: EM_DASH,
    className: "cell-secondary",
    title: NO_EFFORT_TITLE,
  },
  {
    name: "object effort renders an em dash",
    fields: { effort: { key: "max" } },
    text: EM_DASH,
    className: "cell-secondary",
    title: NO_EFFORT_TITLE,
  },
  {
    name: "profile and status without an effort never invent one",
    fields: { effort_profile: "maximum", effort_status: "applied" },
    text: EM_DASH,
    className: "cell-secondary",
    title: NO_EFFORT_TITLE,
  },
  {
    name: "markup in the values stays inert text",
    fields: {
      effort: "<img src=x onerror=alert(1)>",
      effort_profile: "<b>p</b>",
      effort_status: "<i>s</i>",
    },
    text: "<img src=x onerror=alert(1)>",
    className: "cell-primary",
    title: "profile <b>p</b>, status <i>s</i>",
  },
];

function columnLabels(html) {
  const tableStart = html.indexOf("<table");
  const headStart = html.indexOf("<thead", tableStart);
  const headEnd = html.indexOf("</thead>", headStart);
  assert.ok(tableStart !== -1 && headStart !== -1 && headEnd !== -1, "the task table must declare a thead");
  const head = html.slice(headStart, headEnd);
  return Array.from(head.matchAll(/<th\b[^>]*>([^<]*)<\/th>/g), (match) => match[1].trim());
}

function widthPins(css) {
  const pins = new Map();
  for (const match of css.matchAll(/th:nth-child\((\d+)\)\s*\{\s*width:\s*([^;}\s]+)\s*;?\s*\}/g)) {
    pins.set(Number(match[1]), match[2]);
  }
  return pins;
}

function extractFunction(source, name) {
  const start = source.search(new RegExp(`^function ${name}\\(`, "m"));
  assert.ok(start !== -1, `${name} must be declared at top level`);
  const open = source.indexOf("{", source.indexOf(")", start));
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") {
      depth += 1;
    } else if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) {
        return source.slice(start, index + 1);
      }
    }
  }
  return assert.fail(`${name} body must be brace-balanced`);
}

function makeElement(tag) {
  const element = {
    tagName: String(tag).toUpperCase(),
    className: "",
    textContent: "",
    title: "",
    hidden: false,
    dataset: {},
    childNodes: [],
    classList: {
      add(name) {
        element.className = element.className ? `${element.className} ${name}` : name;
      },
    },
    appendChild(child) {
      element.childNodes.push(child);
      return child;
    },
    replaceChildren(...children) {
      element.childNodes = children.flatMap((child) => (child.isFragment ? child.childNodes : [child]));
    },
  };
  Object.defineProperty(element, "innerHTML", {
    get() {
      return "";
    },
    set() {
      throw new Error("innerHTML must never be assigned while rendering the task table");
    },
  });
  return element;
}

function renderRows(script, tasks) {
  const tableBody = makeElement("tbody");
  const sandbox = {
    document: {
      createElement: makeElement,
      createDocumentFragment: () => Object.assign(makeElement("#fragment"), { isFragment: true }),
    },
    state: { selectedTaskId: null },
    elements: {
      tableBody,
      tableLoading: makeElement("div"),
      tableEmpty: makeElement("div"),
      filteredCount: makeElement("span"),
    },
    filteredTasks: () => tasks,
    statusBadge: () => makeElement("span"),
    canonicalStatus: (task) => task.status,
    taskSummaryLine: () => "",
    formatRelativeTime: (value) => String(value || "Unknown"),
    validationClass: () => "",
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `${extractFunction(script, "createElement")}\n${extractFunction(script, "renderTaskTable")}`,
    sandbox,
  );
  sandbox.renderTaskTable();
  return tableBody.childNodes;
}

function effortSpan(row) {
  const cell = row.childNodes[EFFORT_INDEX];
  assert.strictEqual(cell.tagName, "TD", "the effort slot must be a table cell");
  assert.strictEqual(cell.childNodes.length, 1, "the effort cell holds exactly one element");
  return cell.childNodes[0];
}

for (const surface of SURFACES) {
  test(`${surface.name}: header lists Effort right after Model`, () => {
    assert.deepStrictEqual(columnLabels(surface.html), COLUMNS);
  });

  test(`${surface.name}: width pins cover every column and keep Activity and Signal sizes`, () => {
    const pins = widthPins(surface.css);
    assert.deepStrictEqual(
      Array.from(pins.keys()).sort((left, right) => left - right),
      COLUMNS.map((_, index) => index + 1),
    );
    assert.match(pins.get(EFFORT_INDEX + 1), /^\d+px$/, "Effort gets a fixed pixel width");
    const effortWidth = Number.parseInt(pins.get(EFFORT_INDEX + 1), 10);
    assert.ok(effortWidth >= 56 && effortWidth <= 120, `Effort width ${effortWidth}px must fit short labels`);
    assert.strictEqual(pins.get(EFFORT_INDEX + 2), surface.activityWidth);
    assert.strictEqual(pins.get(EFFORT_INDEX + 3), surface.signalWidth);
  });

  test(`${surface.name}: each row has one cell per header column with Effort after Model`, () => {
    const [row] = renderRows(surface.script, [
      { ...BASE_TASK, effort: "max", effort_profile: "maximum", effort_status: "applied" },
    ]);
    assert.strictEqual(row.childNodes.length, columnLabels(surface.html).length);
    assert.strictEqual(row.childNodes[COLUMNS.indexOf("Model")].childNodes[0].textContent, "claude-sonnet-5");
    assert.strictEqual(effortSpan(row).textContent, "max");
    assert.strictEqual(row.childNodes[COLUMNS.indexOf("Activity")].className, "activity-cell");
  });

  test(`${surface.name}: every row keeps its own effort cell`, () => {
    const rows = renderRows(surface.script, [
      { ...BASE_TASK, task_id: "T-1", effort: "max" },
      { ...BASE_TASK, task_id: "T-2" },
    ]);
    assert.deepStrictEqual(rows.map((row) => effortSpan(row).textContent), ["max", EM_DASH]);
  });

  for (const effortCase of EFFORT_CASES) {
    test(`${surface.name}: ${effortCase.name}`, () => {
      const [row] = renderRows(surface.script, [{ ...BASE_TASK, ...effortCase.fields }]);
      const span = effortSpan(row);
      assert.strictEqual(span.textContent, effortCase.text);
      assert.strictEqual(span.className, effortCase.className);
      assert.strictEqual(span.title, effortCase.title);
    });
  }
}
