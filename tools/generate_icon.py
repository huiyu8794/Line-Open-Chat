"""Generate a small robot tray icon locally; no screenshot/image assets in Git."""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
image = Image.new('RGBA', (128, 128), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.line((64, 12, 64, 32), fill='#55ddbc', width=8)
draw.ellipse((55, 3, 73, 21), fill='#55ddbc')
draw.rounded_rectangle((15, 30, 113, 111), radius=22, fill='#233544', outline='#55ddbc', width=5)
draw.rounded_rectangle((30, 46, 98, 83), radius=10, fill='#101d28')
for x in (44, 78):
    draw.ellipse((x-7, 55, x+7, 69), fill='#55ddbc')
draw.line((47, 95, 81, 95), fill='#55ddbc', width=5)
image.save(root / 'line_openchat_robot.png')
image.save(root / 'line_openchat_robot.ico', sizes=[(16,16), (32,32), (48,48), (128,128)])
