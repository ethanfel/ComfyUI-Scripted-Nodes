import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const PACK_LOADER_CLASS = "ComfyPackLoaderNode";
const PACK_ROUTES = Object.freeze({
    list: "/scripted_nodes/packs",
    fetch: "/scripted_nodes/packs/fetch",
    load: "/scripted_nodes/packs/load",
    unload: "/scripted_nodes/packs/unload",
    reload: "/scripted_nodes/packs/reload",
    verify: "/scripted_nodes/packs/verify",
    cache: "/scripted_nodes/packs/cache",
});
const PACKS_CHANGED_EVENT = "scripted_nodes.packs_changed";
const STATUS_PROPERTY = "scripted_node_pack_status";
const NO_PACKS = "(no packs found)";
const MIN_NODE_WIDTH = 460;

// URLs of extension modules already evaluated in this page.  The frontend's
// extension store throws on a duplicate `name`, and an ES module is keyed by its
// exact URL, so a cache-busted re-import would re-run the module body and abort
// it half-way through registration.  Importing each URL at most once is the only
// safe policy; genuinely changed JavaScript needs a page reload.
const importedExtensions = new Set();
const packLoaderNodes = new Set();
let refreshInFlight = null;

function toast(severity, detail) {
    const message = String(detail || "Unknown error");
    const logger = severity === "error" ? console.error : console.info;
    logger(`[Pack Loader] ${message}`);

    try {
        app.extensionManager?.toast?.add?.({
            severity,
            summary: "Pack Loader",
            detail: message,
            life: severity === "error" ? 8000 : 3000,
        });
    } catch (error) {
        console.warn("[Pack Loader] Could not show toast", error);
    }
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
        const missing = payload?.missing_module;
        const detail = payload?.error || `${context} failed (${response.status})`;
        throw new Error(
            missing ? `${detail} — install it, then try again` : detail,
        );
    }
    return payload;
}

function postJson(route, body, context) {
    return fetchJson(
        route,
        {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        },
        context,
    );
}

function widgetByName(node, name) {
    return node.widgets?.find((widget) => widget.name === name);
}

function setButtonLabel(widget, label) {
    if (widget) {
        widget.name = label;
        widget.label = label;
    }
}

// --------------------------------------------------------------------------- //
// Live node-definition refresh
// --------------------------------------------------------------------------- //

function extensionNames() {
    try {
        return new Set((app.extensions || []).map((extension) => extension?.name));
    } catch (error) {
        return new Set();
    }
}

/**
 * Import the JavaScript of packs that were loaded after this page started.
 *
 * `registerExtension` is not called for late extensions by the frontend, and it
 * never invokes `init`/`setup` on them either, so both are driven here for the
 * extensions that appear as a result of the import.
 */
async function importPackExtensions() {
    let urls;
    try {
        urls = await fetchJson("/extensions", {}, "Extension list");
    } catch (error) {
        console.warn("[Pack Loader] Could not list extensions", error);
        return;
    }
    if (!Array.isArray(urls)) {
        return;
    }

    for (const url of urls) {
        if (
            typeof url !== "string" ||
            importedExtensions.has(url) ||
            url.includes("extensions/core")
        ) {
            continue;
        }
        importedExtensions.add(url);

        const before = extensionNames();
        try {
            // `fileURL`, not `apiURL`: extension modules are served as static
            // files, which is how the frontend imports them at boot.
            await import(api.fileURL(url));
        } catch (error) {
            // A pack whose JavaScript was already registered under the same name
            // is expected when the same pack is loaded twice; anything else is
            // reported but must not abort the remaining imports.
            if (!/already registered/i.test(String(error?.message))) {
                console.warn(`[Pack Loader] Could not load ${url}`, error);
            }
            continue;
        }

        for (const extension of app.extensions || []) {
            if (!extension?.name || before.has(extension.name)) {
                continue;
            }
            try {
                await extension.init?.(app);
                await extension.setup?.(app);
            } catch (error) {
                console.warn(
                    `[Pack Loader] ${extension.name} failed during setup`,
                    error,
                );
            }
        }
    }
}

/**
 * Rebuild the node palette from the server's current node definitions.
 *
 * `/object_info` is generated per request from the live class mappings, so a
 * pack loaded a moment ago is already present; `registerNodes()` re-fetches it
 * and re-registers every type, which is what makes new nodes appear without a
 * page reload.
 */
async function refreshNodeDefinitions() {
    if (refreshInFlight) {
        return refreshInFlight;
    }
    refreshInFlight = (async () => {
        await importPackExtensions();
        try {
            await app.registerNodes();
        } catch (error) {
            toast(
                "error",
                `Node definitions could not be refreshed (${error.message}); ` +
                "reload the page to see the change",
            );
        }
    })();
    try {
        await refreshInFlight;
    } finally {
        refreshInFlight = null;
    }
}

function describePack(pack) {
    const bits = [];
    if (pack.node_count) {
        bits.push(`${pack.node_count} nodes`);
    }
    if (pack.state === "loaded") {
        bits.push("loaded");
    } else if (pack.state === "installed") {
        bits.push("installed");
    }
    if (pack.scope === "cache" && pack.short_commit) {
        bits.push(pack.short_commit);
    } else if (pack.scope !== "enabled") {
        bits.push(pack.scope);
    }
    return bits.length ? `${pack.name} — ${bits.join(", ")}` : pack.name;
}

function packSummary(pack) {
    const lines = [`${pack.name} (${pack.scope})`];
    if (pack.state === "loaded") {
        lines.push(`  ${pack.node_count} node(s) registered`);
        if (pack.web_mount) {
            lines.push(`  web assets: ${pack.web_mount}`);
        }
        if (pack.routes?.length) {
            lines.push(`  ${pack.routes.length} endpoint(s) served`);
        }
        if (pack.refused_routes?.length) {
            lines.push(
                `  ${pack.refused_routes.length} endpoint(s) refused: ` +
                pack.refused_routes
                    .slice(0, 3)
                    .map((route) => `${route.path} (${route.reason})`)
                    .join("; "),
            );
        }
        if (pack.collisions?.length) {
            lines.push(
                `  ${pack.collisions.length} display name(s) it tried to take over were kept`,
            );
        }
        for (const note of pack.dirty || []) {
            lines.push(`  ! ${note}`);
        }
    } else if (pack.state === "installed") {
        lines.push("  already installed and loaded by ComfyUI at startup");
    } else {
        lines.push("  available — choose Load Pack to register its nodes");
    }
    return lines.join("\n");
}

// --------------------------------------------------------------------------- //
// Node UI
// --------------------------------------------------------------------------- //

function setStatus(node, text) {
    const widget = node._packStatusWidget;
    if (!widget) {
        return;
    }
    const value = String(text ?? "");
    if (widget.inputEl) {
        widget.inputEl.value = value;
    }
    widget.value = value;
    if (node.properties) {
        node.properties[STATUS_PROPERTY] = value;
    }
    node.setDirtyCanvas?.(true, false);
}

function addStatusWidget(node) {
    if (node._packStatusWidget) {
        return;
    }
    const initial =
        "Nothing has been fetched or loaded yet.\n\n" +
        "Enter a GitHub repository and press Fetch, or pick a pack already on " +
        "disk and press Load.";

    // Mirrors the compatibility-report widget: a read-only textarea through
    // addDOMWidget where available, with a plain multiline widget as fallback
    // for older frontends.  computeSize pins the width so the DOM widget does
    // not collapse when the node is re-laid out.
    if (typeof node.addDOMWidget === "function") {
        const textarea = document.createElement("textarea");
        textarea.className = "comfy-node-pack-report";
        textarea.value = initial;
        textarea.readOnly = true;
        textarea.spellcheck = false;
        textarea.addEventListener("keydown", (event) => event.stopPropagation());

        const widget = node.addDOMWidget("pack_status", "textmultiline", textarea, {
            serialize: false,
            getValue: () => textarea.value,
            setValue: (value) => {
                textarea.value = String(value ?? "");
            },
        });
        widget.serialize = false;
        widget.options ??= {};
        widget.options.serialize = false;
        widget.inputEl = textarea;
        widget.computeSize = (width) => [
            Math.max(Number(width) || 0, MIN_NODE_WIDTH),
            220,
        ];
        node._packStatusWidget = widget;
    } else {
        const widget = node.addWidget("text", "pack_status", initial, () => {}, {
            multiline: true,
        });
        widget.serialize = false;
        widget.disabled = true;
        widget.options ??= {};
        widget.options.serialize = false;
        widget.computeSize = () => [MIN_NODE_WIDTH, 200];
        node._packStatusWidget = widget;
    }

    const stored = node.properties?.[STATUS_PROPERTY];
    if (stored) {
        setStatus(node, stored);
    }
}

function setPackEntries(node, packs) {
    const widget = widgetByName(node, "pack");
    if (!widget) {
        return;
    }
    const previous = widget.value;
    const values = packs.map((pack) => describePack(pack));
    widget.options = widget.options || {};
    widget.options.values = values.length ? values : [NO_PACKS];
    node.packsByLabel = new Map(
        packs.map((pack) => [describePack(pack), pack]),
    );
    if (!widget.options.values.includes(previous)) {
        widget.value = widget.options.values[0];
    }
    node.setDirtyCanvas?.(true, false);
}

function selectedPack(node) {
    const widget = widgetByName(node, "pack");
    const pack = node.packsByLabel?.get(widget?.value);
    if (!pack) {
        throw new Error("Choose a node pack first");
    }
    return pack;
}

function addPackCombo(node) {
    if (widgetByName(node, "pack")) {
        return;
    }
    const widget = node.addWidget(
        "combo",
        "pack",
        NO_PACKS,
        () => updateButtons(node),
        // A combo with no values cannot be created, so it always has a
        // placeholder entry.
        { values: [NO_PACKS], serialize: false },
    );
    widget.serialize = false;
}

function updateButtons(node) {
    let pack = null;
    try {
        pack = selectedPack(node);
    } catch (error) {
        pack = null;
    }
    const loaded = pack?.state === "loaded";
    setButtonLabel(node._packLoadButton, loaded ? "Unload Pack" : "Load Pack");
    node.setDirtyCanvas?.(true, false);
}

async function refreshPacks(node, { announce = false } = {}) {
    const payload = await fetchJson(PACK_ROUTES.list, {}, "Pack list");
    const packs = Array.isArray(payload.packs) ? payload.packs : [];
    for (const target of packLoaderNodes) {
        setPackEntries(target, packs);
        updateButtons(target);
    }
    if (announce && node) {
        const loaded = packs.filter((pack) => pack.state === "loaded");
        setStatus(
            node,
            `${packs.length} pack(s) discovered, ${loaded.length} loaded by this tool.` +
            (loaded.length
                ? `\n\n${loaded.map((pack) => packSummary(pack)).join("\n")}`
                : ""),
        );
    }
    return packs;
}

async function withBusy(node, widget, label, action) {
    const original = widget?.name;
    setButtonLabel(widget, label);
    node.setDirtyCanvas?.(true, false);
    try {
        return await action();
    } finally {
        setButtonLabel(widget, original);
        node.setDirtyCanvas?.(true, false);
    }
}

async function fetchPack(node, widget) {
    const repository = String(widgetByName(node, "repository")?.value || "").trim();
    if (!repository) {
        toast("error", "Enter a GitHub repository first");
        return;
    }
    const body = {
        repository,
        ref_kind: String(widgetByName(node, "ref_kind")?.value || "default"),
        ref: String(widgetByName(node, "ref")?.value || ""),
        subdirectory: String(widgetByName(node, "subdirectory")?.value || ""),
    };

    setStatus(node, `Fetching ${repository}…`);
    const payload = await withBusy(node, widget, "Fetching…", () =>
        postJson(PACK_ROUTES.fetch, body, "Fetch"),
    );
    const pack = payload.pack || {};
    setStatus(
        node,
        `Fetched ${pack.name} at ${String(pack.commit || "").slice(0, 12)}\n` +
        `  ${pack.file_count} files, ${Math.round((pack.total_bytes || 0) / 1024)} KiB\n` +
        (pack.has_submodules
            ? "  ! this repository uses submodules, whose contents were not fetched\n"
            : "") +
        (pack.refused_entries?.length
            ? `  ! ${pack.refused_entries.length} entr(ies) refused: ${pack.refused_entries.slice(0, 3).join(", ")}\n`
            : "") +
        "\nNothing has been executed yet. Choose Load Pack to run its code.",
    );
    await refreshPacks(node);
    const widgetCombo = widgetByName(node, "pack");
    const label = [...(node.packsByLabel?.keys() || [])].find((key) =>
        key.startsWith(`${pack.name} `),
    );
    if (label && widgetCombo) {
        widgetCombo.value = label;
        updateButtons(node);
    }
    toast("info", `Fetched ${pack.name}`);
}

async function toggleLoad(node, widget) {
    const pack = selectedPack(node);
    const loading = pack.state !== "loaded";
    const route = loading ? PACK_ROUTES.load : PACK_ROUTES.unload;

    if (loading) {
        const confirmed = await confirmLoad(pack);
        if (!confirmed) {
            return;
        }
    }

    setStatus(node, `${loading ? "Loading" : "Unloading"} ${pack.name}…`);
    const payload = await withBusy(
        node,
        widget,
        loading ? "Loading…" : "Unloading…",
        () => postJson(route, { id: pack.id }, loading ? "Load" : "Unload"),
    );

    const result = payload.pack || {};
    if (loading) {
        setStatus(node, packSummary({ ...result, state: "loaded" }));
        toast("info", `Loaded ${result.name} (${result.node_count} nodes)`);
    } else {
        setStatus(
            node,
            `Unloaded ${result.name}.\n` +
            (result.dirty?.length
                ? "\nThe node registry was restored, but this pack made changes to " +
                  "the process that cannot be undone:\n" +
                  result.dirty.map((note) => `  ! ${note}`).join("\n") +
                  "\nRestart ComfyUI to be certain they are gone."
                : "") +
            (result.web_mount
                ? "\n\nIts JavaScript stays active in this browser tab until you " +
                  "reload the page."
                : ""),
        );
        toast("info", `Unloaded ${result.name}`);
    }
    await refreshPacks(node);
    await refreshNodeDefinitions();
}

async function confirmLoad(pack) {
    const detail =
        `Load "${pack.name}"?\n\n` +
        "This runs the pack's Python inside ComfyUI, with the same permissions " +
        "as ComfyUI itself. It is not sandboxed: it can read and write your " +
        "files, reach the network, and change your Python environment.\n\n" +
        (pack.scope === "cache"
            ? "Source: fetched repository, pinned to a commit that has been " +
              "verified byte for byte against its manifest.\n\n"
            : `Source: ${pack.path}\n\n`) +
        "Only load packs you trust.";

    const prompt = app.extensionManager?.dialog?.confirm;
    if (typeof prompt === "function") {
        try {
            return await prompt({
                title: "Run this node pack's code?",
                message: detail,
                type: "default",
            });
        } catch (error) {
            // Fall through to the browser dialog below.
        }
    }
    return window.confirm(detail);
}

async function verifyPack(node, widget) {
    const pack = selectedPack(node);
    const payload = await withBusy(node, widget, "Verifying…", () =>
        postJson(PACK_ROUTES.verify, { id: pack.id }, "Verify"),
    );
    if (pack.scope !== "cache") {
        setStatus(
            node,
            `${pack.name} was not fetched by this tool, so there is no manifest ` +
            "to verify it against.",
        );
        return;
    }
    const { changed = [], added = [] } = payload;
    setStatus(
        node,
        changed.length
            ? `${pack.name} NO LONGER MATCHES the commit it was fetched from:\n` +
              changed.map((entry) => `  ! ${entry}`).join("\n") +
              "\n\nRe-fetch it before loading."
            : `${pack.name} matches the commit it was fetched from.` +
              (added.length
                  ? `\n\n${added.length} file(s) were written after the fetch ` +
                    `(packs often store their own settings):\n` +
                    added.slice(0, 10).map((entry) => `  ${entry}`).join("\n")
                  : ""),
    );
}

function addButton(node, label, handler) {
    const widget = node.addWidget("button", label, "", async () => {
        try {
            await handler(node, widget);
        } catch (error) {
            toast("error", error.message);
            setStatus(node, `Error: ${error.message}`);
        }
    });
    widget.serialize = false;
    return widget;
}

function setupPackLoader(node) {
    if (node.packLoaderReady) {
        return;
    }
    node.packLoaderReady = true;
    packLoaderNodes.add(node);

    node.size[0] = Math.max(node.size?.[0] || 0, MIN_NODE_WIDTH);
    addPackCombo(node);
    addButton(node, "Fetch from GitHub", fetchPack);
    node._packLoadButton = addButton(node, "Load Pack", toggleLoad);
    addButton(node, "Verify Files", verifyPack);
    addButton(node, "Refresh List", (target) =>
        refreshPacks(target, { announce: true }),
    );
    addStatusWidget(node);

    const onRemoved = node.onRemoved;
    node.onRemoved = function (...args) {
        packLoaderNodes.delete(node);
        return onRemoved?.apply(this, args);
    };

    refreshPacks(node, { announce: true }).catch((error) =>
        setStatus(node, `Could not list packs: ${error.message}`),
    );
}

app.registerExtension({
    name: "ComfyUI.ScriptedNodes.PackManager",

    setup() {
        // Registered before any load can be triggered: the websocket client
        // discards messages whose type nobody is listening for.
        api.addEventListener(PACKS_CHANGED_EVENT, async (event) => {
            const detail = event?.detail || {};
            if (detail.action === "loaded") {
                await refreshNodeDefinitions();
            }
            for (const node of packLoaderNodes) {
                refreshPacks(node).catch(() => {});
            }
        });

        // Everything already served at boot is, by definition, already imported.
        fetchJson("/extensions", {}, "Extension list")
            .then((urls) => {
                if (Array.isArray(urls)) {
                    urls.forEach((url) => importedExtensions.add(url));
                }
            })
            .catch(() => {});
    },

    async nodeCreated(node) {
        if (node.comfyClass === PACK_LOADER_CLASS) {
            setupPackLoader(node);
        }
    },

    async loadedGraphNode(node) {
        if (node.comfyClass === PACK_LOADER_CLASS) {
            setStatus(node, node.properties?.[STATUS_PROPERTY] || "");
        }
    },
});
