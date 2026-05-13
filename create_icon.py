"""Создаёт иконку PW Editor (favicon.ico и app_icon.png)."""
import struct, zlib, io, os, sys
sys.stdout.reconfigure(encoding='utf-8')
from PIL import Image, ImageDraw, ImageFont

SIZES = [16, 32, 48, 64, 128, 256]


def create_png(w, h, bg_color, text_color, letter):
    """Создаёт PNG изображение с буквой."""
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Закруглённый фон
    r = w // 4
    draw.rounded_rectangle([(0, 0), (w-1, h-1)], radius=r, fill=bg_color)

    # Текст — буква
    font_size = w // 2
    try:
        # Пытаемся загрузить шрифт (Arial/DejaVu)
        font = ImageFont.truetype("arial.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Центрируем текст
    bbox = draw.textbbox((0, 0), letter, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (w - tw) // 2 - bbox[0]
    y = (h - th) // 2 - bbox[1]
    draw.text((x, y), letter, fill=text_color, font=font)

    return img


def create_ico(path, bg_color=(47, 128, 237), text_color=(255, 255, 255), letter="PW"):
    """Создаёт .ico файл с несколькими размерами."""
    images = []
    for size in SIZES:
        img = create_png(size, size, bg_color, text_color, letter)
        images.append(img)

    # Сохраняем как ICO
    images[0].save(path, format='ICO', sizes=[(s, s) for s in SIZES],
                  append_images=images[1:])
    print(f"  ✓ {path}")


def create_png_set(path_base, bg_color=(47, 128, 237), text_color=(255, 255, 255), letter="PW"):
    """Создаёт PNG для разных размеров."""
    for size in [128, 256]:
        img = create_png(size, size, bg_color, text_color, letter)
        p = f"{path_base}_{size}.png"
        img.save(p, 'PNG')
        print(f"  ✓ {p}")


if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))

    print("Создание иконок PW Editor...")

    # Основной .ico файл
    ico_path = os.path.join(script_dir, "app_icon.ico")
    create_ico(ico_path)

    # PNG версии для README etc
    png_base = os.path.join(script_dir, "app_icon")
    create_png_set(png_base)

    print("\nГотово!")
    print(f"Иконка: {ico_path}")
    print("Можете заменить app_icon.ico на свою.")
