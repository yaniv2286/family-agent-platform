"""Generate the Pixar-style tutor avatar image set with gpt-image-1.

For each character we produce four transparent PNG frames in
static/tutor-avatars/:

    {id}_base.png   - gentle closed-mouth smile (idle)
    {id}_talk.png   - mouth open mid-speech    (lip-sync, mouth mask)
    {id}_smile.png  - wide joyful smile        (celebrate / correct answer)
    {id}_blink.png  - eyes closed              (periodic blink)

The base image is generated once per character; the other frames are made
with images.edit + a region mask so the character stays pixel-identical
outside the masked area. Mouth/eye bounding boxes are estimated from the
generated portrait (characters are front-facing, head centered in the top
half) and stored in MASKS below - if a regenerated base shifts the face,
adjust the boxes after eyeballing the PNG.

Usage:
    python scripts/generate_tutor_avatars.py            # everything missing
    python scripts/generate_tutor_avatars.py --only fox # one character
    python scripts/generate_tutor_avatars.py --frames   # only edit frames
"""

import argparse
import io
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "static" / "tutor-avatars"

SIZE = "1024x1536"
MODEL = "gpt-image-1.5"

STYLE = (
    "Cute friendly children's cartoon tutor character, Pixar-style 3D render, "
    "soft studio lighting, big expressive eyes, head-and-shoulders portrait "
    "facing the camera straight on, centered, gentle warm closed-mouth smile, "
    "whole head and shoulders fully visible with margin around them, "
    "isolated on transparent background."
)

CHARACTERS = {
    "fox": "An adorable orange fox cub with a cream muzzle and big amber eyes",
    "owl": "An adorable round purple owl with a lavender belly, tiny beak and huge violet eyes",
    "robot": "An adorable rounded white-and-silver robot kid with a glowing cyan face screen and cyan eyes",
    "bunny": "An adorable white bunny with long pink-lined ears, pink nose and big dark eyes",
}

# Mouth / eye bounding boxes as (x0, y0, x1, y1) fractions of the frame,
# tuned for a centered front-facing head-and-shoulders portrait.
# Overridden per character below after inspecting the generated bases.
MASKS = {
    "mouth": (0.36, 0.40, 0.64, 0.55),
    "eyes": (0.22, 0.22, 0.78, 0.40),
}

def box_for(name, w, h):
    x0, y0, x1, y1 = MASKS[name]
    return int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)


def region_mask(size, box):
    """Transparent rectangle = editable region, opaque elsewhere."""
    mask = Image.new("RGBA", size, (0, 0, 0, 255))
    px = mask.load()
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        for x in range(x0, x1):
            px[x, y] = (0, 0, 0, 0)
    out = io.BytesIO()
    mask.save(out, format="PNG")
    out.name = "mask.png"
    out.seek(0)
    return out


def png_bytes(b64_or_resp):
    import base64
    return base64.b64decode(b64_or_resp)


def gen_base(client, cid):
    prompt = f"{CHARACTERS[cid]}. {STYLE}"
    print(f"[{cid}] generating base ...", flush=True)
    r = client.images.generate(model=MODEL, prompt=prompt, size=SIZE, background="transparent")
    data = png_bytes(r.data[0].b64_json)
    (OUT_DIR / f"{cid}_base.png").write_bytes(data)
    return Image.open(io.BytesIO(data))


def edit_frame(client, cid, base_path, frame, prompt, mask_name):
    print(f"[{cid}] editing {frame} ...", flush=True)
    img = Image.open(base_path)
    mask = region_mask(img.size, box_for(mask_name, *img.size))
    with open(base_path, "rb") as f:
        r = client.images.edit(
            model=MODEL,
            image=f,
            mask=mask,
            prompt=prompt,
            size=SIZE,
            background="transparent",
            input_fidelity="high",
        )
    (OUT_DIR / f"{cid}_{frame}.png").write_bytes(png_bytes(r.data[0].b64_json))


FRAMES = [
    ("talk", "mouth",
     "The same character with its mouth open mid-speech as if happily talking. "
     "Keep the character, pose, colors and everything outside the mouth identical."),
    ("smile", "mouth",
     "The same character with a wide joyful open-mouth smile showing happiness. "
     "Keep the character, pose, colors and everything outside the mouth identical."),
    ("blink", "eyes",
     "The same character with eyes gently closed as if blinking. "
     "Keep the character, pose, colors and everything outside the eyes identical."),
]


def main():
    load_dotenv(ROOT / ".env")
    client = OpenAI()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(CHARACTERS), help="one character only")
    ap.add_argument("--frames", action="store_true", help="skip base generation, only edit frames")
    args = ap.parse_args()

    ids = [args.only] if args.only else list(CHARACTERS)
    for cid in ids:
        base_path = OUT_DIR / f"{cid}_base.png"
        if not args.frames or not base_path.exists():
            gen_base(client, cid)
            time.sleep(1)
        for frame, mask_name, prompt in FRAMES:
            target = OUT_DIR / f"{cid}_{frame}.png"
            if target.exists():
                print(f"[{cid}] {frame} exists, skipping")
                continue
            edit_frame(client, cid, base_path, frame, prompt, mask_name)
            time.sleep(1)

    # The site serves WebP (PNG frames are ~2.3MB each; WebP ~100KB).
    for p in sorted(OUT_DIR.glob("*.png")):
        im = Image.open(p).convert("RGBA")
        im.thumbnail((640, 960), Image.LANCZOS)
        im.save(p.with_suffix(".webp"), "WEBP", quality=85, method=6)
        p.unlink()
    print("done ->", OUT_DIR)


if __name__ == "__main__":
    sys.exit(main())
