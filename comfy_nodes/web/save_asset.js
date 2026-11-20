// 「保存资产」节点的编辑器端行为：
// - 点 subfolder 这一栏：让本机的 ComfyUI 进程弹出系统自带的文件夹选择窗口，把选中的位置填进去。
//   （网页自己拿不到硬盘上的完整路径，所以要由后台代劳；同理，把文件夹拖进来也没法知道它在哪。）
//   按住 Shift 再点，是原来的手动输入框。
// - 把文件夹拖到节点上：提示改用点击选择。
// - 「另存选中的图…」按钮：定稿用。先在节点的预览里点一下要的那张图，再点按钮，弹出系统的文件夹选择窗口，
//   选完后后台把这张图和它的 .json 记录复制过去，文件名是 name 那一栏的名字（不带编号）；已有同名文件时不覆盖，
//   自动加 _2、_3。节点上有多张图又没选中时，会先弹一个列表让你选是哪一张。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const widget = (node, name) => node.widgets?.find((w) => w.name === name);
const toast = (severity, detail) =>
  app.extensionManager?.toast?.add({ severity, summary: "保存资产", detail, life: 7000 });

let choosing = false;

// 返回 undefined = 没选（取消或出错），其余是该填进 subfolder 的值（可能是空字符串 = 输出目录本身）。
async function chooseFolder() {
  if (choosing) {
    toast("warn", "选择文件夹的窗口已经打开了，可能被别的窗口挡住，请切过去看看。");
    return undefined;
  }
  choosing = true;
  try {
    const res = await api.fetchApi("/game_art/choose_folder", { method: "POST" });
    if (res.status === 404 || res.status === 405) {
      // 页面脚本是新的，后台还是旧的：改了节点代码之后 ComfyUI 没有重启
      toast("error", "后台还没有「选择文件夹」这个功能，请重启 ComfyUI 后再试。现在可以按住 Shift 点这一栏手动输入。");
      return undefined;
    }
    let data;
    try {
      data = await res.json();
    } catch {
      toast("error", `选择文件夹失败：后台返回的内容看不懂（状态 ${res.status}）。可以按住 Shift 点这一栏手动输入。`);
      return undefined;
    }
    if (!res.ok) {
      toast("error", data.error ?? `选择文件夹失败（${res.status}）`);
      return undefined;
    }
    return data.path ? (data.value ?? data.path) : undefined;
  } catch (err) {
    toast("error", `选择文件夹失败：${err?.message ?? err}。可以按住 Shift 点这一栏手动输入。`);
    return undefined;
  } finally {
    choosing = false;
  }
}

function setValue(target, value, options, node) {
  if (typeof target.setValue === "function" && options) target.setValue(value, options);
  else {
    target.value = value;
    target.callback?.(value);
  }
  node.setDirtyCanvas?.(true, true);
}

// 这次运行保存了哪些文件（后台给的完整路径），和节点预览里的图片顺序一致
const savedFiles = (node) => node.properties?.ga_files ?? [];
const baseName = (path) => String(path).split(/[\\/]/).pop();

async function saveAs(node, index) {
  const path = savedFiles(node)[index];
  if (!path) return;
  const name = String(widget(node, "name")?.value ?? "").trim() || "asset";
  let res;
  try {
    res = await api.fetchApi("/game_art/save_as", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, name }),
    });
  } catch (err) {
    toast("error", `另存失败：${err?.message ?? err}`);
    return;
  }
  if (res.status === 404 || res.status === 405) {
    toast("error", "后台还没有「另存」这个功能，请重启 ComfyUI 后再试。");
    return;
  }
  let data;
  try {
    data = await res.json();
  } catch {
    toast("error", `另存失败：后台返回的内容看不懂（状态 ${res.status}）。`);
    return;
  }
  if (!res.ok) toast("error", data.error ?? `另存失败（${res.status}）`);
  else if (data.saved) toast("success", `已另存为 ${data.saved}`);
  // data.cancelled：在选择窗口里点了取消，什么都不用说
}

function saveSelected(node, event) {
  const files = savedFiles(node);
  if (!files.length) {
    toast("warn", "还没有可以另存的图：请先运行一次。");
    return;
  }
  if (choosing) {
    toast("warn", "选择文件夹的窗口已经打开了，可能被别的窗口挡住，请切过去看看。");
    return;
  }
  const run = (index) => {
    choosing = true;
    saveAs(node, index).finally(() => (choosing = false));
  };
  const picked = Number.isInteger(node.imageIndex) ? node.imageIndex : files.length === 1 ? 0 : -1;
  if (picked >= 0 && picked < files.length) return run(picked);
  // 有多张图又没在预览里选中某一张：列出来让人选
  const LG = globalThis.LiteGraph;
  if (!LG?.ContextMenu) {
    toast("warn", "节点上有多张图：请先在预览里点一下要另存的那张，再点这个按钮。");
    return;
  }
  const labels = files.map((f, i) => `第 ${i + 1} 张　${baseName(f)}`);
  new LG.ContextMenu(labels, {
    event,
    title: "另存哪一张",
    callback: (label) => {
      const index = labels.indexOf(typeof label === "string" ? label : label?.content);
      if (index >= 0) run(index);
    },
  });
}

const hasDirectory = (e) =>
  [...(e?.dataTransfer?.items ?? [])].some((item) => item.kind === "file" && item.webkitGetAsEntry?.()?.isDirectory);

app.registerExtension({
  name: "GameArt.SaveAsset",
  nodeCreated(node) {
    if (node.comfyClass !== "GASaveAsset") return;
    const target = widget(node, "subfolder");
    if (target) {
      const manual = target.onClick?.bind(target);
      target.onClick = function (options) {
        if (options?.e?.shiftKey && manual) return manual(options); // Shift + 点击：手动输入
        chooseFolder().then((value) => {
          if (value !== undefined) setValue(target, value, options, node);
        });
        return undefined;
      };
    }

    const executed = node.onExecuted?.bind(node);
    node.onExecuted = function (message) {
      executed?.(message);
      if (!Array.isArray(message?.ga_files)) return;
      node.properties = node.properties ?? {};
      node.properties.ga_files = message.ga_files;
    };
    const button = node.addWidget("button", "另存选中的图…", null, (_value, _canvas, _node, _pos, event) =>
      saveSelected(node, event));
    // 按钮只是界面操作，不存进工作流，也不随任务发给后端
    button.serialize = false;
    button.options = { ...(button.options ?? {}), serialize: false };
    button.tooltip = "定稿用：先在预览里点一下要的那张图，再点这里选文件夹，图片和记录会以 name 为文件名复制过去。";

    node.onDragOver = (e) => [...(e?.dataTransfer?.items ?? [])].some((item) => item.kind === "file");
    node.onDragDrop = (e) => {
      if (!hasDirectory(e)) return false; // 不是文件夹：交回给编辑器默认处理
      toast("info", "网页拿不到拖进来的文件夹在硬盘上的位置。请点一下 subfolder 这一栏来选文件夹。");
      return true;
    };
  },
});
