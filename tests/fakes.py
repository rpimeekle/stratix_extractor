"""A stand-in for a Netmiko connection that replays saved Stratix output."""

from pathlib import Path

SAMPLES = Path(__file__).parent / "samples"


class NetmikoAuthenticationException(Exception):
    """Same class name as Netmiko's, so the error text matches real runs."""


class NetmikoTimeoutException(Exception):
    pass


class FakeConnection:
    def __init__(self, sample: str, prompt: str):
        self.folder = SAMPLES / sample
        self.prompt = prompt
        self.commands = []
        self.disconnected = False

    def find_prompt(self):
        return self.prompt

    def enable(self):
        self.prompt = self.prompt.rstrip(">") + "#"

    def send_command(self, command, **kwargs):
        self.commands.append(command)
        file = self.folder / (command.replace(" ", "_") + ".txt")
        if file.exists():
            return file.read_text()
        return "                  ^\n% Invalid input detected at '^' marker.\n"

    def disconnect(self):
        self.disconnected = True


def make_connect(by_ip: dict):
    """by_ip maps an IP to (sample folder, prompt) or to an exception to raise."""
    made = {}

    def connect(params):
        entry = by_ip[params["host"]]
        if isinstance(entry, Exception):
            raise entry
        conn = FakeConnection(*entry)
        made[params["host"]] = conn
        return conn

    connect.made = made
    return connect
