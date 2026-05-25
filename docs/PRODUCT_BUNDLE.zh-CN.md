# Product Viewer Bundle Contract

本文定义 CPIdeas Plus Viewer 支持的交付包结构。它是私有 AI/workbench 仓库与公开 viewer 之间的稳定边界。

## Schema

当前 viewer 支持的 bundle schema：

```text
cpideas-plus-product-v1
```

HTTP capability endpoint 会在 `supported_bundle_schema` 中返回这个值。

## 支持的目录结构

单个题目交付包：

```text
<bundle>/
  run.json
  package/
    config/package.json
    statements/statement.md
    ... native CPIdeas package files ...
```

批量交付包：

```text
<bundle>/
  batch_index.json
  <run-dir>/
    run.json
    package/
      config/package.json
      statements/statement.md
      ... native CPIdeas package files ...
```

私有 CPIdeas Plus 仓库中的 `cpideas export-run --mode product` 会生成这个结构。

## 必需文件

`run.json` 必须是 JSON object，并包含：

- `schema_version`：`cpideas.product_run.v1`。
- `export_mode`：`product`。
- `seed`：用于展示的轻量来源信息。
- `formal_statement`：兼容现有 run reader 的题面字段。
- `idea`：viewer 展示用的产品题面 payload。
- `native_package_path`：通常为 `package`。

`package/config/package.json` 描述题目包元信息。viewer 会用它定位 statement 文件并展示 package metadata。

`package/statements/statement.md` 或 package config 中的 `statements.default` 路径应存在，用作题面 fallback。

## 可选文件

下列文件缺失时，viewer 仍可打开交付包；如果存在，则会只读展示：

- `review.md` 和 `review.<locale>.md`。
- `preview.md` 和 `preview.<locale>.md`。
- `verification/cpideas_report.json`。
- `verification/cpideas_generate_report.json`。
- `package/` 下的文本文件。

## 不应交付的内部文件

Product bundle 不应依赖或暴露私有生成产物：

- `prompts/`。
- `ai_outputs/`。
- `checkpoints/`。
- `repairs/`。
- `ui_jobs/`。
- `submissions/`。

只读 viewer 即使遇到这些路径，也会隐藏并拒绝读取。

## Viewer Capabilities

Public viewer mode 不注入 action backend。能力接口返回：

```json
{
  "actions_enabled": false,
  "submission_enabled": false,
  "internal_files_enabled": false,
  "mode": "viewer",
  "supported_bundle_schema": "cpideas-plus-product-v1"
}
```

私有 workbench mode 可以在自己的仓库中注入 action backend，但这不是 public viewer 的交付范围。

## 兼容规则

- `run.json`、`batch_index.json` 和 package config 允许添加字段。
- 在 `cpideas-plus-product-v1` 生命周期内，已有字段语义应保持稳定。
- 破坏 schema 的修改需要新的 `supported_bundle_schema` 值和 contract tests。
- viewer 应忽略未知字段，并对不安全路径或非法 JSON 清晰失败。
