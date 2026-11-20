# ComfyUI-GameArt

基于 ComfyUI 的游戏美术 AI 工作流：把"一句描述 / 一张参考图"变成可以直接入库的游戏美术资源。

- **云端模型负责创意**：扩写提示词、出图、改图、看图分析、质检。
- **ComfyUI 负责编排和本地处理**：去背景、模板合成、保存。每一步是画布上的一个节点，参数带中文说明。
- **不绑定厂商**：厂商和模型名只写在配置文件里，任何 OpenAI 兼容接口都能用，另外支持 Gemini 原生出图接口。
- **可追溯**：每张图旁边有一份同名 `.json`，记录描述、提示词、模型、质检结果和估算费用。

主要用法是在 **ComfyUI 编辑器**里一张一张地出图、看质检结果、按问题修订。也可以用命令行 `ga` 重复出图或批量出图。

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

## 在编辑器里出图

打开 ComfyUI，左侧"工作流"里有 `game_art/` 文件夹。**打开后先"另存为"自己的工作流再改**：这个文件夹直接指向本仓库的模板文件，原地保存会覆盖模板。

| 工作流 | 用来做什么 |
|---|---|
| `text_to_asset_qc` | **推荐。** 描述（+ 可选参考图）→ 出图 → 质检 → 按问题修订 → 保存 |
| `text_to_asset` | 同上，不带质检 |
| `ref_to_asset` | 以参考图为基础改图：参考图 + 描述 → 出图 → 保存 |
| `style_samples` | 定画风用：一次出 4 张样张 |
| `compose_template` | 把素材图和文字按版式合成一张成品（牌面、徽章等带字的图） |

以 `text_to_asset_qc` 为例，从左到右有这几个节点，按顺序设置：

### 1. 风格卡：规定画风

一份 `.yaml` 文件，写清一整套素材共用的画风。把文件拖到「风格卡」节点上，或在 `path` 里填路径；内容会显示在 `edit` 框里，可以直接改。

```yaml
name: 国潮插画风                  # 必填
description: 平涂配色加描金线条…   # 必填，一句话说明
palette: ["#C8372D", "#D4A93C"]   # 可选，色板
positive: 国潮插画, 平涂上色       # 可选，原样拼在提示词后面，适合写每张图都必须遵守的硬性要求
negative: 写实光影, 3D渲染         # 可选，不希望出现的东西
rules:                            # 可选，给扩写模型看的规则
  - 色块之间用细金线勾边
```

完整示例：`examples/style_cards/guochao.yaml`。

### 2. 参考图：统一画风（可选）

把一张或多张图片拖到「参考图」节点上，会自动生成「加载图像」节点并连好线；也可以在 `paths` 里一行写一个路径。留空就是纯文字出图。

参考图里的内容（兵器、姿势）容易被带进新图。只想借画风时，用不带特定道具的标准图，并在描述里写明"只参考画风"。

### 3. 提示词扩写：写要什么

两种写法，任选其一：

- **直接写**：在 `brief` 里用大白话写一两句，例如"一枚国潮风铜钱图标"。
- **从描述清单里选**：把一批素材的描述写在一份 `.yaml` 里，把它拖到「提示词扩写」节点上（或在 `briefs_path` 里填路径），然后点 `item` 选一项。选中后这一项的描述会填进 `brief`，「保存资产」的文件名改成这一项的 `id`，上一项留下的修订意见会被清空。填进来的描述还可以接着改，出图以 `brief` 里的文字为准。

```yaml
asset_type: character               # 可选：整份清单默认的素材种类
prefix: 全身入画，四周留出明显空白。   # 可选：每条描述前面都加的公共文字
items:
  - id: guanyu                      # 用作保存的文件名
    brief: 关羽，绿袍金甲，长须，手持青龙偃月刀。
  - id: arrow_tower
    brief: 木制箭塔，塔顶有垛口。
    asset_type: prop                # 可选：这一项单独指定
```

完整示例：`examples/briefs/demo.yaml`。

`asset_type` 决定构图：`icon`、`prop`、`character`、`ui_element` 会要求主体居中、背景干净；`background`、`illustration` 铺满画面。

写描述的经验：要什么就具体写什么（鞋子、兵器的结构、服饰的形制）；不要写"不要阴影""不要贴边"这类否定句，模型常常照字面把它画出来，改成正面描述（"脚底下方是干净的空白"）。

### 4. 云端生图

`size` 是尺寸，`n` 是一次出几张（按张计费），`transparent` 是要不要透明底。`profile` 选用哪个模型：不带冒号的（`image`）是配置里的一组模型，前一个失败自动换下一个；带冒号的（`aihubmix:gpt-image-1`）是直接指定一个。

### 5. 质检结果：看问题、决定怎么改

运行后节点上会显示每张图的得分和问题。图片不管得分多少都会往下传并保存，不会因为分低而丢图。

- 每条问题前面有一个开关，标着"必须改"或"建议"。一次出多张时，上方是缩略图，点哪张看哪张；下方另有"整批的共性问题"。
- 「我的补充」：质检没看出来、但你想改的地方，一行一条。
- 每条问题还标着"细节"或"构图"。细节类（某个部件画错、饰品不精致、局部配色、线条毛糙）适合在原图上修；构图类（留白、贴边、比例、姿势）要整张重出。
- 「修这张」：在选中的这张图上直接修改。只带这张图勾选的**细节类**问题（一次最多 3 条）和「我的补充」，构图类问题不会带上。改完的图作为新的一张追加在缩略图后面（带 ✎ 标记），原图不动，可以接着再修。这一步不会重新出图，只花一次改图的钱。
- 「采纳并重新生成」：把勾选的问题和补充追加到「提示词扩写」的 `feedback`，然后整条流程再运行一次，整张重出。什么都不勾直接点运行，就是原样再出一版。
- 「清空修订意见」：重新生成的意见是逐轮累积的，想从头再来时点它。
- **回看和定稿**：点任意一张缩略图（包括修之前的旧图），右边「保存资产」节点的预览会跟着切到这张，方便放大对比。「另存这张…」把当前选中的这张图和它的记录复制到你选的文件夹，文件名用「保存资产」的 `name`。修来修去发现还是之前某一张更好时，点回那张再另存即可。

「修这张」的过程：看图模型先对着这张图写一段完整的修改指令（哪些原样保留、哪几处怎么改），末尾拼上风格卡的硬性要求；然后把这张图和「参考图」节点里的标准风格图一起交给改图模型，风格图只用来对照画风，防止连修几轮后越改越偏（节点上的 `fix_with_refs` 可以关掉）。

修图用哪个模型、什么档次，都直接在「质检结果」节点上选，不用改配置文件：

- `edit_profile`：修图用的模型。下拉里带冒号的选项（如 `aihubmix:gpt-image-2.6-sunburst`）是直接指定一个模型，选一个较新的图片模型即可。
- `edit_model`：手动填模型，写成 `服务商:模型名`（如 `aihubmix:gpt-image-2`），填了就用它。有些服务商的模型列表不全，实际能用的模型不一定出现在下拉里，用它来试。
- `fix_quality`：画质档，默认 `high`。
- `fix_fidelity`：对原图的保真度，默认 `high`，模型会更努力地保持原图的画风、脸和细节。模型不支持这个参数时会自动去掉它再试一次。

**对比不同模型**：选中同一张图、勾同样的问题，换一个模型点一次「修这张」。每次的结果都追加在缩略图后面，鼠标停在缩略图上能看到是哪个模型修的，图片旁的 `.json` 里也有记录。

改图模型是把整张图重画一遍，满意的部分大体保留，细微处仍可能有变化，所以一次少改几条、分几轮修，比一次全改效果好。ComfyUI 重启后要先正常运行一次，才能再「修这张」。

### 6. 去背景和保存

- 「去背景」的 `method` 默认 `none`。出图模型不支持透明底时改成 `rembg`，需要先给 ComfyUI 装依赖：`<comfy_root>/.venv/bin/python3 -m pip install rembg`。
- 「保存资产」的 `subfolder`：点一下会弹出系统的文件夹选择窗口，可以直接存到游戏项目的美术目录；按住 Shift 再点是手动输入。相对路径（如 `ga/icons`）表示 ComfyUI 输出目录下的子文件夹。
- `name` 是文件名前缀，自动加编号（`guanyu_00001.png`），不会覆盖已有文件。
- **「另存选中的图…」**：定稿用。在节点的预览里点一下最终要的那张，再点这个按钮，选一个文件夹，图片和它的 `.json` 记录会复制过去，文件名就是 `name`（如 `guanyu.png`，不带编号）。那里已有同名文件时不覆盖，自动存成 `guanyu_2.png`。窗口会停在上次另存的位置。
- `clean_alpha`（0–255，默认 0）：去掉模型画在主体脚下的淡阴影和抠图留下的雾边，一般用 64–128。薄纱、烟雾这类本来就该半透明的素材保持 0。

### 带字的图：模板合成

图上需要文字或精确图案（牌面、徽章、按钮）时，不要让模型画字，用 `compose_template`：先出不带字的素材图，再按版式文件把底框、图案、文字拼起来。版式示例是 `examples/layouts/demo_tile.yaml`，字体要先按 `examples/fonts/README.md` 下载。

## 配置

配置文件是 `~/.game_art/config.yaml`，从 `config/config.example.yaml` 复制而来，每一项都有注释。

**密钥和接口地址写在 `~/.game_art/.env`，不要写进 `config.yaml`。**

| 配置项 | 含义 |
|---|---|
| `providers.<名>` | 一个服务商：接口地址和密钥用哪两个环境变量 |
| `providers.<名>.gemini_image_models` | 走 Gemini 原生出图接口的模型名（支持通配符） |
| `profiles.<组名>` | 模型组：按顺序尝试的模型列表，写 `服务商:模型名`。节点的 `profile` 下拉里选的就是它 |
| `pricing` | 估算费用用的单价。只是估算，以服务商账单为准 |
| `image_sizes` | 尺寸下拉里的选项 |
| `comfy_url`、`comfy_root` | ComfyUI 的地址和安装目录 |

默认的模型组：`text`（扩写）、`vision`（看图分析、质检、写修图指令）、`image`（出图）、`image_edit`（按参考图出图、修图）。

两处值得用好一点的模型：

- **`vision`**：质检的问题准不准、修图指令写得好不好都靠它，放一个看图能力强的模型。
- **修图用的模型**：不在这里配，直接在「质检结果」节点上选模型、画质和保真度。

改完配置后运行 `uv run ga config check`，它会核对每个模型是否真的存在。只改配置时刷新 ComfyUI 页面即可，不用重启。

Gemini 图片模型不支持透明背景，需要透明底时接「去背景」节点。本机模型（Ollama、LM Studio）可以作为服务商加进来，但只能用于文字类节点。

## 命令行

不开编辑器也能出图。命令在仓库根目录执行，前缀 `uv run`，需要 ComfyUI 开着。

| 目的 | 命令 |
|---|---|
| 检查配置和环境 | `ga config check` |
| 把节点和工作流装进 ComfyUI | `ga comfy install` |
| 描述 → 资源 | `ga run workflows/text_to_asset.json --set expand.brief=<描述>` |
| 从描述清单里取一项 | `ga run workflows/text_to_asset.json --set expand.briefs_path=<清单.yaml> --set expand.item=<id>` |
| 参考图 + 描述 → 资源 | `ga run workflows/ref_to_asset.json --set ref.paths=<图片路径> --set expand.brief=<描述>` |
| 风格样张 | `ga run workflows/style_samples.json --set expand.brief=<描述>` |
| 模板合成 | `ga run workflows/compose_template.json --set 'art.image=@upload{<图>}' --set 'compose.vars_json={"name":"萬"}'` |
| 只看参数，不出图 | 在 `ga run` 后面加 `--dry-run`（不联网、不花钱） |

`--set <节点>.<参数>=<值>` 可以写多个，节点名就是编辑器里节点标题"·"后面的那个词。常用的有 `style.path`（风格卡）、`ref.paths`（参考图，多张用分号隔开）、`expand.asset_type`、`gen.size`、`gen.n`、`save.subfolder`、`save.name`、`save.clean_alpha`。所有命令都支持 `--json`。

出错时的退出码：2 用法写错；3 配置、文件或参数有问题；4 云端或 ComfyUI 连不上；5 等待超时（任务还在跑，用 `ga comfy wait <prompt_id>` 续等，不要重新提交）；6 ComfyUI 执行失败；9 批量超出预算。

### 批量

一次出一整套：逐项出候选图 → 自动质检 → 不合格自动重出 → 拼成一张编号总览图 → 等你挑选 → 定稿。适合图标这类数量多、要求相对统一的素材；角色、主视觉这类关键素材建议在编辑器里一张一张做。

```bash
ga batch start examples/manifests/smoke.yaml     # 开始，清单格式见这个示例
ga batch status <批次目录>                        # 看进度和候选编号
ga batch select <批次目录> 1 3 5                  # 按总览图上的编号挑图定稿
ga batch rerun <批次目录> <项> --brief "新描述"    # 个别项不满意，换描述重出
ga batch resume <批次目录> --budget 5            # 中断或超预算后继续
```

清单里可以设 `candidates`（每项出几张候选）、`qc_threshold`（及格线，默认 7）、`budget`（预算上限，美元；超过后暂停）。成品在批次目录的 `final/` 下。

## 示例文件

| 文件 | 是什么 | 用在哪 |
|---|---|---|
| `config/config.example.yaml` | 配置示例，每项有注释 | 复制成 `~/.game_art/config.yaml` |
| `examples/style_cards/guochao.yaml` | 风格卡 | 「风格卡」节点 |
| `examples/briefs/demo.yaml` | 描述清单 | 「提示词扩写」节点的 `briefs_path` |
| `examples/layouts/demo_tile.yaml` | 合成版式 | 「模板合成」节点 |
| `examples/manifests/smoke.yaml` | 批量清单 | `ga batch start` |

## 常见问题

- **改了节点代码后不生效**：Python 改动要重启 ComfyUI；只有 `comfy_nodes/web/` 下的页面脚本刷新页面即可。
- **节点上点了没反应，提示"请重启 ComfyUI"**：页面脚本是新的，后台还是旧的，重启即可。
- **命令行和编辑器行为不一样**：从 Dock 启动的 ComfyUI 读不到终端里 `export` 的环境变量，密钥请统一写在 `~/.game_art/.env`。
- **拖进来的文件改了没更新**：浏览器不告诉网页文件在硬盘上的位置。`path` 显示"拖入:文件名"时，内容是拖的那一刻读进来的，文件改了要重新拖一次，或者填完整路径。
- **图片脚下有淡阴影、边缘有雾**：把「保存资产」的 `clean_alpha` 调到 64–128。
- **费用**：`.json` 里的 `cost` 是按配置单价的估算，没写单价的模型记为 0，以服务商账单为准。

## 开发

实现原理、代码导读和扩展方法写在 [CLAUDE.md](CLAUDE.md)。提交前三项检查必须全部通过：

```bash
uv run pytest -q -W error
uv run mypy
uv run ruff check
```

## 许可

代码以 [MIT 协议](LICENSE)发布。
