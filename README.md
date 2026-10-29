# 超星汇雅图像阅读器转 PDF

一个 Python 命令行工具：输入超星汇雅电子书的图像阅读网址，读取该阅读器声明的页面清单，下载书页原图并按顺序合并为 PDF。

适用于在页面中提供 `pages` 和 `jpgPath` 的图像阅读器结构。不支持所有版本或任意电子书网站，也不能补齐网站没有提供的页面。

## 功能

- 自动读取各部分页数、图片路径和书名。
- 自定义保存目录，留空使用脚本旁的 `downloads`。
- 下载前设置并发数、请求间隔，显示推荐设置及经验耗时区间。
- 断点续传、缓存校验、临时网络错误重试。
- 单页失败时可重试、跳过、重新开始或退出。
- 异常时显示可能原因和处理建议，重新开始时保留已下载图片。
- 跳过页面的 PDF 标注“缺页”，报告列出缺页信息。

## 快速开始

需要 Python 3.10 或更新版本。

```powershell
python -m pip install -r requirements.txt
python sslibrary_to_pdf.py
```

Windows 也可以双击 `启动.cmd`。启动器优先使用本机 Codex 附带的 Python，没有时使用 PATH 中的 Python；缺少依赖时尝试安装。

按提示依次输入图像阅读网址、保存目录、并发数量和请求间隔。推荐设置是经验起点，不能保证不会被服务器限流。预计耗时不是实测承诺。

```powershell
# 只查看页面清单
python sslibrary_to_pdf.py --inspect

# 先下载前 3 页检查效果
python sslibrary_to_pdf.py --sample 3

# 指定输出目录
python sslibrary_to_pdf.py -o "D:\电子书"

# 自动运行，失败时不等待交互
python sslibrary_to_pdf.py "图像阅读网址" --workers 3 --delay 0.35 --non-interactive
```

完整操作、异常恢复和输出文件说明见 [使用说明](使用说明.md)。

## 测试

测试使用本地模拟阅读器，不访问真实图书网站。

```powershell
python -m unittest -v test_sslibrary_to_pdf.py
```

## 使用边界

只处理你有权访问和保存的内容。不破解登录、验证码、加密或访问限制。需要登录时可提供本人会话的 cookies 文件；脚本不自动继承浏览器登录状态。

不要向仓库上传电子书图片、PDF、cookies、访问令牌或带临时会话参数的真实阅读链接。默认输出目录和常见会话文件已经加入 `.gitignore`，自定义输出目录也应放在仓库外。

PDF 页数通过检查不等于内容完整：是否包含网页声明的全部页面，需检查报告的 `complete` 字段。图片形式的验证码或错页响应仍可能需要人工检查。
