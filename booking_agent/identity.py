from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from PIL import Image


class OcrUnavailableError(RuntimeError):
    """Raised when local Tesseract OCR is not installed."""


@dataclass(frozen=True)
class PassportFields:
    document_number: str
    nationality: str


def validate_afm(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 9 or len(set(digits)) == 1:
        return False
    checksum = sum(int(digits[index]) * (2 ** (8 - index)) for index in range(8))
    return checksum % 11 % 10 == int(digits[-1])


def extract_afm(text: str) -> str | None:
    for match in re.finditer(r"(?<!\d)(\d[\d .-]{7,14}\d)(?!\d)", text):
        candidate = re.sub(r"\D", "", match.group(1))
        if validate_afm(candidate):
            return candidate
    return None


def _mrz_check_digit(value: str) -> str:
    values = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ<"
    weights = (7, 3, 1)
    total = 0
    for index, character in enumerate(value):
        mapped = values.index(character)
        total += mapped * weights[index % len(weights)]
    return str(total % 10)


def parse_passport_mrz(text: str) -> PassportFields | None:
    lines = [re.sub(r"[^A-Z0-9<]", "", line.upper()) for line in text.splitlines()]
    lines = [line for line in lines if len(line) >= 40]
    for line in lines:
        candidate = line[:44].ljust(44, "<")
        document_number = candidate[:9]
        if candidate[9] not in "0123456789" or _mrz_check_digit(document_number) != candidate[9]:
            continue
        nationality = candidate[10:13]
        if not re.fullmatch(r"[A-Z]{3}", nationality):
            continue
        return PassportFields(
            document_number=document_number.replace("<", "").strip(),
            nationality=nationality,
        )
    return None


def _run_tesseract(image_path: Path) -> str:
    executable = shutil.which("tesseract")
    if executable is None:
        raise OcrUnavailableError(
            "Tesseract is not installed; run `brew install tesseract`"
        )
    result = subprocess.run(
        [executable, str(image_path), "stdout", "--psm", "6", "-l", "eng"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def ocr_attachment(contents: bytes, *, filename: str, mime_type: str) -> str:
    """OCR an image or PDF locally; temporary files are removed on return."""

    if len(contents) > 10 * 1024 * 1024:
        raise ValueError("identity attachment exceeds the 10 MiB limit")
    suffix = Path(filename).suffix.lower() or ".bin"
    with tempfile.TemporaryDirectory(prefix="booking_identity_") as temporary_dir:
        source = Path(temporary_dir) / f"source{suffix}"
        source.write_bytes(contents)
        images: list[Path] = []
        if mime_type == "application/pdf" or suffix == ".pdf":
            document = pymupdf.open(stream=contents, filetype="pdf")
            try:
                for index, page in enumerate(document[:2]):
                    target = Path(temporary_dir) / f"page-{index}.png"
                    page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).save(target)
                    images.append(target)
            finally:
                document.close()
        else:
            target = Path(temporary_dir) / "normalized.png"
            with Image.open(source) as image:
                image.convert("RGB").save(target, format="PNG")
            images.append(target)
        return "\n".join(_run_tesseract(image) for image in images)
