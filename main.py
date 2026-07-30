"""Entry point for WindowsAppPacker — a GUI wrapper around PyInstaller."""
from ui.app import App


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
