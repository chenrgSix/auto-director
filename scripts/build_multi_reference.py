"""Convert the supplied H3 canvas using its named widgets and explicit links."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "workflow_examples" / "h3_multi_reference"


def build():
    canvas = json.loads((FOLDER / "source.comfy.json").read_text())
    links = {link[0]: link for link in canvas["links"]}
    graph = {}
    for node in canvas["nodes"]:
        inputs = dict(node.get("widgets_values_named", {}))
        inputs.pop("upload", None)
        inputs.pop("control_after_generate", None)
        for port in node.get("inputs", []):
            if port.get("link") is not None:
                link = links[port["link"]]
                inputs[port["name"]] = [str(link[1]), link[2]]
        graph[str(node["id"])] = {"class_type": node["type"], "inputs": inputs}
    # Fixed system canvas supports the director's budget, without inheriting source dimensions.
    del graph["4"]
    graph["5"]["inputs"].update(width=640, height=384, edit_instruction="Render the requested scene using the explicitly selected references.")
    graph["0"]["inputs"]["image"] = "REQUIRES_REAL_REFERENCE.png"
    graph["13"]["inputs"]["filename_prefix"] = "autodirector/H3_MultiReference"
    bindings = {
        "prompt": {"node_id": "5", "input": "edit_instruction"},
        "reference_image": {"node_id": "0", "input": "image"},
        "width": {"node_id": "5", "input": "width"},
        "height": {"node_id": "5", "input": "height"},
        "seed": {"node_id": "6", "input": "noise_seed"},
    }
    for number in range(2, 10):
        id = str(100 + number)
        graph[id] = {"class_type": "LoadImage", "inputs": {"image": "REQUIRES_REAL_REFERENCE.png"}}
        graph["5"]["inputs"][f"reference_image_{number}"] = [id, 0]
        bindings[f"reference_image_{number}"] = {"node_id": id, "input": "image", "optional": True}
    profile = {
        "name": "H3-多图参考-按镜头选图",
        "capability": "IMAGE_TO_IMAGE",
        "workflow": graph,
        "bindings": bindings,
        "outputs": {"image": "13"},
        "capabilities": {"supports_multi_reference": True},
    }
    for name, value in (("h3_multi_reference.api.json", graph), ("h3_multi_reference.profile.json", profile)):
        (FOLDER / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return profile


if __name__ == "__main__":
    build()
