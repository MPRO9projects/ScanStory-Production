from PIL import Image
from pathlib import Path

image_paths = [
    "static/assets/landing/hand_phone1.png",
    "static/assets/landing/education.jpg",
    "static/assets/landing/artist.jpg",
    "static/assets/landing/photographer.jpg",
    "static/assets/landing/realestate.jpg",
    "static/assets/landing/business_owners.jpg",
    "static/assets/landing/marketing_agencies.jpeg",
    "static/assets/landing/event_planners.jpeg",
    "static/assets/landing/print_shops.jpeg",
    "static/assets/landing/product_brands.jpeg",
]

for path in image_paths:
    src = Path(path)

    if not src.exists():
        print(f"Missing: {src}")
        continue

    dest = src.with_suffix(".webp")

    img = Image.open(src).convert("RGB")
    img.save(dest, "WEBP", quality=82, optimize=True)

    print(f"Converted: {src} -> {dest}")