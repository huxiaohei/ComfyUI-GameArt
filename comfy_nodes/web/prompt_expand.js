// 「提示词扩写」节点的编辑器端行为：从描述清单（yaml）里选一项。
// - briefs_path 填清单文件的路径，或把 .yaml 拖到节点上。拿得到完整路径时填进 briefs_path；
//   拿不到时（浏览器不告诉网页文件在哪）显示 "拖入:<文件名>"，清单内容记在节点里，文件改了要重新拖一次。
// - 点 item：列出清单里的每一项。选中后把这一项的描述填进 brief、素材种类填进 asset_type，
//   清空上一项留下的修订意见，并把下游「保存资产」的文件名改成这一项的 id。
//   按住 Shift 再点，是原来的手动输入框。
// 实际出图以 brief 里的文字为准：选完以后还可以接着改。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "GAPromptExpand";
const SAVE_CLASS = "GASaveAsset";
// 要和 Python 端 prompt_expand.py 的 DROPPED_PREFIX 一致
const DROPPED_PREFIX = "拖入:";

const widget = (node, name) => node.widgets?.find((w) => w.name === name);
const toast = (severity, detail) =>
  app.extensionManager?.toast?.add({ severity, summary: "提示词扩写", detail, life: 7000 });
const isYaml = (file) => /\.ya?ml$/i.test(file.name ?? "");

function setWidget(node, name, value) {
  const w = widget(node, name);
  if (!w) return false;
  w.value = value;
  w.callback?.(value);
  return true;
}

// 菜单里每一项显示成 "id — 描述开头"，描述太长就截断。
export function menuLabel(item, max = 28) {
  const text = String(item.brief ?? "").replace(/\s+/g, " ").trim();
  return `${item.id} — ${text.length > max ? `${text.slice(0, max)}…` : text}`;
}

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

// 让后台解析清单（网页这边没有 yaml 解析器）。成功返回每一项的列表，失败弹提示并返回 null。
async function fetchItems(body) {
  let res;
  try {
    res = await api.fetchApi("/game_art/briefs", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (err) {
    toast("error", `读取描述清单失败：${err?.message ?? err}`);
    return null;
  }
  if (res.status === 404 || res.status === 405) {
    toast("error", "后台还没有「描述清单」这个功能，请重启 ComfyUI 后再试。");
    return null;
  }
  let data;
  try {
    data = await res.json();
  } catch {
    toast("error", `读取描述清单失败：后台返回的内容看不懂（状态 ${res.status}）。`);
    return null;
  }
  if (!res.ok) {
    toast("error", data.error ?? `读取描述清单失败（${res.status}）`);
    return null;
  }
  return Array.isArray(data.items) ? data.items : null;
}

function remember(node, items, source) {
  node.properties = node.properties ?? {};
  node.properties.ga_briefs = { items, source };
}

// 取清单里的每一项。路径是真实文件时每次都重新读（文件改了马上生效）；拖进来的用记在节点里的那份。
async function currentItems(node) {
  const path = String(widget(node, "briefs_path")?.value ?? "").trim();
  if (!path) {
    toast("warn", "还没有描述清单：把 .yaml 文件拖到本节点上，或者在 briefs_path 里填它的路径。");
    return null;
  }
  if (path.startsWith(DROPPED_PREFIX)) {
    const kept = node.properties?.ga_briefs;
    if (kept?.source === path && kept.items?.length) return kept.items;
    toast("warn", "这份描述清单是拖进来的，但内容没有存下来。请把文件重新拖一次。");
    return null;
  }
  const items = await fetchItems({ path });
  if (items) remember(node, items, path);
  return items;
}

// 顺着连线往下游找「保存资产」；没连上时，画布上只有一个就用它。
function findSave(node) {
  const seen = new Set();
  const queue = [node];
  while (queue.length) {
    const cur = queue.shift();
    if (!cur || seen.has(cur.id)) continue;
    seen.add(cur.id);
    if (cur.comfyClass === SAVE_CLASS) return cur;
    for (let i = 0; i < (cur.outputs?.length ?? 0); i++) for (const next of cur.getOutputNodes?.(i) ?? []) queue.push(next);
  }
  const all = (app.graph?._nodes ?? []).filter((n) => n.comfyClass === SAVE_CLASS);
  return all.length === 1 ? all[0] : null;
}

export function applyItem(node, item) {
  setWidget(node, "item", item.id);
  setWidget(node, "brief", item.brief);
  const type = widget(node, "asset_type");
  if (item.asset_type && type && (type.options?.values ?? []).includes(item.asset_type)) setWidget(node, "asset_type", item.asset_type);
  setWidget(node, "feedback", ""); // 换了一个素材：上一个素材的修订意见不该带过来
  const save = findSave(node);
  const renamed = save ? setWidget(save, "name", item.id) : false;
  save?.setDirtyCanvas?.(true, true);
  node.setDirtyCanvas?.(true, true);
  toast("info", renamed
    ? `已选 ${item.id}：描述已填进 brief，保存的文件名改成了 ${item.id}。`
    : `已选 ${item.id}：描述已填进 brief。`);
}

async function pickItem(node, event) {
  const items = await currentItems(node);
  if (!items?.length) return;
  const LG = globalThis.LiteGraph;
  if (!LG?.ContextMenu) {
    toast("error", "这个版本的编辑器弹不出选择菜单。请按住 Shift 点 item，手动填清单里的 id。");
    return;
  }
  const current = String(widget(node, "item")?.value ?? "");
  const labels = items.map((it) => (it.id === current ? `✓ ${menuLabel(it)}` : menuLabel(it)));
  new LG.ContextMenu(labels, {
    event,
    title: "选择要出的素材",
    callback: (label) => {
      const index = labels.indexOf(typeof label === "string" ? label : label?.content);
      if (index >= 0) applyItem(node, items[index]);
    },
  });
}

async function loadDropped(node, file) {
  const full = await droppedFilePath(file);
  if (full) {
    const items = await fetchItems({ path: full });
    if (!items) return;
    setWidget(node, "briefs_path", full);
    remember(node, items, full);
  } else {
    const items = await fetchItems({ text: await file.text() });
    if (!items) return;
    const source = `${DROPPED_PREFIX}${file.name}`;
    setWidget(node, "briefs_path", source);
    remember(node, items, source);
  }
  node.setDirtyCanvas?.(true, true);
  const count = node.properties.ga_briefs.items.length;
  toast("info", `已载入 ${file.name}（${count} 项）。点 item 选一项。${full ? "" : "之后文件有改动，需要重新拖一次。"}`);
}

app.registerExtension({
  name: "GameArt.PromptExpand",
  nodeCreated(node) {
    if (node.comfyClass !== NODE_CLASS) return;
    const item = widget(node, "item");
    if (item) {
      const manual = item.onClick?.bind(item);
      item.onClick = function (options) {
        if (options?.e?.shiftKey && manual) return manual(options); // Shift + 点击：手动输入
        pickItem(node, options?.e).catch((err) => toast("error", String(err?.message ?? err)));
        return undefined;
      };
    }
    node.onDragOver = (e) => [...(e?.dataTransfer?.items ?? [])].some((it) => it.kind === "file");
    node.onDragDrop = (e) => {
      // 文件列表要在事件处理函数返回前取出来
      const file = [...(e?.dataTransfer?.files ?? [])].find(isYaml);
      if (!file) return false; // 不是 .yaml：交回给编辑器默认处理
      loadDropped(node, file).catch((err) => toast("error", String(err?.message ?? err)));
      return true;
    };
  },
});
