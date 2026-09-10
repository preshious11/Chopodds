with open(r"c:\Users\USER\Desktop\Sportsbot\bot.py", encoding="utf-8") as f:
    lines = f.readlines()
    print(f"Total lines: {len(lines)}")
    # Show last 5 lines
    for i, line in enumerate(lines[-5:], len(lines) - 4):
        print(f"{i}: {line.rstrip()}")
