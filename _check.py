import os
path = os.path.join(r"c:\Users\USER\Desktop\Sportsbot", "bot.py")
with open(path, encoding="utf-8") as f:
    lines = f.readlines()
print(f"Total lines: {len(lines)}")
