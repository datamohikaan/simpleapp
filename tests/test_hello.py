import pytest
import os
# Configure directories
from pathlib import Path

root = Path(__file__).parent / 'test_files/files/'
print(root)
empty_folder = str(root / 'empty_folder')
print(empty_folder)

@pytest.fixture(scope='function')
def before_and_after():
    print("before")

class TestTheTests:

    def test_1(self, before_and_after):
        print('cwd: ' + os.getcwd())
        assert ((1 + 1) == 2) is True
