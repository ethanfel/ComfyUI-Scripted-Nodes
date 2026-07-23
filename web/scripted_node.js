import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "ComfyScriptedNode";
const SCHEMA_ROUTE = "/scripted_nodes/schema";
const SCHEMA_PROPERTY = "scripted_node_schema";
const SCHEMA_VERSION_PROPERTY = "scripted_node_schema_version";
const SCHEMA_VERSION = 1;
const MAX_OUTPUTS = 32;
const MIN_NODE_WIDTH = 460;
const MIN_EDITOR_HEIGHT = 220;

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

function styleCodeWidget(node) {
    const widget = widgetByName(node, "code");
    if (!widget) return;

    widget.label = "Python Script";
    const textarea = widgetTextarea(widget);
    if (textarea && !textarea.dataset.scriptedNodeEditor) {
        textarea.dataset.scriptedNodeEditor = "true";
        textarea.classList.add("comfy-scripted-node-editor");
        textarea.spellcheck = false;
        textarea.wrap = "off";
        textarea.title = "Edit trusted Python. Press Ctrl/Cmd+Enter to apply sockets.";
        textarea.addEventListener("keydown", (event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                event.preventDefault();
                event.stopPropagation();
                void applyScript(node);
                return;
            }
            // Keep LiteGraph and global ComfyUI shortcuts from firing while
            // editing Python.
            event.stopPropagation();
        });
    }

    if (!widget._scriptedNodeComputeSize) {
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
        widget._scriptedNodeComputeSize = true;
    }
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
    const oldSlots = groupSlotsByNameAndType(node.inputs || []);
    const nextInputs = inputSpecs.map((spec) => {
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
    });

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

function reconcileOutputs(node, outputSpecs) {
    const oldSlots = groupSlotsByNameAndType(node.outputs || []);
    const nextOutputs = outputSpecs.map((spec) => {
        const slot = takeReusableSlot(oldSlots, spec.name, spec.type) || {
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

function reconcileSchema(node, schema, { shrinkToFit = false } = {}) {
    reconcileInputs(node, schema.inputs);
    reconcileOutputs(node, schema.outputs);
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
            reconcileSchema(node, schema);
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
    if (node._scriptedNodeApplying) return;

    const codeWidget = widgetByName(node, "code");
    if (!codeWidget) {
        toast("error", "The backend `code` widget is missing");
        return;
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
    } catch (error) {
        setApplyState(node, "error");
        toast("error", error instanceof Error ? error.message : String(error));
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

function setupNode(node) {
    if (node._scriptedNodeFrontendReady) return;
    node._scriptedNodeFrontendReady = true;
    node._scriptedNodeWasConfigured = false;

    injectStyles();
    styleCodeWidget(node);
    hideSchemaWidget(node);
    addApplyButton(node);

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

app.registerExtension({
    name: "comfy.scripted_node",

    async beforeRegisterNodeDef(nodeType, nodeData) {
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
