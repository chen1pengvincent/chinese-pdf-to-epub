# 参与贡献

欢迎提交 Issue 和 Pull Request。请遵守以下最小门槛：

1. 不提交任何真实书籍、扫描页、OCR 原文、API Key、请求账本或本机绝对路径。
2. OCR 网络调用不得进入 CI；集成测试使用合成页面和模拟响应。
3. 任何会改变页状态、缓存复用、导航或 EPUB 构建的修改，都必须同时增加失败路径测试。
4. 运行：

   ```bash
   python -m pytest
   ruff check .
   python -m build
   python scripts/prepublish_check.py .
   ```

5. 对上游派生代码的版权和来源说明不得删除。
