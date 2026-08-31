# 安全说明

## 报告漏洞

若仓库的 **Security** 页面提供 **Report a vulnerability**，请优先通过 GitHub Security
Advisories 私下报告漏洞。若该入口尚未启用，请先通过维护者 GitHub 主页上的公开联系方式
请求一个私密报告渠道；公开消息中只说明“需要安全联系方式”，不要附漏洞细节、API Key、
原书页面、OCR 响应或本机路径。

## 凭据边界

- CLI 不接受 `--api-key`，只读取当前进程的 `OPENCODE_GO_API_KEY`。
- 项目不会自动读取 `.env`，也不会把凭据写入运行目录、日志或 EPUB。
- OCR 结束后应执行 `unset OPENCODE_GO_API_KEY`；在同一 shell 中不清除时，后续进程仍可能
  继承该变量。Poppler、Pandoc、`file`、EPUBCheck 和 Git 扫描子进程会移除凭据样式环境变量。
- 页面图像会发送给第三方 OpenCode Go 服务；运行前请确认你有权上传并转换这些页面，
  并自行核对服务商当前的数据保留和隐私政策。
- 请求台账仅保存去敏后的模型、页面哈希、token 用量和状态，不保存图片、提示词正文、
  Authorization 头或响应正文。

## 本地书籍与断点数据

`books/`、`scans/`、`work/`、PDF、EPUB、请求台账、OCR 原文和修订文件都可能包含受版权
保护或隐私敏感的内容。它们默认被 `.gitignore` 排除，但 `.gitignore` 不是访问控制；分享
压缩包、复制目录或使用强制添加前仍需人工检查。逐页 checkpoint 和请求台账用于恢复与
审计，不应上传到 Issue、公开仓库或第三方日志服务。

`scripts/prepublish_check.py` 会扫描工作树、发行归档和当前可见 Git 历史，并且只报告规则名，
不回显命中的值。它是发布门禁，不是形式化的“绝无秘密”证明；发布前仍应核对 Git remote
不含 userinfo/token、提交作者邮箱符合公开预期，并在 fresh clone 中复跑扫描。

## 不可信内容

PDF 页面和 OCR Markdown 一律视为不可信数据。构建前会移除 OCR 生成的图片、链接、
原始 HTML 和脚本；EPUB 只允许构建器生成的本地图片引用。
