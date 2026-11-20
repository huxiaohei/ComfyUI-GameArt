// 「参考图」节点的编辑器端行为：
// - 把图片文件拖到节点上：每张图自动建一个「加载图像」节点（上传并显示缩略图），并连到本节点的一个接线口。
// - 接线口按需出现：始终只留一个空位，接满一个再多出一个，最多 8 个（名字要和 Python 端的 IMAGE_SLOTS 一致）。
// - paths 输入框里的灰色提示文字跟着变：列出当前已经接进来的图，免得误以为还要再写一遍路径。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const SLOT_NAMES = ["images", ...Array.from({ length: 7 }, (_, i) => `image_${i + 2}`)];
const LOAD_NODE_HEIGHT = 344; // 「加载图像」节点带缩略图时的大致高度，用来把新建的节点竖着排开
const GAP = 25;

const toast = (severity, detail) =>
  app.extensionManager?.toast?.add({ severity, summary: "参考图", detail, life: 6000 });

const imageSlots = (node) =>
  (node.inputs ?? []).map((input, index) => ({ input, index })).filter((s) => SLOT_NAMES.includes(s.input.name));

// 接进来的图叫什么：「加载图像」节点取它选中的文件名，其他节点取标题。
function linkedImageNames(node) {
  const graph = node.graph ?? app.graph;
  return imageSlots(node)
    .filter((s) => s.input.link != null)
    .map((s) => {
      const link = graph?.links?.get?.(s.input.link) ?? graph?.links?.[s.input.link];
      const origin = link ? graph?.getNodeById?.(link.origin_id) : null;
      if (!origin) return "（已连接）";
      watchOrigin(node, origin);
      const value = origin.widgets?.find((w) => w.name === "image")?.value;
      return typeof value === "string" && value ? value : origin.title || origin.type || "（已连接）";
    });
}

// 上游「加载图像」节点换了图（包括刚拖进来、上传完成的那一刻）时，刷新提示文字。
function watchOrigin(node, origin) {
  if (origin.__gaRefWatched) return;
  origin.__gaRefWatched = true;
  const original = origin.onWidgetChanged;
  origin.onWidgetChanged = function (...args) {
    const result = original?.apply(this, args);
    setTimeout(() => updatePlaceholder(node), 0);
    return result;
  };
}

function updatePlaceholder(node) {
  const widget = node.widgets?.find((w) => w.name === "paths");
  const el = widget?.element ?? widget?.inputEl;
  if (!widget) return;
  if (node.__gaRefDefaultPlaceholder === undefined) {
    node.__gaRefDefaultPlaceholder = el?.placeholder ?? widget.options?.placeholder ?? "";
  }
  const names = linkedImageNames(node);
  const text = names.length
    ? [
        `已接入 ${names.length} 张参考图：`,
        ...names.map((name, i) => `${i + 1}. ${name}`),
        "",
        "这些图不用再写路径。",
        "还想加别的图，可以在这里写路径，一行一个",
        "（写在这里的图排在上面这些图的前面）。",
      ].join("\n")
    : node.__gaRefDefaultPlaceholder;
  if (el && "placeholder" in el) el.placeholder = text;
  if (widget.options) widget.options.placeholder = text;
}

// 只留一个空的图片接线口；一个空位都没有、且还没到上限时补一个。
function syncSlots(node) {
  const empty = imageSlots(node).filter((s) => s.input.link == null);
  for (const s of empty.slice(1).reverse()) node.removeInput(s.index);
  if (empty.length === 0) {
    const used = new Set(imageSlots(node).map((s) => s.input.name));
    const free = SLOT_NAMES.find((name) => !used.has(name));
    if (free) node.addInput(free, "IMAGE");
  }
  updatePlaceholder(node);
  node.setDirtyCanvas?.(true, true);
}

function freeSlotIndex(node) {
  syncSlots(node);
  const slot = imageSlots(node).find((s) => s.input.link == null);
  return slot ? slot.index : -1;
}

// 「加载图像」节点自带 pasteFiles（上传 + 选中 + 显示缩略图）；老版本前端没有时自己上传并填进下拉框。
async function putFileOnLoadNode(loadNode, file) {
  if (typeof loadNode.pasteFiles === "function") {
    loadNode.pasteFiles([file]);
    return;
  }
  const body = new FormData();
  body.append("image", file);
  const res = await api.fetchApi("/upload/image", { method: "POST", body });
  if (res.status !== 200) throw new Error(`上传失败（${res.status} ${res.statusText}）`);
  const data = await res.json();
  const path = data.subfolder ? `${data.subfolder}/${data.name}` : data.name;
  const widget = loadNode.widgets?.find((w) => w.name === "image");
  if (!widget) throw new Error("「加载图像」节点上找不到 image 控件");
  if (widget.options?.values && !widget.options.values.includes(path)) widget.options.values.push(path);
  widget.value = path;
  widget.callback?.(path);
}

async function addImages(node, files) {
  const graph = node.graph ?? app.graph;
  let added = 0;
  for (const file of files) {
    const target = freeSlotIndex(node);
    if (target < 0) {
      toast("warn", `最多接 ${SLOT_NAMES.length} 张图，剩下的 ${files.length - added} 张没有加入。也可以把路径写进 paths。`);
      break;
    }
    const loadNode = globalThis.LiteGraph?.createNode("LoadImage");
    if (!loadNode || !graph) {
      toast("error", "无法创建「加载图像」节点。");
      break;
    }
    const row = imageSlots(node).filter((s) => s.input.link != null).length;
    const width = loadNode.size?.[0] ?? 340;
    loadNode.pos = [node.pos[0] - width - 80, node.pos[1] + row * (LOAD_NODE_HEIGHT + GAP)];
    graph.add(loadNode);
    try {
      await putFileOnLoadNode(loadNode, file);
    } catch (err) {
      graph.remove(loadNode);
      toast("error", `${file.name}：${err?.message ?? err}`);
      continue;
    }
    loadNode.connect(0, node, target);
    added += 1;
  }
  syncSlots(node);
  graph?.change?.();
}

const draggedImageFiles = (e) => [...(e?.dataTransfer?.files ?? [])].filter((f) => f.type.startsWith("image/"));

app.registerExtension({
  name: "GameArt.ReferenceImages",
  nodeCreated(node) {
    if (node.comfyClass !== "GAReferenceImages") return;
    syncSlots(node);

    node.onDragOver = (e) => [...(e?.dataTransfer?.items ?? [])].some((item) => item.kind === "file");
    node.onDragDrop = (e) => {
      // 文件列表要在事件处理函数返回前取出来，之后浏览器就不让读了
      const files = draggedImageFiles(e);
      if (!files.length) return false; // 不是图片：交回给编辑器默认处理
      addImages(node, files).catch((err) => toast("error", String(err?.message ?? err)));
      return true;
    };

    const original = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
      const result = original?.apply(this, args);
      // 载入工作流的过程中连线还没恢复完，等 loadedGraphNode 统一整理
      if (!app.configuringGraph) setTimeout(() => syncSlots(node), 0);
      return result;
    };
  },
  loadedGraphNode(node) {
    if (node.comfyClass === "GAReferenceImages") syncSlots(node);
  },
});
