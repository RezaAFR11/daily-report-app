"""Print before/after sizes without writing simulated photos to disk."""

import io
import sys
from pathlib import Path

from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daily_report_app import compress_photo_bytes


def kb(byte_count):
    return round(byte_count / 1024, 1)


photos = list(Path('users').glob('*/temp_photos/*'))

print('EXISTING')
for path in photos:
    original = path.read_bytes()
    compressed = compress_photo_bytes(original)
    reduction = round((1 - len(compressed) / len(original)) * 100, 1)
    print(path.name, kb(len(original)), kb(len(compressed)), reduction)

print('SIMULATED_12MP')
for path in photos:
    source = Image.open(path).convert('RGB')
    source = source.resize((3024, 4032), Image.Resampling.LANCZOS)
    noise = Image.effect_noise(source.size, 18).convert('RGB')
    noise = noise.filter(ImageFilter.GaussianBlur(0.35))
    simulated = Image.blend(source, noise, 0.07)

    buffer = io.BytesIO()
    simulated.save(buffer, 'JPEG', quality=95, subsampling=0)
    original = buffer.getvalue()
    compressed = compress_photo_bytes(original)
    reduction = round((1 - len(compressed) / len(original)) * 100, 1)

    with Image.open(io.BytesIO(compressed)) as result:
        dimensions = result.size
    print(path.name, kb(len(original)), kb(len(compressed)), reduction, dimensions)
