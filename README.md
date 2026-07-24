# ComfyUI Scripted Nodes

Two ways to get a node into ComfyUI without installing a node pack:

- **Scripted Node** — create a node from a small Python script. The script
  declares its input and output sockets and provides a `run()` function;
  choosing **Apply Script** updates the node to match those declarations.
- **[Node Pack Loader](#node-pack-loader)** — point at a GitHub repository and
  use its nodes directly. The pack is fetched, pinned to a commit and registered
  into the running server; nothing is written to `custom_nodes` and ComfyUI is
  not restarted. Unloading puts the registries back.

> [!CAUTION]
> **Scripts and node packs are executable Python code and are not sandboxed.**
> Only load scripts, packs and workflows from sources you trust. When run, they
> have the same permissions as ComfyUI and can access your files, network,
> credentials, and installed programs. Always review the code before running it.

## Install

Place this repository in ComfyUI's `custom_nodes` directory:

```text
ComfyUI/
└── custom_nodes/
    └── ComfyUI-Scripted-Nodes/
```

For example:

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/ethanfel/ComfyUI-Scripted-Nodes.git
```

Restart ComfyUI, refresh the browser, and search the add-node menu for
**Scripted Node**. The scripted and library nodes add no Python-package
dependencies beyond ComfyUI. The compatibility tester and the pack loader also
require a `git` executable on `PATH` and network access to public GitHub
repositories.

This repository is the one thing that does have to be installed the ordinary
way: its own HTTP endpoints have to be registered while ComfyUI is building its
route table, which only happens at startup.

## Quick start

Add a Scripted Node and replace its code with:

```python
INPUTS = {
    "image": "IMAGE",
    "strength": (
        "FLOAT",
        {"default": 1.0},
    ),
}

OUTPUTS = {
    "result": "IMAGE",
}

def run(image, strength):
    return {"result": image * strength}
```

Choose **Apply Script**, connect the new sockets, then queue the prompt. Apply
Script only reads the literal declarations; it does not execute the script.
The Python code runs when ComfyUI executes the node.

The code and applied schema are stored in the workflow. After changing
`INPUTS` or `OUTPUTS`, apply the schema again before queuing.

## Script collection

Ready-to-paste examples live in [`scripts/`](scripts/README.md).
[`load_text_file.py`](scripts/text/load_text_file.py) returns an entire UTF-8
text file with numbered lines, while
[`line_from_file.py`](scripts/text/line_from_file.py) returns a requested
1-based line.

## Saved script library

User scripts are stored in:

```text
ComfyUI/models/scripted_nodes/
```

Names may contain subfolders, such as `text/line_picker`. The `.py` extension
is added automatically. Absolute paths, `..`, hidden path components, symbolic
links, and files outside this managed folder are rejected.

### Save a script

Add **Save Script**, enter a `script_name`, and paste the Python into `code`.
Choose **Save Now** or press Ctrl/Cmd+Enter in the editor. The node can also
save when queued and passes through the script source from its first output.

Existing files are protected unless `overwrite` is enabled. Saving through
the button uses the editor's current text; when `code` is connected, queue the
node to save the connected value.

### Browse, load, and delete scripts

Add **Script Browser** and **Scripted Node**, then connect the browser's
`script` output to the Scripted Node's `code` input. On an older ComfyUI
frontend, choose **Enable Script Input** on the Scripted Node first.

Choose a script and select **Load & Apply**. The browser copies the source into
the main editor, applies its declared sockets, and also provides the source
through the graph connection when queued. **Refresh Scripts** discovers files
added outside ComfyUI.

Bundled examples from this repository also appear in the browser and are
read-only. **Delete Selected** is available only for user scripts under
`models/scripted_nodes` and always asks for confirmation.

The library endpoints reject request-controlled path traversal and links.
They are not a sandbox against another local process running as the same user
and concurrently changing the models directory; such a process is already
inside this extension's trusted-code boundary.

## Node pack compatibility tester

Add **Node Pack Compatibility Tester** to estimate which classes from a GitHub
node pack could later run through a temporary-node adapter.

Enter:

- `repository`: a public HTTPS `github.com/owner/repository` URL, or
  `owner/repository`. Credentials and private repositories are not accepted.
- `ref_kind`: the kind of revision to fetch (`default`, `branch`, `tag`, or
  `commit`).
- `ref`: the branch, tag, or full commit identifier; leave it empty when using
  `default`.
- `subdirectory`: an optional relative folder for a node pack inside a
  monorepo.

Choose **Test Compatibility** to see a grouped report directly in the node.
The same human-readable report and its structured JSON are available as node
outputs when connected downstream and queued. An unconnected tester does not
perform network work during ordinary workflow queues.

The classifications are deliberately conservative:

- **Compatible** means static metadata matches the basic legacy
  `NODE_CLASS_MAPPINGS`, `INPUT_TYPES`, `RETURN_TYPES`, and synchronous
  `FUNCTION` contract, including compatible input arguments and a literal
  output shape.
- **Partial** means an adapter could probably call the class, but dynamic
  schemas or returns, conditional registration, validation/caching hooks,
  dependencies, server routes, output-node behavior, or custom UI may behave
  differently.
- **Unsupported** means the class uses a known blocker such as hidden, lazy, or
  list inputs; list outputs; asynchronous execution; dynamic expansion; or a
  V3-only node definition.

> [!NOTE]
> This is a static estimate, not an execution test. The tester downloads Git
> objects into a temporary directory and parses regular Python source files
> without checking them out, importing them, installing their dependencies, or
> executing pack code. Runtime dependencies and dynamically generated schemas
> therefore remain unverified. File, syntax-tree, report, temporary-disk, and
> concurrent-scan limits reject unusually large packs; temporary data is
> removed after each scan.

Testing a pack does not install or register any of its nodes. To actually run
one, use the **Node Pack Loader** below, which is an explicitly trusted action
because importing a node pack executes Python with ComfyUI's permissions.

> [!NOTE]
> The tester answers "would a static shim be able to re-implement this class?".
> The loader does not need that shim -- it registers the pack's real classes -- so
> most of what the tester calls *partial* or *unsupported* loads and runs
> normally. Treat the report as a description of a pack, not as a prediction of
> whether the loader will work.

## Node pack loader

Add **Node Pack Loader** to use a third-party node pack without installing it:
nothing is written to `custom_nodes`, and ComfyUI is not restarted.

The loader hands the pack to ComfyUI's own `nodes.load_custom_node`, so the
pack's *real* classes are registered and the real executor runs them. Hidden
inputs, `INPUT_IS_LIST`, list outputs, lazy evaluation, `IS_CHANGED`,
`VALIDATE_INPUTS`, async execution and V3 schemas all behave exactly as they
would in a normal install, and the nodes render their real widgets because
`/object_info` is rebuilt from the live class mappings on every request.

### Fetch a pack from GitHub

Enter a `repository` (and optionally `ref_kind`, `ref` and `subdirectory`, which
work exactly as they do in the tester) and choose **Fetch from GitHub**.

The revision is resolved to a commit and materialized under
`ComfyUI/user/scripted_node_packs/<host>/<owner>/<repository>/<commit>/<name>/`.
Fetching runs no pack code. Every file's mode and Git blob id are recorded in a
manifest so the tree can be checked later; **Verify Files** re-checks it, and a
load is refused outright if a file the commit named has changed.

Symbolic links and submodule entries are refused rather than materialized, and
the tree is read with `ls-tree` and `cat-file` rather than `git archive`, whose
`export-subst` and `export-ignore` attributes would let a repository serve
different bytes than its commit names.

### Load and unload

Choose a pack and select **Load Pack**. Packs already on disk are offered too,
including ones sitting in `custom_nodes/.disabled`. Loading asks for
confirmation, then registers the pack's nodes; they appear in the node search
immediately, with no page reload.

**Unload Pack** removes the pack's node classes, display names, web mount,
HTTP endpoints, `folder_paths` entries and modules again. What it cannot undo
is reported instead of being papered over: a pack that spawned a thread,
imported a C extension or patched a core module stays resident until ComfyUI
restarts, and JavaScript it registered stays active in the browser tab until
the page is reloaded.

Loading is refused while the queue is running, when the pack's name is already
claimed by an installed pack, and when a pack registers no nodes at all.

### What the loader refuses to let a pack do

A loaded pack is not sandboxed, but the loader does keep it from quietly
taking over parts of ComfyUI that belong to somebody else:

- **Node ids** are never overwritten; a pack whose ids collide with existing
  ones has those entries skipped, and the collisions are reported.
- **Display names** are restored for every node the pack did not itself
  register, so it cannot retitle core nodes.
- **The `/extensions` mount** uses the loader's validated pack name, not the
  `project.name` a pack declares in its own `pyproject.toml`, which ComfyUI
  does not validate and which would otherwise let a pack serve its JavaScript
  in an installed pack's place.
- **HTTP routes** the pack registers are served at their real paths, but ones
  claiming a reserved core prefix (`/prompt`, `/userdata`, `/api`, `/view`, …),
  using a wildcard that can match outside its own prefix, or duplicating an
  existing path are refused and listed in the node's status.

### Queueing the node does not load anything

**Node Pack Loader** only reports which packs are currently loaded when it is
queued; its outputs are a status string and the same information as JSON.
Fetching and loading happen through its buttons. This keeps the invariant that
opening or running someone else's workflow never downloads or executes a
repository on your behalf.

> [!CAUTION]
> Loading a pack runs its Python inside ComfyUI with ComfyUI's permissions. It
> is not a sandbox: a pack can read and write your files, reach the network, and
> change your Python environment. Several popular packs run `pip install` from
> their own `__init__.py` and will modify your environment the moment they are
> loaded. Pinning to a commit and verifying the manifest tell you *which* code
> runs; they do not limit what that code may do. Only load packs you trust.

## Script format

Every script has three parts:

- `INPUTS`: an insertion-ordered dictionary of input names and ComfyUI types.
- `OUTPUTS`: an insertion-ordered dictionary of output names and ComfyUI
  types.
- `run(...)`: the function called when the node executes.

Names must be valid Python identifiers. `INPUTS` and `OUTPUTS` must be literal
dictionaries so the schema can be inspected without running the script.

An input may use either a type string:

```python
"image": "IMAGE"
```

or `(type, options)`:

```python
"steps": ("INT", {"default": 20})
```

The current version creates linkable input sockets; it does not generate
per-script INT, FLOAT, or STRING widgets. Connect a Primitive or another node
when a value should be adjustable in the graph.

The backend acts on the `default` and `optional` options. Other JSON-safe
options are preserved in the stored schema but are not currently rendered as
widgets. Use `"optional": True` for an optional socket:

```python
"mask": ("MASK", {"optional": True})
```

An omitted input uses its declared `default`, when present. An omitted
optional input is not supplied to `run()`, so give the corresponding Python
parameter a default (commonly `None`).

Scripts can declare at most 32 outputs. Output order is the declaration order,
regardless of the order of keys in the value returned by `run()`.

### Return values

`run()` can return:

1. A dictionary containing exactly the declared output names:

   ```python
   def run(image):
       return {"result": image, "description": "unchanged"}
   ```

2. A tuple or list with one value per declared output:

   ```python
   def run(image):
       return image, "unchanged"
   ```

3. A single value when exactly one output is declared:

   ```python
   def run(image):
       return image
   ```

Missing or extra dictionary keys and the wrong number of positional values
produce a node execution error.

### Imports and helpers

Normal Python imports and helper functions are allowed:

```python
import torch

INPUTS = {
    "image": "IMAGE",
}

OUTPUTS = {
    "clamped": "IMAGE",
}

def clamp_image(image):
    return torch.clamp(image, 0.0, 1.0)

def run(image):
    return {"clamped": clamp_image(image)}
```

Top-level code is part of the script and executes when the node runs. Keep
side effects inside `run()` unless they are intentionally setup work.

## Schema errors

If Apply Script reports an error, check that:

- both declarations are literal dictionaries;
- every socket name is a valid Python identifier;
- every type is a non-empty ComfyUI type string;
- an input is either `"TYPE"` or `("TYPE", {options})`;
- every option can be represented as JSON;
- at least one output is declared;
- no more than 32 outputs are declared; and
- exactly one synchronous, top-level `def run(...)` is present.

Changing a socket's name or type can disconnect an incompatible link. Review
connections after applying a schema change.

## Security

There is no sandbox. A queued script can read or modify files, access the
network and environment variables, allocate GPU memory, start subprocesses,
or stop the ComfyUI process. A workflow containing a Scripted Node should be
treated like a Python program:

- inspect its script before queuing it;
- do not run workflows or scripts from people you do not trust;
- do not expose a ComfyUI instance containing this extension to untrusted
  users; and
- save work before experimenting with code that may exhaust memory or crash.

Loading a workflow and applying its schema do not execute its script. Queuing
the node does.

The same applies to node packs, more sharply, because a pack is somebody else's
whole program rather than a script you can read at a glance:

- fetching a pack executes nothing; loading it executes all of it, starting with
  its `__init__.py`;
- a pinned commit and a verified manifest establish *which* code runs, not what
  it is allowed to do;
- packs commonly `pip install` from their own `__init__.py`, which changes the
  Python environment ComfyUI and every other pack share;
- opening or queueing a workflow never fetches or loads a pack — only the
  loader's buttons do; and
- unloading restores ComfyUI's registries, not the process. Threads, native
  modules, monkeypatches and already-evaluated JavaScript survive it, and the
  node says so when they do.

## Development

Install the test dependency and run the suite from this directory:

```bash
python -m pip install -e ".[test]"
pytest
```

The tests exercise schema parsing, registration, return-value mapping, default
and optional inputs, error handling, the fixed 32-output ComfyUI adapter, the
static pack analyser, and the pack loader — including the load/unload
transaction against a stand-in for ComfyUI's registries, route filtering, cache
manifest verification, and the cases where a pack tries to claim node ids,
display names, web mounts or endpoints that are not its own.

The loader's behaviour against real ComfyUI needs a booted server and real packs
on disk, so it lives in a separate script rather than in the unit suite:

```bash
python tools/verify_pack_loader.py --comfy-root /path/to/ComfyUI \
    --pack disabled:SomePack --repository owner/repository
```

It boots ComfyUI in process, loads each pack, and asserts that unloading leaves
`NODE_CLASS_MAPPINGS`, `NODE_DISPLAY_NAME_MAPPINGS`, `EXTENSION_WEB_DIRS`,
`LOADED_MODULE_DIRS`, the aiohttp router, `sys.path`, `sys.modules` and
`folder_paths` exactly as it found them. It runs pack code, so it installs a
`pip` firewall first and reports any pack that tried to install something.
