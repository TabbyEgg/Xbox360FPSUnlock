"""Double-click launcher; failures are shown rather than disappearing."""
import traceback

if __name__ == "__main__":
    try:
        from rghfps.gui import main
        main()
    except Exception:
        error = traceback.format_exc()
        print(error)
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("RGH FPS Workbench could not start", error)
            root.destroy()
        except Exception:
            pass
