"""ComfyUI Scripted Nodes custom-node package."""

if __package__:
    from .scripted_node import ComfyScriptedNode
else:  # Pytest may collect this file as a top-level module.
    from scripted_node import ComfyScriptedNode


NODE_CLASS_MAPPINGS = {
    "ComfyScriptedNode": ComfyScriptedNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ComfyScriptedNode": "Scripted Node",
}

WEB_DIRECTORY = "./web"


__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
