# ComfyUI-GameArt

基于 ComfyUI、厂商无关的游戏美术 AI 工作流：把"一句描述 / 一张参考图"变成可以直接入库的游戏美术资源。

- **云端模型负责创意**：扩写提示词、出图、改图、看图分析、质检打分。
- **ComfyUI 负责编排和本地处理**：去背景、模板合成、保存。每一步都是画布上的一个节点，参数带中文说明。
- **文字和精确图案不让 AI 画**：带字的图（牌面、徽章、按钮）由模板按版式程序化合成。
- **不绑定厂商**：厂商和模型名只出现在配置文件里。任何 OpenAI 兼容接口都能用，另外支持 Gemini 原生出图接口。
- **可审计**：每张图旁边有一份同名 `.json`（提示词、模型、种子、估算费用），每次云端调用都记入账本，批量任务可以设预算上限。

三种用法，背后是同一套节点和工作流：

| 用法 | 适合 | 入口 |
|---|---|---|
| ComfyUI 编辑器 | 调参数、看中间结果、搭新流程 | 侧边栏"工作流"里的 `game_art/` |
| 命令行 `ga` | 重复出图、脚本化、批量 | `uv run ga run ...`、`uv run ga batch ...` |
| 对话驱动 | 用自然语言提需求，让 AI 助手替你敲命令 | 见[对话驱动](#对话驱动) |

## 快速开始

第一次在一台新机器上用，按下面 7 步走，大约 10 分钟。

**准备：** 装好 [ComfyUI Desktop](https://www.comfy.org/download) 并能正常打开；装好 [uv](https://docs.astral.sh/uv/)；有一个 OpenAI 兼容接口的密钥（示例配置按 aihubmix 写，换成别家只改配置）。

1. 拿到本仓库，进入目录，安装依赖：

   ```bash
   git clone https://github.com/huxiaohei/ComfyUI-GameArt.git
   cd ComfyUI-GameArt
   uv sync
   ```

2. 复制示例配置：

   ```bash
   mkdir -p ~/.game_art && cp config/config.example.yaml ~/.game_art/config.yaml
   ```

3. **改 `~/.game_art/config.yaml` 里的 `comfy_root`**，写成你机器上 ComfyUI 的目录（里面有 `custom_nodes/` 和 `main.py` 的那一层）。不改的话第 5 步会报"ComfyUI 根目录不存在"。

4. 新建 `~/.game_art/.env`，写入自己的密钥和接口地址（不要提交、不要发给别人）：

   ```
   COMFYUI_LLM_BASE_KEY=sk-...
   COMFYUI_LLM_BASE_URL=https://aihubmix.com/v1
   ```

5. 把节点和工作流装进 ComfyUI，然后**重启 ComfyUI**：

   ```bash
   uv run ga comfy install
   ```

6. 保持 ComfyUI 开着，检查一遍。四项都是 ✓ 才算装好，有 ✗ 时按输出里的修复建议处理：

   ```bash
   uv run ga config check
   ```

7. 出第一张图（会调用云端模型，按示例配置约 0.04 美元）：

   ```bash
   uv run ga run workflows/text_to_asset.json --set expand.brief=一枚国潮风铜钱图标 --set gen.n=1
   ```

   命令结束时会打印图片保存的位置。也可以在 ComfyUI 编辑器左侧"工作流"里打开 `game_art/text_to_asset`，填好"提示词扩写 · expand"节点的 `brief` 后点运行。

## 工作流和节点

仓库自带四条工作流：

| 工作流 | 做什么 | 流程 |
|---|---|---|
| `text_to_asset` | 文案（+ 可选参考图）→ 资源 | 风格卡 → 提示词扩写 → 云端生图（参考图节点里有图时按参考图生图）→ 去背景 → 保存 |
| `ref_to_asset` | 参考图 + 文案 → 资源 | 参考图 → 参考图分析 + 风格卡 → 提示词扩写 → 云端改图 → 去背景 → 保存 |
| `style_samples` | 先看风格再定：一次出 4 张样张 | 风格卡 → 提示词扩写 → 云端生图（可带参考图）→ 保存 |
| `compose_template` | 把素材图和文字按版式合成一张成品 | 载入素材 → 模板合成 → 保存 |

十个节点（节点菜单里的 `GameArt` 分类）：

| 节点 | 作用 | 调云端 |
|---|---|---|
| 风格卡 `GAStyleCard` | 读入画风定义（YAML 文件，或直接在节点上编辑） | 否 |
| 参考图 `GAReferenceImages` | 载入一张或多张参考图（一行一个路径），供生图、改图、分析使用 | 否 |
| 提示词扩写 `GAPromptExpand` | 把一两句大白话扩写成完整的正向/反向提示词 | 文字模型 |
| 参考图分析 `GAReferenceAnalyze` | 把参考图描述成文字，供扩写使用 | 看图模型 |
| 云端生图 `GACloudGenerate` | 按提示词出图；接了参考图时按参考图生图 | 出图模型 |
| 云端改图 `GACloudEdit` | 以参考图为基础出图或改图 | 出图模型 |
| 去背景 `GARemoveBackground` | 本机抠图得到透明底 | 否 |
| 质检打分 `GAQCScore` | 给成品打分并给出理由 | 看图模型 |
| 模板合成 `GAComposeTemplate` | 按版式文件把底框、图案、文字拼成成品 | 否 |
| 保存资产 `GASaveAsset` | 写出 PNG 和同名 `.json` 元数据；可选清理半透明残留 | 否 |

**参数说明：** 每个参数都带中文说明，鼠标停在参数上会显示，选中节点后右侧"信息"页也有。资产类型、尺寸、去背景方式、模型都是下拉选项。

**模型下拉**里有两类选项：不带冒号的（`text`、`image` 等）是配置里的"模型组"，按顺序尝试、失败自动换下一个；带冒号的（`aihubmix:gpt-4o`）是直接指定一个模型。带冒号的选项包括配置里用到的模型，以及各服务商 `/models` 接口返回的全部模型（自动获取，每小时更新）。列表不区分用途，给出图节点选了聊天模型，运行时会报错并带上服务商给出的原因。

**风格卡**字段：`name`、`description`（必填），`palette`、`positive`、`negative`、`rules`（可选，留空表示不设置）。可以读文件，也可以在节点的 `edit` 框里直接写；编辑框里有内容时以它为准。也可以把 `.yaml` 文件直接拖到节点上：能拿到完整路径时会填进 `path`；拿不到时（浏览器不向网页透露文件位置）会把文件内容放进编辑框，`path` 显示成"拖入:文件名"（只用来标明这张卡的来源，不再读文件），之后文件有改动需要重新拖一次。示例见 `examples/style_cards/guochao.yaml`。

**参考图**节点有两种给图的方式，可以混用，两边的图会合在一起发给模型：

- **拖图片**：在编辑器里把一张或多张图片文件直接拖到节点上，每张图会自动生成一个「加载图像」节点（带缩略图）并连到本节点。节点上始终留一个空的图片接线口，接满一个再多出一个，最多 8 个。拖进来的图会被上传（复制）到 ComfyUI 的 `input` 目录，之后改动原文件不会跟着变。
- **写路径**：`paths` 一行一个图片路径，可以写多张，留空表示不用。点运行时会先检查这些文件是否存在，有问题当场报错并指出是哪一个，不会等前面的节点跑完才发现。

`paths` 的写法：可以写完整路径、`@repo/...`，或只写 ComfyUI `input` 目录里的文件名；空行和 `#` 开头的行会被忽略。参考图的先后顺序是 `paths` 里的在前，接线口进来的在后。输出的 `refs` 接到「云端生图」「云端改图」「参考图分析」。「云端生图」接了参考图且里面有图时，会改走"按参考图生图"，用它的 `edit_profile` 模型组。模型分不清"参考画风"和"参考内容"，参考图里最显眼的人物、道具、姿势容易被照搬；只想统一画风时，放几张内容不同、画风相同的图，并在描述里写明哪些地方按文字重新设计。风格卡不再支持 `reference_images` 字段，旧风格卡里有这一项时会报错并提示改用本节点。

**清理半透明残留**：「保存资产」的 `clean_alpha`（0–255，默认 0 不清理）。大于 0 时，透明度低于这个值、又不紧贴主体的像素会被擦成全透明，用来去掉模型画在主体脚下的淡阴影和抠图留下的雾边，淡阴影一般用 64–128。只对带透明底的图有效；薄纱、烟雾、光晕这类本来就该半透明的素材保持 0。用了它时，数值会写进图片旁边的 `.json`。

**保存位置**：「保存资产」的 `subfolder` 有两种写法。相对路径（如 `ga/text_to_asset`）是 ComfyUI 输出目录下的子文件夹；完整路径（如 `/Users/me/project/art`）是电脑上的任意文件夹，图片和 `.json` 直接存到那里，比如游戏项目的美术目录，这时输出目录里不留副本，编辑器里的预览用的是临时目录里的一份拷贝。在编辑器里点一下 `subfolder` 这一栏，会弹出系统自带的文件夹选择窗口；选中的文件夹在输出目录里面时自动写成相对路径，其余写完整路径。想手动输入时按住 Shift 再点，或者在右侧的属性面板里改。不支持把文件夹拖到节点上，因为网页拿不到被拖入文件夹在硬盘上的位置。保存位置或 `name` 写错时，点运行就会报错，不会等图出完才发现。`ga run` 只会把输出目录里的成品下载到 `--out`；`subfolder` 写了完整路径时，成品已经在那个文件夹里，不再下载。

**资产类型**：`icon`、`prop`、`character`、`ui_element`、`background`、`illustration`。完整场景选 `background` 或 `illustration`，其余类型会要求主体居中、背景干净。

**两种工作流格式：** `workflows/*.json` 是 API 格式，给 `ga run` 和批量用，是唯一的事实来源。`workflows/editor/*.json` 是由它生成的编辑器格式，侧边栏打开的是这一份。改了 API 格式文件后，在 ComfyUI 运行时执行 `ga comfy export-editor` 重新生成。在编辑器里做的修改不会回写到 API 文件，需要用"导出（API）"手动保存。节点标题里"·"后面的就是 `--set` 用的节点名（如"云端生图 · gen"）。

## 配置

配置查找顺序：环境变量 `GAME_ART_CONFIG` → `~/.game_art/config.yaml` → 仓库内 `config/config.example.yaml`。示例配置里每一项都有注释。

**密钥和接口地址写在 `~/.game_art/.env`，不要写进 `config.yaml`。** 从 Finder / Dock 启动的 ComfyUI Desktop 不会继承 shell 里 `export` 的环境变量，而 `.env` 对命令行和 ComfyUI 两边都生效。shell 里已有的同名环境变量优先于 `.env`；两边值不一致会导致"命令行和 ComfyUI 行为不同"。

| 配置项 | 含义 |
|---|---|
| `providers.<名>` | 一个服务商：`type`（目前是 `openai_compatible`）、`base_url_env` / `base_url`、`api_key_env`（本机服务不需要密钥时可不写） |
| `providers.<名>.models` | 额外出现在模型下拉里的模型名 |
| `providers.<名>.unlisted_models` | 服务商 `/models` 不列出、但接口实际可用的模型，`ga config check` 对它们放行 |
| `providers.<名>.gemini_image_models` | 走 Gemini 原生出图接口的模型名（支持通配符，如 `gemini-*`） |
| `profiles.<组名>` | 模型组：按顺序尝试的目标列表，写 `provider:model` 或 `{provider, model, params}`，`params` 原样发给上游 |
| `pricing` | 估算费用用的单价，键是 `provider:model`。只是估算，以服务商账单为准 |
| `image_sizes` | 尺寸下拉里的选项，按出图模型支持的尺寸填写 |
| `comfy_url`、`comfy_root` | ComfyUI 的地址和安装目录 |
| `data_dir` | 账本（`ledger.jsonl`）和模型列表缓存的位置，默认 `~/.game_art` |
| `max_retries` | 批量时每项自动重跑的上限 |

**模型名要核对：** 示例里的模型名可能已下线或改名。`ga config check` 会请求 `<base_url>/models` 并核对 `profiles` 里用到的每个模型，`base_url` 写错（如少了 `/v1`）或模型不存在都会报 ✗ 并给出修复建议。

**两类出图接口：** 普通模型走 OpenAI 图片接口（`/images/generations`、`/images/edits`）。写进 `gemini_image_models` 的模型走 Gemini 原生接口：尺寸换算成最接近的宽高比（`1536x1024` → 3:2），一次只出一张（`n` 大于 1 时连续请求），不支持透明背景和遮罩局部重绘。Gemini 接口地址默认由 `base_url` 推出（结尾的 `/v1` 换成 `/gemini`），不一样时用 `gemini_base_url` 指定。

**本机模型**（Ollama、LM Studio 等提供 OpenAI 兼容接口的服务）可以作为一个 provider 加进来，写法见示例配置里的 `local` 注释。只适用于文字类节点；本机出图请用 ComfyUI 自带的采样节点另搭工作流。

**给 ComfyUI 装可选依赖：** ComfyUI 运行在它自己的 Python 环境里，不是 `uv sync` 的环境。去背景用的 `rembg` 要装到那里：

```bash
<comfy_root>/.venv/bin/python3 -m pip install rembg
```

工作流里 `bg.method` 默认 `none`（不去背景）；改成 `rembg` 前必须先装好。

## 命令速查

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
| 模板合成 | `ga run workflows/compose_template.json --set 'art.image=@upload{<图>}' --set 'compose.vars_json={"name":"萬"}'`（先按 `examples/fonts/README.md` 下载示例字体） |
| 只看补丁后的工作流 | `ga run <工作流> --set ... --dry-run`（不联网、不花钱） |
| 批量开始 | `ga batch start <清单.yaml> [--out D]` |
| 批次状态 | `ga batch status <批次目录>` |
| 挑图 | `ga batch select <批次目录> 1 3 5` 或 `--item coin=1,2 --item cloud=3` |
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

## 批量

一次出一整套：逐项出候选图 → 自动质检 → 不合格自动重跑 → 拼成一张编号总览图 → 等你挑选 → 定稿。

```
generate ──▶ qc ──▶ route ──┐ 还有没处理完的项：回到 generate
                     │      │
                     ▼ 全部处理完
                   sheet ──▶ select（停下来等你挑） ──▶ finalize
```

清单格式见 `examples/manifests/smoke.yaml`：`batch_id`、`workflow`、`style_card`、`candidates`（每项候选数）、`qc_threshold`（及格线）、`max_retries`、`budget`（美元）、`defaults`、`items[].{id, brief, sets}`。项 `id` 只能含字母、数字、汉字和 `_ - .`，不能以 `_<数字>` 结尾，不能重复。

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

## 对话驱动

不想记命令时，可以让 AI 编程助手替你操作：你用自然语言提需求，助手把它翻译成 `ga` 命令、执行、把结果拿给你看。任何能读文件、能在终端执行命令的助手都可以，做法是让它先读本节。

下面是写给助手的操作约定。

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
| "给我一枚铜钱图标" | `ga run workflows/text_to_asset.json --set expand.brief=<描述> --set gen.n=1 --json` |
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
4. 提交：`ga batch select <批次目录> 1 3 5`，或按项 `--item coin=1,2 --item cloud=3`。
5. 退出码 3（`bad_selection`）表示编号不存在、有项漏选或编号不属于该项，状态不会改变。把原因转述给用户，请其重选。
6. 成功后 `stage=finished`，成品在 `final/`，汇总在 `manifest.json`。告诉用户路径和总费用。

用户说"第 3 张不行重来"时，先用编号表换算出它属于哪一项，再 `ga batch rerun <批次目录> <项> [--brief "新描述"]`。`--brief` 会应用到所有指定的项，不同的新描述要分开调用。重跑后编号会变，要重新展示总览图。

**遇到暂停和出错**

- 退出码 9（预算超限）：汇报已花多少、预算多少、还剩几项，问用户是否提高预算。用户同意后 `ga batch resume <目录> --budget <新上限>`。
- 退出码 5（超时）：任务还在 ComfyUI 里跑。用 `ga comfy wait <prompt_id>` 续等，**不要重新提交**，否则会重复花钱。
- 退出码 4：ComfyUI 没开就请用户启动；云端报错时错误信息里有每个目标的原因，照实转述。
- `batch_locked`：另一个 `ga batch` 进程还在跑，等它结束。`batch_exists`：批次目录已存在，改用 `status` / `resume`，或换一个 `batch_id`。
- 汇报错误时说清楚：发生了什么、已经做了什么、还需要用户做什么。

## 代码导读

约 4000 行 Python，分四块：

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
| 编辑器里的脚本 | `comfy_nodes/web/` | `style_card.js`：把风格卡文件内容读进编辑框，对应的接口在 `comfy_nodes/routes.py`。`reference_images.js`：把拖到参考图节点上的图片变成「加载图像」节点并连好线。`save_asset.js`：点 `subfolder` 时调用 `routes.py` 里弹系统选择窗口的接口 |

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
| `GAPromptExpand` | `expand_prompt` | `steps.py`，模板在 `prompts/expand.md` |
| `GAReferenceAnalyze` | `analyze_reference` | `steps.py`，模板在 `prompts/analyze.md` |
| `GACloudGenerate` / `GACloudEdit` | `router.generate` / `router.edit` | `providers/router.py` |
| `GARemoveBackground` | `remove_background` | `imaging.py` |
| `GAQCScore` | `qc_score` | `steps.py`，模板在 `prompts/qc.md` |
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

**想改 X，该动哪里**

| 想做的事 | 改哪里 |
|---|---|
| 换模型、换厂商、调画质档 | 配置文件，不用改代码 |
| 调整 AI 扩写提示词的方式 | `src/game_art/prompts/expand.md` |
| 增减资产类型 | `steps.py` 的 `ASSET_TYPES` |
| 给节点加参数 | `comfy_nodes/nodes/<节点>.py` 里的 `INPUT_TYPES` 和 `run`；重启 ComfyUI，再 `ga comfy export-editor` |
| 新增节点 | 在 `comfy_nodes/nodes/` 新建一个文件写节点类（公共输入定义和辅助函数在 `comfy_nodes/common.py`），再到 `comfy_nodes/nodes/__init__.py` 里登记 |
| 改风格卡的字段 | `style.py` 的 `StyleCard` |
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

## 已知限制

- **Gemini 图片模型**不支持透明背景和遮罩局部重绘；需要透明底时接"去背景"节点。
- **预算是"超过后暂停"**，不是硬上限，最多会超出约"一项出图加质检"的费用。
- **费用是估算**：按配置里的单价计算，没写单价的模型记为 0。以服务商账单为准。
- **预算核算要求 ComfyUI 和命令行共用同一份配置**（同一个 `data_dir`），否则账本会分叉。
- **模板合成的文字需要自备字体**：字体文件不随仓库分发。示例版式引用的思源宋体要先按 `examples/fonts/README.md` 下载，否则会报"字体文件不存在"。`font: default` 是 Pillow 自带字体，没有中文字形。
- **本机模型只能用于文字类节点。**

## 开发

```bash
uv run pytest -q -W error                          # 默认跳过 e2e；任何告警都视为失败
uv run mypy                                        # 严格模式：所有函数和参数必须有类型标注
uv run ruff check                                  # 代码检查
GA_E2E=1 uv run pytest -m e2e tests/e2e -s         # 真实 ComfyUI + 密钥，会花钱
```

约定：提交前三项检查都必须 0 错误、0 告警；不使用 `# type: ignore` / `# noqa` 压制。

## 许可

代码以 [MIT 协议](LICENSE)发布。
