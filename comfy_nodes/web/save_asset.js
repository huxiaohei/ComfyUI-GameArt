// 「保存资产」节点的编辑器端行为：
// - 点 subfolder 这一栏：让本机的 ComfyUI 进程弹出系统自带的文件夹选择窗口，把选中的位置填进去。
//   （网页自己拿不到硬盘上的完整路径，所以要由后台代劳；同理，把文件夹拖进来也没法知道它在哪。）
//   按住 Shift 再点，是原来的手动输入框。
// - 把文件夹拖到节点上：提示改用点击选择。
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

    node.onDragOver = (e) => [...(e?.dataTransfer?.items ?? [])].some((item) => item.kind === "file");
    node.onDragDrop = (e) => {
      if (!hasDirectory(e)) return false; // 不是文件夹：交回给编辑器默认处理
      toast("info", "网页拿不到拖进来的文件夹在硬盘上的位置。请点一下 subfolder 这一栏来选文件夹。");
      return true;
    };
  },
});
