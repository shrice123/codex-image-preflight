"""A local Windows interface for preparing screenshots and image attachments."""

import os
import sys
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, ttk

from PIL import Image, ImageGrab

from preflight import ENCODED_BATCH_LIMIT, Source, clipboard_sources, image_file_extensions, prepare
from windows_clipboard import clipboard_owner, copy_files, sequence
from windows_paste import foreground_codex, last_input, paste_files, wait_for_shortcut_release


class ImagePrep:
    def __init__(self, root: tk.Tk, sources=None, batch=None, error=None):
        self.root = root
        self.root.title("Codex 图片附件压缩")
        self.root.minsize(595, 369)
        self.sources = list(sources or ())
        self.screenshot_count = sum(isinstance(source.image, Image.Image) for source in self.sources)
        self.batch = batch
        self.executor = ThreadPoolExecutor(max_workers=1)

        panel = ttk.Frame(root, padding=16)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(panel)
        toolbar.grid(row=0, sticky="ew", pady=(0, 10))
        self.files_button = self.button(toolbar, "选择图片…", self.add_files)
        self.clipboard_button = self.button(toolbar, "添加剪贴板图片", self.add_clipboard)
        self.remove_button = self.button(toolbar, "移除所选", self.remove_selected)

        table = ttk.Frame(panel)
        table.grid(row=1, sticky="nsew")
        self.items = ttk.Treeview(table, columns=("name", "size"), show="headings", height=8)
        self.items.heading("name", text="图片")
        self.items.heading("size", text="压缩后大小")
        self.items.column("name", width=350, minwidth=210)
        self.items.column("size", width=125, minwidth=100, anchor="e", stretch=False)
        self.items.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.items.yview)
        scroll.pack(side="right", fill="y")
        self.items.configure(yscrollcommand=scroll.set)
        self.items.bind("<<TreeviewSelect>>", lambda _: self.update_controls())

        self.status = tk.StringVar(value="选择图片，或从剪贴板添加截图和已复制的图片文件。")
        status = ttk.Label(panel, textvariable=self.status, wraplength=550, justify="left")
        status.grid(row=2, sticky="w", pady=(10, 8))

        actions = ttk.Frame(panel)
        actions.grid(row=3, sticky="ew")
        self.start_button = self.button(actions, "压缩这批图片", self.start)
        self.folder_button = self.button(actions, "打开附件目录", self.open_directory)
        self.copy_button = self.button(actions, "复制附件文件", self.copy)

        self.job = None
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        if error:
            self.status.set(error)
        if sources is None:
            self.root.after(0, self.read_initial_inputs)

    @staticmethod
    def button(parent, label, command):
        button = ttk.Button(parent, text=label, command=command)
        button.pack(side="left", padx=(0, 8))
        return button

    def read_initial_inputs(self):
        if len(sys.argv) > 1:
            self.add_paths(sys.argv[1:])
        else:
            self.add_clipboard(quiet=True)
        if self.sources:
            self.start()

    def update_controls(self):
        busy = self.job is not None
        self.files_button.state(["disabled" if busy else "!disabled"])
        self.clipboard_button.state(["disabled" if busy else "!disabled"])
        self.remove_button.state(["!disabled" if self.items.selection() and not busy else "disabled"])
        self.start_button.state(["!disabled" if self.sources and not busy else "disabled"])
        for button in (self.folder_button, self.copy_button):
            button.state(["!disabled" if self.batch and not busy else "disabled"])

    def refresh(self):
        self.items.delete(*self.items.get_children())
        for index, source in enumerate(self.sources):
            size = f"{self.batch.sizes[index]:,} B" if self.batch else "—"
            self.items.insert("", "end", iid=str(index), values=(source.name, size))
        self.update_controls()

    def sources_changed(self):
        self.batch = None
        if self.sources:
            self.status.set(f"已添加 {len(self.sources)} 张。压缩前可继续添加或移除。")
        else:
            self.status.set("请选择图片，或从剪贴板添加截图。")
        self.refresh()

    def add_paths(self, paths):
        supported = image_file_extensions()
        existing = {source.image for source in self.sources if isinstance(source.image, Path)}
        additions = []
        for value in paths:
            path = Path(value)
            if path.suffix.lower() in supported and path.is_file() and path not in existing:
                additions.append(Source(path.name, path))
                existing.add(path)
        if additions:
            self.sources.extend(additions)
            self.sources_changed()
        return len(additions)

    def add_files(self):
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="选择要放进同一条消息的图片",
            filetypes=[
                ("常用图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff"),
                ("所有文件", "*.*"),
            ],
        )
        if paths and not self.add_paths(paths):
            self.status.set("没有新的可读取图片。")

    def add_clipboard(self, quiet=False):
        try:
            contents = ImageGrab.grabclipboard()
            if isinstance(contents, Image.Image):
                self.screenshot_count += 1
                self.sources.append(Source(f"截图 {self.screenshot_count}.png", contents))
                self.sources_changed()
                return
            if isinstance(contents, list) and self.add_paths(contents):
                return
        except OSError as error:
            if not quiet:
                self.status.set(f"读取剪贴板失败：{error}")
            return
        if not quiet:
            self.status.set("剪贴板没有新的图片或图片文件。")

    def remove_selected(self):
        for index in sorted(map(int, self.items.selection()), reverse=True):
            del self.sources[index]
        self.sources_changed()

    def start(self):
        self.batch = None
        self.status.set(f"正在压缩 {len(self.sources)} 张图片…")
        self.job = self.executor.submit(prepare, tuple(self.sources))
        self.refresh()
        self.root.after(75, self.check_job)

    def check_job(self):
        if not self.job.done():
            self.root.after(75, self.check_job)
            return
        try:
            self.batch = self.job.result()
        except (ValueError, OSError, RuntimeError) as error:
            self.status.set(f"压缩失败：{error}")
        else:
            batch = self.batch
            self.status.set(
                f"{len(batch.files)} 张 · 文件合计 {sum(batch.sizes):,} B · "
                f"编码后估算 {batch.estimated_bytes:,} / {ENCODED_BATCH_LIMIT:,} B"
            )
        finally:
            self.job = None
            self.refresh()

    def open_directory(self):
        try:
            os.startfile(self.batch.directory)
        except OSError as error:
            self.status.set(f"打开目录失败：{error}")

    def copy(self):
        try:
            copy_files(self.root.winfo_id(), self.batch.files)
        except (OSError, ValueError) as error:
            self.status.set(f"复制失败：{error}")
        else:
            self.status.set("已复制附件文件。若 Codex 未识别粘贴，请从附件目录拖入。")

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    target = foreground_codex() if len(sys.argv) == 1 else None
    if target and wait_for_shortcut_release():
        paste_from_clipboard(target)
        return
    root = tk.Tk()
    ImagePrep(root)
    root.mainloop()


def paste_from_clipboard(target: int):
    if foreground_codex() != target:
        return
    sources = ()
    batch = None
    try:
        gesture = last_input()
        owner = clipboard_owner()
        contents = ImageGrab.grabclipboard()
        clipboard = sequence()
        if clipboard_owner() != owner or foreground_codex() != target or last_input() != gesture:
            return
        sources = clipboard_sources(contents)
        if not sources:
            raise ValueError("剪贴板中没有可压缩的一组图片。")
        batch = prepare(sources)
        if foreground_codex() != target or last_input() != gesture or sequence() != clipboard:
            return
        root = tk.Tk()
        root.withdraw()
        try:
            root.update_idletasks()
            paste_files(root.winfo_id(), target, gesture, clipboard, batch.files)
        except (OSError, ValueError) as error:
            root.destroy()
            raise ValueError(f"压缩已完成。自动粘贴未完成：{error}") from error
        else:
            root.destroy()
            return
    except (ValueError, OSError, RuntimeError) as error:
        if foreground_codex() != target:
            return
        root = tk.Tk()
        ImagePrep(root, sources=sources, batch=batch, error=str(error))
        root.mainloop()


if __name__ == "__main__":
    main()
