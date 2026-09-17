"""Load and validate the locked ACTIVE_REFERENCE_SET pack."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ReferenceSetError(RuntimeError):
    """Empty / unapproved / missing hero / hash mismatch — fail closed."""


@dataclass(frozen=True)
class RefImage:
    role: str
    filename: str
    path: Path
    sha256: str
    never_use_to_redesign_face: bool
    allowed_uses: tuple[str, ...] = ()


@dataclass
class ActiveReferenceSet:
    pack_id: str
    manifest_id: str
    likeness_approval_status: str
    content_hash: str
    hero: RefImage
    images: list[RefImage] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def identity_refs(self) -> list[RefImage]:
        return [i for i in self.images if not i.never_use_to_redesign_face]

    def identity_only_or_raise(self) -> list[RefImage]:
        ids = self.identity_refs
        if not ids:
            raise ReferenceSetError("no identity-capable refs (motion/texture only is forbidden)")
        return ids


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _pack_content_hash(images: list[RefImage], pack_id: str) -> str:
    """Stable hash binding scores to the exact ref bytes + pack id."""
    parts = [pack_id]
    for img in sorted(images, key=lambda x: x.filename):
        parts.append(f"{img.filename}:{img.sha256}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def default_reference_set_path() -> Path:
    env = (os.environ.get("ACTIVE_REFERENCE_SET_PATH") or "").strip()
    if env:
        return Path(env)
    # Prefer repo-local copy
    repo = Path(__file__).resolve().parents[3] / "reference_images" / "ACTIVE_REFERENCE_SET.json"
    if repo.is_file():
        return repo
    gt = Path("/workspace/ground-truth/ACTIVE_REFERENCE_SET.json")
    return gt


def load_active_reference_set(path: Path | str | None = None) -> ActiveReferenceSet:
    path = Path(path) if path else default_reference_set_path()
    if not path.is_file():
        raise ReferenceSetError(f"ACTIVE_REFERENCE_SET missing: {path}")

    data = json.loads(path.read_text())
    if not data:
        raise ReferenceSetError("ACTIVE_REFERENCE_SET is empty JSON")

    status = (data.get("likeness_approval_status") or "").lower()
    if status != "approved":
        raise ReferenceSetError(
            f"likeness_approval_status={status!r} — refuse (need approved)"
        )

    images_raw = data.get("images") or []
    if not images_raw:
        raise ReferenceSetError("ACTIVE_REFERENCE_SET.images is empty — refuse")

    base_dir = path.parent
    loaded: list[RefImage] = []
    for item in images_raw:
        filename = item.get("filename") or Path(item.get("local_path", "")).name
        # Prefer repo-local file beside the JSON; fall back to local_path
        candidates = [
            base_dir / filename,
            Path(item.get("local_path") or ""),
            Path(item["local_path"].replace("/workspace/ground-truth/refs/", str(base_dir) + "/"))
            if item.get("local_path") else Path(),
        ]
        file_path = next((p for p in candidates if p and p.is_file()), None)
        if file_path is None:
            raise ReferenceSetError(f"ref file missing for {filename}")
        digest = _sha256_file(file_path)
        expected = (item.get("sha256") or "").lower()
        if expected and digest != expected.lower():
            raise ReferenceSetError(
                f"sha256 mismatch for {filename}: got {digest} expected {expected}"
            )
        loaded.append(
            RefImage(
                role=item.get("role") or "unknown",
                filename=filename,
                path=file_path,
                sha256=digest,
                never_use_to_redesign_face=bool(item.get("never_use_to_redesign_face")),
                allowed_uses=tuple(item.get("allowed_uses") or ()),
            )
        )

    # Hero identity
    identity = data.get("identity_lock") or {}
    hero_name = Path(identity.get("primary_local_path") or "").name
    hero = next((i for i in loaded if i.role == "hero_identity" or i.filename == hero_name), None)
    if hero is None:
        # fall back to primary_sha256 match
        hero_sha = (identity.get("primary_sha256") or "").lower()
        hero = next((i for i in loaded if i.sha256 == hero_sha), None)
    if hero is None:
        raise ReferenceSetError("hero_identity ref not found in pack")
    expected_hero = (identity.get("primary_sha256") or "").lower()
    if expected_hero and hero.sha256 != expected_hero:
        raise ReferenceSetError("hero sha256 does not match identity_lock.primary_sha256")

    # Motion/texture cannot be sole identity
    if not any(not i.never_use_to_redesign_face for i in loaded):
        raise ReferenceSetError("pack has no identity refs (only motion/texture)")

    pack_id = data.get("pack_id") or "UNKNOWN"
    content_hash = _pack_content_hash(loaded, pack_id)

    return ActiveReferenceSet(
        pack_id=pack_id,
        manifest_id=data.get("manifest_id") or "",
        likeness_approval_status=status,
        content_hash=content_hash,
        hero=hero,
        images=loaded,
        raw=data,
    )
