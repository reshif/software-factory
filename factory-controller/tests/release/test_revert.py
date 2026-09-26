from factory.release.revert import auto_revert


class FakeGitHubForRevert:
    def __init__(self):
        self.reverted = []

    def revert_commit(self, repo, sha, *, branch="main"):
        self.reverted.append((repo, sha, branch))
        return f"revert-of-{sha}"


def test_auto_revert_calls_github_and_returns_sha():
    github = FakeGitHubForRevert()
    revert_sha = auto_revert(github, "acme/app", "deadbeef")
    assert revert_sha == "revert-of-deadbeef"
    assert github.reverted == [("acme/app", "deadbeef", "main")]
