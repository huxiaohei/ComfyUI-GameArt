// 「保存资产」节点的编辑器端行为：
// - 点 subfolder 这一栏：让本机的 ComfyUI 进程弹出系统自带的文件夹选择窗口，把选中的位置填进去。
//   （网页自己拿不到硬盘上的完整路径，所以要由后台代劳；同理，把文件夹拖进来也没法知道它在哪。）
//   按住 Shift 再点，是原来的手动输入框。
// - 把文件夹拖到节点上：提示改用点击选择。
// - 「保存为定稿」按钮：定稿用，一键完成。先在节点的预览里点一下要的那张图，再点按钮，后台把这张图和它的 .json
//   记录存成 subfolder 里的 <name>.png（不带编号），不弹窗；已有同名的定稿时直接替换。
// - 「定稿后删除候选图」开关（默认关）：打开后，定稿存好的同时把 subfolder 里这个名字的带编号候选图全部删掉。
//   开关的状态记在 node.properties.ga_final_cleanup 里，跟着工作流走。
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

// 定稿提示里怎么说：替换了旧的要讲明白
export const finalMessage = (data) =>
  (data.replaced ? `已保存为定稿（替换了原来的）：${data.saved}` : `已保存为定稿：${data.saved}`) +
  (data.removed > 0 ? `，并删除了 ${data.removed} 张候选图` : "");

const CLEANUP_PROP = "ga_final_cleanup";
// 这个保存节点上有没有打开「定稿后删除候选图」
export const wantsCleanup = (save) => save?.properties?.[CLEANUP_PROP] === true;

// 把一张图存成定稿：save 是「保存资产」节点（位置和名字取它上面填的），path 是这张图在硬盘上的完整路径。
// 成功时返回后台的结果，失败时已经弹过提示，返回 null。
export async function saveFinal(save, path) {
  const name = String(widget(save, "name")?.value ?? "").trim() || "asset";
  const subfolder = String(widget(save, "subfolder")?.value ?? "");
  let res;
  try {
    res = await api.fetchApi("/game_art/save_final", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, name, subfolder, cleanup: wantsCleanup(save) }),
    });
  } catch (err) {
    toast("error", `保存定稿失败：${err?.message ?? err}`);
    return null;
  }
  if (res.status === 404 || res.status === 405) {
    toast("error", "后台还没有「保存为定稿」这个功能，请重启 ComfyUI 后再试。");
    return null;
  }
  let data;
  try {
    data = await res.json();
  } catch {
    toast("error", `保存定稿失败：后台返回的内容看不懂（状态 ${res.status}）。`);
    return null;
  }
  if (!res.ok || !data.saved) {
    toast("error", data.error ?? `保存定稿失败（${res.status}）`);
    return null;
  }
  return data;
}

async function finalize(node, index) {
  const path = savedFiles(node)[index];
  if (!path) return;
  const data = await saveFinal(node, path);
  if (data) toast("success", finalMessage(data));
}

// action：对选中的那张图做什么（saveAs 另存 / finalize 定稿）；verb 用在提示里
function saveSelected(node, event, action = saveAs, verb = "另存") {
  const files = savedFiles(node);
  if (!files.length) {
    toast("warn", `还没有可以${verb}的图：请先运行一次。`);
    return;
  }
  if (choosing) {
    toast("warn", "选择文件夹的窗口已经打开了，可能被别的窗口挡住，请切过去看看。");
    return;
  }
  const run = (index) => {
    choosing = true;
    action(node, index).finally(() => (choosing = false));
  };
  const picked = Number.isInteger(node.imageIndex) ? node.imageIndex : files.length === 1 ? 0 : -1;
  if (picked >= 0 && picked < files.length) return run(picked);
  // 有多张图又没在预览里选中某一张：列出来让人选
  const LG = globalThis.LiteGraph;
  if (!LG?.ContextMenu) {
    toast("warn", `节点上有多张图：请先在预览里点一下要${verb}的那张，再点这个按钮。`);
    return;
  }
  const labels = files.map((f, i) => `第 ${i + 1} 张　${baseName(f)}`);
  new LG.ContextMenu(labels, {
    event,
    title: `${verb}哪一张`,
    callback: (label) => {
      const index = labels.indexOf(typeof label === "string" ? label : label?.content);
      if (index >= 0) run(index);
    },
  });
}

// 顺着连线往上游找「质检结果」节点
function findReview(node) {
  const seen = new Set();
  const queue = [node];
  while (queue.length) {
    const cur = queue.shift();
    if (!cur || seen.has(cur.id)) continue;
    seen.add(cur.id);
    if (cur.comfyClass === "GAQCReview") return cur;
    for (let i = 0; i < (cur.inputs?.length ?? 0); i++) queue.push(cur.getInputNode?.(i));
  }
  return null;
}

// 保存完以后，把"哪张图存成了哪个文件"记到上游「质检结果」节点对应的那几条上（按顺序对号入座）。
// 这样在质检结果里点任意一张历史图片，都能找回它的文件：让本节点的预览切过去，或者直接另存。
// 逻辑要和 qc_review.js 的 attachSaved 保持一致。
function recordOnReview(node, message) {
  const state = findReview(node)?.properties?.ga_qc;
  const batch = state?.lastBatch ?? [];
  const files = message.ga_files;
  if (!batch.length || batch.length !== files.length) return;
  batch.forEach((index, k) => {
    const r = state.results?.[index];
    if (r) r.saved = { image: message.images?.[k] ?? null, file: String(files[k]) };
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
      recordOnReview(node, message);
    };
    // 按钮只是界面操作，不存进工作流，也不随任务发给后端
    const addButton = (label, onClick, tooltip) => {
      const b = node.addWidget("button", label, null, (_value, _canvas, _node, _pos, event) => onClick(event));
      b.serialize = false;
      b.options = { ...(b.options ?? {}), serialize: false };
      b.tooltip = tooltip;
      return b;
    };
    node.properties = node.properties ?? {};
    const cleanup = node.addWidget("toggle", "定稿后删除候选图", wantsCleanup(node), (value) => {
      node.properties[CLEANUP_PROP] = value === true;
    }, { on: "删除", off: "保留" });
    cleanup.serialize = false;
    cleanup.options = { ...(cleanup.options ?? {}), serialize: false };
    cleanup.tooltip = "默认保留。打开后，点「保存为定稿」时会把 subfolder 里这个 name 的带编号候选图（如 guanyu_00001.png）和它们的记录全部删掉，只留定稿。删掉的图找不回来，确定不再对比了再打开。";
    // 打开已保存的工作流时，properties 在节点建好之后才读进来：这时把开关拨到存下来的状态
    const configured = node.onConfigure?.bind(node);
    node.onConfigure = function (info) {
      configured?.(info);
      cleanup.value = wantsCleanup(node);
    };
    addButton("保存为定稿", (event) => saveSelected(node, event, finalize, "定稿"),
      "一键定稿：先在预览里点一下要的那张图，再点这里。图片和记录会存到上面 subfolder 的位置，文件名就是 name（不带编号）；已有同名的定稿会被替换。上面的开关打开时，还会删掉这个 name 的候选图。");
    addButton("另存选中的图…", (event) => saveSelected(node, event), "要存到别的地方时用：先在预览里点一下要的那张图，再点这里选文件夹，图片和记录会以 name 为文件名复制过去。");

    node.onDragOver = (e) => [...(e?.dataTransfer?.items ?? [])].some((item) => item.kind === "file");
    node.onDragDrop = (e) => {
      if (!hasDirectory(e)) return false; // 不是文件夹：交回给编辑器默认处理
      toast("info", "网页拿不到拖进来的文件夹在硬盘上的位置。请点一下 subfolder 这一栏来选文件夹。");
      return true;
    };
  },
});
