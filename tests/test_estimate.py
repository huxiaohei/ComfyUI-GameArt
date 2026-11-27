import json
from pathlib import Path
from typing import Any

from game_art.cli.preset import prune_to
from game_art.config import Settings
from game_art.estimate import estimate_cost

SETTINGS = Settings.model_validate({
    "comfy_root": "/tmp/comfy", "config_dir": "/tmp/cfg",
    "profiles": {"image": ["p:img"], "image_edit": ["p:edit", "p:edit_cheap"], "text": ["p:txt"],
                 "vision": ["p:vis"]},
    "pricing": {"p:img": {"per_image": 0.04}, "p:edit": {"per_image": 0.05}, "p:edit_cheap": {"per_image": 0.01},
                "p:txt": {"per_1k_input_tokens": 0.001, "per_1k_output_tokens": 0.002},
                "p:vis": {"per_1k_input_tokens": 0.002, "per_1k_output_tokens": 0.004},
                "p:manual": {"per_image": 0.2}},
})
TXT, VIS = 0.003 + 0.002, 0.006 + 0.004  # 一次调用按 3000 输入 + 1000 输出 token 估


def _wf() -> dict[str, Any]:
    raw = (Path(__file__).resolve().parents[1] / "workflows" / "text_to_asset_qc.json").read_text(encoding="utf-8")
    wf: dict[str, Any] = json.loads(raw)
    return wf


def test_full_run() -> None:
    wf = _wf()
    # 扩写 + 出图（带参考图时走改图模型，取两者中贵的）+ 每张一次质检；PreviewAny 不花钱
    assert estimate_cost(wf, SETTINGS) == round(TXT + 0.05 + VIS, 4)
    wf["gen"]["inputs"]["n"] = 2  # 两张：出图翻倍，质检两次，再归并一次共性问题
    assert estimate_cost(wf, SETTINGS) == round(TXT + 0.10 + 2 * VIS + TXT, 4)
    wf["qc"]["inputs"]["auto_qc"] = False
    assert estimate_cost(wf, SETTINGS) == round(TXT + 0.10, 4)


def test_fix_and_qc_modes_skip_upstream() -> None:
    wf = _wf()
    wf["qc"]["inputs"]["fix"] = json.dumps({"file": "/x.png", "notes": ["a"]})
    # 「查看提示词」节点还在时扩写会跑一次；出图不跑
    assert estimate_cost(wf, SETTINGS) == round(TXT + VIS + 0.05 + VIS, 4)
    wf = prune_to(wf, "save")  # ga fix 提交前会去掉它们：写指令 + 改图 + 质检
    assert estimate_cost(wf, SETTINGS) == round(VIS + 0.05 + VIS, 4)
    wf["qc"]["inputs"]["edit_model"] = "p:manual"
    assert estimate_cost(wf, SETTINGS) == round(VIS + 0.2 + VIS, 4)
    wf["qc"]["inputs"]["fix"] = json.dumps({"file": "/x.png", "qc": True})
    assert estimate_cost(wf, SETTINGS) == round(VIS, 4)
    wf["qc"]["inputs"]["fix"] = json.dumps({"file": "/x.png", "resize": True})
    assert estimate_cost(wf, SETTINGS) == 0.0
