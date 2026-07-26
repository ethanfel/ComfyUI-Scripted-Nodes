import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const frontendPath = new URL("../web/scripted_node.js", import.meta.url);

function loadFrontendHooks() {
    let source = readFileSync(frontendPath, "utf8");
    source = source.replace(/^import .*?;\s*$/gm, "");

    const registrationStart = source.indexOf("app.registerExtension({");
    assert.notEqual(
        registrationStart,
        -1,
        "could not find the extension registration boundary",
    );
    source = `${source.slice(0, registrationStart)}
        globalThis.__scriptedNodeTestHooks = { reconcileOutputs };
    `;

    const context = vm.createContext({
        console,
        Map,
        structuredClone,
    });
    vm.runInContext(source, context, {
        filename: frontendPath.pathname,
    });
    return context.__scriptedNodeTestHooks;
}

const { reconcileOutputs } = loadFrontendHooks();

function placeholder(index, links = null) {
    return {
        marker: `placeholder-${index}`,
        name: `output_${index + 1}`,
        type: "*",
        links,
    };
}

function graphWithLinks(...links) {
    const removed = [];
    const graph = {
        links: new Map(links.map((link) => [link.id, link])),
        removeLink(linkId) {
            removed.push(linkId);
            this.links.delete(linkId);
        },
    };
    return { graph, removed };
}

test("workflow restore reclaims backend placeholders and preserves links", () => {
    const firstLink = { id: 7, origin_slot: 0, type: "*" };
    const secondLink = { id: 8, origin_slot: 1, type: "*" };
    const { graph, removed } = graphWithLinks(firstLink, secondLink);
    const placeholders = Array.from({ length: 32 }, (_, index) =>
        placeholder(
            index,
            index === 0 ? [firstLink.id] : index === 1 ? [secondLink.id] : null,
        ),
    );
    const node = { graph, outputs: placeholders };

    reconcileOutputs(
        node,
        [
            { name: "image", type: "IMAGE" },
            { name: "caption", type: "STRING" },
        ],
        { reuseBackendPlaceholders: true },
    );

    assert.equal(node.outputs[0], placeholders[0]);
    assert.equal(node.outputs[1], placeholders[1]);
    assert.deepEqual(
        node.outputs.map(({ name, type, links }) => ({ name, type, links })),
        [
            { name: "image", type: "IMAGE", links: [7] },
            { name: "caption", type: "STRING", links: [8] },
        ],
    );
    assert.deepEqual(removed, []);
    assert.equal(graph.links.get(7), firstLink);
    assert.equal(graph.links.get(8), secondLink);
    assert.deepEqual(
        [firstLink.origin_slot, firstLink.type],
        [0, "IMAGE"],
    );
    assert.deepEqual(
        [secondLink.origin_slot, secondLink.type],
        [1, "STRING"],
    );
});

test("same-index placeholders win over colliding dynamic names", () => {
    const firstLink = { id: 10, origin_slot: 0, type: "*" };
    const secondLink = { id: 11, origin_slot: 1, type: "*" };
    const { graph, removed } = graphWithLinks(firstLink, secondLink);
    const first = placeholder(0, [firstLink.id]);
    const second = placeholder(1, [secondLink.id]);
    const node = { graph, outputs: [first, second] };

    reconcileOutputs(
        node,
        [
            { name: "output_2", type: "*" },
            { name: "text", type: "STRING" },
        ],
        { reuseBackendPlaceholders: true },
    );

    assert.equal(node.outputs[0], first);
    assert.equal(node.outputs[1], second);
    assert.deepEqual(removed, []);
    assert.deepEqual(first.links, [10]);
    assert.deepEqual(second.links, [11]);
    assert.deepEqual(
        [firstLink.origin_slot, secondLink.origin_slot],
        [0, 1],
    );
});

test("manual schema application removes an incompatible linked output", () => {
    const oldLink = { id: 20, origin_slot: 0, type: "*" };
    const { graph, removed } = graphWithLinks(oldLink);
    const oldOutput = placeholder(0, [oldLink.id]);
    const node = { graph, outputs: [oldOutput] };

    reconcileOutputs(node, [{ name: "image", type: "IMAGE" }]);

    assert.notEqual(node.outputs[0], oldOutput);
    assert.deepEqual(removed, [20]);
    assert.equal(graph.links.has(20), false);
});

test("manual output reorder preserves exact linked sockets", () => {
    const imageLink = { id: 30, origin_slot: 0, type: "IMAGE" };
    const textLink = { id: 31, origin_slot: 1, type: "STRING" };
    const { graph, removed } = graphWithLinks(imageLink, textLink);
    const image = { name: "image", type: "IMAGE", links: [30] };
    const text = { name: "text", type: "STRING", links: [31] };
    const node = { graph, outputs: [image, text] };

    reconcileOutputs(node, [
        { name: "text", type: "STRING" },
        { name: "image", type: "IMAGE" },
    ]);

    assert.equal(node.outputs[0], text);
    assert.equal(node.outputs[1], image);
    assert.deepEqual(removed, []);
    assert.deepEqual(
        [textLink.origin_slot, imageLink.origin_slot],
        [0, 1],
    );
});

test("restore does not reclaim a noncanonical wildcard output", () => {
    const oldLink = { id: 40, origin_slot: 0, type: "*" };
    const { graph, removed } = graphWithLinks(oldLink);
    const oldOutput = { name: "wildcard", type: "*", links: [40] };
    const node = { graph, outputs: [oldOutput] };

    reconcileOutputs(
        node,
        [{ name: "image", type: "IMAGE" }],
        { reuseBackendPlaceholders: true },
    );

    assert.notEqual(node.outputs[0], oldOutput);
    assert.deepEqual(removed, [40]);
    assert.equal(graph.links.has(40), false);
});
