import select
import sys
import threading


class TerminalUI:
    def __init__(self):
        self._lock = threading.RLock()
        self._input_active = False
        self._prompt = ""
        self._buffer = ""
        self._last_render_len = 0

    @staticmethod
    def _normalize_terminal_newlines(text):
        # type: (str) -> str
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")

    def _write_terminal_locked(self, text):
        # type: (str) -> None
        sys.stdout.write(self._normalize_terminal_newlines(text))

    def print_line(self, *args, sep=" ", end="\n"):
        text = sep.join(str(arg) for arg in args) + end
        self.write(text)

    def write(self, text):
        # type: (str) -> None
        with self._lock:
            if self._input_active:
                self._clear_line_locked()
                self._write_terminal_locked(text)

                if text and not text.endswith(("\n", "\r")):
                    self._write_terminal_locked("\n")

                self._render_input_locked()
            else:
                sys.stdout.write(text)

            sys.stdout.flush()

    def input(self, prompt):
        # type: (str) -> str
        if not sys.stdin.isatty():
            return input(prompt)

        with self._lock:
            self._input_active = True
            self._prompt = prompt
            self._buffer = ""
            self._last_render_len = 0
            self._render_input_locked()

        try:
            return self._input_linux()
        finally:
            with self._lock:
                self._input_active = False
                self._prompt = ""
                self._buffer = ""
                self._last_render_len = 0

    def _clear_line_locked(self):
        # type: () -> None
        visible_len = max(
            self._last_render_len,
            len(self._prompt) + len(self._buffer),
        )
        sys.stdout.write("\r" + (" " * (visible_len + 8)) + "\r")

    def _render_input_locked(self):
        # type: () -> None
        self._clear_line_locked()
        line = self._prompt + self._buffer
        sys.stdout.write(line)
        sys.stdout.flush()
        self._last_render_len = len(line)

    def _append_char(self, ch):
        # type: (str) -> None
        with self._lock:
            self._buffer += ch
            self._render_input_locked()

    def _backspace(self):
        # type: () -> None
        with self._lock:
            if self._buffer:
                self._buffer = self._buffer[:-1]
                self._render_input_locked()

    def _finish_input(self):
        # type: () -> str
        with self._lock:
            value = self._buffer
            self._write_terminal_locked("\n")
            sys.stdout.flush()
            return value

    def _cancel_input(self):
        # type: () -> None
        with self._lock:
            self._write_terminal_locked("\n")
            sys.stdout.flush()

        raise KeyboardInterrupt

    def _input_linux(self):
        # type: () -> str
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        try:
            tty.setraw(fd)

            while True:
                ch = sys.stdin.read(1)

                if ch == "\x03":
                    self._cancel_input()

                if ch == "\x04":
                    raise EOFError

                if ch in {"\r", "\n"}:
                    return self._finish_input()

                if ch in {"\b", "\x7f"}:
                    self._backspace()
                    continue

                if ch == "\x1b":
                    while True:
                        readable, _, _ = select.select([sys.stdin], [], [], 0.001)
                        if not readable:
                            break
                        sys.stdin.read(1)
                    continue

                if ch and ch.isprintable():
                    self._append_char(ch)

        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


_TERMINAL = TerminalUI()


def ui_print(*args, sep=" ", end="\n"):
    _TERMINAL.print_line(*args, sep=sep, end=end)


def ui_input(prompt):
    # type: (str) -> str
    return _TERMINAL.input(prompt)