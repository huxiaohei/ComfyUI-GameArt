// 「风格卡」节点：把文件内容直接显示在 edit 编辑框里，方便查看和修改。
// - 打开工作流时，edit 为空就读入文件内容；已经有内容（你改过）则不动。
// - 改了 path，或点「从文件重新载入」，用文件内容替换 edit。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const widget = (node, name) => node.widgets?.find((w) => w.name === name);

async function fill(node, force) {
  const path = widget(node, "path");
  const edit = widget(node, "edit");
  if (!path || !edit) return;
  const value = String(path.value ?? "").trim();
  if (!value || (!force && String(edit.value ?? "").trim())) return;
  try {
    const res = await api.fetchApi(`/game_art/style_card?path=${encodeURIComponent(value)}`);
    const data = await res.json();
    if (!res.ok) {
      if (force) app.extensionManager?.toast?.add({ severity: "warn", summary: "风格卡", detail: data.error, life: 6000 });
      return;
    }
    edit.value = data.text;
    node.setDirtyCanvas(true, true);
  } catch (err) {
    console.warn("[game_art] 读取风格卡失败", err);
  }
}

app.registerExtension({
  name: "GameArt.StyleCard",
  nodeCreated(node) {
    if (node.comfyClass !== "GAStyleCard") return;
    const path = widget(node, "path");
    if (path) {
      const original = path.callback;
      path.callback = function (...args) {
        const result = original?.apply(this, args);
        fill(node, true);
        return result;
      };
    }
    const button = node.addWidget("button", "从文件重新载入", null, () => fill(node, true));
    // 按钮只是界面操作，不存进工作流，也不随任务发给后端
    button.serialize = false;
    button.options = { ...(button.options ?? {}), serialize: false };
  },
  loadedGraphNode(node) {
    if (node.comfyClass === "GAStyleCard") fill(node, false);
  },
});
