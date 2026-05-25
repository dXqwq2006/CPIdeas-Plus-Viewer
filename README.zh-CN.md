# CPIdeas Plus Viewer

[English](README.md)

CPIdeas Plus Viewer 是用于交付包的本地只读 Web 查看器。它面向评审、客户和项目相关方，用来浏览已经交付的题目包，而不暴露私有 AI 生成流程。

## 支持什么

- 浏览单个题目交付包，或浏览一批题目交付包。
- 查看题面、输入输出格式、约束、样例和预期解法说明。
- 如果交付包中包含本地化文件，可以查看多语言 statement/review/preview。
- 如果包含 `verification/cpideas_report.json`，可以查看验证摘要。
- 查看交付 `package/` 内的文本文件，例如 statement、solution、generator、validator、checker、package config。
- 完全本地运行；依赖安装完成后，不需要外部服务或远端后端。
- 使用稳定的产品包 schema：`cpideas-plus-product-v1`。

## 不包含什么

- 不包含 AI prompt 或原始模型输出。
- 不包含 checkpoints、repair traces、batch runners、LiveCodeBench importers 或私有 workbench actions。
- 独立 public viewer 不提供 preview/verify/resume/submit 按钮。
- 不会在服务端修改交付包内容。

独立 viewer 是只读模式。能力接口会返回：

```json
{
  "actions_enabled": false,
  "submission_enabled": false,
  "internal_files_enabled": false,
  "mode": "viewer",
  "supported_bundle_schema": "cpideas-plus-product-v1"
}
```

## 交付包结构

单个题目交付包通常是：

```text
product_single/
  run.json
  package/
    config/package.json
    statements/statement.md
    solutions/main.cpp
    generator/generator.cpp
    generator_script/generate.json
```

批量交付包会在根目录包含 `batch_index.json`，并且每个题目一个子目录。

完整契约见 `docs/PRODUCT_BUNDLE.zh-CN.md`。

## 用 uv 运行

```bash
uv sync
uv run cpideas-viewer --runs-dir examples/product_single --host 127.0.0.1 --port 8765
```

然后在浏览器中打开命令行打印的 URL。

查看批量示例：

```bash
uv run cpideas-viewer --runs-dir examples/product_batch
```

`--runs-dir` 可以指向包含 `run.json` 的单个 run 目录，也可以指向包含多个 run/batch 的父目录。

`uv sync` 创建的环境可能不带 `pip`；请使用 `uv run ...` 或 `uv pip install -e .`，不要使用 `uv run python -m pip install -e .`。

## 用 pip 运行

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cpideas-viewer --runs-dir examples/product_single --host 127.0.0.1 --port 8765
```

## 交付前检查

- 只交付 product bundle，不交付私有生成产物。
- 确认每个 bundle 包含 `run.json` 和 `package/config/package.json`。
- 交付前先用 viewer 在本地打开一次。
- 如果交付批量题目，根目录需要包含 `batch_index.json`。
- 如果包含本地化 Markdown，文件名建议保持 `review.zh-CN.md`、`preview.zh-CN.md` 这类格式，并放在英文文件旁边。

## 开发检查

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall src/cpideas_plus_viewer
git diff --check
```

## License

MIT。见 `LICENSE`。
