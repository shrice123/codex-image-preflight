# Codex 图片附件压缩

**[下载 Windows x64 版 EXE](https://github.com/shrice123/codex-image-preflight/releases/latest/download/Codex-Image-Prep-Windows-x64.exe)**

下载后双击 `Codex-Image-Prep-Windows-x64.exe`，看到启动提示即可回到 Codex。无需安装 Python，也无需解压。EXE 控制在 10 MB 以内。再次双击同一个 EXE，确认后停止。[查看版本、ZIP 和校验值](https://github.com/shrice123/codex-image-preflight/releases/latest)

更新时，先双击旧版并确认停止，再双击新版启动。

## 日常使用

- 在 Windows Codex 桌面端按 **Ctrl+V** 粘贴截图或图片文件，也可以从资源管理器拖入图片，或用 **＋ → 文件和文件夹** 选择本地图片。看到缩略图后照常发送。
- 同一条消息有多张图时，建议一次选中一起添加。单张图片最多 99,999 字节，整批图片的 Base64 长度加上估算的附件开销最多 200,000 字节。分几次添加时，每批独立计算。正文和历史上下文也占完整请求的空间。
- 压缩在本机完成，原图保留，压缩后的文件写入 Windows 临时目录。Windows 重启后需要重新双击启动。
- 在选择窗口中，图片按同一批次压缩，混选的普通文件使用原路径。网页端和右键粘贴按原有方式工作。

独立 EXE 支持 JPEG、PNG、WebP、GIF、BMP 和 TIFF 等常用格式。AVIF 可使用源码版处理。

## 从源码运行

安装 Windows x64 Python 3.11 后，在源码目录打开 PowerShell。

```powershell
py -3.11 -m pip install Pillow==12.3.0 pywin32==312
py -3.11 -B smart_paste.py
```

保持窗口打开。可以另开 PowerShell，在同一目录执行 `py -3.11 -B smart_paste.py --status` 查询状态，或执行 `py -3.11 -B smart_paste.py --stop` 停止。

源码目录也提供 `image_prep.py` 的手动批量压缩界面。运行 `Install-Shortcut.ps1` 可创建该界面的桌面快捷方式。

## 构建 Windows EXE

```powershell
py -3.11 -m pip install -r requirements-build.txt
py -3.11 build_windows.py dist
```

构建脚本检查 EXE 能否启动 Windows 监听器，并输出 EXE、ZIP 和 SHA-256 校验文件。第三方许可随 EXE 和 ZIP 一起分发。
