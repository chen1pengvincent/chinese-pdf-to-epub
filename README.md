# chinese-pdf-to-epub

[![CI](https://github.com/chen1pengvincent/chinese-pdf-to-epub/actions/workflows/ci.yml/badge.svg)](https://github.com/chen1pengvincent/chinese-pdf-to-epub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](pyproject.toml)

把现代横排中文扫描 PDF 转成**可审计、可恢复的混合 EPUB**：普通正文可重排；
图表、复杂公式和表格同时保留原页图像；空白、不支持版式或 OCR 失败的页面使用原图兜底。

项目的核心承诺不是“OCR 永不出错”，而是：

> 对每个纳入范围的 PDF 源页，要么生成可重排文字，要么保留原始页图；绝不静默丢页、
> 自动删除疑似重复页，或根据上下文补写源文件中不存在的内容。

当前为 **0.1.0 Alpha**。请先用代表页小样验证自己的书，再执行整本转换。

## 为什么有这个项目

对一次 700+ 页中文扫描书转换工程的复盘暴露出，真正困难的不只是 OCR：源 PDF 可能重复或缺页，
纸质目录可能污染电子目录，复杂页面不能只靠纯文本表达，失败重试会串缓存，最终 EPUB
也可能“能打开但已丢页”。本项目把这些工程经验收回到默认主流程；这段历史经验不等于
当前版本已经完成真实整书 E2E：

- **显式页清单**：PDF 页数、人工排除页、每页图像哈希和最终表达方式是一套事实源。
- **混合 EPUB 是唯一构建路径**：`reflow`、`hybrid`、`image` 三种终态，不走纯文本捷径。
- **首次请求 + 最多 5 次恢复尝试**：请求台账在联网前落盘；跨进程、跨次运行累计后，
  每页绝对上限仍为 6 次。
- **逐页断点**：每个已完成页面在 worker 返回时立即把正文与来源记录写入 checkpoint，
  不等整批结束才保存；重启时先核验来源、模型、提示词、上下文和页图，再决定是否复用。
- **缓存绑定**：页图、渲染参数、固定模型、提示词和上下文任一变化都拒绝复用旧结果。
- **非破坏性修订**：原始 OCR 缓存只读；精确替换和重试覆盖进入派生层并记录前后哈希。
- **源缺陷只报告**：精确重复图和近重复文字段会进入审计报告，但不会自动删页或补页。
- **导航去伪**：纸质目录页出现多个章节标题时标为可疑；EPUB 目录只使用独立导航映射。
- **严格验收**：页锚点守恒、原图哈希、内部引用、最大 XHTML、外部资源、敏感信息，
  并可强制 EPUBCheck `--failonwarnings`。

## 支持边界

当前公开 CLI 的设计与支持边界是：

- 本地 PDF；
- 现代横排中文单页，或已确认左页先读的横排双页；
- OpenCode Go 的固定模型 `deepseek-v4-flash-vision-exp`；
- macOS / Linux，Python 3.10+；
- Poppler 用于独立页数核验、原图提取和复杂页渲染；Pandoc 用于生成 EPUB。

不支持或不承诺：竖排古籍、复杂眉批夹注、DRM、翻译、自动修复源 PDF 缺页、自动发布
受版权保护的书籍、Google Drive、rclone、漫画/CBZ/CBR/MOBI。超出版式边界时保留原图，
不会猜测重排。

## 安装

先安装系统工具：

```bash
# macOS
brew install poppler pandoc

# Debian / Ubuntu
sudo apt-get install poppler-utils pandoc
```

然后安装项目：

```bash
git clone https://github.com/chen1pengvincent/chinese-pdf-to-epub.git
cd chinese-pdf-to-epub
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/zhpdf2epub doctor
```

正式验收建议另外安装 [EPUBCheck](https://github.com/w3c/epubcheck/releases)。项目不会自动下载
或执行未知二进制；没有 EPUBCheck 时只能声明“通过内置校验”，不能声明 EPUBCheck 正式验收。

## 安全设置 API Key

CLI 不接受 `--api-key`，也不会自动读取 `.env`。请只在本机终端隐藏输入并导出到当前进程。
zsh 使用：

```zsh
read -s "OPENCODE_GO_API_KEY?OpenCode Go API Key: "
export OPENCODE_GO_API_KEY
printf '\n'
```

Bash 使用：

```bash
read -rsp 'OpenCode Go API Key: ' OPENCODE_GO_API_KEY
export OPENCODE_GO_API_KEY
printf '\n'
```

转换完成后，从当前 shell 清除它：

```bash
unset OPENCODE_GO_API_KEY
```

不要把真实 Key 写进命令历史、Issue、聊天、截图或仓库。页面图像会发送给第三方视觉模型；
运行前请确认你有权上传和转换这些内容，并自行核对服务商当前的数据保留与隐私政策。

## 推荐工作流

### 1. 导入 PDF 并冻结源页清单

```bash
.venv/bin/zhpdf2epub init ./book.pdf ./books/my-book
```

只有在人工确认某页不属于书籍时才排除，并必须写理由：

```bash
.venv/bin/zhpdf2epub init ./book.pdf ./books/my-book \
  --exclude-pages 120 \
  --exclude-reason "人工确认：扫描包附带的非书籍说明页"
```

排除中间页不会重新编号后续页面。清单位于 `work/page-manifest.json`，只记录 PDF 文件名和
哈希，不保存本机绝对路径。

### 2. 先做代表页小样

```bash
.venv/bin/zhpdf2epub smoke ./books/my-book --title "书名"
```

程序从全书均匀抽取代表页，单线程 OCR，并生成 `work/smoke.epub`。请人工检查简繁、标点、
专名、表格、图表、公式和章节标题；程序无法替你证明 OCR 与原书逐字一致。

### 3. 完整转换

```bash
.venv/bin/zhpdf2epub run ./books/my-book \
  --title "书名" \
  --author "作者" \
  --workers 4 \
  --yes
```

完成后得到：

```text
books/my-book/
├── scans/
│   ├── archive/              # EPUB 使用的高保真页图
│   └── ocr/                  # API 输入图；默认与存档图同字节
├── work/
│   ├── page-manifest.json    # 唯一事实源
│   ├── context.json          # 与页图/模型/提示词哈希绑定
│   ├── request-ledger.jsonl  # 联网前持久化的请求尝试台账
│   ├── ocr/raw/              # 原始 OCR，禁止直接修改
│   ├── ocr/final/            # 清洗和修订后的派生层
│   ├── navigation.json       # 独立章节导航映射
│   └── audit/                # 源缺陷与验收报告
└── dist/
    ├── book.epub
    └── acceptance.json
```

若要求 EPUBCheck 不得缺席：

```bash
.venv/bin/zhpdf2epub verify ./books/my-book --require-epubcheck
```

## 人工修订、重新构建与导航

`work/corrections.json` 是可选的精确修订契约。替换次数不等于声明值时立即失败：

```json
{
  "schema_version": 1,
  "overrides": [],
  "replacements": [
    {
      "page": 42,
      "old": "待核错字",
      "new": "核验后文字",
      "expected_count": 1
    }
  ]
}
```

不要直接改 `work/ocr/raw/`。若要用人工复核或独立重试产生的整页 Markdown 覆盖失败页，
先把文件放进 `work/retries/`，再同时声明页号、相对路径、源图 SHA-256 和 Markdown
SHA-256。下面的尖括号是占位符，不能原样复制：

```json
{
  "schema_version": 1,
  "overrides": [
    {
      "page": 42,
      "markdown": "work/retries/page_000042.md",
      "source_sha256": "<page-manifest.json 中第 42 页的 archive_sha256>",
      "markdown_sha256": "<page_000042.md 的 SHA-256>"
    }
  ],
  "replacements": []
}
```

初次 `run` 会完成 OCR、生成派生层并构建 EPUB。之后修改 `corrections.json` 时，不再调用
API；用第二阶段命令显式重建：

```bash
.venv/bin/zhpdf2epub build ./books/my-book \
  --title "书名" \
  --author "作者" \
  --rederive-final
```

`--rederive-final` 会把旧的 `work/ocr/final/` 归档到
`work/ocr/final-history/<版本指纹>/`，再按当前修订生成新派生层。若修订输入已变化但没有传
该参数，命令会拒绝覆盖。`build` 本身不读取 API Key，也不发起网络请求。

## 封面与导航契约

`work/context.json` 的 `cover_page` 只能是纳入页对应的 OCR 图像文件名，例如
`page_000001.jpg`；不能写绝对路径、`scans/archive/...` 或任意外部文件。值为 `null` 时不
生成封面；文件名不能唯一映射到页清单时构建失败。封面使用对应的高保真存档图，并在验收
时核对哈希。人工只应在核对原页后调整 `cover_page`；不要修改程序写入的页图、模型或提示词
绑定字段。

自动导航只接受单页、唯一的中文章节标题候选。若源书缺少真实章节起始页，可在
`work/navigation.json` 的 `entries` 中显式增加恢复锚点，但不能冒充原始章节页。下面是
最小合法顶层结构；若文件已由程序生成，应把新对象合并进现有 `entries`，保留已有的
`text_fingerprint`、候选记录和其他导航项，不要用示例覆盖整份文件：

```json
{
  "schema_version": 1,
  "entries": [
    {
      "source_page": 527,
      "title": "第十六章（恢复锚点）",
      "source": "recovery-anchor",
      "status": "source-start-missing"
    }
  ]
}
```

`source_page` 必须是页清单中的纳入页；页号和标题在导航项中都必须唯一。修订导致 OCR
派生文本变化时，自动导航会按新指纹重建；存在 `manual` 或 `recovery-anchor` 项时会要求
人工重新核对，而不会静默迁移。

## 错误和退出状态

- `0`：命令通过其声明的门禁；对 `smoke`、`run`、`build` 而言，没有
  `failed`、`unsupported` 或 `ambiguous` 页。空白候选保留原图，但不单独触发退出码 2。
- `1`：配置、页守恒、缓存、构建或验证失败；`audit` 发现精确/近重复风险时也返回 1；
  认证、权限、端点或额度类系统性错误同样返回 1。
- `2`：`smoke`、`run` 或 `build` 已生成产物，但至少一页处于 `failed`、
  `unsupported` 或 `ambiguous` 终态，需要人工复核。

命令行参数本身写错时，Python `argparse` 也会在执行工作流之前返回 2，并同时打印
`usage:`。自动化调用方应结合命令输出及产物/验收报告是否生成来区分这两种情况。

429/5xx 会在每页上限内退避重试。超时或网络中断可能已经在服务端执行并计费，因此记录为
`ambiguous`，不伪装成成功；最终 EPUB 仍保留源页图。检测到 401/402/403/404 后会停止
**继续提交**新页面，未提交页保持 `pending`，不会批量伪装成失败。并发运行时，在错误被某个
worker 观察到之前，最多可能已有 `--workers` 个请求处于队列或执行中；这些请求仍可能完成
或计费，因此“停止继续提交”不等于撤销已在途请求。

逐页 checkpoint 只保证恢复已经返回本机并完成本地持久化的结果。服务端已执行、但客户端
尚未收到完整响应或尚未完成 checkpoint 的请求，无法被当作成功恢复；它们会按保守规则记入
请求台账，并可能标为 `ambiguous`。

## 架构

```text
inspect → import/render → source audit → representative smoke
       → page OCR + provenance → verified corrections
       → independent navigation → manifest-driven hybrid EPUB
       → internal verify → optional EPUBCheck → acceptance report
```

本项目直接用 `urllib` 调用完整 Chat Completions HTTP 地址，所以 endpoint 以
`/chat/completions` 结尾是有意设计。若未来改用 OpenAI SDK，`base_url` 只能配置到 `/v1`，
因为 SDK 会自行追加 `/chat/completions`；把完整 endpoint 填进 SDK `base_url` 会形成重复路径。

## 开发与验证

```bash
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
python scripts/prepublish_check.py .
```

提交补丁前请同时阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

CI 只使用合成页面和模拟 API 响应，不接触真实 Key、真实书籍或真实 OCR 服务。
它验证 Python 3.10/3.12、离线测试、静态检查、构建、敏感信息扫描和 fresh-wheel CLI
烟测；这些结果**不证明**固定视觉模型当前可用，也不证明真实 700+ 页书籍的逐字 OCR 质量
或完整线上 E2E。真实书籍仍必须按“代表页小样 → 人工复核 → 整本运行”执行。

## 来源与致敬

本项目基于 [phuc-nt/scan-to-ebook](https://github.com/phuc-nt/scan-to-ebook) 的 MIT 许可代码
改造，固定来源提交为
[`01b3dbb3ac35cd0c67c211f19aa2851cd05b3ab3`](https://github.com/phuc-nt/scan-to-ebook/commit/01b3dbb3ac35cd0c67c211f19aa2851cd05b3ab3)。
感谢原作者 **phucnt** 奠定的 PDF 导入、视觉 OCR、断点续跑和 EPUB 构建基础。
完整改造清单见 [ATTRIBUTION.md](ATTRIBUTION.md)。

## 许可证与版权

[MIT](LICENSE)。请只转换你有权处理的材料；转换工具和技术可用性不等于获得了复制、上传、
改编或传播原书的权利。

最后审阅：2026-08-31。
