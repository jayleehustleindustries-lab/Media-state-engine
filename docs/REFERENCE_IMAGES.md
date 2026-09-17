# Reference images (identity lock)

- **Engine load path:** `reference_images/ACTIVE_REFERENCE_SET.json` (sha256-bound)
- **Upstream lock:** `/workspace/ground-truth/ACTIVE_REFERENCE_SET.json` (Jordan-confirmed pack `JLF_AVATAR_V1_ACTIVE_REF_SET`)
- **Drive mirror:** folder id in pack `canonical_folder_id` — Drive remains the human-facing SoT; engine binds scores to local sha256 content hash.
- Motion/texture/wardrobe refs MUST NOT be used as sole identity (`never_use_to_redesign_face: true`).
- Empty set / unapproved likeness → refuse (fail closed).
