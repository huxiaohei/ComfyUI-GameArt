"""GameArt 节点注册表：每个节点一个文件，在这里汇总。

新增节点：在本目录新建文件写节点类，然后在下面 import 并加进 _NODES 和 NODE_DISPLAY_NAME_MAPPINGS。
"""
from .cloud_edit import GACloudEdit
from .cloud_generate import GACloudGenerate
from .compose_template import GAComposeTemplate
from .prompt_expand import GAPromptExpand
from .qc_score import GAQCScore
from .reference_analyze import GAReferenceAnalyze
from .remove_background import GARemoveBackground
from .save_asset import GASaveAsset
from .style_card import GAStyleCard

_NODES: tuple[type, ...] = (
    GAStyleCard, GAPromptExpand, GAReferenceAnalyze, GACloudGenerate, GACloudEdit,
    GARemoveBackground, GAQCScore, GAComposeTemplate, GASaveAsset)

NODE_CLASS_MAPPINGS: dict[str, type] = {c.__name__: c for c in _NODES}

NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {
    "GAStyleCard": "GA Style Card", "GAPromptExpand": "GA Prompt Expand",
    "GAReferenceAnalyze": "GA Reference Analyze", "GACloudGenerate": "GA Cloud Generate",
    "GACloudEdit": "GA Cloud Edit", "GARemoveBackground": "GA Remove Background",
    "GAQCScore": "GA QC Score", "GAComposeTemplate": "GA Compose Template",
    "GASaveAsset": "GA Save Asset",
}

__all__ = [
    "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS",
    "GACloudEdit", "GACloudGenerate", "GAComposeTemplate", "GAPromptExpand", "GAQCScore",
    "GAReferenceAnalyze", "GARemoveBackground", "GASaveAsset", "GAStyleCard",
]
