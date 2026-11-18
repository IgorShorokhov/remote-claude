class TmuxError(Exception):
    """Session backend error (tmux or agent host); the HTTP layer turns it into status and detail."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
