# CLAUDE.md

给 Claude 和其他 AI 工具的工作说明。[README.md](README.md) 只讲怎么使用；干活的规矩、实现原理、代码导读和扩展方法都在这里。

## 文档分工

- **README 只写怎么用**，控制在 400 行以内，不写实现细节。
- **实现原理、代码导读、扩展方法写在本文件。**
- 每个功能在 `examples/` 下保留一份对应的配置或描述示例（配置、风格卡、描述清单、标准风格图、合成版式、批量清单）。风格卡、描述清单和标准风格图是配套的一组，取自《三国守御录》项目的 `art/style/`，那边定稿有变化时这里要跟着换，README 的"示例文件"表里要列出来。
- 新增功能时三处一起更新：用法进 README，原理进本文件，示例进 `examples/`。

## 提交

- **提交信息里不要加 AI 署名**：不写 `Co-Authored-By: Claude …`，不写 `Claude-Session: …`，也不写"Generated with …"之类的行。提交信息只有标题和正文。这条规矩优先于工具自带的默认做法。
- 提交信息用简体中文，标题格式 `类型(范围): 说明`，类型用 `feat`、`fix`、`refactor`、`docs`、`test`、`chore`。
- 按功能分批提交，一个提交只做一件事，每个提交单独都能通过检查。
- 提交前先征得同意；推送默认由仓库主人自己执行，仓库主人明确要求推送时直接推送，不用再确认。

## 提交前的检查

三项都必须 0 错误、0 告警，不用 `# type: ignore`、`# noqa` 压制：

```bash
uv run pytest -q -W error
uv run mypy
uv run ruff check
```

## 改动时要注意

- `workflows/*.json` 是给命令行和批量流程用的 API 格式；`workflows/editor/*.json` 是由它们生成的编辑器格式（`ga comfy export-editor`），只在本机生成、不入库（已写进 `.gitignore`）。改了节点的输入输出或 API 工作流，要重新生成编辑器格式。
- ComfyUI 侧边栏里的 `game_art/` 就是链接到 `workflows/editor/` 的，使用者在编辑器里存的工作现场也会落在这里，所以整个目录不提交。
- 改了 Python 代码要重启 ComfyUI 才生效；改了 `comfy_nodes/web/` 下的脚本刷新页面即可。改完要提醒使用者。
- 节点的说明文字（`DESCRIPTION`、`tooltip`）是写给不懂技术的美术和策划看的，用大白话。
- 新增节点：在 `comfy_nodes/nodes/` 新建文件，并加进 `comfy_nodes/nodes/__init__.py` 的注册表；用法写进 README，对应的核心代码登记到本文件的"节点对应的核心代码"表里。

## 安全

- 不要读取、打印或索要任何密钥，不要查看 `~/.game_art/.env`。
- 出图和改图要花钱：不要自行运行会调用云端出图模型的命令（包括 e2e 测试），除非使用者明确要求。

## 实现原理和代码导读

分四块：

```
        命令行 / AI 助手                              ComfyUI 编辑器
             │ 敲命令                                      │ 点"运行"
             ▼                                            ▼
   ┌──────────────────┐   HTTP：提交工作流 JSON   ┌───────────────────┐
   │ ① 命令行 ga      │ ────────────────────────▶ │  ComfyUI 服务      │
   │ src/game_art/cli │ ◀──────────────────────── │                   │
   └────────┬─────────┘   HTTP：查进度、下载图片   └─────────┬─────────┘
            │ 批量时                                         │ 按连线顺序执行节点
            ▼                                              ▼
   ┌──────────────────┐                          ┌───────────────────┐
   │ ④ 批量流程       │                          │ ② 节点            │
   │ src/game_art/    │                          │ comfy_nodes/      │
   │ batch            │                          │ nodes/            │
   └────────┬─────────┘                          └─────────┬─────────┘
            │ 质检时直接调用                                 │ 每个节点的 run() 只是薄薄一层
            ▼                                              ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ ③ 核心库 src/game_art                                           │
   │ steps（扩写/看图/质检） · providers（调云端模型） · ledger（记账）  │
   │ style（风格卡） · compose（模板合成） · imaging（图像处理） · config │
   └────────────────────────────────────────────────────────────────┘
```

**有两个进程。** `ga` 在终端里运行，它自己不出图，只负责"提交、等待、下载"，完全不知道工作流里有什么节点。真正执行节点、调用云端模型的是 ComfyUI 进程。核心库被两边共用，只依赖 requests / pydantic / PyYAML / Pillow / numpy，所以能在 ComfyUI 自带的 Python 里直接运行；torch 只出现在 `comfy_nodes/`。

**三个入口**

| 入口 | 位置 | 说明 |
|---|---|---|
| 命令行 | `src/game_art/cli/app.py` 的 `main` | 每个子命令是一个函数，真正的逻辑在其内部的 `body()` 里，外层 `_guarded()` 把错误统一转成中文提示和退出码 |
| ComfyUI 加载节点 | `comfy_nodes/__init__.py` | `ga comfy install` 在 `custom_nodes/` 下建符号链接指向 `comfy_nodes/`；这个文件把 `src/` 加进搜索路径，并导出 `NODE_CLASS_MAPPINGS` |
| 编辑器里的脚本 | `comfy_nodes/web/` | 每个文件服务一个节点，需要后台配合的接口在 `comfy_nodes/routes.py`。`style_card.js`：把风格卡文件读进编辑框、支持拖入。`reference_images.js`：把拖进来的图片变成「加载图像」节点并连线。`prompt_expand.js`：读描述清单、点 `item` 选一项并填进 `brief`。`save_asset.js`：点 `subfolder` 弹系统的文件夹选择窗口；「另存选中的图…」按钮调 `/game_art/save_as`，后台弹窗选文件夹后用 `copy_asset` 把图和记录复制过去（节点运行时在 `ui.ga_files` 里给出每张图的完整路径）。`qc_review.js`：「质检结果」节点上的面板，把勾选的问题写回「提示词扩写」的 `feedback` |

**一次 `ga run` 走过的路**（`cli/app.py` 的 `run`）

```
load_workflow ─▶ apply_sets ─▶ _fill_tags ─▶ upload ─▶ submit ─▶ wait ─▶ download_outputs
   读 JSON        套用 --set     填记账标签    传参考图   提交      轮询等待    下载图片
```

前两步在 `comfy/workflow.py`，后四步在 `comfy/client.py`。

**工作流文件**是一个字典，键是节点名：

```json
"gen": {
  "class_type": "GACloudGenerate",
  "inputs": { "prompt": ["expand", 0], "negative": ["expand", 1], "size": "1024x1024", "n": 1 }
}
```

`class_type` 对应 `comfy_nodes/nodes/` 里的一个节点类；普通值是参数；`["expand", 0]` 是连线，表示取 `expand` 节点的第 0 个输出。`--set gen.n=1` 改的就是 `gen` 节点的 `n`。

**节点类的结构**都一样，读任何一个只需要看 `INPUT_TYPES`（有哪些参数）和 `run`（做了什么）：

```python
class GACloudGenerate:
    RETURN_TYPES = ("IMAGE", "MASK", "GA_META")   # 输出口的类型和顺序
    RETURN_NAMES = ("image", "mask", "meta")      # 输出口的名字

    @classmethod
    def INPUT_TYPES(cls): ...                     # 输入口和控件

    def run(self, prompt, negative, size, n, ...):
        return (image, mask, meta)                # 按 RETURN_TYPES 的顺序返回
```

输出口的顺序和名字由 `RETURN_TYPES` / `RETURN_NAMES` 决定，`return` 的元组按位置填进去。改顺序要这几处一起改，并更新工作流里按序号引用的连线。

**节点对应的核心代码**

| 节点类 | `run` 里的关键调用 | 核心库位置 |
|---|---|---|
| `GAStyleCard` | `load_style_card` | `style.py` |
| `GAReferenceImages` | `parse_ref_paths`、`load_ref_images` | `refs.py` |
| `GAPromptExpand` | `expand_prompt`；`brief` 为空时按 `item` 用 `load_briefs` 从描述清单取 | `steps.py`，模板在 `prompts/expand.md`；描述清单在 `briefs.py` |
| `GAReferenceAnalyze` | `analyze_reference` | `steps.py`，模板在 `prompts/analyze.md` |
| `GACloudGenerate` / `GACloudEdit` | `router.generate` / `router.edit` | `providers/router.py` |
| `GARemoveBackground` | `remove_background` | `imaging.py` |
| `GAMakeSeamless` | `make_seamless`、`tile_preview`（本机处理，不调模型） | `imaging.py` |
| `GAQCScore` | `qc_score` | `steps.py`，模板在 `prompts/qc.md` |
| `GAQCReview` | `qc_score`、`common_issues`；修图时 `fix_instruction`、`router.edit` | `steps.py`，模板在 `prompts/qc.md`、`prompts/common_issues.md`；节点上的面板在 `comfy_nodes/web/qc_review.js` |
| `GAComposeTemplate` | `compose` | `compose.py` |
| `GASaveAsset` | 直接写 PNG 和 `.json`；`clean_faint_alpha` | 写文件的逻辑在节点里，清理在 `imaging.py` |

节点之间传六种数据：`STRING`、`IMAGE`、`MASK`、`GA_STYLE`（风格卡对象）、`GA_REFS`（参考图列表，每张保留原始尺寸）、`GA_META`（随图流转的元数据字典）。ComfyUI 的 `IMAGE` 不带透明度，所以透明度单独放在 `MASK` 里并排传，约定 `mask = 1 - alpha`；少接一根 `mask` 线，透明背景就丢了。转换代码在 `comfy_nodes/tensors.py`。

**调用云端模型的链路**

```
节点.run() ─▶ Router.generate(profile, req, tag)      providers/router.py
                │  resolve：模型组名 → 目标列表；或直接解析 provider:model
                │  _run：按顺序逐个尝试，失败换下一个，每次尝试都记账
                ▼
              get_provider(settings, 名字)             providers/registry.py
                ▼
              OpenAICompatProvider.generate(...)       providers/openai_compat.py
                │  普通模型：POST <base_url>/images/generations
                │  Gemini 图片模型：providers/gemini_image.py 组请求体、解析响应
                ▼
              ImageResult ─▶ 回到节点，转成 IMAGE + MASK
```

`providers/base.py` 定义数据结构和 `Provider` 协议（`chat`、`generate`、`edit` 三个方法）；`ledger.py` 是账本，每次调用追加一行到 `<data_dir>/ledger.jsonl`，按标签前缀统计花费；`models.py` 获取并缓存服务商的模型列表；`config.py` 读配置并加载同目录的 `.env`。

**批量**在 `src/game_art/batch/`：`manifest.py`（清单格式和校验）、`store.py`（批次目录里的文件怎么摆）、`graph.py`（用 LangGraph 写的状态图，流程定义在 `build_graph`）。它和单次运行的衔接点是 `build_workflow`：把这一项的描述填到 `expand.brief`，候选数填到 `gen.n`（或 `edit.n`），风格卡填到 `style.path`。所以批量用的工作流要求节点叫 `expand`、`gen` 或 `edit`、`style`、`save`。

**编辑器里的交互怎么做的**

ComfyUI 的图不能成环，节点之间也只能顺着连线传数据。凡是"下游的结果影响上游的输入"或"点一下弹个东西"的功能，都不走连线，而是由 `comfy_nodes/web/` 下的页面脚本直接改节点上的控件值：

- **质检问题回流**：`GAQCReview.run` 返回 `{"ui": {"ga_qc": [...]}, "result": (...)}`，页面脚本在 `onExecuted` 里拿到 `ui` 部分画面板，状态存在 `node.properties.ga_qc`。点「采纳并重新生成」时，脚本顺着输入连线往上找到 `GAPromptExpand`，把勾选的问题追加到它的 `feedback` 控件，再调用 `app.queuePrompt`。
- **修这张**：`GAQCReview` 的 `image` / `mask` / `meta` 是按需读取的输入（`"lazy": True`）。页面脚本把 `{"file", "notes"}` 写进节点的 `fix` 控件再排队；`check_lazy_status` 看到 `fix` 非空就不向上游要这三个输入，上游的扩写和出图不执行。节点改从临时目录 `game_art_qc/` 取上次存下的原图和记录（每次质检都会存一份）。`fix_instruction`（模板 `prompts/fix.md`）让看图模型**对着这张图**写完整指令：先如实描述要保留的画面，再写具体改动，末尾由代码拼上风格卡的 `positive`。改图请求里原图排第一，`refs` 输入里的标准风格图跟在后面当画风的锚（`fix_with_refs` 可关）。修图的模型、画质、保真度都是节点上的控件（`edit_profile`、`fix_quality`、`fix_fidelity`），作为 `ImageRequest.params` 发出去，优先于配置里给模型写的 `params`；模型不认 `input_fidelity` 时去掉它重试一次。使用者不想为这些事去改配置文件，以后同类的可调项也优先做成节点上的控件。返回的 `ui` 里 `mode` 是 `fix`，脚本据此把新图追加到列表而不是替换。排队后脚本立刻清空 `fix`，所以它只对那一次运行有效。
- **为什么修图要这样设计**（2026-11 实测关羽连修三轮的教训）：只发"原图 + 改动清单"时，指令里没有画风，质检意见里又混着"太卡通""细节太少"这类和 Q 版风格相反的话，每修一轮画风就往写实偏一点，而且下一轮是在已经偏了的图上继续改。所以现在：指令带完整的保留描述和风格卡；带画风锚；质检的问题分 `detail` / `layout`，页面上只把细节类带进修图且一次最多 3 条（`MAX_FIX_ISSUES`）；`qc.md` 明确"风格卡规定的画风特征不是问题"。给节点加可选输入时排在 `optional` 的最后，旧工作流里存的控件值才不会错位。
- **质检结果和保存资产的联动**：两个节点之间没有回传的连线，靠页面脚本对号入座。`qc_review.js` 在每次运行后把"这次产出的是第几张"记在 `state.lastBatch`；紧接着 `save_asset.js` 收到保存结果（`ui.images` 和 `ui.ga_files`），顺着输入连线找到上游的质检结果节点，按顺序把文件记到对应那几条的 `saved` 上（张数对不上就不记）。之后点缩略图时，`showInSave` 把 `app.nodeOutputs[保存节点 id]` 换成这张图，让编辑器重画预览；「另存这张…」调 `/game_art/save_as`。没有 `saved` 的图退回用质检时存在临时目录里的原样（`path`）。
- **省钱的三个开关**（2026-11 按账本实测加的：带参考图出一张图，输入约占费用的两到三成；质检每次约 1 美分）：
  - 参考图缩小：`GAReferenceImages` 的 `shrink` / `shrink_size`，在节点里用 `imaging.shrink_to` 缩到长边 512 再输出，下游拿到的就是小图。「修这张」的底图取自质检节点自己存的原图，不受影响。
  - 扩写复用：`GAPromptExpand` 的 `reuse`。`steps.expand_key` 把描述、素材种类、风格卡内容、参考图描述、整理后的修订意见、模型组算成指纹，命中时直接返回 `<data_dir>/expand_cache.json` 里存的提示词（最多 300 条）。关着 `reuse` 时仍会写入，所以重新打开后沿用的是最近一版。
  - 主体大小由程序管，不问模型：`imaging.subject_ratio` 按透明度（不低于 `SUBJECT_ALPHA`）量主体的高度占比，随质检结果一起发给页面（`subject`，不透明的图是 `null`）；`prompts/qc.md` 里明确要求模型不评价主体大小，因为它估不准。「调整大小」把 `{"file", "resize": true}` 写进 `fix` 再排队，节点调 `imaging.fit_subject`（裁出主体、等比例缩放、居中贴回原尺寸画布），不调任何模型，`ui` 的 `mode` 是 `fix` 并带 `resized: true`，页面脚本的 `appendFix` 把原来那张的得分和问题沿用过来。命令行的 `ga fit` 用的是同一个函数。主体很宽时 `fit_subject` 以宽度不超过画面的 `MAX_SUBJECT_WIDTH` 为准，高度到不了目标；`imaging.subject_target` 算出这张图实际能到几成（`subject_target`），页面和 `ga fit` 都拿它而不是目标值来判断"要不要调"，否则会一直提示调整却调不动。差多少算偏离是节点参数 `subject_tolerance`，随结果发给页面（`tolerance`）；负数表示不检查，页面不显示占比也不出按钮（`subjectChecked`）。占比是按整个不透明范围量的，举着长兵器的角色身体会比空手的略小，这是已知的局限。
  - 质检按需：`GAQCReview` 的 `auto_qc`。关掉后结果里 `score` 是 `null`；「质检这张」把 `{"file", "qc": true}` 写进 `fix` 再排队，节点只给那一张调 `qc_score`，输出用 `ExecutionBlocker` 拦住下游（否则保存节点会把同一张图再存一遍），`ui` 的 `mode` 是 `qc`，页面脚本用 `applyQC` 把分数填回原来那一条。
- **描述清单**：网页没有 yaml 解析器，也拿不到拖入文件的路径，所以清单交给后台的 `/game_art/briefs` 解析。选中一项后脚本把描述写进 `brief` 控件，后台运行时只认 `brief`；`brief` 为空且给了 `item` 时才自己读文件（命令行用法）。
- **拖入的文件**：拿不到完整路径时，控件里写 `拖入:<文件名>`（Python 和 JS 两边的 `DROPPED_PREFIX` 要一致），内容存在编辑框或 `node.properties` 里。
- **节点上的自定义面板**用 `node.addDOMWidget`，并设 `serialize: false`，不占 `widgets_values` 的位置，也不随任务发给后台。面板的宽度要自己卡住：编辑器给面板的宽度取的是 `node.width`，在新版前端里它是"节点大小和内容量出来的大小里较大的那个"，节点被拖窄后会比画出来的节点宽，内容冲出右边。`qc_review.js` 在 `onDraw` 里把面板的 `max-width` 设成 `node.size[0]` 减去两侧边距。

页面脚本里的纯函数（如 `buildState`、`selectedLines`、`mergeFeedback`、`menuLabel`）都 `export` 出来了，改动后可以在浏览器里用假的 `app` / `api` 模块加载脚本做验证。

**想改 X，该动哪里**

| 想做的事 | 改哪里 |
|---|---|
| 换模型、换厂商、调画质档 | 配置文件，不用改代码 |
| 调整 AI 扩写提示词的方式 | `src/game_art/prompts/expand.md` |
| 增减资产类型 | `steps.py` 的 `ASSET_TYPES` |
| 给节点加参数 | `comfy_nodes/nodes/<节点>.py` 里的 `INPUT_TYPES` 和 `run`；重启 ComfyUI，再 `ga comfy export-editor` |
| 新增节点 | 在 `comfy_nodes/nodes/` 新建一个文件写节点类（公共输入定义和辅助函数在 `comfy_nodes/common.py`），再到 `comfy_nodes/nodes/__init__.py` 里登记 |
| 改风格卡的字段 | `style.py` 的 `StyleCard` |
| 改描述清单的字段 | `briefs.py` 的 `BriefList` / `BriefItem`；接口在 `routes.py` 的 `briefs_items` |
| 调整质检的标准和问题的写法 | `src/game_art/prompts/qc.md`；共性问题的归并在 `prompts/common_issues.md` |
| 改合成版式 | `examples/layouts/` 下的 YAML，不用改代码 |

改了 `comfy_nodes/` 或核心库的 Python 代码后要重启 ComfyUI；只改配置时刷新页面即可。

边读边验证：`ga run <工作流> --set ... --dry-run` 看补丁后的工作流；`curl -s http://127.0.0.1:8188/api/object_info/GACloudGenerate` 看节点向 ComfyUI 声明了哪些输入；`tests/` 下每个模块都有同名测试，测试名基本就是行为说明。

## 扩展

**新增厂商**

1. 在 `src/game_art/providers/` 新建一个类，实现 `providers/base.py` 里的 `Provider` 协议，参考 `openai_compat.py`。要求：返回 URL 时自动下载、数量不足时 `partial=True`、返回的不是图片时报上游错误并带报文片段。
2. 在 `providers/registry.py` 的 `get_provider` 里为新的 `type` 增加一个分支。
3. 在配置里增加 `providers.<名>`，把 `<名>:<模型>` 放进 `profiles` 和 `pricing`。
4. 在 `tests/providers/` 补测试。业务代码里不要出现厂商名或模型名。

**新增工作流**

1. 在 ComfyUI 编辑器里搭好，用"导出（API）"保存为 API 格式 JSON，放进 `workflows/`。
2. 把节点 id 改成可读名（如 `expand`、`gen`、`save`），给每个节点设 `_meta.title`。要用于批量时，节点名按上文的约定取。
3. 仓库内的资源路径写成 `@repo/...`，不要写绝对路径。
4. 所有 `tag` 输入留空字符串：`ga run` 和批量会自动填入，账本按 tag 前缀统计预算。
5. `ga run <工作流> --dry-run --set ...` 验证，再 `ga comfy export-editor` 生成编辑器格式。

## 命令行细节

命令在仓库根目录执行，前缀 `uv run`。所有命令都支持 `--json`：stdout 恰好一个 JSON 对象，进度和错误走 stderr。

| 目的 | 命令 |
|---|---|
| 检查配置和环境 | `ga config check` |
| 把节点和工作流装进 ComfyUI | `ga comfy install [--comfy-root P]` |
| 重新生成编辑器格式工作流 | `ga comfy export-editor` |
| 续等已提交的任务 | `ga comfy wait <prompt_id> [--timeout S] [--out D]` |
| 文案 → 资源 | `ga run workflows/text_to_asset.json --set expand.brief=<描述> --set gen.n=1` |
| 参考图 + 文案 → 资源 | `ga run workflows/ref_to_asset.json --set ref.paths=<图片路径> --set expand.brief=<描述>`（多张用分号隔开） |
| 风格样张 | `ga run workflows/style_samples.json --set expand.brief=<描述>` |
| 无缝贴图 | `ga run workflows/seamless_texture.json --set expand.brief=<描述>`（出图时 `transparent=false`，再过 `GAMakeSeamless`） |
| 模板合成 | `ga run workflows/compose_template.json --set 'art.image=@upload{<图>}' --set 'compose.vars_json={"name":"萬"}'`（先按 `examples/fonts/README.md` 下载示例字体） |
| 只看补丁后的工作流 | `ga run <工作流> --set ... --dry-run`（不联网、不花钱） |
| 批量开始 | `ga batch start <清单.yaml> [--out D]` |
| 批次状态 | `ga batch status <批次目录>` |
| 挑图 | `ga batch select <批次目录> 1 3 5` 或 `--item warrior=1,2 --item granary=3` |
| 重跑 | `ga batch rerun <批次目录> <项...> [--brief 新描述]` |
| 续跑 / 提高预算 | `ga batch resume <批次目录> [--budget 新上限]` |

`ga run` 的要点：

- `--set <节点>.<输入>=<值>` 可重复。节点或输入写错会报错并列出可用的名字。原值是字符串时按原文传入，是数字、布尔、列表时按 JSON 解析。
- `@upload{<本地路径>}` 会先把文件上传到 ComfyUI；`@repo/...` 表示仓库根目录下的路径。
- `--out` 默认 `./outputs/<日期>/<tag>`；`--timeout` 默认 600 秒，超时**不会**取消 ComfyUI 里的任务，用 `ga comfy wait` 续等。
- `gen.seed` 默认不发给上游（多数图片接口不支持），只用于 ComfyUI 的缓存判断：`-1` 每次都重新出图，固定值在输入不变时复用上次结果。

退出码：

| 码 | 含义 | 处理 |
|---|---|---|
| 0 | 成功 | |
| 2 | 用法错误 | 对照 `--help` 修正 |
| 3 | 本地输入错误（配置、缺密钥、文件、`--set` 路径、挑选编号等） | 按错误信息修正 |
| 4 | 上游错误（云端或 ComfyUI 不可达、报错） | `ga config check` 定位；错误里会列出每个目标的原因 |
| 5 | 等待超时，任务**未**取消 | `ga comfy wait <prompt_id>`；批量用 `ga batch resume` |
| 6 | ComfyUI 执行失败 | 看错误详情，常见是节点缺依赖 |
| 9 | 预算超限，批次已暂停 | `ga batch resume <目录> --budget <新上限>` |
| 130 | 用户中断 | 批量用 `resume` 续跑 |

`--json` 失败时 stdout 为 `{"ok": false, "error": {"code", "message", "details"}}`。

## 批量流程细节

一次出一整套：逐项出候选图 → 自动质检 → 不合格自动重跑 → 拼成一张编号总览图 → 等你挑选 → 定稿。

```
generate ──▶ qc ──▶ route ──┐ 还有没处理完的项：回到 generate
                     │      │
                     ▼ 全部处理完
                   sheet ──▶ select（停下来等你挑） ──▶ finalize
```

清单格式见 `examples/manifests/smoke.yaml`：`batch_id`、`workflow`、`style_card`、`candidates`（每项候选数）、`qc_threshold`（及格线，默认 7）、`max_retries`、`budget`（美元）、`defaults`、`items[].{id, brief, sets}`。项 `id` 只能含字母、数字、汉字和 `_ - .`，不能以 `_<数字>` 结尾，不能重复。

批次目录：

```
<out>/<batch_id>/              默认 out = ./outputs/batches
  batch.json                   启动时冻结的清单
  state.sqlite                 断点
  items/<项>/<第几次>/          候选 PNG 和元数据
  sheets/selection.png         挑选用的总览图（带全局编号）
  final/<项>.png               定稿图和同名 .json；同一项多选时是 <项>_1.png、<项>_2.png
  manifest.json                最终清单（含费用）
  archive/<项>/<n>/            重跑时挪走的旧图
```

- **断点续跑**：进程被杀、关机、Ctrl-C 之后 `ga batch resume <目录>`，已完成的项不会重复花钱，已提交到 ComfyUI 的任务会续等而不是重新提交。
- **预算**：每项出图前检查累计费用，超过后暂停（退出码 9）。是"超过后才暂停"，实际花费可能略超上限。
- **挑选**：每个出图成功的项至少选一张；质检多次不过（`needs_review`）和出图失败（`failed`）的项不会出现在总览图上，可以 `rerun`。
- **重跑**：`ga batch rerun` 只在等待挑选或已完成时可用，旧图移到 `archive/`，总览图和编号会重新生成。

## 替使用者操作 `ga` 命令

使用者用自然语言提需求时，把它翻译成 `ga` 命令、执行、把结果拿给他看。约定如下。

**基本规则**

- 命令在仓库根目录执行，前缀 `uv run`，默认加 `--json` 并解析 stdout 里的 JSON；不确定用法时先 `ga <子命令> --help`。
- **绝不读取、打印、复述或索要用户的密钥。** 不要查看 `~/.game_art/.env`，不要输出相关环境变量，不要把密钥写进命令、日志或回复。缺密钥时只告诉用户自己把它写进 `~/.game_art/.env`。
- 图上需要文字或精确数量的图案时，走模板合成，不要让模型画字。
- 首次使用或遇到退出码 3、4 时，先跑 `ga config check`。
- 会花钱的命令（`run`、`batch start`、`batch rerun`）开跑前，说明一次大致规模（张数 × 单价）。
- 提高预算、重跑、覆盖文件这类决定由用户做，不要自作主张。

**需求怎么翻译成命令**

| 用户说 | 做法 |
|---|---|
| "给我一座粮仓" | `ga run workflows/text_to_asset.json --set expand.brief=<描述> --set gen.n=1 --json` |
| 给了一张图，"做同风格的灯笼" | `ga run workflows/ref_to_asset.json --set ref.paths=<绝对路径> --set expand.brief=<描述> --json` |
| "先看看风格" | `ga run workflows/style_samples.json --set expand.brief=<描述> --json` |
| "把这张图做成牌面，字写'萬'" | `ga run workflows/compose_template.json --set 'art.image=@upload{<路径>}' --set 'compose.vars_json={"name":"萬"}' --json` |
| 给了一批物品 | 写一份清单 YAML，**先给用户确认**（建议总是设 `budget`），再 `ga batch start <清单> --json` |
| "换一种画风" | 新写一张风格卡，`--set style.path=<卡路径>` |
| "照这几张图的画风来" | 在出图命令上加 `--set 'ref.paths=<图1>;<图2>'`（`text_to_asset`、`style_samples` 也支持） |
| "脚下有淡阴影 / 抠图有雾边" | 加 `--set save.clean_alpha=96` |

常用可调项：`expand.asset_type`、`gen.size`、`gen.n`、`gen.profile`（用哪个模型）、`ref.paths`（参考图）、`bg.method`、`save.clean_alpha`、`save.subfolder`（保存位置，可写完整路径）。完整场景要把 `expand.asset_type` 设为 `illustration` 或 `background`，并保持 `bg.method=none`。不确定有哪些键时用 `--dry-run` 看全貌。

每次出图后：把图片给用户看，说明用了什么描述和风格、文件在哪、估算费用多少（来自同名 `.json` 或批次的 `cost`；是按配置单价的估算）。

**批量的挑图流程**

1. `ga batch status <批次目录> --json`，关注 `stage`、`items[].status`、`numbers`、`sheet_path`、`cost`、`budget`。
2. 把总览图 `sheet_path` 展示给用户，同时用文字列出编号表（编号、所属项、质检分）。图上是全局编号。
3. 请用户回复编号。每个状态为 `generated` 的项都必须至少选一张。
4. 提交：`ga batch select <批次目录> 1 3 5`，或按项 `--item warrior=1,2 --item granary=3`。
5. 退出码 3（`bad_selection`）表示编号不存在、有项漏选或编号不属于该项，状态不会改变。把原因转述给用户，请其重选。
6. 成功后 `stage=finished`，成品在 `final/`，汇总在 `manifest.json`。告诉用户路径和总费用。

用户说"第 3 张不行重来"时，先用编号表换算出它属于哪一项，再 `ga batch rerun <批次目录> <项> [--brief "新描述"]`。`--brief` 会应用到所有指定的项，不同的新描述要分开调用。重跑后编号会变，要重新展示总览图。

**遇到暂停和出错**

- 退出码 9（预算超限）：汇报已花多少、预算多少、还剩几项，问用户是否提高预算。用户同意后 `ga batch resume <目录> --budget <新上限>`。
- 退出码 5（超时）：任务还在 ComfyUI 里跑。用 `ga comfy wait <prompt_id>` 续等，**不要重新提交**，否则会重复花钱。
- 退出码 4：ComfyUI 没开就请用户启动；云端报错时错误信息里有每个目标的原因，照实转述。
- `batch_locked`：另一个 `ga batch` 进程还在跑，等它结束。`batch_exists`：批次目录已存在，改用 `status` / `resume`，或换一个 `batch_id`。
- 汇报错误时说清楚：发生了什么、已经做了什么、还需要用户做什么。

## 已知限制

- **Gemini 图片模型**不支持透明背景和遮罩局部重绘；需要透明底时接"去背景"节点。
- **无缝贴图只适合没有主体的材质**：`make_seamless` 分两遍做，先左右后上下：每一遍把图沿这个方向错开半张，再和没错开的图融合（靠近两边用错开后的图，中间用没错开的图，过渡带宽度是边长的 `blend` 倍）。不要改成一次同时错开两个方向，那样错开后的图中间的十字接缝会在四边露出来。草地、土地、石板、水面效果好；图上有单独的物体，或纹理有明显方向（斜向木纹、透视的砖墙）时，过渡带里会出现重影，这类图要换描述重出，不是调 `blend` 能解决的。
- **预算是"超过后暂停"**，不是硬上限，最多会超出约"一项出图加质检"的费用。
- **费用是估算**：按配置里的单价计算，没写单价的模型记为 0。以服务商账单为准。
- **预算核算要求 ComfyUI 和命令行共用同一份配置**（同一个 `data_dir`），否则账本会分叉。
- **模板合成的文字需要自备字体**：字体文件不随仓库分发。示例版式引用的思源宋体要先按 `examples/fonts/README.md` 下载，否则会报"字体文件不存在"。`font: default` 是 Pillow 自带字体，没有中文字形。
- **本机模型只能用于文字类节点。**
