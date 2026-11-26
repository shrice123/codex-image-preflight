"""Prepare bounded image files before attaching them to a Codex message."""

from dataclasses import dataclass
from io import BytesIO
from math import sqrt
from pathlib import Path
from tempfile import mkdtemp

from PIL import Image, ImageOps


FILE_LIMIT = 99_999
ENCODED_BATCH_LIMIT = 200_000
ATTACHMENT_OVERHEAD = 128
MAX_EDGE = 2560
FORMATS = {"PNG": ".png", "JPEG": ".jpg"}


@dataclass(frozen=True)
class Source:
    name: str
    image: Path | Image.Image


def image_file_extensions() -> set[str]:
    return {
        extension for extension, format_name in Image.registered_extensions().items()
        if format_name in Image.OPEN and Image.MIME.get(format_name, "image/").startswith("image/")
    }


def clipboard_sources(contents: Image.Image | list[str] | None) -> tuple[Source, ...]:
    if isinstance(contents, Image.Image):
        return (Source("截图.png", contents),)
    if isinstance(contents, list) and contents:
        paths = tuple(dict.fromkeys(Path(value) for value in contents))
        supported = image_file_extensions()
        if all(path.suffix.lower() in supported and path.is_file() for path in paths):
            return tuple(Source(path.name, path) for path in paths)
    return ()


@dataclass(frozen=True)
class EncodedImage:
    content: bytes
    format: str

    @property
    def base64_units(self) -> int:
        return (len(self.content) + 2) // 3


@dataclass(frozen=True)
class Batch:
    directory: Path
    files: tuple[Path, ...]
    sizes: tuple[int, ...]
    estimated_bytes: int


def encode(image: Image.Image, format_name: str, **options) -> EncodedImage:
    with BytesIO() as output:
        image.save(output, format_name, **options)
        return EncodedImage(output.getvalue(), format_name)


def fit_image(image: Image.Image, limit: int, prefer_png: bool) -> EncodedImage:
    oriented = ImageOps.exif_transpose(image)
    oriented.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    if "A" in oriented.getbands() or "transparency" in oriented.info:
        oriented = oriented.convert("RGBA")
        prepared = Image.new("RGB", oriented.size, "white")
        prepared.paste(oriented, mask=oriented.getchannel("A"))
    else:
        prepared = oriented.convert("RGB")
    prepared.info.clear()

    if prefer_png:
        oriented.info.clear()
        png = encode(oriented if oriented.mode in {"1", "L", "RGB", "RGBA"} else prepared, "PNG")
        if len(png.content) <= limit:
            return png

    while True:
        for quality in (91, 83, 74):
            jpeg = encode(prepared, "JPEG", quality=quality, optimize=True, subsampling=0)
            if len(jpeg.content) <= limit:
                return jpeg

        width, height = prepared.size
        if max(width, height) <= 128:
            png = encode(prepared, "PNG")
            if len(png.content) <= limit:
                return png
        if max(width, height) == 1:
            raise ValueError(f"每张分配的空间只有 {limit:,} 字节，请减少这次的图片数量。")

        scale = min(0.88, max(0.50, 0.95 * sqrt(limit / len(jpeg.content))))
        prepared = prepared.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS
        )


def fit_source(source: Source, limit: int) -> EncodedImage:
    try:
        if isinstance(source.image, Image.Image):
            return fit_image(source.image, limit, prefer_png=True)

        with source.image.open("rb") as stream, Image.open(stream) as image:
            format_name = image.format
            if format_name in FORMATS and getattr(image, "n_frames", 1) == 1:
                stream.seek(0)
                original = stream.read(limit + 1)
                if len(original) <= limit:
                    image.load()
                    return EncodedImage(original, format_name)
            return fit_image(image, limit, prefer_png=format_name != "JPEG")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValueError(f"{source.name}：{error}") from error


def distribute_units(images: list[EncodedImage], available: int) -> list[int]:
    """Leave compact images alone and share the remaining space among larger ones."""
    allocated = [0] * len(images)
    ranked = sorted(range(len(images)), key=lambda index: images[index].base64_units)
    for position, index in enumerate(ranked):
        share = available // (len(ranked) - position)
        allocated[index] = min(images[index].base64_units, share)
        available -= allocated[index]
    return allocated


def prepare(sources: tuple[Source, ...], batch_limit: int = ENCODED_BATCH_LIMIT) -> Batch:
    if not sources:
        raise ValueError("请先添加图片。")

    overhead = ATTACHMENT_OVERHEAD * len(sources)
    available = (batch_limit - overhead) // 4
    if available < len(sources):
        raise ValueError("这次的图片数量超过附件预算，请分批添加。")

    images = [fit_source(source, FILE_LIMIT) for source in sources]
    if sum(image.base64_units for image in images) > available:
        allocations = distribute_units(images, available)
        images = [
            fit_source(source, 3 * units) if image.base64_units > units else image
            for source, image, units in zip(sources, images, allocations, strict=True)
        ]

    sizes = tuple(len(image.content) for image in images)
    estimated = 4 * sum(image.base64_units for image in images) + overhead
    if max(sizes) > FILE_LIMIT or estimated > batch_limit:
        raise RuntimeError("附件未通过大小校验。")

    directory = Path(mkdtemp(prefix="Codex-attachments-"))
    files = tuple(
        directory / f"{index:03d}-{Path(source.name).stem[:80].rstrip(' .')}{FORMATS[image.format]}"
        for index, (source, image) in enumerate(zip(sources, images, strict=True), start=1)
    )
    try:
        for file, image in zip(files, images, strict=True):
            file.write_bytes(image.content)
        if tuple(file.stat().st_size for file in files) != sizes:
            raise OSError("写入后的附件大小与校验结果不一致。")
    except OSError:
        for file in files:
            file.unlink(missing_ok=True)
        directory.rmdir()
        raise

    return Batch(directory, files, sizes, estimated)
