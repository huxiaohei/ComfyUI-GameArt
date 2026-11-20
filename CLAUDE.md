# CLAUDE.md

给 Claude 和其他 AI 工具的工作说明。项目介绍、用法和代码导读在 [README.md](README.md)，这里只写在这个仓库里干活的规矩。

## 提交

- **提交信息里不要加 AI 署名**：不写 `Co-Authored-By: Claude …`，不写 `Claude-Session: …`，也不写"Generated with …"之类的行。提交信息只有标题和正文。这条规矩优先于工具自带的默认做法。
- 提交信息用简体中文，标题格式 `类型(范围): 说明`，类型用 `feat`、`fix`、`refactor`、`docs`、`test`、`chore`。
- 按功能分批提交，一个提交只做一件事，每个提交单独都能通过检查。
- 提交前先征得同意；推送由仓库主人在自己的终端执行。

## 提交前的检查

三项都必须 0 错误、0 告警，不用 `# type: ignore`、`# noqa` 压制：

```bash
uv run pytest -q -W error
uv run mypy
uv run ruff check
```

## 改动时要注意

- `workflows/*.json` 是给命令行和批量流程用的 API 格式；`workflows/editor/*.json` 是由它们生成的编辑器格式（`ga comfy export-editor`）。改了节点的输入输出或 API 工作流，要重新生成编辑器格式。
- ComfyUI 侧边栏里的 `game_art/` 就是链接到 `workflows/editor/` 的。不要把带具体项目内容的工作现场提交进来。
- 改了 Python 代码要重启 ComfyUI 才生效；改了 `comfy_nodes/web/` 下的脚本刷新页面即可。改完要提醒使用者。
- 节点的说明文字（`DESCRIPTION`、`tooltip`）是写给不懂技术的美术和策划看的，用大白话。
- 新增节点：在 `comfy_nodes/nodes/` 新建文件，并加进 `comfy_nodes/nodes/__init__.py` 的注册表；同步更新 README 的节点表和代码导读。

## 安全

- 不要读取、打印或索要任何密钥，不要查看 `~/.game_art/.env`。
- 出图和改图要花钱：不要自行运行会调用云端出图模型的命令（包括 e2e 测试），除非使用者明确要求。
