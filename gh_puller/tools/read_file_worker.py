"""Container-side Read implementation; installed by content hash, without host imports.

Behavioral reference: OpenClaude 5cd11336, FileReadTool, notebook.ts and pdf.ts.
Pillow replaces the optional JS image processor; Poppler remains the PDF renderer.
"""

import base64
import difflib
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
BINARY_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "webp", "tiff", "tif", "mp4", "mov", "avi",
    "mkv", "webm", "wmv", "flv", "m4v", "mpeg", "mpg", "mp3", "wav", "ogg", "flac", "aac",
    "m4a", "wma", "aiff", "opus", "zip", "tar", "gz", "bz2", "7z", "rar", "xz", "z",
    "tgz", "iso", "exe", "dll", "so", "dylib", "bin", "o", "a", "obj", "lib", "app",
    "msi", "deb", "rpm", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods",
    "odp", "ttf", "otf", "woff", "woff2", "eot", "pyc", "pyo", "class", "jar", "war", "ear",
    "node", "wasm", "rlib", "sqlite", "sqlite3", "db", "mdb", "idx", "psd", "ai", "eps", "sketch",
    "fig", "xd", "blend", "3ds", "max", "swf", "fla", "lockb", "dat", "data",
}
BLOCKED_DEVICES = {f"/dev/{name}" for name in (
    "zero", "random", "urandom", "full", "stdin", "tty", "console", "stdout", "stderr", "fd/0", "fd/1", "fd/2",
)}
MAX_BYTES = 256 * 1024
MAX_TOKENS = 25000
IMAGE_MAX_RAW = 5 * 1024 * 1024 * 3 // 4


def file_size(size):
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def js_length(text):
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def validate_tokens(content, extension, maximum, path=None, total_lines=None):
    # OpenAI-compatible transports have no Anthropic countTokens endpoint.
    # Use upstream's documented fallback, including UTF-16 string length.
    ratio = 2 if extension in {"json", "jsonl", "jsonc"} else 4
    count = math.floor(js_length(content) / ratio + 0.5)
    if count <= maximum:
        return
    message = f"File content ({count:,} tokens, estimated) exceeds maximum allowed tokens ({maximum:,})."
    if total_lines is not None:
        message += f" The file has {total_lines:,} total lines."
    message += " Use offset and limit parameters to read specific portions of the file"
    message += ", or search for specific content instead of reading the whole file."
    if path:
        message += "\n" + "\n".join(json.dumps({"file_path": path, "offset": start, "limit": 200},
                                              ensure_ascii=False) for start in (1, 201))
    raise ValueError(message)


def page_range(pages):
    if pages is None:
        return None

    def number(value):
        match = re.match(r"\s*([+-]?\d+)", value)
        return int(match[1]) if match else 0

    trimmed = pages.strip()
    if trimmed.endswith("-"):
        first, last = number(trimmed[:-1]), math.inf
    elif "-" in trimmed:
        left, right = trimmed.split("-", 1)
        first, last = number(left), number(right)
    else:
        first = last = number(trimmed)
    if first < 1 or last < first:
        raise ValueError(f'Invalid pages parameter: "{pages}". Use formats like "1-5", "3", or "10-20". '
                         "Pages are 1-indexed.")
    if last - first + 1 > 20:
        raise ValueError(f'Page range "{pages}" exceeds maximum of 20 pages per request. Please use a smaller range.')
    return first, last


def expanded_path(raw, cwd):
    path = os.path.expanduser(raw.strip())
    return Path(os.path.abspath(os.path.join(cwd, path)))


def checked_path(raw, cwd):
    path = expanded_path(raw, cwd)
    resolved = str(path.resolve())
    if (str(path) in BLOCKED_DEVICES or resolved in BLOCKED_DEVICES
            or re.fullmatch(r"/proc/(?:self|thread-self|\d+)/fd/[012]", str(path))):
        raise ValueError(f"Cannot read '{raw}': this device file would block or produce infinite output.")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        # macOS screenshots use either a regular space or U+202F before AM/PM.
        alternate = Path(re.sub(r"[ \u202f](AM|PM)\.png$", lambda m:
                               ("\u202f" if m[0][0] == " " else " ") + m[1] + ".png", str(path)))
        if alternate != path and alternate.is_file():
            return alternate, alternate.stat()
        message = f"File does not exist. Note: your current working directory is {cwd}."
        if path.parent.is_dir():
            matches = difflib.get_close_matches(path.name, [p.name for p in path.parent.iterdir()], n=1)
            if matches:
                message += f" Did you mean {path.parent / matches[0]}?"
        raise FileNotFoundError(message) from exc
    if stat.S_ISDIR(metadata.st_mode):
        raise IsADirectoryError(f"EISDIR: illegal operation on a directory, read '{raw}'")
    if not stat.S_ISREG(metadata.st_mode) and str(path) != "/dev/null":
        raise ValueError(f"Cannot read '{raw}': not a regular file; use bash for streams and device files.")
    return path, metadata


def read_text(path, metadata, offset, limit, maximum):
    if limit is None and metadata.st_size > maximum:
        raise ValueError(f"File content ({file_size(metadata.st_size)}) exceeds maximum allowed size "
                         f"({file_size(maximum)}). Use offset and limit parameters to read specific portions "
                         "of the file, or search for specific content instead of reading the whole file.")
    begin = max(0, offset - 1)
    end = math.inf if limit is None else begin + limit
    if metadata.st_size < 10 * 1024 * 1024:
        raw = path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r", "")
        lines = raw.split("\n") if raw else []
        selected = lines[begin:] if limit is None else lines[begin:begin + limit]
        return "\n".join(selected), len(selected), len(lines)
    selected, total, last = [], 0, None
    # Count the entire file without retaining lines outside the selected range.
    with path.open(encoding="utf-8-sig", errors="replace", newline="\n") as stream:
        for total, last in enumerate(stream, 1):
            if begin <= total - 1 < end:
                selected.append(last.removesuffix("\n").replace("\r", ""))
    if last is not None and last.endswith("\n"):
        if begin <= total < end:
            selected.append("")
        total += 1
    return "\n".join(selected), len(selected), total


def image_data(raw, *, max_tokens=None):
    if not raw:
        raise ValueError("Image file is empty. Please provide a valid image.")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise RuntimeError("Image processing requires Pillow in the container: pip install Pillow") from exc
    try:
        source = Image.open(io.BytesIO(raw))
        source.load()
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError(f"Unable to read image: {exc}") from exc
    original = source.size
    media = (source.format or "PNG").lower()
    cap = min(IMAGE_MAX_RAW, max_tokens * 6) if max_tokens is not None else IMAGE_MAX_RAW
    display = source.copy()
    display.thumbnail((1568, 1568), Image.Resampling.LANCZOS)
    result = raw
    if display.size != original or len(raw) > cap or media not in {"png", "jpeg", "gif", "webp"}:
        encoded = io.BytesIO()
        display.save(encoded, format="PNG", optimize=True)
        result, media = encoded.getvalue(), "png"
    if len(result) > cap:
        # Lossless PNG first, then palette, then progressively smaller JPEGs.
        encoded = io.BytesIO()
        display.convert("RGB").quantize(colors=256).save(encoded, format="PNG", optimize=True)
        if len(encoded.getvalue()) < len(result):
            result, media = encoded.getvalue(), "png"
    if len(result) > cap:
        display = display.convert("RGB")
        for edge, quality in ((1568, 80), (1280, 65), (1024, 50), (800, 40), (600, 30), (400, 20)):
            display.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            encoded = io.BytesIO()
            display.save(encoded, format="JPEG", quality=quality, optimize=True)
            result, media = encoded.getvalue(), "jpeg"
            if len(result) <= cap:
                break
    if len(result) > cap:
        raise ValueError(f"Unable to compress image to fit within {file_size(cap)}. Please use a smaller image.")
    return {"base64": base64.b64encode(result).decode(), "type": f"image/{media}", "originalSize": len(raw),
            "dimensions": {"originalWidth": original[0], "originalHeight": original[1],
                           "displayWidth": display.width, "displayHeight": display.height}}


def image_block(data):
    return {"type": "image", "source": {"type": "base64", "media_type": data["type"], "data": data["base64"]}}


def joined(value):
    return "".join(value) if isinstance(value, list) else value or ""


def notebook(path, maximum, max_tokens, bash_name):
    book = json.loads(path.read_text(encoding="utf-8"))
    language = book.get("metadata", {}).get("language_info", {}).get("name", "python")
    cells, blocks = [], []

    def text(value):
        if blocks and blocks[-1]["type"] == "text":
            blocks[-1]["text"] += "\n" + value
        else:
            blocks.append({"type": "text", "text": value})

    for index, cell in enumerate(book["cells"]):
        kind = cell["cell_type"]
        item = {"cellType": kind, "source": joined(cell.get("source")), "cell_id": cell.get("id", f"cell-{index}")}
        if kind == "code":
            item["language"] = language
            if cell.get("execution_count"):
                item["execution_count"] = cell["execution_count"]
            outputs = []
            for entry in cell.get("outputs", []):
                output_kind = entry["output_type"]
                value = {"output_type": output_kind}
                if output_kind == "stream":
                    value["text"] = joined(entry.get("text"))
                elif output_kind in {"execute_result", "display_data"}:
                    data = entry.get("data", {})
                    value["text"] = joined(data.get("text/plain"))
                    for media in ("image/png", "image/jpeg"):
                        if isinstance(data.get(media), str):
                            value["image"] = {"image_data": re.sub(r"\s", "", data[media]), "media_type": media}
                            break
                elif output_kind == "error":
                    value["text"] = (f"{entry.get('ename')}: {entry.get('evalue')}\n"
                                     + "\n".join(entry.get("traceback", [])))
                else:
                    continue
                outputs.append(value)
            size = sum(js_length(o.get("text", "")) + len(o.get("image", {}).get("image_data", "")) for o in outputs)
            if size > 10000:
                outputs = [{"output_type": "stream", "text": f"Outputs are too large to include. Use {bash_name} "
                            f"with: cat <notebook_path> | jq '.cells[{index}].outputs'"}]
            if outputs:
                item["outputs"] = outputs
        cells.append(item)
        metadata = f"<cell_type>{kind}</cell_type>" if kind != "code" else (
            f"<language>{language}</language>" if language != "python" else "")
        text(f'<cell id="{item["cell_id"]}">{metadata}{item["source"]}</cell id="{item["cell_id"]}">')
        for output in item.get("outputs", []):
            if output.get("text"):
                text("\n" + output["text"])
            if output.get("image"):
                picture = output["image"]
                blocks.append(image_block({"type": picture["media_type"], "base64": picture["image_data"]}))
    content = json.dumps(cells, ensure_ascii=False, separators=(",", ":"))
    if len(content.encode()) > maximum:
        raise ValueError(f"Notebook content ({file_size(len(content.encode()))}) exceeds maximum allowed size "
                         f"({file_size(maximum)}). Use {bash_name} with jq to read specific portions:\n"
                         f"  cat \"{path}\" | jq '.cells[:20]' # First 20 cells\n"
                         f"  cat \"{path}\" | jq '.cells[100:120]' # Cells 100-120\n"
                         f"  cat \"{path}\" | jq '.cells | length' # Count total cells\n"
                         f"  cat \"{path}\" | jq '.cells[] | select(.cell_type==\"code\") | .source'"
                         " # All code sources")
    validate_tokens(content, "ipynb", max_tokens)
    return {"type": "notebook", "file": {"filePath": str(path), "cells": cells}, "blocks": blocks}


def pdf(path, metadata, pages, args):
    if not metadata.st_size:
        raise ValueError(f"PDF file is empty: {path}")
    maximum = 100 * 1024 * 1024 if pages else 20 * 1024 * 1024
    if metadata.st_size > maximum:
        raise ValueError(f"PDF file exceeds maximum allowed size of {file_size(maximum)}.")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError(f"File is not a valid PDF (missing %PDF- header): {path}")
    count = None
    if shutil.which("pdfinfo"):
        info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=10, check=False)
        if info.returncode:
            raise ValueError(f"Unable to read PDF: {info.stderr.strip()}")
        match = re.search(r"^Pages:\s+(\d+)", info.stdout, re.MULTILINE)
        count = int(match[1]) if match else None
    if pages is None:
        if count is not None and count > 10:
            raise ValueError(f"This PDF has {count} pages, which is too many to read at once. Use the pages "
                             'parameter to read specific page ranges (e.g., pages: "1-5"). '
                             "Maximum 20 pages per request.")
        if not args.get("pdf_supported", True):
            raise ValueError('Reading full PDFs is not supported with this model. Use a newer model, or use '
                             'the pages parameter to read specific page ranges (e.g., pages: "1-5", '
                             "maximum 20 pages per request). Page extraction requires poppler-utils.")
        raw = base64.b64encode(path.read_bytes()).decode()
        return {"type": "pdf", "file": {"filePath": str(path), "base64": raw, "originalSize": metadata.st_size},
                "blocks": [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                            "data": raw}, "filename": path.name}]}
    if not shutil.which("pdftoppm"):
        raise RuntimeError("PDF page extraction requires poppler-utils: install with apt-get install poppler-utils.")
    directory = Path(args["task_root"]) / "read-results" / ("pdf-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    process = subprocess.run(["pdftoppm", "-jpeg", "-r", "100", "-f", str(pages[0]), "-l", str(pages[1]),
                              str(path), str(directory / "page")], capture_output=True, text=True,
                             timeout=120, check=False)
    if process.returncode:
        raise ValueError(f"PDF page extraction failed: {process.stderr.strip()}")
    pictures = sorted(directory.glob("page-*.jpg"))
    if not pictures:
        raise ValueError("No pages were extracted from the PDF. Check that the page range exists.")
    return {"type": "parts", "file": {"filePath": str(path), "originalSize": metadata.st_size,
                                     "count": len(pictures), "outputDir": str(directory)},
            "blocks": [image_block(image_data(p.read_bytes())) for p in pictures]}


def read_file(args):
    pages = page_range(args.get("pages"))
    raw = args["file_path"]
    ext = Path(raw.strip()).suffix.lower().lstrip(".")
    if ext in BINARY_EXTENSIONS and ext not in IMAGE_EXTENSIONS | {"pdf"}:
        raise ValueError(f"This tool cannot read binary files. The file appears to be a binary .{ext} file. "
                         "Please use appropriate tools for binary file analysis.")
    if ext in IMAGE_EXTENSIONS and args.get("supports_vision") is False:
        raise ValueError("The current model does not support image input. Use a vision-capable model, "
                         "or use bash with file, identify or OCR to inspect this image as text.")
    path, metadata = checked_path(raw, args["cwd"])
    offset, limit = args.get("offset", 1), args.get("limit")
    maximum, max_tokens = args.get("max_bytes", MAX_BYTES), args.get("max_tokens", MAX_TOKENS)
    state = {"timestamp": metadata.st_mtime_ns // 1000000, "offset": offset, "limit": limit}
    previous = args.get("read_state", {}).get(str(path))
    if ext not in IMAGE_EXTENSIONS | {"pdf"} and previous and all(previous.get(k) == v for k, v in state.items()):
        return {"type": "file_unchanged", "file": {"filePath": raw}}
    if ext in IMAGE_EXTENSIONS:
        data = image_data(path.read_bytes(), max_tokens=max_tokens)
        return {"type": "image", "file": data, "blocks": [image_block(data)]}
    if ext == "pdf":
        return pdf(path, metadata, pages, args)
    if ext == "ipynb":
        result = notebook(path, maximum, max_tokens, args.get("bash_name", "bash"))
    else:
        content, line_count, total = read_text(path, metadata, offset, limit, maximum)
        validate_tokens(content, ext, max_tokens, raw, total)
        result = {"type": "text", "file": {"filePath": raw, "content": content, "numLines": line_count,
                                          "startLine": offset, "totalLines": total}}
    result["read_state"] = {"path": str(path), **state}
    return result


def shell_image(args):
    path = Path(args["file_path"])
    if path.stat().st_size > 20 * 1024 * 1024:
        return {"blocks": []}
    match = re.fullmatch(r"data:([^;]+);base64,(.+)", path.read_text(errors="replace").strip())
    if not match:
        return {"blocks": []}
    data = image_data(base64.b64decode(match[2]))
    return {"type": "image", "file": data, "blocks": [image_block(data)]}


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        response = shell_image(request) if request["action"] == "shell_image" else read_file(request)
    except Exception as error:
        response = {"error": {"type": type(error).__name__, "message": str(error)}}
    print(json.dumps(response, ensure_ascii=False))
