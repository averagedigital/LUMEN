import io
import sys

from PIL import Image


def webp_dimensions(content):
    # Pillow allocates WebP canvases during open(), before exposing their dimensions.
    if (len(content) < 25 or content[:4] != b'RIFF' or content[8:12] != b'WEBP'
            or int.from_bytes(content[4:8], 'little') != len(content) - 8):
        raise ValueError('source raster WebP header')
    chunk = content[12:16]
    if chunk == b'VP8X' and len(content) >= 30:
        if content[20] & 2:
            raise ValueError('source raster animation')
        return (1 + int.from_bytes(content[24:27], 'little'),
                1 + int.from_bytes(content[27:30], 'little'))
    if chunk == b'VP8L' and content[20] == 0x2f:
        packed = int.from_bytes(content[21:25], 'little')
        return (1 + (packed & 0x3fff), 1 + ((packed >> 14) & 0x3fff))
    if chunk == b'VP8 ' and len(content) >= 30 and content[23:26] == b'\x9d\x01\x2a':
        return (int.from_bytes(content[26:28], 'little') & 0x3fff,
                int.from_bytes(content[28:30], 'little') & 0x3fff)
    raise ValueError('source raster WebP header')


def raster_pixels(content, suffix):
    if suffix not in ('jpg', 'png', 'webp'):
        return 0
    try:
        if suffix == 'webp':
            width, height = webp_dimensions(content)
            if not 0 < width * height <= 16_777_216:
                raise ValueError('source raster dimensions')
        expected = {'jpg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}[suffix]
        with Image.open(io.BytesIO(content), formats=[expected]) as image:
            pixels = image.width * image.height
            if (image.format != expected or not 0 < pixels <= 16_777_216
                    or (suffix != 'jpg' and image.n_frames != 1)):
                raise ValueError('source raster dimensions, frames or format')
            return pixels
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError('source raster header') from error


if __name__ == '__main__':
    content = sys.stdin.buffer.read(8 * 2**20 + 1)
    if len(content) > 8 * 2**20:
        raise ValueError('source asset size limit')
    print(raster_pixels(content, sys.argv[1]))
