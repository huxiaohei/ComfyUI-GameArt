// 「质检结果」节点的编辑器端行为：
// - 运行完后，在节点上显示每张图的缩略图和得分；点缩略图切换，下面列出这张图的问题，每条一个开关。
// - 一次出多张时，另外列出"共性问题"（多张图都有的），默认勾选过半数图片都出现的那些。
// - 点「采纳并重新生成」：把勾选的问题和「我的补充」追加到上游「提示词扩写」的 feedback，然后排队再运行一次。
//   ComfyUI 的图不能成环，所以不是用连线回传，而是由这里直接改上游节点的输入框。
// - 「清空修订意见」：把上游 feedback 清空（换素材、或想从头再来时用）。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "GAQCReview";
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
    issues: (r.issues ?? []).map((i) => ({ text: String(i.text ?? ""), level: i.level === "suggest" ? "suggest" : "must" })),
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
  return { results, common, current, note: "", threshold: Number(payload?.threshold) || 0, info: payload?.note ?? "" };
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
    counter.textContent = n ? `已选 ${n} 条` : "未选问题：直接点运行就是原样再出一版";
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
        title: `第 ${i + 1} 张`,
        onclick: () => {
          state.current = i;
          render(node, root);
        },
      }, [img, el("div", { class: `s ${r.passed ? "ok" : "bad"}`, text: `${i + 1} · ${r.score.toFixed(1)}` })]));
    });
    root.append(strip);
  }

  const cur = state.results[state.current];
  const verdict = cur.passed ? "通过" : "未通过";
  root.append(el("div", {
    class: "ga-qc-head",
    text: `${many ? `第 ${state.current + 1} 张　` : ""}得分 ${cur.score.toFixed(1)} / 10（${verdict}，及格线 ${state.threshold}）`,
  }));
  if (!cur.issues.length) root.append(el("div", { class: "ga-qc-none", text: "没有发现问题。" }));
  for (const issue of cur.issues) {
    const tag = el("span", { class: `ga-qc-tag ${issue.level}`, text: issue.level === "must" ? "必须改" : "建议" });
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

  const regenerate = el("button", { class: "primary", text: "采纳并重新生成", onclick: () => {
    const expand = feedbackTarget(node);
    if (!expand) return;
    const merged = mergeFeedback(widget(expand, "feedback").value, selectedLines(state));
    setWidget(expand, "feedback", merged);
    app.queuePrompt(0, 1);
  } });
  const clear = el("button", { text: "清空修订意见", title: "把「提示词扩写」里累积的修订意见清空", onclick: () => {
    const expand = feedbackTarget(node);
    if (expand && setWidget(expand, "feedback", "")) toast("info", "已清空「提示词扩写」里的修订意见。");
  } });
  root.append(el("div", { class: "ga-qc-actions" }, [regenerate, clear, counter]));
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
      node.properties.ga_qc = buildState(payload);
      redraw();
    };
    const configured = node.onConfigure?.bind(node);
    node.onConfigure = function (info) {
      configured?.(info);
      redraw(); // 打开存过的工作流：把上次的结果画出来
    };
    redraw();
  },
});
