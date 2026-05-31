import console_log
from app_gui import TranscriberApp

def main() -> None:
    console_log.setup()
    app = TranscriberApp()
    app.mainloop()
    console_log.divider()
    console_log.info("Application closed. Goodbye!")
    console_log.divider()

if __name__ == "__main__":
    main()