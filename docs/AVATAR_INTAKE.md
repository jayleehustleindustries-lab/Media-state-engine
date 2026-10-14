# Strict Avatar Photo Intake

The **Avatar Intake** page at `/avatar` is the secure operator interface for creating a HeyGen Photo Avatar from public image links. It keeps the HeyGen API key on the server. The page asks only for the Media State Engine access key for the active request; it is not saved in the browser by the page. The application exposes the same workflow through authenticated `POST /avatars/photo/preflight` and `POST /avatars/photo` endpoints.

## What the intake does

The first supplied link is the **primary identity anchor**. It is the only image sent to `POST /v3/avatars` because HeyGen’s Photo Avatar creation endpoint accepts one source `file` per Photo Avatar look. Any later links are preflighted as supporting references for human review and planned future looks; they are not silently blended into a person’s identity.

The intake accepts a public HTTPS URL. It follows at most five HTTPS redirects, rejects obvious private-network targets, limits downloads to 32 MB, and decodes the photo by content rather than filename. It accepts JPEG, PNG, WebP, GIF, TIFF, BMP, HEIC/HEIF, and AVIF when the installed decoder supports the source. It corrects EXIF camera orientation and emits a **lossless PNG** at exactly the original pixels. It never crops, stretches, resizes, removes a background, smooths skin, applies a beauty effect, or changes facial geometry. If lossless normalization would exceed the HeyGen 32 MB cap, it rejects the source rather than degrading it.

The strict gate blocks a primary image whose short edge is below **1080 pixels** or whose total area is under **2.0 megapixels**. It also returns clear manual-review checks, because resolution and file integrity alone cannot prove natural lighting, facial symmetry, consent, or a faithful likeness.

## Recommended capture pack

Use a recent camera photo session rather than unrelated photos collected across years. Lock hair, facial hair, glasses, makeup, and other identity-defining features to the intended final look. Avoid phone portrait-mode blur, beauty filters, face-tuning, high-contrast LUTs, and wide-angle selfie lenses.

| Priority | Capture | Framing and expression | Why it matters |
| --- | --- | --- | --- |
| 1 — required | **Hero identity anchor** | Vertical 4:5 or 9:16; eye-level; front-facing; head, shoulders, and upper torso; relaxed neutral expression | Use this as the first link. It gives HeyGen the cleanest read of facial landmarks, symmetry, lighting, and gesture space. |
| 2 | Clean 3/4 left | Same distance and lens; turn only 30–45° | Preserves jaw, nose, cheek, and hairline depth for later looks. |
| 3 | Clean 3/4 right | Mirror the left 3/4 setup | Reveals asymmetries honestly and helps a human choose a consistent look. |
| 4 | Full-body vertical | Eye-level, 35–50 mm-equivalent lens; hands visible; natural posture | Supplies body proportion and gesture reference for future avatar looks. It is **not** a replacement for the hero face anchor. |
| 5 | Warm natural smile | Same camera position and lighting as the hero | Captures a usable positive expression without letting a smile distort the baseline face. |
| 6 | Speaking expression | Mouth gently open mid-word; no exaggerated expression | Useful later for motion direction and realistic articulation review. |

For the hero anchor, use soft, even key light slightly above eye level, with a subtle fill so both eyes and the jawline remain visible. A little background separation is useful, but avoid dramatic colored lights, hard chin shadows, or strong beauty effects. Keep the lens at eye level and use the rear camera or a 35–50 mm-equivalent focal length. Stand far enough from the camera to avoid nose enlargement and edge warping. The subject should fill roughly 55–70% of the vertical frame with breathing room above the head and around the shoulders.

> **No source photo can guarantee zero distortion in generated video.** This intake prevents unnecessary input distortion and blocks objectively weak sources. Review the generated HeyGen avatar for face shape, eyes, mouth, jaw, lighting, and voice lip-sync before using it in a production job.

## Use

1. Open `/avatar` and enter the Media State Engine access key. Do **not** paste a HeyGen key—the server already holds it.
2. Enter a descriptive avatar name and paste the hero photo as the first public HTTPS link. Add the remaining capture-pack images one per line if desired.
3. Confirm that you have the person’s explicit likeness rights, then select **Run strict preflight**.
4. Resolve any blocker. Read every manual-review check; a passed technical preflight is not a final likeness approval.
5. Select **Create HeyGen Photo Avatar** only after the preflight passes. The response gives an `avatar_id`; wait for HeyGen to report `completed` before passing it to `POST /jobs/{job_id}/generate-avatar`.

## Provider references

HeyGen’s [Photo Avatar API guide](https://developers.heygen.com/photo-avatar) specifies a clear, front-facing, well-lit PNG or JPEG as the best starting point and creates a Photo Avatar with `POST /v3/avatars`. Its [asset upload guide](https://developers.heygen.com/docs/upload-assets) permits PNG and JPEG uploads up to 32 MB. The broader [HeyGen photo-avatar guidance](https://help.heygen.com/en/articles/10034438-how-to-get-started-with-photo-avatars) recommends a clear front-facing image with natural posture and visible hands when applicable. These provider constraints inform the engine’s strict preflight rules.
