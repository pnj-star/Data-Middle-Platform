"""Image processing pipeline: validate, resize, CLIP embedding, Milvus insert."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .config import config as app_config
from . import milvus_client as mc
from .logging_config import get_logger
from .vlm import generate_image_description

_log = get_logger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_SIZE_MB = app_config.app.max_image_size_mb
MAX_DIMENSION = 2048
JPEG_QUALITY = 85

_clip_model: Any = None
_clip_processor: Any = None
_clip_lock = threading.Lock()


@dataclass
class ImageResult:
    image_id: str
    stored_path: str
    embedding: list[float] | None = None


def validate_and_resize(file_path: str | Path) -> str:
    """Validate image format/size, resize if > MAX_SIZE_MB, save normalized copy.

    Returns the path to the saved normalized image in outputs/images/.
    """
    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"Image not found: {fp}")

    ext = fp.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported image type: {ext}. Supported: {SUPPORTED_EXTENSIONS}")

    img = Image.open(fp)
    # Convert RGBA to RGB
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    size_mb = fp.stat().st_size / (1024 * 1024)
    if size_mb > MAX_SIZE_MB:
        # Resize: longest side to MAX_DIMENSION, maintain aspect ratio
        w, h = img.size
        if max(w, h) > MAX_DIMENSION:
            ratio = MAX_DIMENSION / max(w, h)
            new_size = (int(w * ratio), int(h * ratio))
            img = img.resize(new_size, Image.LANCZOS)

    # Save normalized copy
    out_dir = Path(app_config.output_dir_abs) / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    img_id = uuid.uuid4().hex
    out_path = out_dir / f"{img_id}.jpg"
    img.save(out_path, "JPEG", quality=JPEG_QUALITY, optimize=True)

    return str(out_path)


def _get_clip_model():
    """Lazy-load Chinese-CLIP model (ViT-B-16, 512 dims). Thread-safe."""
    global _clip_model, _clip_processor
    if _clip_model is None:
        with _clip_lock:
            if _clip_model is None:
                _log.info("Loading Chinese-CLIP model: ViT-B-16")
                import cn_clip.clip as clip
                _clip_model, _clip_processor = clip.load_from_name("ViT-B-16", device="cpu")
    return _clip_model, _clip_processor


def get_clip_embedding(image_path: str | Path) -> list[float]:
    """Generate CLIP embedding for an image (512 dims)."""
    import cn_clip.clip as clip
    model, preprocess = _get_clip_model()
    image = preprocess(Image.open(image_path)).unsqueeze(0)
    with __import__("torch").no_grad():
        image_features = model.encode_image(image)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
    return image_features[0].cpu().numpy().tolist()


def insert_image(
    file_path: str | Path,
    metadata: dict[str, str],
) -> ImageResult:
    """Full image ingest pipeline: normalize, embed, insert into Milvus.

    Args:
        file_path: Path to the original image.
        metadata: Dict with keys: mushroom_type, product_id, source.

    Returns:
        ImageResult with image_id, stored path, and embedding.
    """
    _log.info("Processing image: %s", file_path)

    # Step 1: Normalize
    stored_path = validate_and_resize(file_path)

    # Step 2: CLIP embedding
    embedding = get_clip_embedding(stored_path)

    # Step 3: Description (reserved for future VLM integration)
    description = generate_image_description(stored_path)

    # Step 4: Insert into Milvus
    img_id = Path(stored_path).stem
    entity = {
        "id": img_id,
        # Relative web path served by the /media/images static mount, so a RAG
        # service can fetch the image without knowing the filesystem layout.
        "image_url": f"/media/images/{Path(stored_path).name}",
        "description": description,
        "mushroom_type": metadata.get("mushroom_type", ""),
        "product_id": metadata.get("product_id", ""),
        "source": metadata.get("source", ""),
        "embedding": embedding,
    }
    mc.insert_image_entities([entity])

    _log.info("Image inserted: id=%s", img_id)
    return ImageResult(image_id=img_id, stored_path=stored_path, embedding=embedding)


def delete_image_by_source(source: str) -> int:
    """Delete all image vectors for a given source file from mushroom_images."""
    from . import milvus_expr as mx

    expr = mx.eq("source", source)
    _log.info("Deleting image vectors for source: %s", source)
    count = mc.delete_by_expr(expr, app_config.milvus.image_collection)
    _log.info("Deleted %d image vectors", count)
    return count
