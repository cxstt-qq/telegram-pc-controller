"""Run explicitly to verify real mouse and keyboard input in a temporary Tk window."""

import tkinter as tk

import windows_control as desktop


root = tk.Tk()
root.title("Remote PC Control input check")
root.geometry("460x160+100+100")
root.attributes("-topmost", True)
entry = tk.Entry(root, font=("Arial", 16))
entry.pack(padx=20, pady=15, fill="x")
clicked = [False]
button = tk.Button(root, text="Click test", command=lambda: clicked.__setitem__(0, True))
button.pack(pady=8)
try:
    root.update()
    window = next(w for w in desktop.windows() if w.title == "Remote PC Control input check")
    desktop.activate(window.hwnd)
    entry.focus_force()
    root.update()
    desktop.type_text("Привет 42")
    root.update()
    assert entry.get() == "Привет 42", repr(entry.get())
    x = button.winfo_rootx() + button.winfo_width() // 2
    y = button.winfo_rooty() + button.winfo_height() // 2
    desktop.click(x, y)
    root.update()
    assert clicked[0], "Mouse click was not delivered"
    print("Mouse and Unicode keyboard input: OK")
finally:
    root.destroy()
