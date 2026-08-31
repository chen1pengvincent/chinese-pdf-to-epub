# 来源与致谢

本项目基于 [phuc-nt/scan-to-ebook](https://github.com/phuc-nt/scan-to-ebook)
进行定向改造，固定基线为提交
[`01b3dbb3ac35cd0c67c211f19aa2851cd05b3ab3`](https://github.com/phuc-nt/scan-to-ebook/commit/01b3dbb3ac35cd0c67c211f19aa2851cd05b3ab3)。

我们向原作者 **phucnt** 致敬并感谢其公开的 MIT 许可实现。新项目保留了原始
MIT 版权与许可声明，并在以下方向做了面向中文扫描书的重构：

- 公开 CLI 只暴露本地 PDF、现代横排中文和固定 OpenCode Go 视觉模型主链；
- 将显式页清单、缓存来源绑定和混合 EPUB 设为唯一主流程；
- 增加失败页原图兜底、重复页段报告、非破坏性修订和章节导航门禁；
- 增加 EPUB 页守恒、图片哈希、最大 XHTML、外部资源和敏感信息验证；
- 删除 Google Drive、rclone、漫画和真实书籍示例；
- 为了上游兼容和回归审查，源码与离线测试仍保留部分越南语、日语提示词选择和兼容模块；
  它们不是当前公开 CLI 的支持承诺，也没有经过本项目的真实多语种 E2E 验收。

上游文件的版权仍归原作者所有；本项目新增与修改部分同样以 MIT 许可证发布。
