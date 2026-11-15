from pathlib import Path
from types import SimpleNamespace

import pytest

from geo import catalog

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cat():
    return catalog.load(ROOT / "catalog.example.yaml")


class FakeBetaMessages:
    """Stands in for client.beta.messages. Records calls, returns queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, kw):
        self.calls.append(kw)
        return self.responses.pop(0)

    def create(self, **kw):
        return self._next(kw)

    def parse(self, **kw):
        return self._next(kw)


def fake_client(*responses):
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeBetaMessages(responses)))


class Block(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


def parsed(obj, stop="end_turn"):
    return SimpleNamespace(parsed_output=obj, stop_reason=stop, stop_details=None)
