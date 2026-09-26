"""Small, real file contents the scripted `FakeRuntime` agents "write" during the demo.

Checks run for real (`LocalSandbox.exec` shells out to the actual interpreter),
so these have to be genuinely valid -- or genuinely broken, on purpose -- Python.
"""

GREET_APP = '''"""A tiny greeting helper (demo fixture)."""


def greet(name: str) -> str:
    return f"Hello, {name}!"
'''

GREET_TEST_PASS = '''import unittest

from app.greet import greet


class GreetTest(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet("world"), "Hello, world!")


if __name__ == "__main__":
    unittest.main()
'''

GREET_APP_BROKEN = '''"""A tiny greeting helper (demo fixture) -- broken on purpose."""


def greet(name: str) -> str:
    return f"Hello {name}"  # missing the comma and "!" the test expects
'''

GREET_APP_FIXED = GREET_APP

PATCH_NOTE = "A one-line documentation clarification for the demo patch scenario.\n"

BIG_MODULE_TEMPLATE = '''"""A generated module, deliberately large to exceed the standing mandate's diff-size limit."""

{body}
'''


def big_module_body(lines: int = 40) -> str:
    return "\n".join(f"VALUE_{i} = {i}  # padding line to exceed max_diff_lines" for i in range(lines))


FORBIDDEN_WORKFLOW_EDIT = """name: ci
on: [pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo "an agent tried to edit this forbidden CI workflow"
"""

__all__ = ["BIG_MODULE_TEMPLATE", "FORBIDDEN_WORKFLOW_EDIT", "GREET_APP", "GREET_APP_BROKEN", "GREET_APP_FIXED",
          "GREET_TEST_PASS", "PATCH_NOTE", "big_module_body"]
