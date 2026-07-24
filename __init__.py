"""ComfyUI Scripted Nodes custom-node package."""

if __package__:
    from .node_pack_tester import ComfyNodePackTesterNode
    from .pack_routes import ComfyPackLoaderNode
    from .scripted_node import ComfyScriptedNode
    from .script_library import ComfySaveScriptNode, ComfyScriptBrowserNode
else:  # Pytest may collect this file as a top-level module.
    from node_pack_tester import ComfyNodePackTesterNode
    from pack_routes import ComfyPackLoaderNode
    from scripted_node import ComfyScriptedNode
    from script_library import ComfySaveScriptNode, ComfyScriptBrowserNode


NODE_CLASS_MAPPINGS = {
    "ComfyScriptedNode": ComfyScriptedNode,
    "ComfyScriptBrowserNode": ComfyScriptBrowserNode,
    "ComfySaveScriptNode": ComfySaveScriptNode,
    "ComfyNodePackTesterNode": ComfyNodePackTesterNode,
    "ComfyPackLoaderNode": ComfyPackLoaderNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ComfyScriptedNode": "Scripted Node",
    "ComfyScriptBrowserNode": "Script Browser",
    "ComfySaveScriptNode": "Save Script",
    "ComfyNodePackTesterNode": "Node Pack Compatibility Tester",
    "ComfyPackLoaderNode": "Node Pack Loader",
}

WEB_DIRECTORY = "./web"


__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
