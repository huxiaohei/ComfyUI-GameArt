// 「风格卡」节点：把文件内容直接显示在 edit 编辑框里，方便查看和修改。
// - 打开工作流时，edit 为空就读入文件内容；已经有内容（你改过）则不动。
// - 改了 path，或点「从文件重新载入」，用文件内容替换 edit。
// - 把 .yaml 文件拖到节点上：能拿到文件的完整路径时填进 path 并载入；拿不到时（浏览器不告诉网页
//   文件在哪）把文件内容放进 edit，path 显示成 "拖入:<文件名>"，这时风格完全由编辑框里的内容决定。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const widget = (node, name) => node.widgets?.find((w) => w.name === name);
// path 以它开头表示"这张卡是拖进来的，内容在 edit 里，不读文件"。要和 Python 端 style_card.py 的 DROPPED_PREFIX 一致。
const DROPPED_PREFIX = "拖入:";

async function fill(node, force) {
  const path = widget(node, "path");
  const edit = widget(node, "edit");
  if (!path || !edit) return;
  const value = String(path.value ?? "").trim();
  if (value.startsWith(DROPPED_PREFIX)) {
    // 拖进来的卡没有路径可读；只有用户主动点「重新载入」时才提示
    if (force === "button") {
      app.extensionManager?.toast?.add({
        severity: "warn", summary: "风格卡", life: 6000,
        detail: "这张风格卡是拖进来的，网页拿不到它在硬盘上的位置。文件有改动时请重新拖一次，或者在 path 里填完整路径。",
      });
    }
    return;
  }
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

const toast = (severity, detail) =>
  app.extensionManager?.toast?.add({ severity, summary: "风格卡", detail, life: 6000 });

// 桌面版（Electron）有时能给出拖入文件的完整路径；普通浏览器一律拿不到。
async function droppedFilePath(file) {
  if (typeof file.path === "string" && file.path) return file.path;
  try {
    const found = await globalThis.electronAPI?.getPathForFile?.(file);
    return typeof found === "string" ? found : "";
  } catch {
    return "";
  }
}

async function loadDropped(node, file) {
  const path = widget(node, "path");
  const edit = widget(node, "edit");
  if (!path || !edit) return;
  const full = await droppedFilePath(file);
  if (full) {
    path.value = full;
    await fill(node, true);
    toast("info", `已载入 ${file.name}。之后改了文件，点「从文件重新载入」即可。`);
  } else {
    edit.value = await file.text();
    path.value = `${DROPPED_PREFIX}${file.name}`;
    toast("info", `已把 ${file.name} 的内容放进编辑框。之后改了文件，需要重新拖一次。`);
  }
  node.setDirtyCanvas?.(true, true);
}

const isYaml = (file) => /\.ya?ml$/i.test(file.name ?? "");

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
    node.onDragOver = (e) => [...(e?.dataTransfer?.items ?? [])].some((item) => item.kind === "file");
    node.onDragDrop = (e) => {
      // 文件列表要在事件处理函数返回前取出来
      const file = [...(e?.dataTransfer?.files ?? [])].find(isYaml);
      if (!file) return false; // 不是 .yaml：交回给编辑器默认处理
      loadDropped(node, file).catch((err) => toast("error", String(err?.message ?? err)));
      return true;
    };
    const button = node.addWidget("button", "从文件重新载入", null, () => fill(node, "button"));
    // 按钮只是界面操作，不存进工作流，也不随任务发给后端
    button.serialize = false;
    button.options = { ...(button.options ?? {}), serialize: false };
  },
  loadedGraphNode(node) {
    if (node.comfyClass === "GAStyleCard") fill(node, false);
  },
});
