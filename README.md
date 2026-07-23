# ComfyUI Scripted Nodes

Create a ComfyUI node from a small Python script. The script declares its input
and output sockets and provides a `run()` function; choosing **Apply Script**
updates the node to match those declarations.

This extension is intended for local, trusted use. It deliberately executes
ordinary Python with the same permissions as ComfyUI.

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
**Scripted Node**. There are no extra runtime dependencies beyond those
already supplied by ComfyUI.

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

## Development

Install the test dependency and run the suite from this directory:

```bash
python -m pip install -e ".[test]"
pytest
```

The tests exercise schema parsing, registration, return-value mapping, default
and optional inputs, error handling, and the fixed 32-output ComfyUI adapter.
