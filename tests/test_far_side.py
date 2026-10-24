import pytest

from mmw.core import far_side
from mmw.core.config import NetworkConfig


def test_require_mmw():
    far_side.require_mmw(NetworkConfig(name="x", far_side="mmw"), "mmw stage")
    with pytest.raises(ValueError, match=r'set far_side = "mmw" to use mmw stage'):
        far_side.require_mmw(NetworkConfig(name="x"), "mmw stage")
