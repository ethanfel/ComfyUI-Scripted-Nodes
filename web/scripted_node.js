import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "ComfyScriptedNode";
const SCRIPT_BROWSER_CLASS = "ComfyScriptBrowserNode";
const SAVE_SCRIPT_CLASS = "ComfySaveScriptNode";
const NODE_PACK_TESTER_CLASS = "ComfyNodePackTesterNode";
const SCHEMA_ROUTE = "/scripted_nodes/schema";
const NODE_PACK_TEST_ROUTE = "/scripted_nodes/node-packs/test";
const SCRIPT_ROUTES = Object.freeze({
    list: "/scripted_nodes/scripts",
    load: "/scripted_nodes/scripts/load",
    save: "/scripted_nodes/scripts",
    delete: "/scripted_nodes/scripts",
});
const SCHEMA_PROPERTY = "scripted_node_schema";
const SCHEMA_VERSION_PROPERTY = "scripted_node_schema_version";
const SCHEMA_VERSION = 1;
const NODE_PACK_REPORT_PROPERTY = "node_pack_compatibility_report";
const NODE_PACK_REPORT_JSON_PROPERTY = "node_pack_compatibility_json";
const NODE_PACK_SOURCE_PROPERTY = "node_pack_compatibility_source";
const MAX_OUTPUTS = 32;
const MIN_NODE_WIDTH = 460;
const MIN_EDITOR_HEIGHT = 220;
const EMPTY_SCRIPT_ID = "";
const RESERVED_INPUT_NAMES = new Set(["code"]);
const scriptBrowserNodes = new Set();

function widgetByName(node, name) {
    return node.widgets?.find((widget) => widget.name === name);
}

function toast(severity, detail) {
    const message = String(detail || "Unknown error");
    const logger = severity === "error" ? console.error : console.info;
    logger(`[Scripted Node] ${message}`);

    try {
        app.extensionManager?.toast?.add?.({
            severity,
            summary: "Scripted Node",
            detail: message,
            life: severity === "error" ? 7000 : 3000,
        });
    } catch (error) {
        console.warn("[Scripted Node] Could not show toast", error);
    }
}

function injectStyles() {
    if (document.getElementById("comfy-scripted-node-styles")) return;

    const style = document.createElement("style");
    style.id = "comfy-scripted-node-styles";
    style.textContent = `
        .comfy-scripted-node-editor {
            min-height: ${MIN_EDITOR_HEIGHT}px !important;
            box-sizing: border-box;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas,
                         "Liberation Mono", "Courier New", monospace !important;
            font-size: 12px !important;
            line-height: 1.4 !important;
            tab-size: 4;
            white-space: pre;
            overflow: auto;
            resize: vertical;
        }
        .comfy-node-pack-report {
            width: 100%;
            min-height: 240px;
            box-sizing: border-box;
            padding: 10px;
            border: 1px solid #555;
            border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
            border-radius: 6px;
            background: #111;
            background: color-mix(in srgb, #111 90%, transparent);
            color: #f2f2f2;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas,
                         "Liberation Mono", "Courier New", monospace;
            font-size: 12px;
            line-height: 1.4;
            white-space: pre;
            overflow: auto;
            resize: vertical;
        }
    `;
    document.head.appendChild(style);
}

function widgetTextarea(widget) {
    const candidates = [
        widget?.inputEl,
        widget?.element,
        widget?.options?.element,
    ];

    for (const candidate of candidates) {
        if (!candidate) continue;
        if (candidate.tagName === "TEXTAREA") return candidate;
        const textarea = candidate.querySelector?.("textarea");
        if (textarea) return textarea;
    }
    return null;
}

function stylePythonWidget(
    node,
    {
        widgetName,
        label,
        title,
        datasetKey,
        computeFlag,
        onSubmit,
    },
) {
    const widget = widgetByName(node, widgetName);
    if (!widget) return;

    widget.label = label;
    const textarea = widgetTextarea(widget);
    if (textarea && !textarea.dataset[datasetKey]) {
        textarea.dataset[datasetKey] = "true";
        textarea.classList.add("comfy-scripted-node-editor");
        textarea.spellcheck = false;
        textarea.wrap = "off";
        textarea.title = title;
        textarea.addEventListener("keydown", (event) => {
            if (
                onSubmit &&
                (event.ctrlKey || event.metaKey) &&
                event.key === "Enter"
            ) {
                event.preventDefault();
                event.stopPropagation();
                void onSubmit(node);
                return;
            }
            // Keep LiteGraph and global ComfyUI shortcuts from firing while
            // editing Python.
            event.stopPropagation();
        });
    }

    if (!widget[computeFlag]) {
        const originalComputeSize = widget.computeSize?.bind(widget);
        widget.computeSize = function () {
            const size = originalComputeSize?.(...arguments);
            const width = Array.isArray(size) && Number.isFinite(size[0])
                ? size[0]
                : MIN_NODE_WIDTH;
            const height = Array.isArray(size) && Number.isFinite(size[1])
                ? size[1]
                : 0;
            return [width, Math.max(height, MIN_EDITOR_HEIGHT)];
        };
        widget[computeFlag] = true;
    }
}

function styleCodeWidget(node) {
    stylePythonWidget(node, {
        widgetName: "code",
        label: "Python Script",
        title: "Edit trusted Python. Press Ctrl/Cmd+Enter to apply sockets.",
        datasetKey: "scriptedNodeEditor",
        computeFlag: "_scriptedNodeComputeSize",
        onSubmit: applyScript,
    });
}

function styleSaveCodeWidget(node) {
    stylePythonWidget(node, {
        widgetName: "code",
        label: "Python Script",
        title: "Edit trusted Python. Press Ctrl/Cmd+Enter to save now.",
        datasetKey: "scriptedNodeSaveEditor",
        computeFlag: "_scriptedNodeSaveComputeSize",
        onSubmit: saveScriptNow,
    });
}

function hideSchemaWidget(node) {
    const widget = widgetByName(node, "schema_json");
    if (!widget) return;

    // Keep this as a real backend STRING widget so its widgets_values position
    // remains stable. Only its presentation is hidden.
    widget.hidden = true;
    widget.options ??= {};
    widget.options.hidden = true;
    widget.computeSize = () => [0, -4];

    const element =
        widget.inputEl || widget.element || widget.options?.element;
    if (element?.style) element.style.display = "none";
}

function normalizeName(value, context) {
    if (typeof value !== "string" || !value) {
        throw new Error(`${context} has an invalid name`);
    }
    return value;
}

function normalizeType(value, context) {
    if (typeof value !== "string" || !value.trim()) {
        throw new Error(`${context} has an invalid socket type`);
    }
    return value.trim();
}

function normalizeSchema(value) {
    let source = value;
    if (typeof source === "string") {
        source = JSON.parse(source);
    }
    if (!source || typeof source !== "object" || Array.isArray(source)) {
        throw new Error("The schema response is not an object");
    }
    if (!Array.isArray(source.inputs) || !Array.isArray(source.outputs)) {
        throw new Error("The schema response must contain inputs and outputs arrays");
    }
    if (source.outputs.length > MAX_OUTPUTS) {
        throw new Error(`A Scripted Node supports at most ${MAX_OUTPUTS} outputs`);
    }

    const inputNames = new Set();
    const outputNames = new Set();
    const inputs = source.inputs.map((item, index) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) {
            throw new Error(`Input ${index + 1} is invalid`);
        }
        const name = normalizeName(item.name, `Input ${index + 1}`);
        const type = normalizeType(item.type, `Input "${name}"`);
        if (inputNames.has(name)) {
            throw new Error(`Input name "${name}" is declared more than once`);
        }
        inputNames.add(name);
        const options =
            item.options && typeof item.options === "object" && !Array.isArray(item.options)
                ? structuredCloneSafe(item.options)
                : {};
        return { name, type, options };
    });
    const outputs = source.outputs.map((item, index) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) {
            throw new Error(`Output ${index + 1} is invalid`);
        }
        const name = normalizeName(item.name, `Output ${index + 1}`);
        const type = normalizeType(item.type, `Output "${name}"`);
        if (outputNames.has(name)) {
            throw new Error(`Output name "${name}" is declared more than once`);
        }
        outputNames.add(name);
        return { name, type };
    });

    return { inputs, outputs };
}

function structuredCloneSafe(value) {
    if (typeof structuredClone === "function") {
        try {
            return structuredClone(value);
        } catch (error) {
            // JSON is the backend contract, so the fallback below is sufficient.
        }
    }
    return JSON.parse(JSON.stringify(value));
}

function linkById(graph, linkId) {
    const links = graph?.links;
    if (!links) return null;
    if (links instanceof Map) return links.get(linkId) ?? null;
    return links[linkId] ?? null;
}

function removeGraphLink(graph, linkId) {
    if (linkId == null || !graph) return;
    try {
        graph.removeLink(linkId);
    } catch (error) {
        console.warn(`[Scripted Node] Could not remove link ${linkId}`, error);
    }
}

function slotKey(name, type) {
    return `${name}\u0000${type}`;
}

function takeReusableSlot(slotsByKey, name, type) {
    const candidates = slotsByKey.get(slotKey(name, type));
    if (!candidates?.length) return null;
    const slot = candidates.shift();
    if (!candidates.length) slotsByKey.delete(slotKey(name, type));
    return slot;
}

function takeSlotByIdentity(slotsByKey, slot) {
    if (!slot) return null;
    const key = slotKey(String(slot.name ?? ""), String(slot.type ?? ""));
    const candidates = slotsByKey.get(key);
    if (!candidates?.length) return null;
    const index = candidates.indexOf(slot);
    if (index < 0) return null;
    candidates.splice(index, 1);
    if (!candidates.length) slotsByKey.delete(key);
    return slot;
}

function groupSlotsByNameAndType(slots) {
    const grouped = new Map();
    for (const slot of slots || []) {
        const key = slotKey(String(slot?.name ?? ""), String(slot?.type ?? ""));
        const entries = grouped.get(key);
        if (entries) entries.push(slot);
        else grouped.set(key, [slot]);
    }
    return grouped;
}

function remainingSlots(grouped) {
    return Array.from(grouped.values()).flat();
}

function reconcileInputs(node, inputSpecs) {
    // A widget-backed `code` input must survive schema application so a
    // Script Browser -> Scripted Node link remains intact while the script's
    // own dynamic sockets are reconciled.
    const preservedInputs = (node.inputs || []).filter((slot) =>
        RESERVED_INPUT_NAMES.has(String(slot?.name ?? "")),
    );
    const dynamicInputs = (node.inputs || []).filter(
        (slot) => !RESERVED_INPUT_NAMES.has(String(slot?.name ?? "")),
    );
    const oldSlots = groupSlotsByNameAndType(dynamicInputs);
    const nextInputs = [...preservedInputs, ...inputSpecs.map((spec) => {
        const slot = takeReusableSlot(oldSlots, spec.name, spec.type) || {
            name: spec.name,
            type: spec.type,
            link: null,
        };
        slot.name = spec.name;
        slot.label = spec.name;
        slot.type = spec.type;
        slot.scripted_options = structuredCloneSafe(spec.options);
        return slot;
    })];

    for (const slot of remainingSlots(oldSlots)) {
        removeGraphLink(node.graph, slot?.link);
    }

    node.inputs = nextInputs;
    for (let index = 0; index < nextInputs.length; index += 1) {
        const slot = nextInputs[index];
        const link = linkById(node.graph, slot.link);
        if (link) {
            link.target_slot = index;
            link.type = slot.type;
        }
    }
}

function reconcileOutputs(
    node,
    outputSpecs,
    { reuseBackendPlaceholders = false } = {},
) {
    const oldSlots = groupSlotsByNameAndType(node.outputs || []);
    const currentOutputs = node.outputs || [];
    const nextOutputs = outputSpecs.map((spec, index) => {
        let slot = null;
        if (reuseBackendPlaceholders) {
            // ComfyUI's generated configure() replaces saved dynamic output
            // metadata with the backend output_N/* definition by index, while
            // leaving the saved link IDs on those slots. Reclaim that exact
            // slot during restoration so its links survive the rename.
            const placeholder = currentOutputs[index];
            if (
                placeholder?.name === `output_${index + 1}` &&
                placeholder?.type === "*"
            ) {
                slot = takeSlotByIdentity(oldSlots, placeholder);
            }
        }
        slot ||= takeReusableSlot(oldSlots, spec.name, spec.type);
        slot ||= {
            name: spec.name,
            type: spec.type,
            links: null,
        };
        slot.name = spec.name;
        slot.label = spec.name;
        slot.type = spec.type;
        return slot;
    });

    for (const slot of remainingSlots(oldSlots)) {
        for (const linkId of [...(slot?.links || [])]) {
            removeGraphLink(node.graph, linkId);
        }
    }

    node.outputs = nextOutputs;
    for (let index = 0; index < nextOutputs.length; index += 1) {
        const slot = nextOutputs[index];
        for (const linkId of slot.links || []) {
            const link = linkById(node.graph, linkId);
            if (link) {
                link.origin_slot = index;
                link.type = slot.type;
            }
        }
    }
}

function resizeAndRedraw(node, shrinkToFit = false) {
    const computed = node.computeSize?.() || node.size || [MIN_NODE_WIDTH, 0];
    const width = Math.max(
        MIN_NODE_WIDTH,
        Number(node.size?.[0]) || 0,
        Number(computed[0]) || 0,
    );
    const height = shrinkToFit
        ? Math.max(Number(computed[1]) || 0, MIN_EDITOR_HEIGHT + 80)
        : Math.max(Number(node.size?.[1]) || 0, Number(computed[1]) || 0);
    node.setSize?.([width, height]);
    node.setDirtyCanvas?.(true, true);
    node.graph?.setDirtyCanvas?.(true, true);
    app.graph?.setDirtyCanvas?.(true, true);
}

function reconcileSchema(
    node,
    schema,
    {
        shrinkToFit = false,
        reuseBackendOutputPlaceholders = false,
    } = {},
) {
    reconcileInputs(node, schema.inputs);
    reconcileOutputs(node, schema.outputs, {
        reuseBackendPlaceholders: reuseBackendOutputPlaceholders,
    });
    resizeAndRedraw(node, shrinkToFit);
}

function persistSchema(node, schema, schemaJson) {
    const canonical =
        typeof schemaJson === "string" && schemaJson
            ? schemaJson
            : JSON.stringify(schema);
    const schemaWidget = widgetByName(node, "schema_json");
    if (schemaWidget) schemaWidget.value = canonical;

    node.properties ??= {};
    node.properties[SCHEMA_PROPERTY] = structuredCloneSafe(schema);
    node.properties[SCHEMA_VERSION_PROPERTY] = SCHEMA_VERSION;
    node._scriptedNodeSchema = structuredCloneSafe(schema);
    node._scriptedNodeSchemaJson = canonical;
}

function schemaCandidates(node, info) {
    const schemaWidget = widgetByName(node, "schema_json");
    return [
        schemaWidget?.value,
        info?.properties?.[SCHEMA_PROPERTY],
        node.properties?.[SCHEMA_PROPERTY],
        node._scriptedNodeSchema,
    ].filter((candidate) => candidate !== undefined && candidate !== null && candidate !== "");
}

function restoreSchema(node, info) {
    for (const candidate of schemaCandidates(node, info)) {
        try {
            const schema = normalizeSchema(candidate);
            const schemaJson =
                typeof candidate === "string" && candidate
                    ? candidate
                    : JSON.stringify(schema);
            reconcileSchema(node, schema, {
                reuseBackendOutputPlaceholders: true,
            });
            persistSchema(node, schema, schemaJson);
            return true;
        } catch (error) {
            console.warn("[Scripted Node] Ignoring invalid saved schema candidate", error);
        }
    }
    return false;
}

function setApplyState(node, state) {
    const widget = node._scriptedNodeApplyWidget;
    if (!widget) return;

    const labels = {
        idle: "Apply Script",
        applying: "Applying…",
        success: "Applied ✓",
        error: "Apply failed",
    };
    widget.name = labels[state] || labels.idle;
    widget.label = widget.name;
    node.setDirtyCanvas?.(true, true);

    if (node._scriptedNodeApplyReset) {
        clearTimeout(node._scriptedNodeApplyReset);
        node._scriptedNodeApplyReset = null;
    }
    if (state === "success" || state === "error") {
        node._scriptedNodeApplyReset = setTimeout(() => {
            setApplyState(node, "idle");
        }, state === "success" ? 1800 : 3000);
    }
}

async function applyScript(
    node,
    { silentSuccess = false, shrinkToFit = false } = {},
) {
    if (node._scriptedNodeApplying) return false;

    const codeWidget = widgetByName(node, "code");
    if (!codeWidget) {
        toast("error", "The backend `code` widget is missing");
        return false;
    }

    node._scriptedNodeApplying = true;
    setApplyState(node, "applying");
    try {
        const response = await api.fetchApi(SCHEMA_ROUTE, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code: String(codeWidget.value ?? "") }),
        });

        let payload;
        try {
            payload = await response.json();
        } catch (error) {
            throw new Error(
                response.statusText ||
                `Schema endpoint returned an unreadable response (${response.status})`,
            );
        }
        if (response.ok === false || payload?.ok === false) {
            throw new Error(payload?.error || `Schema request failed (${response.status})`);
        }
        if (!payload?.schema) {
            throw new Error("Schema endpoint did not return a schema");
        }

        const schema = normalizeSchema(payload.schema);
        reconcileSchema(node, schema, { shrinkToFit });
        persistSchema(node, schema, payload.schema_json);
        if (silentSuccess) {
            setApplyState(node, "idle");
        } else {
            setApplyState(node, "success");
            toast(
                "success",
                `Applied ${schema.inputs.length} input${schema.inputs.length === 1 ? "" : "s"} ` +
                `and ${schema.outputs.length} output${schema.outputs.length === 1 ? "" : "s"}`,
            );
        }
        return true;
    } catch (error) {
        setApplyState(node, "error");
        toast("error", error instanceof Error ? error.message : String(error));
        return false;
    } finally {
        node._scriptedNodeApplying = false;
    }
}

function addApplyButton(node) {
    if (node._scriptedNodeApplyWidget) return;
    const widget = node.addWidget("button", "Apply Script", null, () => {
        void applyScript(node);
    });
    widget.serialize = false;
    widget.options ??= {};
    widget.options.serialize = false;
    node._scriptedNodeApplyWidget = widget;
}

function updateScriptInputButton(node) {
    const widget = node?._scriptedNodeInputWidget;
    if (!widget) return;
    const enabled = (node.inputs || []).some((input) => input?.name === "code");
    setButtonLabel(
        widget,
        enabled ? "Script Input Ready ✓" : "Enable Script Input",
    );
}

function addScriptInputButton(node) {
    if (node._scriptedNodeInputWidget) return;
    const widget = node.addWidget("button", "Enable Script Input", null, () => {
        if ((node.inputs || []).some((input) => input?.name === "code")) {
            toast("info", "The Scripted Node already has a `code` input");
            return;
        }

        const codeWidget = widgetByName(node, "code");
        if (!codeWidget) {
            toast("error", "The backend `code` widget is missing");
            return;
        }
        if (typeof node.convertWidgetToInput !== "function") {
            toast(
                "error",
                "This ComfyUI frontend cannot convert the script editor to an input",
            );
            return;
        }

        try {
            node.convertWidgetToInput(codeWidget);
            updateScriptInputButton(node);
            resizeAndRedraw(node, true);
            toast(
                "success",
                "Script input enabled; connect a Script Browser output to `code`",
            );
        } catch (error) {
            toast("error", error instanceof Error ? error.message : String(error));
        }
    });
    widget.serialize = false;
    widget.options ??= {};
    widget.options.serialize = false;
    node._scriptedNodeInputWidget = widget;
    updateScriptInputButton(node);
}

function setupNode(node) {
    if (node._scriptedNodeFrontendReady) return;
    node._scriptedNodeFrontendReady = true;
    node._scriptedNodeWasConfigured = false;

    injectStyles();
    styleCodeWidget(node);
    hideSchemaWidget(node);
    addApplyButton(node);
    addScriptInputButton(node);

    // New nodes initially inherit the backend's 32 wildcard return slots.
    // Loaded nodes call onConfigure before this microtask and restore their
    // serialized active schema instead. A genuinely new node is first collapsed
    // and then its backend-provided default code is analyzed (never executed),
    // so it opens with useful sockets without requiring a first button press.
    queueMicrotask(() => {
        if (node._scriptedNodeWasConfigured) return;
        reconcileSchema(node, { inputs: [], outputs: [] }, { shrinkToFit: true });
        void applyScript(node, { silentSuccess: true, shrinkToFit: true });
    });
}

async function fetchJson(route, options = {}, context = "Request") {
    const response = await api.fetchApi(route, options);
    let payload;
    try {
        payload = await response.json();
    } catch (error) {
        throw new Error(
            response.statusText ||
            `${context} returned an unreadable response (${response.status})`,
        );
    }
    if (response.ok === false || payload?.ok === false) {
        throw new Error(payload?.error || `${context} failed (${response.status})`);
    }
    return payload;
}

function normalizeScriptEntries(payload) {
    if (!Array.isArray(payload?.scripts)) {
        throw new Error("Script list response is missing its `scripts` array");
    }

    const seen = new Set();
    return payload.scripts.map((entry, index) => {
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
            throw new Error(`Script entry ${index + 1} is invalid`);
        }
        const id = String(entry.id ?? "");
        const name = String(entry.name ?? "");
        if (!id || !name) {
            throw new Error(`Script entry ${index + 1} is missing an id or name`);
        }
        if (seen.has(id)) {
            throw new Error(`Script id "${id}" appears more than once`);
        }
        seen.add(id);
        return {
            id,
            name,
            source: String(entry.source ?? ""),
            deletable: entry.deletable === true,
        };
    });
}

function scriptEntryLabel(entry) {
    if (!entry) return "(no scripts available)";
    const source =
        entry.source === "user"
            ? "User"
            : entry.source === "bundled"
                ? "Bundled"
                : entry.source;
    return source ? `${entry.name}  [${source}]` : entry.name;
}

function setButtonLabel(widget, label) {
    if (!widget) return;
    widget.name = label;
    widget.label = label;
}

function updateBrowserButtons(node) {
    const id = String(widgetByName(node, "script_name")?.value ?? "");
    const entry = node._scriptLibraryEntries?.get(id);
    const deleteWidget = node._scriptDeleteWidget;
    const canDelete = entry?.deletable === true;
    setButtonLabel(
        deleteWidget,
        entry && !canDelete
            ? "Delete Selected (read-only)"
            : "Delete Selected",
    );
    if (deleteWidget) {
        deleteWidget.disabled = !canDelete;
        deleteWidget.options ??= {};
        deleteWidget.options.disabled = !canDelete;
    }
    node.setDirtyCanvas?.(true, true);
}

function setScriptComboEntries(node, entries, preferredId) {
    const widget = widgetByName(node, "script_name");
    if (!widget) throw new Error("The backend `script_name` widget is missing");

    const byId = new Map(entries.map((entry) => [entry.id, entry]));
    const ids = entries.map((entry) => entry.id);
    const previous = String(widget.value ?? "");
    const next =
        (preferredId && byId.has(preferredId) && preferredId) ||
        (byId.has(previous) && previous) ||
        ids[0] ||
        EMPTY_SCRIPT_ID;

    widget.options ??= {};
    // Legacy ComfyUI combo widgets do not handle an empty values array well.
    widget.options.values = ids.length ? ids : [EMPTY_SCRIPT_ID];
    widget.options.getOptionLabel = (value) =>
        scriptEntryLabel(byId.get(String(value ?? "")));
    widget.value = next;
    node._scriptLibraryEntries = byId;
    updateBrowserButtons(node);
    resizeAndRedraw(node);
}

async function refreshScriptBrowser(
    node,
    { preferredId = null, silent = false } = {},
) {
    const revision = (node._scriptLibraryRefreshRevision || 0) + 1;
    node._scriptLibraryRefreshRevision = revision;
    node._scriptLibraryRefreshing = true;
    setButtonLabel(node._scriptRefreshWidget, "Refreshing…");
    node.setDirtyCanvas?.(true, true);
    try {
        const payload = await fetchJson(
            SCRIPT_ROUTES.list,
            {},
            "Script list request",
        );
        if (revision !== node._scriptLibraryRefreshRevision) return false;
        const entries = normalizeScriptEntries(payload);
        setScriptComboEntries(node, entries, preferredId);
        if (!silent) {
            toast(
                "success",
                `Found ${entries.length} script${entries.length === 1 ? "" : "s"}`,
            );
        }
        return true;
    } catch (error) {
        if (revision !== node._scriptLibraryRefreshRevision) return false;
        if (!silent) {
            toast("error", error instanceof Error ? error.message : String(error));
        } else {
            console.warn("[Scripted Node] Could not refresh script library", error);
        }
        return false;
    } finally {
        if (revision === node._scriptLibraryRefreshRevision) {
            node._scriptLibraryRefreshing = false;
            setButtonLabel(node._scriptRefreshWidget, "Refresh Scripts");
            node.setDirtyCanvas?.(true, true);
        }
    }
}

function graphNodeById(graph, id) {
    return graph?.getNodeById?.(id) ?? graph?._nodes_by_id?.[id] ?? null;
}

function isScriptedNode(node) {
    return node?.comfyClass === NODE_CLASS || node?.type === NODE_CLASS;
}

function connectedScriptedNodes(browserNode) {
    const targets = new Set();
    const graph = browserNode.graph || app.graph;
    for (const linkId of browserNode.outputs?.[0]?.links || []) {
        const link = linkById(graph, linkId);
        if (!link) continue;
        const target = graphNodeById(graph, link.target_id);
        const targetInput = target?.inputs?.[link.target_slot];
        if (isScriptedNode(target) && targetInput?.name === "code") {
            targets.add(target);
        }
    }
    return [...targets];
}

function selectedNodes() {
    const selected = app.canvas?.selected_nodes;
    if (selected instanceof Map || selected instanceof Set) {
        return [...selected.values()];
    }
    if (selected && typeof selected === "object") {
        return Object.values(selected);
    }
    return (app.graph?._nodes || []).filter((node) => node?.is_selected);
}

function scriptedLoadTargets(browserNode) {
    const connected = connectedScriptedNodes(browserNode);
    if (connected.length) return connected;

    const selected = selectedNodes().filter(
        (node) => node !== browserNode && isScriptedNode(node),
    );
    return selected.length === 1 ? selected : [];
}

function setWidgetText(widget, value) {
    const text = String(value ?? "");
    widget.value = text;
    const textarea = widgetTextarea(widget);
    if (textarea && textarea.value !== text) {
        textarea.value = text;
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
        textarea.dispatchEvent(new Event("change", { bubbles: true }));
    }
}

function applyLoadedSchema(target, payload) {
    if (!payload.schema) return false;
    const schema = normalizeSchema(payload.schema);
    reconcileSchema(target, schema, { shrinkToFit: true });
    persistSchema(target, schema, payload.schema_json);
    setApplyState(target, "success");
    return true;
}

async function loadSelectedScript(node) {
    const id = String(widgetByName(node, "script_name")?.value ?? "");
    if (!id) {
        toast("error", "No script is selected");
        return;
    }

    const revision = (node._scriptLibraryLoadRevision || 0) + 1;
    node._scriptLibraryLoadRevision = revision;
    node._scriptLibraryLoading = true;
    setButtonLabel(node._scriptLoadWidget, "Loading…");
    node.setDirtyCanvas?.(true, true);
    try {
        const payload = await fetchJson(
            SCRIPT_ROUTES.load,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id }),
            },
            "Script load request",
        );
        if (!payload?.script || typeof payload.code !== "string") {
            throw new Error("Script load response is missing script source");
        }
        if (
            revision !== node._scriptLibraryLoadRevision ||
            String(widgetByName(node, "script_name")?.value ?? "") !== id
        ) {
            return;
        }

        const targets = scriptedLoadTargets(node);
        let applied = 0;
        for (const target of targets) {
            const codeWidget = widgetByName(target, "code");
            if (!codeWidget) continue;
            setWidgetText(codeWidget, payload.code);
            styleCodeWidget(target);
            if (payload.schema) {
                if (applyLoadedSchema(target, payload)) applied += 1;
            } else if (!payload.schema_error) {
                // Compatibility fallback for an older backend which only
                // returned source text from the load endpoint.
                if (await applyScript(target, { shrinkToFit: true })) applied += 1;
            }
        }

        const name = String(payload.script.name || id);
        if (payload.schema_error && targets.length) {
            toast(
                "error",
                `Loaded "${name}", but its sockets could not be applied: ` +
                String(payload.schema_error),
            );
        } else if (targets.length) {
            toast(
                "success",
                `Loaded "${name}" and applied it to ${applied} Scripted Node` +
                `${applied === 1 ? "" : "s"}`,
            );
        } else {
            toast(
                "info",
                `Loaded "${name}". Connect this node to a Scripted Node ` +
                "`code` input, or select one and click Load & Apply again.",
            );
        }
    } catch (error) {
        if (revision !== node._scriptLibraryLoadRevision) return;
        toast("error", error instanceof Error ? error.message : String(error));
    } finally {
        if (revision === node._scriptLibraryLoadRevision) {
            node._scriptLibraryLoading = false;
            setButtonLabel(node._scriptLoadWidget, "Load & Apply");
            node.setDirtyCanvas?.(true, true);
        }
    }
}

async function refreshAllScriptBrowsers(preferredId = null) {
    const liveNodes = [...scriptBrowserNodes].filter((node) => node?.graph);
    await Promise.all(
        liveNodes.map((node) =>
            refreshScriptBrowser(node, { preferredId, silent: true }),
        ),
    );
}

async function deleteSelectedScript(node) {
    if (node._scriptLibraryDeleting) return;
    const id = String(widgetByName(node, "script_name")?.value ?? "");
    const entry = node._scriptLibraryEntries?.get(id);
    if (!id || !entry) {
        toast("error", "No script is selected");
        return;
    }
    if (!entry.deletable) {
        toast("error", `"${entry.name}" is bundled and cannot be deleted`);
        return;
    }
    if (!window.confirm(`Delete the saved script "${entry.name}"?`)) return;

    node._scriptLibraryDeleting = true;
    setButtonLabel(node._scriptDeleteWidget, "Deleting…");
    node.setDirtyCanvas?.(true, true);
    try {
        await fetchJson(
            SCRIPT_ROUTES.delete,
            {
                method: "DELETE",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id }),
            },
            "Script delete request",
        );
        await refreshAllScriptBrowsers();
        toast("success", `Deleted "${entry.name}"`);
    } catch (error) {
        toast("error", error instanceof Error ? error.message : String(error));
    } finally {
        node._scriptLibraryDeleting = false;
        updateBrowserButtons(node);
    }
}

function wrapScriptComboCallback(node) {
    const widget = widgetByName(node, "script_name");
    if (!widget || widget._scriptLibraryCallbackWrapped) return;
    const originalCallback = widget.callback;
    widget.callback = function () {
        const result = originalCallback?.apply(this, arguments);
        updateBrowserButtons(node);
        if (!app.configuringGraph && connectedScriptedNodes(node).length) {
            void loadSelectedScript(node);
        }
        return result;
    };
    widget._scriptLibraryCallbackWrapped = true;
}

function setupScriptBrowser(node) {
    if (node._scriptLibraryFrontendReady) return;
    node._scriptLibraryFrontendReady = true;
    scriptBrowserNodes.add(node);
    wrapScriptComboCallback(node);

    node._scriptRefreshWidget = node.addWidget(
        "button",
        "Refresh Scripts",
        null,
        () => void refreshScriptBrowser(node),
    );
    node._scriptLoadWidget = node.addWidget(
        "button",
        "Load & Apply",
        null,
        () => void loadSelectedScript(node),
    );
    node._scriptDeleteWidget = node.addWidget(
        "button",
        "Delete Selected",
        null,
        () => void deleteSelectedScript(node),
    );
    for (const widget of [
        node._scriptRefreshWidget,
        node._scriptLoadWidget,
        node._scriptDeleteWidget,
    ]) {
        widget.serialize = false;
        widget.options ??= {};
        widget.options.serialize = false;
    }

    const onRemoved = node.onRemoved;
    node.onRemoved = function () {
        scriptBrowserNodes.delete(this);
        return onRemoved?.apply(this, arguments);
    };

    const onConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function () {
        const result = onConnectionsChange?.apply(this, arguments);
        const connected = arguments[2] === true;
        if (
            connected &&
            !app.configuringGraph &&
            connectedScriptedNodes(this).length
        ) {
            queueMicrotask(() => void loadSelectedScript(this));
        }
        return result;
    };

    queueMicrotask(() => {
        void refreshScriptBrowser(node, { silent: true });
    });
}

function overwriteEnabled(value) {
    return value === true || value === 1 || value === "true" || value === "1";
}

async function saveScriptNow(node) {
    if (node._scriptLibrarySaving) return;
    const nameWidget = widgetByName(node, "script_name");
    const codeWidget = widgetByName(node, "code");
    const overwriteWidget = widgetByName(node, "overwrite");
    const name = String(nameWidget?.value ?? "").trim();
    const code = String(codeWidget?.value ?? "");

    if (!name) {
        toast("error", "Enter a script name before saving");
        return;
    }
    if (!code.trim()) {
        toast("error", "The script source is empty");
        return;
    }

    node._scriptLibrarySaving = true;
    setButtonLabel(node._scriptSaveWidget, "Saving…");
    node.setDirtyCanvas?.(true, true);
    try {
        const payload = await fetchJson(
            SCRIPT_ROUTES.save,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name,
                    code,
                    overwrite: overwriteEnabled(overwriteWidget?.value),
                }),
            },
            "Script save request",
        );
        if (!payload?.script?.id) {
            throw new Error("Script save response is missing the saved script id");
        }
        if (nameWidget && payload.script.name) {
            nameWidget.value = payload.script.name;
        }
        await refreshAllScriptBrowsers(String(payload.script.id));
        toast("success", `Saved "${payload.script.name || name}"`);
    } catch (error) {
        toast("error", error instanceof Error ? error.message : String(error));
    } finally {
        node._scriptLibrarySaving = false;
        setButtonLabel(node._scriptSaveWidget, "Save Now");
        node.setDirtyCanvas?.(true, true);
    }
}

function setupSaveScript(node) {
    if (node._scriptLibrarySaveFrontendReady) return;
    node._scriptLibrarySaveFrontendReady = true;
    injectStyles();
    styleSaveCodeWidget(node);

    const widget = node.addWidget("button", "Save Now", null, () => {
        void saveScriptNow(node);
    });
    widget.serialize = false;
    widget.options ??= {};
    widget.options.serialize = false;
    node._scriptSaveWidget = widget;

    queueMicrotask(() => resizeAndRedraw(node, true));
}

function nodePackTestSource(node) {
    return {
        repository: String(widgetByName(node, "repository")?.value ?? "").trim(),
        ref_kind: String(widgetByName(node, "ref_kind")?.value ?? "default"),
        ref: String(widgetByName(node, "ref")?.value ?? "").trim(),
        subdirectory: String(
            widgetByName(node, "subdirectory")?.value ?? "",
        ).trim(),
    };
}

function nodePackSourceFingerprint(source) {
    return JSON.stringify({
        repository: source.repository,
        ref_kind: source.ref_kind,
        ref: source.ref,
        subdirectory: source.subdirectory,
    });
}

function setNodePackReportText(node, value) {
    const text = String(value ?? "");
    node._nodePackReportText = text;
    if (node._nodePackReportElement) {
        node._nodePackReportElement.value = text;
    }
    if (node._nodePackReportFallbackWidget) {
        node._nodePackReportFallbackWidget.value = text;
    }
    node.setDirtyCanvas?.(true, true);
}

function nodePackStoredSourceFingerprint(source) {
    if (!source || typeof source !== "object" || Array.isArray(source)) return null;
    try {
        return nodePackSourceFingerprint(source);
    } catch (error) {
        return null;
    }
}

function persistNodePackReport(
    node,
    text,
    report,
    source,
    { markGraphChanged = true } = {},
) {
    const reportJson =
        typeof report === "string" ? report : JSON.stringify(report ?? {});
    const sourceCopy = structuredCloneSafe(source);
    const previousSourceFingerprint = nodePackStoredSourceFingerprint(
        node.properties?.[NODE_PACK_SOURCE_PROPERTY],
    );
    const nextSourceFingerprint = nodePackSourceFingerprint(sourceCopy);
    const materiallyChanged =
        node.properties?.[NODE_PACK_REPORT_PROPERTY] !== String(text ?? "") ||
        node.properties?.[NODE_PACK_REPORT_JSON_PROPERTY] !== reportJson ||
        previousSourceFingerprint !== nextSourceFingerprint;

    setNodePackReportText(node, text);
    node.properties ??= {};
    node.properties[NODE_PACK_REPORT_PROPERTY] = text;
    node.properties[NODE_PACK_REPORT_JSON_PROPERTY] = reportJson;
    node.properties[NODE_PACK_SOURCE_PROPERTY] = sourceCopy;
    node._nodePackTestSourceFingerprint = nextSourceFingerprint;

    if (materiallyChanged && markGraphChanged && !app.configuringGraph) {
        node.graph?.change?.();
    }
}

function addNodePackReportWidget(node) {
    if (node._nodePackReportWidget || node._nodePackReportFallbackWidget) return;

    const initial =
        "Static compatibility estimate only — pack Python is not executed.\n\n" +
        "Enter a GitHub repository and press Test Compatibility.";
    if (typeof node.addDOMWidget === "function") {
        const textarea = document.createElement("textarea");
        textarea.className = "comfy-node-pack-report";
        textarea.value = initial;
        textarea.readOnly = true;
        textarea.spellcheck = false;
        textarea.wrap = "off";
        textarea.title =
            "Static compatibility report. This tester never imports pack Python.";
        textarea.addEventListener("keydown", (event) => event.stopPropagation());

        const widget = node.addDOMWidget(
            "compatibility_report",
            "textmultiline",
            textarea,
            {
                serialize: false,
                getValue: () => textarea.value,
                setValue: (value) => {
                    textarea.value = String(value ?? "");
                },
            },
        );
        widget.serialize = false;
        widget.options ??= {};
        widget.options.serialize = false;
        widget.inputEl = textarea;
        widget.computeSize = (width) => [
            Math.max(Number(width) || 0, MIN_NODE_WIDTH),
            260,
        ];
        node._nodePackReportWidget = widget;
        node._nodePackReportElement = textarea;
    } else {
        const widget = node.addWidget(
            "text",
            "Compatibility Report",
            initial,
            () => {},
            { multiline: true },
        );
        widget.serialize = false;
        widget.disabled = true;
        widget.options ??= {};
        widget.options.serialize = false;
        widget.computeSize = () => [MIN_NODE_WIDTH, 240];
        node._nodePackReportFallbackWidget = widget;
    }
    node._nodePackReportText = initial;
}

function restoreNodePackReport(node) {
    const stored = node.properties?.[NODE_PACK_REPORT_PROPERTY];
    const source = node.properties?.[NODE_PACK_SOURCE_PROPERTY];
    const storedFingerprint = nodePackStoredSourceFingerprint(source);
    const currentFingerprint = nodePackSourceFingerprint(nodePackTestSource(node));
    node._nodePackObservedSourceFingerprint = currentFingerprint;
    node._nodePackTestSourceFingerprint = storedFingerprint;

    if (
        typeof stored === "string" &&
        stored &&
        storedFingerprint === currentFingerprint
    ) {
        setNodePackReportText(node, stored);
    } else if (typeof stored === "string" && stored && storedFingerprint) {
        setNodePackReportText(
            node,
            "Inputs changed after the last test.\n\n" +
            "Press Test Compatibility to refresh.",
        );
    }
}

function handleNodePackInputChange(node, { force = false } = {}) {
    const currentSource = nodePackTestSource(node);
    const currentFingerprint = nodePackSourceFingerprint(currentSource);
    const previousFingerprint = node._nodePackObservedSourceFingerprint;
    node._nodePackObservedSourceFingerprint = currentFingerprint;
    if (!force && currentFingerprint === previousFingerprint) return;
    if (app.configuringGraph) return;

    const requestWasActive = node._nodePackTesting === true;
    node._nodePackTestRevision = (node._nodePackTestRevision || 0) + 1;
    node._nodePackTesting = false;
    node._nodePackActiveSourceFingerprint = null;
    setButtonLabel(node._nodePackTestWidget, "Test Compatibility");

    if (currentFingerprint === node._nodePackTestSourceFingerprint) {
        restoreNodePackReport(node);
        return;
    }
    if (node._nodePackTestSourceFingerprint || requestWasActive) {
        setNodePackReportText(
            node,
            "Inputs changed after the last test.\n\n" +
            "Press Test Compatibility to refresh.",
        );
    }
}

function wrapNodePackInputCallbacks(node) {
    for (const name of ["repository", "ref_kind", "ref", "subdirectory"]) {
        const widget = widgetByName(node, name);
        if (!widget || widget._nodePackTesterCallbackWrapped) continue;
        const originalCallback = widget.callback;
        widget.callback = function () {
            const result = originalCallback?.apply(this, arguments);
            queueMicrotask(() => handleNodePackInputChange(node));
            return result;
        };
        widget._nodePackTesterCallbackWrapped = true;
    }
}

function nodePackReportText(payload) {
    if (typeof payload?.report_text === "string") return payload.report_text;
    if (typeof payload?.report?.report_text === "string") {
        return payload.report.report_text;
    }
    throw new Error("Compatibility response is missing its text report");
}

function nodePackSummaryMessage(report) {
    const summary = report?.summary;
    if (!summary || typeof summary !== "object") return "Compatibility scan finished";
    const compatible = Number(summary.compatible) || 0;
    const partial = Number(summary.partial) || 0;
    const unsupported = Number(summary.unsupported) || 0;
    return (
        `Compatibility estimate: ${compatible} compatible, ` +
        `${partial} partial, ${unsupported} unsupported`
    );
}

async function testNodePackCompatibility(node) {
    if (node._nodePackTesting) return;
    const source = nodePackTestSource(node);
    const sourceFingerprint = nodePackSourceFingerprint(source);
    if (!source.repository) {
        toast("error", "Enter a GitHub repository before testing");
        return;
    }

    const revision = (node._nodePackTestRevision || 0) + 1;
    node._nodePackTestRevision = revision;
    node._nodePackTesting = true;
    node._nodePackActiveSourceFingerprint = sourceFingerprint;
    setButtonLabel(node._nodePackTestWidget, "Testing…");
    setNodePackReportText(
        node,
        "Fetching repository source and running a static scan…\n\n" +
        "The pack's Python code is not being imported or executed.",
    );
    resizeAndRedraw(node);
    try {
        const payload = await fetchJson(
            NODE_PACK_TEST_ROUTE,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(source),
            },
            "Node pack compatibility request",
        );
        if (revision !== node._nodePackTestRevision) return;
        if (
            nodePackSourceFingerprint(nodePackTestSource(node)) !==
            sourceFingerprint
        ) {
            handleNodePackInputChange(node, { force: true });
            return;
        }
        const report = payload?.report;
        if (!report || typeof report !== "object" || Array.isArray(report)) {
            throw new Error("Compatibility response is missing structured results");
        }
        const text = nodePackReportText(payload);
        persistNodePackReport(node, text, report, source);
        resizeAndRedraw(node);
        setButtonLabel(node._nodePackTestWidget, "Tested ✓");
        toast("success", nodePackSummaryMessage(report));
    } catch (error) {
        if (revision !== node._nodePackTestRevision) return;
        if (
            nodePackSourceFingerprint(nodePackTestSource(node)) !==
            sourceFingerprint
        ) {
            handleNodePackInputChange(node, { force: true });
            return;
        }
        const message = error instanceof Error ? error.message : String(error);
        persistNodePackReport(
            node,
            `Test failed\n\n${message}`,
            {},
            source,
        );
        setButtonLabel(node._nodePackTestWidget, "Test failed");
        toast("error", message);
    } finally {
        if (revision === node._nodePackTestRevision) {
            node._nodePackTesting = false;
            node._nodePackActiveSourceFingerprint = null;
            setTimeout(() => {
                if (
                    !node._nodePackTesting &&
                    revision === node._nodePackTestRevision
                ) {
                    setButtonLabel(
                        node._nodePackTestWidget,
                        "Test Compatibility",
                    );
                    node.setDirtyCanvas?.(true, true);
                }
            }, 2200);
        }
    }
}

function firstUiValue(value) {
    if (Array.isArray(value)) return value.length ? value[0] : null;
    return value;
}

function nodePackCompatibilitySource(message) {
    let source = firstUiValue(message?.compatibility_source);
    if (typeof source === "string") {
        try {
            source = JSON.parse(source);
        } catch (error) {
            return null;
        }
    }
    if (!source || typeof source !== "object" || Array.isArray(source)) {
        return null;
    }
    const names = ["repository", "ref_kind", "ref", "subdirectory"];
    if (!names.every((name) => typeof source[name] === "string")) return null;
    return Object.fromEntries(names.map((name) => [name, source[name]]));
}

function applyExecutedNodePackReport(node, message) {
    const text = firstUiValue(
        message?.compatibility_report ?? message?.report,
    );
    const reportJson = firstUiValue(
        message?.compatibility_json ?? message?.report_json,
    );
    if (typeof text !== "string" || !text) return;
    const source = nodePackCompatibilitySource(message);
    if (!source) {
        console.warn(
            "[Scripted Node] Queued compatibility result is missing its source",
        );
        return;
    }
    if (
        nodePackSourceFingerprint(source) !==
        nodePackSourceFingerprint(nodePackTestSource(node))
    ) {
        console.info(
            "[Scripted Node] Ignored a queued compatibility result for older inputs",
        );
        return;
    }

    let report = {};
    if (typeof reportJson === "string" && reportJson) {
        try {
            report = JSON.parse(reportJson);
        } catch (error) {
            console.warn("[Scripted Node] Invalid queued compatibility JSON", error);
        }
    }
    persistNodePackReport(node, text, report, source);
    restoreNodePackReport(node);
    resizeAndRedraw(node);
}

function setupNodePackTester(node) {
    if (node._nodePackTesterFrontendReady) return;
    node._nodePackTesterFrontendReady = true;
    injectStyles();
    addNodePackReportWidget(node);
    wrapNodePackInputCallbacks(node);
    restoreNodePackReport(node);

    const widget = node.addWidget(
        "button",
        "Test Compatibility",
        null,
        () => void testNodePackCompatibility(node),
    );
    widget.serialize = false;
    widget.options ??= {};
    widget.options.serialize = false;
    node._nodePackTestWidget = widget;

    const onExecuted = node.onExecuted;
    node.onExecuted = function (message) {
        const result = onExecuted?.apply(this, arguments);
        applyExecutedNodePackReport(this, message);
        return result;
    };

    queueMicrotask(() => {
        wrapNodePackInputCallbacks(node);
        restoreNodePackReport(node);
        resizeAndRedraw(node, true);
    });
}

app.registerExtension({
    name: "comfy.scripted_node",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name === SCRIPT_BROWSER_CLASS) {
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const result = onNodeCreated?.apply(this, arguments);
                setupScriptBrowser(this);
                return result;
            };

            const onConfigure = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const result = onConfigure?.apply(this, arguments);
                setupScriptBrowser(this);
                wrapScriptComboCallback(this);
                queueMicrotask(() => {
                    void refreshScriptBrowser(this, { silent: true });
                });
                return result;
            };
            return;
        }

        if (nodeData.name === SAVE_SCRIPT_CLASS) {
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const result = onNodeCreated?.apply(this, arguments);
                setupSaveScript(this);
                return result;
            };

            const onConfigure = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const result = onConfigure?.apply(this, arguments);
                setupSaveScript(this);
                styleSaveCodeWidget(this);
                return result;
            };
            return;
        }

        if (nodeData.name === NODE_PACK_TESTER_CLASS) {
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const result = onNodeCreated?.apply(this, arguments);
                setupNodePackTester(this);
                return result;
            };

            const onConfigure = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const result = onConfigure?.apply(this, arguments);
                setupNodePackTester(this);
                wrapNodePackInputCallbacks(this);
                restoreNodePackReport(this);
                return result;
            };
            return;
        }

        if (nodeData.name !== NODE_CLASS) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            setupNode(this);
            return result;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const result = onConfigure?.apply(this, arguments);
            this._scriptedNodeWasConfigured = true;
            styleCodeWidget(this);
            hideSchemaWidget(this);
            updateScriptInputButton(this);

            if (!restoreSchema(this, info)) {
                reconcileSchema(this, { inputs: [], outputs: [] });
                const savedCode = String(widgetByName(this, "code")?.value ?? "");
                if (savedCode) {
                    console.info(
                        "[Scripted Node] Loaded without an applied schema; " +
                        "press Apply Script before queueing.",
                    );
                }
            }
            return result;
        };

        const onSerialize = nodeType.prototype.onSerialize;
        nodeType.prototype.onSerialize = function (info) {
            const result = onSerialize?.apply(this, arguments);
            if (this._scriptedNodeSchema) {
                info.properties ??= {};
                info.properties[SCHEMA_PROPERTY] =
                    structuredCloneSafe(this._scriptedNodeSchema);
                info.properties[SCHEMA_VERSION_PROPERTY] = SCHEMA_VERSION;
            }
            return result;
        };
    },
});
