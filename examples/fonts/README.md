# 字体目录

模板合成里的文字用这里的字体绘制。字体文件体积大，**不随仓库分发**，需要自己放进来；本目录除这份说明外都被 git 忽略。

示例版式 `examples/layouts/demo_tile.yaml` 引用的是 `SourceHanSerifHK-Bold.otf`（思源宋体 香港字形 粗体）。没有这个文件时，用示例版式合成会报"字体文件不存在"。

## 下载示例字体

思源宋体由 Adobe 以 SIL Open Font License 1.1 发布，可免费商用。在仓库根目录执行：

```bash
curl -L -o /tmp/SourceHanSerifHK.zip https://github.com/adobe-fonts/source-han-serif/releases/download/2.003R/16_SourceHanSerifHK.zip
```

```bash
unzip -j /tmp/SourceHanSerifHK.zip '*SourceHanSerifHK-Bold.otf' '*LICENSE*' -d examples/fonts
```

需要其他字重时从同一个压缩包里取，或换用思源黑体（Source Han Sans）。

## 使用自己的字体

把字体文件放进本目录，在版式 YAML 的文字层里引用，路径相对版式文件所在目录：

```yaml
font: ../fonts/<你的字体文件>
```

`font: default` 是 Pillow 自带字体，不含中文字形，只适合测试占位。使用字体前请确认授权允许你的用途。
