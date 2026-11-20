// 「质检结果」节点的编辑器端行为：
// - 运行完后，在节点上显示每张图的缩略图和得分；点缩略图切换，下面列出这张图的问题，每条一个开关。
// - 一次出多张时，另外列出"共性问题"（多张图都有的），默认勾选过半数图片都出现的那些。
// - 点「采纳并重新生成」：把勾选的问题和「我的补充」追加到上游「提示词扩写」的 feedback，然后排队再运行一次。
//   ComfyUI 的图不能成环，所以不是用连线回传，而是由这里直接改上游节点的输入框。
// - 点「修这张」：在选中的这张图上直接改。把"修哪张、改什么"写进本节点的 fix 再运行一次；这一次后台不向上游要图，
//   而是拿上次存下的原图去改，改完的图作为新的一张追加在缩略图后面。排队之后马上把 fix 清空，
//   所以之后正常点运行还是整条流程重跑。
// - 「清空修订意见」：把上游 feedback 清空（换素材、或想从头再来时用）。
// - 和「保存资产」联动：每张图存到了哪个文件，由 save_asset.js 在保存后记到这里对应的那一条上（saved）。
//   点缩略图时，下游「保存资产」节点的预览跟着切到这张图；「另存这张…」把它复制到选定的文件夹。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "GAQCReview";
const SAVE_CLASS = "GASaveAsset";
const EXPAND_CLASS = "GAPromptExpand";
const STYLE_ID = "ga-qc-review-style";
const CSS = `
.ga-qc { font-size: 12px; line-height: 1.45; color: var(--fg-color, #ddd); padding: 4px 2px; box-sizing: border-box;
  width: 100%; overflow-y: auto; }
.ga-qc-empty { opacity: .6; padding: 8px 4px; }
.ga-qc-strip { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.ga-qc-thumb { width: 64px; cursor: pointer; border: 2px solid transparent; border-radius: 6px; padding: 2px;
  text-align: center; background: var(--comfy-input-bg, #222); }
.ga-qc-thumb.on { border-color: var(--p-primary-color, #4a90e2); }
.ga-qc-thumb img { width: 100%; height: 56px; object-fit: contain; display: block; border-radius: 4px;
  background: repeating-conic-gradient(#8884 0% 25%, transparent 0% 50%) 50% / 12px 12px; }
.ga-qc-thumb .s { font-weight: 600; }
.ga-qc-thumb .s.bad { color: #e5734f; }
.ga-qc-thumb .s.ok { color: #58b368; }
.ga-qc-head { font-weight: 600; margin: 6px 0 2px; }
.ga-qc-row { display: flex; gap: 6px; align-items: flex-start; padding: 2px 0; cursor: pointer; }
.ga-qc-row input { margin-top: 3px; flex: none; }
.ga-qc-tag { flex: none; font-size: 11px; padding: 0 5px; border-radius: 8px; border: 1px solid currentColor; opacity: .75; }
.ga-qc-tag.must { color: #e5734f; }
.ga-qc-tags { display: flex; gap: 3px; flex: none; }
.ga-qc-none { opacity: .6; padding: 2px 0; }
.ga-qc textarea { width: 100%; box-sizing: border-box; min-height: 40px; resize: vertical; margin-top: 4px;
  background: var(--comfy-input-bg, #222); color: inherit; border: 1px solid var(--border-color, #555);
  border-radius: 4px; padding: 4px; font: inherit; }
.ga-qc-actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 6px; }
.ga-qc button { cursor: pointer; border-radius: 4px; border: 1px solid var(--border-color, #555); padding: 3px 10px;
  background: var(--comfy-input-bg, #222); color: inherit; font: inherit; }
.ga-qc button.primary { background: var(--p-primary-color, #4a90e2); border-color: transparent; color: #fff; }
.ga-qc-hint { opacity: .65; }
.ga-qc hr { border: 0; border-top: 1px solid var(--border-color, #555); margin: 8px 0 4px; opacity: .6; }
`;

const toast = (severity, detail) =>
  app.extensionManager?.toast?.add({ severity, summary: "质检结果", detail, life: 7000 });
const widget = (node, name) => node.widgets?.find((w) => w.name === name);

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node[key] = value;
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

// ---------------------------------------------------------------- 数据
// 把后台发来的结果整理成节点自己保存的状态（存在 node.properties 里，工作流存盘后重新打开还在）。
// 默认勾选：只有一张图时勾"必须改"的；多张图时只勾过半数图片都出现的共性问题，单张图特有的问题不勾，
// 免得为了某一张图的毛病把整批的描述改偏。
export function buildState(payload) {
  const results = (payload?.results ?? []).map((r) => ({
    score: Number(r.score) || 0,
    passed: !!r.passed,
    thumb: r.thumb ?? null,
    full: r.full ?? "", // 原图在后台临时目录里的名字，「修这张」拿它当底图
    path: r.path ?? "", // 同一张图的完整路径（质检时的原样）；没有保存记录时「另存这张」用它
    saved: null, // 「保存资产」把这张图存成了哪个文件：{ image: 预览用的描述, file: 完整路径 }，保存后才有
    issues: (r.issues ?? []).map((i) => ({
      text: String(i.text ?? ""),
      level: i.level === "suggest" ? "suggest" : "must",
      kind: i.kind === "layout" ? "layout" : "detail", // layout = 构图类，要整张重画；detail = 细节类，可以修图
    })),
  }));
  const single = results.length === 1;
  for (const r of results) for (const i of r.issues) i.on = single && i.level === "must";
  const common = (payload?.common ?? []).map((c) => ({
    text: String(c.text ?? ""),
    count: Number(c.count) || 0,
    on: (Number(c.count) || 0) * 2 > results.length,
  }));
  let current = 0; // 默认看得分最高的那张
  results.forEach((r, i) => {
    if (r.score > results[current].score) current = i;
  });
  return {
    results, common, current, note: "", threshold: Number(payload?.threshold) || 0, info: payload?.note ?? "",
    lastBatch: results.map((_, i) => i), // 最近一次运行产出的是哪几张；紧接着「保存资产」按这个顺序对号入座
  };
}

// 「修这张」的结果：不替换原来的列表，把新图追加在后面并切过去，记下它是从哪一张改来的。
export function appendFix(state, payload) {
  const added = buildState(payload).results;
  const parent = state.results.findIndex((r) => r.full && r.full === payload?.base);
  state.lastBatch = added.map((_, i) => state.results.length + i);
  for (const r of added) {
    for (const i of r.issues) i.on = i.level === "must";
    r.from = parent >= 0 ? parent + 1 : 0;
    r.model = String(payload?.model ?? ""); // 这一版是哪个模型修的，方便对比不同模型的效果
    state.results.push(r);
  }
  if (added.length) state.current = state.results.length - 1;
  state.threshold = Number(payload?.threshold) || state.threshold;
  return state;
}

// 一次修图最多带几条质检问题。改得越多，模型越像是整张重画，满意的部分越保不住。
export const MAX_FIX_ISSUES = 3;

// 「修这张」用的意见：只取当前这张图勾选的"细节类"问题，加上我的补充。
// 构图类问题（留白、贴边、比例、姿势）在原图上改不好，留给重新生成；整批的共性问题也是给重新生成用的。
export function fixIssues(state) {
  const out = [];
  for (const i of state?.results?.[state.current]?.issues ?? []) {
    const t = String(i.text ?? "").trim();
    if (i.on && i.kind !== "layout" && t && !out.includes(t)) out.push(t);
  }
  return out;
}

export function fixLines(state) {
  const lines = fixIssues(state);
  for (const line of String(state?.note ?? "").split("\n")) {
    const t = line.trim();
    if (t && !lines.includes(t)) lines.push(t);
  }
  return lines;
}

// 勾选的问题 + 我的补充，去重后按"共性问题 → 各张图 → 补充"的顺序排好。
export function selectedLines(state) {
  const lines = [];
  const add = (text) => {
    const t = String(text ?? "").trim();
    if (t && !lines.includes(t)) lines.push(t);
  };
  for (const c of state?.common ?? []) if (c.on) add(c.text);
  for (const r of state?.results ?? []) for (const i of r.issues) if (i.on) add(i.text);
  for (const line of String(state?.note ?? "").split("\n")) add(line);
  return lines;
}

// 新的意见追加在已有意见后面（已经有的不重复加），这样上一轮修好的问题不会在这一轮又冒出来。
export function mergeFeedback(existing, lines) {
  const out = [];
  for (const line of [...String(existing ?? "").split("\n"), ...lines]) {
    const t = line.trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out.join("\n");
}

// 「保存资产」保存完以后调用：把这次存下的文件按顺序记到最近一次运行产出的那几张图上。
// images 是预览用的描述（{filename, subfolder, type}），files 是完整路径，两者一一对应。
export function attachSaved(state, images, files) {
  const batch = state?.lastBatch ?? [];
  if (!batch.length || batch.length !== (files?.length ?? 0)) return false; // 张数对不上（中间有别的节点增减了图）：不猜
  batch.forEach((index, k) => {
    const r = state.results[index];
    if (r) r.saved = { image: images?.[k] ?? null, file: String(files[k]) };
  });
  return true;
}

// ---------------------------------------------------------------- 找下游的「保存资产」
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
  return null;
}

// 让下游「保存资产」节点的预览显示这张图，它的「另存选中的图」也跟着指向这张。
// 预览是编辑器按"节点的输出记录"画的，所以这里换掉那条记录再请它重画。
function showInSave(node, result) {
  const save = findSave(node);
  if (!save || !result?.saved?.image) return false;
  const output = { images: [result.saved.image], ga_files: [result.saved.file] };
  save.properties = save.properties ?? {};
  save.properties.ga_files = output.ga_files;
  try {
    const outputs = app.nodeOutputs;
    if (outputs) outputs[String(save.id)] = output;
  } catch (err) {
    console.warn("[game_art] 切换保存节点的预览失败", err);
  }
  save.imageIndex = null;
  save.setDirtyCanvas?.(true, true);
  app.graph?.setDirtyCanvas?.(true, true);
  return true;
}

async function saveResultAs(node, result) {
  const save = findSave(node);
  const name = String(widget(save ?? {}, "name")?.value ?? "").trim() || "asset";
  const path = result?.saved?.file || result?.path;
  if (!path) {
    toast("warn", "这张图是旧版本留下的结果，后台没有存原图。请先正常运行一次，再另存。");
    return;
  }
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
  else if (data.saved) {
    toast("success", result.saved?.file
      ? `已另存为 ${data.saved}`
      : `已另存为 ${data.saved}（这张图没有经过「保存资产」，存的是质检时的原样，没有去背景和清理）`);
  }
}

// ---------------------------------------------------------------- 找上游的「提示词扩写」
function findExpand(node) {
  const seen = new Set();
  const queue = [node];
  while (queue.length) {
    const cur = queue.shift();
    if (!cur || seen.has(cur.id)) continue;
    seen.add(cur.id);
    if (cur.comfyClass === EXPAND_CLASS) return cur;
    for (let i = 0; i < (cur.inputs?.length ?? 0); i++) queue.push(cur.getInputNode?.(i));
  }
  // 没连上（比如只接了图片）：画布上只有一个「提示词扩写」时就用它
  const all = (app.graph?._nodes ?? []).filter((n) => n.comfyClass === EXPAND_CLASS);
  return all.length === 1 ? all[0] : null;
}

function setWidget(node, name, value) {
  const w = widget(node, name);
  if (!w) return false;
  w.value = value;
  w.callback?.(value);
  node.setDirtyCanvas?.(true, true);
  return true;
}

function feedbackTarget(node) {
  const expand = findExpand(node);
  if (!expand) {
    toast("error", "没找到上游的「提示词扩写」节点。请把它的 meta 一路连到本节点，或者手动把问题写进它的 feedback。");
    return null;
  }
  if (!widget(expand, "feedback")) {
    toast("error", "「提示词扩写」节点上没有 feedback 这一栏：节点代码更新后需要重启 ComfyUI，并重新放一个该节点。");
    return null;
  }
  return expand;
}

// ---------------------------------------------------------------- 画界面
function thumbUrl(thumb) {
  if (!thumb?.filename) return "";
  const q = new URLSearchParams({ filename: thumb.filename, subfolder: thumb.subfolder ?? "", type: thumb.type ?? "temp" });
  return api.apiURL(`/view?${q}`);
}

function issueRow(item, label, onChange) {
  const box = el("input", { type: "checkbox", checked: !!item.on });
  box.addEventListener("change", () => {
    item.on = box.checked;
    onChange();
  });
  return el("label", { class: "ga-qc-row" }, [box, label, el("span", { text: item.text })]);
}

function render(node, root) {
  const state = node.properties?.ga_qc;
  root.replaceChildren();
  if (!state?.results?.length) {
    root.append(el("div", { class: "ga-qc-empty", text: "运行后在这里显示每张图的得分和问题。" }));
    return;
  }
  const counter = el("span", { class: "ga-qc-hint" });
  const refresh = () => {
    const n = selectedLines(state).length;
    const f = fixLines(state).length;
    counter.textContent = n || f ? `修这张用 ${f} 条，重新生成用 ${n} 条` : "未选问题：直接点运行就是原样再出一版";
  };
  const many = state.results.length > 1;
  state.current = Math.min(Math.max(0, state.current | 0), state.results.length - 1);

  if (many) {
    const strip = el("div", { class: "ga-qc-strip" });
    state.results.forEach((r, i) => {
      const img = el("img", { src: thumbUrl(r.thumb), alt: `第 ${i + 1} 张` });
      img.addEventListener("error", () => (img.style.visibility = "hidden")); // 临时目录被清掉了：只留得分
      strip.append(el("div", {
        class: `ga-qc-thumb${i === state.current ? " on" : ""}`,
        title: r.from ? `第 ${i + 1} 张（由第 ${r.from} 张修改${r.model ? `，${r.model}` : ""}）` : `第 ${i + 1} 张`,
        onclick: () => {
          state.current = i;
          render(node, root);
          showInSave(node, r); // 下游「保存资产」的预览跟着切到这张
        },
      }, [img, el("div", { class: `s ${r.passed ? "ok" : "bad"}`, text: `${i + 1}${r.from ? "✎" : ""} · ${r.score.toFixed(1)}` })]));
    });
    root.append(strip);
  }

  const cur = state.results[state.current];
  const verdict = cur.passed ? "通过" : "未通过";
  root.append(el("div", {
    class: "ga-qc-head",
    text: `${many ? `第 ${state.current + 1} 张${cur.from ? `（由第 ${cur.from} 张修改${cur.model ? `，${cur.model}` : ""}）` : ""}　` : ""}得分 ${cur.score.toFixed(1)} / 10（${verdict}，及格线 ${state.threshold}）`,
  }));
  if (!cur.issues.length) root.append(el("div", { class: "ga-qc-none", text: "没有发现问题。" }));
  for (const issue of cur.issues) {
    const tag = el("span", { class: "ga-qc-tags" }, [
      el("span", { class: `ga-qc-tag ${issue.level}`, text: issue.level === "must" ? "必须改" : "建议" }),
      el("span", {
        class: "ga-qc-tag", text: issue.kind === "layout" ? "构图" : "细节",
        title: issue.kind === "layout"
          ? "构图类问题：在原图上改不好，「修这张」不会带上它，请用「采纳并重新生成」"
          : "细节类问题：适合用「修这张」在原图上改",
      }),
    ]);
    root.append(issueRow(issue, tag, refresh));
  }

  if (many) {
    root.append(el("hr"), el("div", { class: "ga-qc-head", text: "整批的共性问题" }));
    if (!state.common.length) {
      root.append(el("div", { class: "ga-qc-none", text: state.info || "没有多张图都出现的问题。" }));
    }
    for (const c of state.common) {
      root.append(issueRow(c, el("span", { class: "ga-qc-tag", text: `${c.count}/${state.results.length} 张` }), refresh));
    }
  }

  const note = el("textarea", { placeholder: "我的补充：质检没看出来、但你想改的地方，一行一条", value: state.note ?? "" });
  note.addEventListener("input", () => {
    state.note = note.value;
    refresh();
  });
  for (const type of ["keydown", "keyup", "wheel", "pointerdown"]) note.addEventListener(type, (e) => e.stopPropagation());
  root.append(el("hr"), note);

  const regenerate = el("button", {
    class: "primary", text: "采纳并重新生成", title: "把勾选的问题带回「提示词扩写」，整张重新出图",
    onclick: () => {
      const expand = feedbackTarget(node);
      if (!expand) return;
      const merged = mergeFeedback(widget(expand, "feedback").value, selectedLines(state));
      setWidget(expand, "feedback", merged);
      setWidget(node, "fix", ""); // 确保这一次是整条流程重跑，不是修图
      app.queuePrompt(0, 1);
    },
  });
  const fixThis = el("button", {
    class: "primary", text: many ? `修第 ${state.current + 1} 张` : "修这张",
    title: "在这张图上直接修改：只按这张图勾选的细节类问题（最多 3 条）和「我的补充」来改，其余部分基本保留。按张计费。",
    onclick: async () => {
      if (!widget(node, "fix")) {
        toast("error", "节点上没有 fix 这一栏：节点代码更新后需要重启 ComfyUI，并重新放一个「质检结果」节点。");
        return;
      }
      if (!cur.full) {
        toast("warn", "这张图是旧版本留下的结果，后台没有存原图。请先正常运行一次，再点「修这张」。");
        return;
      }
      const lines = fixLines(state);
      if (!lines.length) {
        toast("warn", "还没说要改什么：请勾选这张图的细节类问题，或者在「我的补充」里写上。构图类问题请用「采纳并重新生成」。");
        return;
      }
      if (fixIssues(state).length > MAX_FIX_ISSUES) {
        toast("warn", `一次最多修 ${MAX_FIX_ISSUES} 条细节问题：改得越多，越像整张重画，满意的部分越保不住。请先去掉几条，改完再修下一轮。`);
        return;
      }
      setWidget(node, "fix", JSON.stringify({ file: cur.full, notes: lines }));
      try {
        await app.queuePrompt(0, 1);
      } finally {
        setWidget(node, "fix", ""); // 只对这一次运行有效
      }
    },
  });
  const clear = el("button", { text: "清空修订意见", title: "把「提示词扩写」里累积的修订意见清空", onclick: () => {
    const expand = feedbackTarget(node);
    if (expand && setWidget(expand, "feedback", "")) toast("info", "已清空「提示词扩写」里的修订意见。");
  } });
  let saving = false;
  const saveAs = el("button", {
    text: many ? `另存第 ${state.current + 1} 张…` : "另存这张…",
    title: "定稿用：选一个文件夹，把这张图和它的记录复制过去，文件名用「保存资产」节点的 name。",
    onclick: () => {
      if (saving) {
        toast("warn", "选择文件夹的窗口已经打开了，可能被别的窗口挡住，请切过去看看。");
        return;
      }
      saving = true;
      saveResultAs(node, cur).finally(() => (saving = false));
    },
  });
  root.append(el("div", { class: "ga-qc-actions" }, [fixThis, regenerate, saveAs, clear, counter]));
  refresh();
}

function fitNode(node) {
  // 让节点的高度跟上内容；宽度保持用户拖出来的大小
  requestAnimationFrame(() => {
    const want = node.computeSize?.();
    if (want && node.size && want[1] > node.size[1]) node.setSize?.([node.size[0], want[1]]);
    node.setDirtyCanvas?.(true, true);
  });
}

app.registerExtension({
  name: "GameArt.QCReview",
  setup() {
    if (!document.getElementById(STYLE_ID)) document.head.append(el("style", { id: STYLE_ID, textContent: CSS }));
  },
  nodeCreated(node) {
    if (node.comfyClass !== NODE_CLASS) return;
    const root = el("div", { class: "ga-qc" });
    node.addDOMWidget?.("ga_qc_panel", "ga_qc_panel", root, {
      serialize: false, // 状态存在 node.properties 里，不占控件值的位置
      hideOnZoom: false,
      getMinHeight: () => Math.min(Math.max(root.scrollHeight, 60), 640),
    });
    const fix = widget(node, "fix");
    if (fix) {
      // fix 由「修这张」按钮填写，不给人看：藏起来，并且每次打开工作流都清空
      fix.hidden = true;
      fix.computeSize = () => [0, -4];
      fix.value = "";
    }
    const redraw = () => {
      render(node, root);
      fitNode(node);
    };

    const executed = node.onExecuted?.bind(node);
    node.onExecuted = function (message) {
      executed?.(message);
      const payload = message?.ga_qc?.[0];
      if (!payload) return;
      node.properties = node.properties ?? {};
      const old = node.properties.ga_qc;
      node.properties.ga_qc = payload.mode === "fix" && old?.results?.length ? appendFix(old, payload) : buildState(payload);
      redraw();
    };
    const configured = node.onConfigure?.bind(node);
    node.onConfigure = function (info) {
      configured?.(info);
      setWidget(node, "fix", ""); // 存盘时万一带着上次的修图任务，打开后不该再执行
      redraw(); // 打开存过的工作流：把上次的结果画出来
    };
    redraw();
  },
});
